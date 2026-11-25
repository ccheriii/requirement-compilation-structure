"""Full SDK and HTTP model I/O audit, with concise progress on the terminal."""

from datetime import datetime, timezone
import json
import os
import re
from threading import Event, Lock, Thread
import traceback
import time
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4
from contextvars import ContextVar, copy_context
import socket

import httpx

from infrastructure.utils.presentation.console import current_activity, preview, show_panel, WaitingStatus
from infrastructure.utils.presentation.logging import session_info, full_detail
from rich.console import Group
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text
from infrastructure.utils.presentation.artifacts import proposal_kind, render_artifact, table, text


_call = ContextVar("compiler_model_call", default=None)


def _exception_chain(error: BaseException) -> list[dict[str, str]]:
    """Return every linked exception without losing explicit or implicit context."""
    chain = []
    seen = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append({"type": type(current).__name__, "message": str(current)})
        # Explicit ``raise ... from ...`` takes precedence over implicit context.
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return chain


def _record(event: str, **data) -> None:
    """Append untruncated values; never record authentication headers."""
    call = _call.get()
    session = session_info()
    if not session or not session.get("model_log", True):
        return
    record = {"timestamp": datetime.now(timezone.utc).isoformat(),
        "stage": session["stage"], "call_id": call["id"] if call else None,
        "activity": call["activity"] if call else current_activity(), "event": event, **data}
    with session["lock"]:
        with (session["output"] / "model_calls.jsonl").open("a", encoding="utf-8", newline="\n") as log:
            log.write(json.dumps(record, ensure_ascii=False) + "\n")


def _message_text(message: dict) -> str:
    """Return message text verbatim, without the transport JSON envelope."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part["text"] for part in content if part.get("type") == "text")
    return ""


def _http_request(request: httpx.Request) -> None:
    """Capture the actual serialized body on every HTTP attempt, including retries."""
    session, call = session_info(), _call.get()
    if session and session.get("debug_model_io") and call:
        call["attempt"] = call.get("attempt", 0) + 1
        stage = session["stage"].split(" | ", 1)[0].removeprefix("Stage ")
        task = re.sub(r"[^\w.-]+", "_", call["activity"].split(" | ", 1)[0]).strip("._")[:24] or "model"
        directory = session["output"] / ".arc" / "model_logs"
        directory.mkdir(parents=True, exist_ok=True)
        prefix = directory / f"{stage.replace(' ', '_')}.{task}.{call['id'][:8]}.{call['attempt']}"
        request.extensions["compiler_model_log_prefix"] = prefix
        call["log_prefix"] = prefix  # Streamed outputs are written by invoke().
        messages = json.loads(request.content).get("messages", [])
        text = "\n\n".join(_message_text(message) for message in messages)
        prefix.with_name(prefix.name + ".model.input").write_text(text, encoding="utf-8", newline="\n")
    _record("http_request", method=request.method, url=str(request.url.copy_with(query=None)),
        body=request.content.decode("utf-8"))


def _http_response(response: httpx.Response) -> None:
    """Read and retain the complete non-streaming response before SDK parsing."""
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        # Reading here would block until the whole stream ends and defeat stall
        # detection; invoke() records the assembled streamed response instead.
        _record("http_response", status_code=response.status_code,
            request_id=response.headers.get("x-request-id"), body="(streamed; see response event)")
        return
    response.read()
    prefix = response.request.extensions.get("compiler_model_log_prefix")
    if prefix is not None:
        try:
            body = response.json()
        except ValueError:
            text = response.text
        else:
            if isinstance(body, dict) and "choices" in body:
                text = "\n\n".join(_message_text(choice.get("message", {})) for choice in body["choices"])
            elif isinstance(body, dict) and isinstance(body.get("error"), dict):
                text = str(body["error"].get("message", ""))
            else:
                text = response.text
        prefix.with_name(prefix.name + ".model.ouput").write_text(text, encoding="utf-8", newline="\n")
    _record("http_response", status_code=response.status_code,
        request_id=response.headers.get("x-request-id"), body=response.text)


def http_client() -> httpx.Client:
    """Connect directly, retaining audit hooks and the SDK's transport limits."""
    from openai import DefaultHttpxClient
    return DefaultHttpxClient(trust_env=False, timeout=httpx.Timeout(600),
        event_hooks={"request": [_http_request], "response": [_http_response]})


def _input_summary(arguments: dict) -> str:
    messages = arguments.get("messages", [])
    lines = [f"Model: {arguments.get('model')} | Messages: {len(messages)}"]
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, str):
            line = str(message["role"])
            if message["role"] == "user":
                try:
                    payload = json.loads(content)
                except ValueError:
                    payload = None
                if isinstance(payload, dict):
                    line += " | sections: " + ", ".join(payload)
                    files = payload.get("files")
                    if isinstance(files, dict):
                        writable = sum(bool(item.get("writable")) for item in files.values() if isinstance(item, dict))
                        line += f" | files: {len(files)} ({writable} writable)"
            lines.append(line)
        else:
            images = sum(part.get("type") == "image_url" for part in content)
            lines.append(f"{message['role']}: text and images | images: {images}")
    return "\n".join(lines)


def _usage_summary(usage: dict) -> str:
    """Keep a compact plain-text usage line in the persistent log."""
    counts = _usage_counts(usage)
    summary = "Reported tokens | " + " | ".join(
        f"{name.lower()}: {counts[name]:,}" if counts[name] is not None else f"{name.lower()}: unavailable"
        for name in ("Input", "Cached input", "Output", "Total"))
    if counts["Reasoning"] is not None:
        summary += f"\nReasoning tokens: {counts['Reasoning']:,} | Non-reasoning output: {counts['Non-reasoning output']}"
    return summary


def _input_view(arguments):
    """Show bounded literal prompts and a file access inventory, excluding image data."""
    views = [Text(_input_summary(arguments))]
    for message in arguments.get("messages", [])[:6]:
        prompt = _message_text(message)
        views.append(Panel(Text(preview(prompt, max_lines=10, max_chars=1000)),
            title=Text(str(message.get("role", "message"))), border_style="dim blue"))
        if message.get("role") == "user":
            try:
                payload = json.loads(prompt)
            except ValueError:
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("files"), dict):
                files = table("File", "Access")
                for path, record in list(payload["files"].items())[:16]:
                    files.add_row(text(path), text("edit" if isinstance(record, dict) and record.get("writable") else "read"))
                views.append(files)
    return Group(*views)


def _usage_counts(usage):
    """Normalize provider aliases, retaining unknown values and reported zeroes."""
    def first(*values):
        return next((value for value in values if type(value) is int), None)
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    output = first(usage.get("completion_tokens"), usage.get("output_tokens"))
    reasoning = first((usage.get("completion_tokens_details") or {}).get("reasoning_tokens"))
    return {"Input": first(usage.get("prompt_tokens"), usage.get("input_tokens")),
        "Cached input": first(details.get("cached_tokens"), usage.get("prompt_cache_hit_tokens")),
        "Output": output, "Reasoning": reasoning,
        "Non-reasoning output": output - reasoning if output is not None and reasoning is not None else None,
        "Total": first(usage.get("total_tokens"))}


def _output_view(content, usage, elapsed):
    """Rendering an incomplete proposal must not change the model call outcome."""
    try:
        return _structured_output_view(content, usage, elapsed)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return Group(Text(_usage_summary(usage)), Text(f"Round-trip latency: {elapsed:.2f} s"), Text(preview(content)))


def _usage_view(usage, elapsed):
    """Usage and timing remain visible even when detailed model logging is off."""
    metrics = table("Usage / timing", "Reported value")
    for name, value in _usage_counts(usage).items():
        metrics.add_row(text(name), text(f"{value:,}" if value is not None else "unavailable"))
    metrics.add_row(text("Round-trip latency"), Text(f"{elapsed:.2f} s", style="bold cyan"))
    return metrics


def _structured_output_view(content, usage, elapsed):
    views = [_usage_view(usage, elapsed), Text("Model proposal · acceptance belongs to the current compiler pass", style="dim")]
    try:
        value = json.loads(content)
    except ValueError:
        value = None
    if isinstance(value, dict) and isinstance(value.get("tool_calls"), list):
        calls = table("File tool", "Path", "Operation / lines")
        for call in value["tool_calls"][:16]:
            if not isinstance(call, dict):
                continue
            detail = call.get("operation", f"{call.get('start_line', 1)}..{call.get('end_line', 'EOF')}")
            calls.add_row(text(call.get("tool", "?")), text(call.get("path", "?")), text(detail))
        views.append(calls)
    elif isinstance(value, dict) and isinstance(value.get("edits"), list):
        edits = table("File edits", "Source lines")
        for edit in value["edits"][:16]:
            if isinstance(edit, dict):
                edits.add_row(text(edit.get("path", "?")), text(len(str(edit.get("content", "")).splitlines())))
        views.append(edits)
        for edit in value["edits"][:2]:
            if not isinstance(edit, dict) or not isinstance(edit.get("content"), str):
                continue
            path = str(edit.get("path", ""))
            extension = path.rsplit(".", 1)[-1]
            lexer = {"ts": "typescript", "tsx": "tsx", "js": "javascript", "py": "python", "sql": "sql", "json": "json"}.get(extension, "text")
            views.append(Panel(Syntax(preview(edit["content"], max_lines=10, max_chars=1200), lexer,
                theme="ansi_dark", word_wrap=True, background_color="default"), title=text(path), border_style="dim"))
    elif isinstance(value, dict) and isinstance(value.get("content"), str):
        path = str(value.get("path", "SQL proposal"))
        views.append(Panel(Syntax(preview(value["content"], max_lines=16, max_chars=1800), "sql",
            theme="ansi_dark", word_wrap=True, background_color="default"), title=text(path), border_style="dim"))
    elif isinstance(value, dict) and isinstance(value.get("contract"), dict):
        contract = table("Condition", "Description")
        for key in ("precondition", "postcondition"):
            for item in value["contract"].get(key, [])[:12]:
                contract.add_row(text(key), text(item.get("description", item) if isinstance(item, dict) else item, 600))
        views.append(contract)
    elif isinstance(value, dict):
        try:
            views.append(render_artifact(proposal_kind(value), value, context=(session_info() or {}).get("artifacts", {})))
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            views.append(Text(preview(content)))
    else:
        views.append(Text(preview(content)))
    return Group(*views)


def _count_call(usage, elapsed, failed=False):
    session = session_info()
    if session is None:
        return
    stats = session.setdefault("model_stats", {})
    stats["calls"] = stats.get("calls", 0) + 1
    stats["failed"] = stats.get("failed", 0) + int(failed)
    stats["seconds"] = stats.get("seconds", 0) + elapsed
    for key, value in _usage_counts(usage).items():
        if value is not None:
            stats[key] = stats.get(key, 0) + value
            stats[key + " reports"] = stats.get(key + " reports", 0) + 1


def show_call_totals():
    """Summarize this stage's SDK calls; cached input is a subset of input."""
    stats = (session_info() or {}).get("model_stats", {})
    if not stats:
        return
    view = table("Stage model calls", "Value", "Usage availability")
    view.add_row(text("Calls / failed"), text(f"{stats['calls']} / {stats['failed']}"), text("SDK calls; time includes retries"))
    view.add_row(text("Total round-trip time"), text(f"{stats['seconds']:.2f} s"), text("sum of calls"))
    for key in ("Input", "Cached input", "Output", "Reasoning", "Non-reasoning output", "Total"):
        view.add_row(text(key), text(f"{stats[key]:,}" if key in stats else "unavailable"),
            text(f"reported in {stats.get(key + ' reports', 0)}/{stats['calls']} calls"))
    show_panel("Stage model usage | reported counts only", view, tone="output")


# --- Stall detection ---------------------------------------------------------
# Providers sometimes leave a request queued for minutes before generating
# (observed: 890 tokens in 106 s, while the identical retry took 5 s). Responses
# are streamed so a request that has produced nothing by a deadline can be
# cancelled and resent. A healthy long generation keeps streaming and is never
# interrupted. The last attempt waits without a deadline. ARC_STREAM=0 restores
# plain non-streaming requests.
def _env_seconds(name, default):
    try:
        return max(0.0, float(os.getenv(name, default)))
    except ValueError:
        return float(default)


def _abort_stream(stream):
    """Shut the connection down so a blocked reader wakes and the provider stops work."""
    try:
        network = stream.response.extensions.get("network_stream")
        sock = network.get_extra_info("socket") if network is not None else None
        if sock is not None:
            sock.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass
    try:
        stream.close()
    except Exception:
        pass


class ModelStalled(RuntimeError):
    """No streamed output arrived before the stall deadline."""


def _stream_enabled(client):
    return (os.getenv("ARC_STREAM", "1").strip().lower() not in {"0", "false", "no", "off"}
        and not getattr(client, "_arc_stream_unsupported", False))


def _stream_rejected(error):
    message = str(error).lower()
    return getattr(error, "status_code", None) in {400, 404, 415, 422} and "stream" in message


def _stream_completion(client, arguments, first_deadline, idle_deadline):
    """Stream one completion; abandon it if no output arrives within the deadlines.

    first_deadline covers everything until the first chunk, including waiting for
    response headers (a queued request sends none). idle_deadline limits silence
    after output has started. Both are enforced by this thread; the socket read
    timeout is only the mid-answer limit, so a healthy answer that pauses briefly
    is never cut off.
    """
    from openai import APITimeoutError
    from openai.types.chat import ChatCompletion
    options = dict(arguments, stream=True)
    if not getattr(client, "_arc_no_stream_usage", False):
        options["stream_options"] = {"include_usage": True}
    requested = monotonic()
    api = client
    if first_deadline:
        # SDK retries are off for deadline attempts (_complete() resends stalls and
        # retries transient errors itself).
        api = client.with_options(max_retries=0, timeout=httpx.Timeout(
            connect=10.0, read=max(idle_deadline or 0.0, first_deadline), write=60.0, pool=10.0))
    state = {"last": requested, "started": False, "stream": None, "abandoned": False}
    guard, finished, outcome = Lock(), Event(), {}
    content, reasoning, refusal = [], [], []
    meta = {"finish": None, "usage": None, "model": None, "id": None}

    def consume():
        try:
            stream = api.chat.completions.create(**options)
            with guard:
                state["stream"] = stream
                abandoned = state["abandoned"]
            if abandoned:  # The caller gave up while this request waited for headers.
                _abort_stream(stream)
                return
            http_response = getattr(stream, "response", None)
            if http_response is not None and not http_response.headers.get("content-type", "").startswith("text/event-stream"):
                # The provider ignored stream=true and sent an ordinary completion.
                try:
                    body = http_response.read()
                finally:
                    stream.close()
                outcome["completion"] = ChatCompletion.model_validate(json.loads(body))
                return
            for chunk in stream:
                if not state["started"]:
                    state["first_chunk"] = monotonic() - requested
                state["last"], state["started"] = monotonic(), True
                meta["model"], meta["id"] = chunk.model or meta["model"], chunk.id or meta["id"]
                if chunk.usage is not None:
                    meta["usage"] = chunk.usage.model_dump(mode="json")
                for choice in chunk.choices:
                    delta = choice.delta
                    if delta is not None:
                        if delta.content:
                            content.append(delta.content)
                        if delta.refusal:
                            refusal.append(delta.refusal)
                        extra = getattr(delta, "model_extra", None) or {}
                        piece = extra.get("reasoning_content") or extra.get("reasoning")
                        if isinstance(piece, str):
                            reasoning.append(piece)
                    if choice.finish_reason:
                        meta["finish"] = choice.finish_reason
        except BaseException as error:
            outcome["error"] = error
        finally:
            finished.set()

    # The request and the stream run on a worker thread; this thread enforces the
    # deadlines. Closing a stream does not reliably wake a read blocked in another
    # thread, so an abandoned stream's socket is shut down, and this thread never
    # waits on the worker for long.
    Thread(target=copy_context().run, args=(consume,), name="model-stream", daemon=True).start()
    stalled = None
    try:
        while not finished.wait(0.25):
            limit = idle_deadline if state["started"] else first_deadline
            if limit and monotonic() - state["last"] > limit:
                stalled = limit
                break
    finally:
        if stalled is not None or not finished.is_set():
            with guard:
                state["abandoned"] = True
                stream = state["stream"]
            if stream is not None:
                _abort_stream(stream)
            finished.wait(5 if stream is not None else 0.5)
    if stalled is not None:
        raise ModelStalled(f"no streamed output for {stalled:.0f} s "
            + ("after generation started" if state["started"] else "(request still queued)"))
    if "completion" in outcome:
        client._arc_stream_unsupported = True  # Stop asking this provider to stream.
        completion = outcome["completion"]
        return completion, completion.model_dump(mode="json")
    if "error" in outcome:
        error = outcome["error"]
        if "stream_options" in options and _stream_rejected(error) and "stream_options" in str(error):
            client._arc_no_stream_usage = True
            return _stream_completion(client, arguments, first_deadline, idle_deadline)
        if first_deadline and isinstance(error, (httpx.TimeoutException, APITimeoutError)):
            raise ModelStalled(f"no streamed output for {max(idle_deadline or 0.0, first_deadline):.0f} s "
                "(network read timeout)") from error
        raise error
    finish, usage, model, response_id = meta["finish"], meta["usage"], meta["model"], meta["id"]
    message = {"role": "assistant", "content": "".join(content) or None, "refusal": "".join(refusal) or None}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    raw = {"id": response_id, "model": model, "object": "chat.completion",
        "choices": [{"index": 0, "finish_reason": finish, "message": message}], "usage": usage or {},
        "_first_chunk_seconds": state.get("first_chunk")}
    response = SimpleNamespace(id=response_id, model=model, usage=usage,
        choices=[SimpleNamespace(index=0, finish_reason=finish, message=SimpleNamespace(**message))])
    return response, raw


# Time to first streamed output for successful requests, per kind of request.
# Environments differ (the benchmark runner answered image requests far slower
# than a laptop did), so the stall deadline adapts to what this run observes.
_FIRST_CHUNK_TIMES = {"text": [], "vision": []}
_FIRST_CHUNK_LOCK = Lock()
MAX_STALL_DEADLINE = 180.0


def _request_kind(arguments):
    for message in arguments.get("messages", []):
        content = message.get("content")
        if isinstance(content, list) and any(isinstance(part, dict) and part.get("type") == "image_url" for part in content):
            return "vision"
    return "text"


def _record_first_chunk(kind, seconds):
    with _FIRST_CHUNK_LOCK:
        samples = _FIRST_CHUNK_TIMES[kind]
        samples.append(seconds)
        del samples[:-50]


def stall_deadline(kind, attempt):
    """First-output deadline: configured base, at least 3x the observed median, doubled per retry."""
    if kind == "vision":
        base = _env_seconds("ARC_STALL_FIRST_CHUNK_SECONDS_VISION", 60)
    else:
        base = _env_seconds("ARC_STALL_FIRST_CHUNK_SECONDS", 10)
    with _FIRST_CHUNK_LOCK:
        samples = sorted(_FIRST_CHUNK_TIMES[kind])
    if len(samples) >= 3:
        base = max(base, 3 * samples[len(samples) // 2])
    return min(MAX_STALL_DEADLINE, base * (2 ** attempt))


def _transient(error):
    from openai import APIConnectionError, InternalServerError, RateLimitError
    return isinstance(error, (APIConnectionError, InternalServerError, RateLimitError)) or getattr(error, "status_code", None) in {408, 409, 429, 500, 502, 503, 504}


def _complete(client, arguments, label):
    """Run one completion with stall detection; return (response, raw JSON envelope)."""
    if not _stream_enabled(client):
        response = client.chat.completions.create(**arguments)
        return response, response.model_dump(mode="json")
    idle = _env_seconds("ARC_STALL_IDLE_SECONDS", 60)
    retries = int(_env_seconds("ARC_STALL_RETRIES", 5))
    kind = _request_kind(arguments)
    for attempt in range(retries + 1):
        last = attempt == retries
        first = None if last else stall_deadline(kind, attempt)
        try:
            response, raw = _stream_completion(client, arguments, first, None if last else idle)
            if raw.get("_first_chunk_seconds") is not None:
                _record_first_chunk(kind, raw.pop("_first_chunk_seconds"))
            return response, raw
        except ModelStalled as error:
            _record("stalled", attempt=attempt + 1, message=str(error))
            show_panel(f"[MODEL STALLED] {label}", f"{error}; cancelling and resending "
                f"(retry {attempt + 1}/{retries}{', final attempt waits without a deadline' if attempt + 1 == retries else ''}).",
                tone="error")
        except Exception as error:
            if attempt == 0 and _stream_rejected(error):
                client._arc_stream_unsupported = True
                print("Provider rejected streaming; using non-streaming requests (no stall detection).", flush=True)
                response = client.chat.completions.create(**arguments)
                return response, response.model_dump(mode="json")
            if last or not _transient(error):
                raise
            # What the SDK would have retried (5xx, 429, dropped connection): back off, resend.
            delay = min(8.0, 0.5 * 2 ** attempt)
            _record("transient_retry", attempt=attempt + 1, error_type=type(error).__name__, message=str(error))
            print(f"Transient model error ({type(error).__name__}); retrying in {delay:.1f}s.", flush=True)
            time.sleep(delay)
    raise AssertionError("unreachable")


def invoke(client, arguments: dict):
    """Audit one SDK call and show a static waiting message until it returns."""
    call_id = uuid4().hex
    activity = current_activity()
    session = session_info()
    label = f"{session['stage']} | {activity}" if session else activity
    token = _call.set({"id": call_id, "activity": activity})
    verbose = session is None or session.get("model_log", True)
    if verbose:
        full_detail(f"[MODEL INPUT] {label}", "\n\n".join(_message_text(message) for message in arguments.get("messages", [])))
        show_panel(f"[MODEL INPUT] {label} | call {call_id[:8]}", _input_view(arguments), tone="input")
    else:
        show_panel(f"[MODEL CALL] {label} | call {call_id[:8]}", f"Model: {arguments.get('model')}", tone="input")
    waiting = WaitingStatus()
    started = monotonic()

    try:
        _record("request", arguments=arguments)
        started = monotonic()
        response, raw = _complete(client, arguments, label)
        elapsed = monotonic() - started
        # Preserve the envelope and untouched message content before any JSON repair.
        _record("response", elapsed_seconds=elapsed, response=raw)
        prefix = (_call.get() or {}).get("log_prefix")
        if prefix is not None and arguments.get("stream") is None and _stream_enabled(client):
            prefix.with_name(prefix.name + ".model.ouput").write_text(
                "\n\n".join(_message_text(choice.get("message", {})) for choice in raw["choices"]),
                encoding="utf-8", newline="\n")
        waiting.close()
        content = "\n".join(choice.message.content or "" for choice in response.choices)
        usage = raw.get("usage") or {}
        _count_call(usage, elapsed)
        full_detail(f"[MODEL OUTPUT] {label}", _usage_summary(usage) + f"\nRound-trip latency: {elapsed:.2f} s" + ("\n" + content if verbose else ""))
        show_panel(f"[MODEL OUTPUT] {label} | {elapsed:.2f}s",
            _output_view(content, usage, elapsed) if verbose else _usage_view(usage, elapsed), tone="output")
        return response
    except BaseException as error:
        elapsed = monotonic() - started
        _count_call({}, elapsed, failed=True)
        waiting.close()
        chain = _exception_chain(error)
        cause = ": ".join(f"{item['type']}: {item['message']}" for item in chain[1:]) or None
        _record("error", elapsed_seconds=elapsed,
            error_type=type(error).__name__, message=str(error), cause=cause,
            exception_chain=chain,
            traceback="".join(traceback.format_exception(error)))
        detail = f"Round-trip latency: {elapsed:.2f} s\n{type(error).__name__}: {error}"
        if cause:
            detail += "\nCause: " + cause
        show_panel(f"[MODEL FAILED] {label}", detail, tone="error", max_lines=12, max_chars=1800)
        raise
    finally:
        waiting.close()
        _call.reset(token)
