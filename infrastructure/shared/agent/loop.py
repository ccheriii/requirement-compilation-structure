"""Bounded file editing with failure attribution and real acceptance checks."""

from hashlib import sha256
import json
import os
from threading import Lock
from uuid import uuid4

from infrastructure.shared.agent.discovery import DISCOVERY_RULES, DiscoverySession
from infrastructure.shared.agent.conventions import task_conventions

from infrastructure.shared.agent.feedback import clean_feedback
from infrastructure.shared.agent.tools import FileToolError, FileTools
from infrastructure.shared.agent.tools.files import read_file
from infrastructure.shared.agent.tools.protocol import FILE_TOOL_RULES
from infrastructure.shared.agent.proposals import RESPONSE_ERRORS, response_feedback
from infrastructure.shared.project.dependencies import EnvironmentFailure
from infrastructure.shared.project.git import checkpoint
from infrastructure.utils.files import read_source


class AgentExhausted(ValueError):
    """A recoverable task failure with the last real repair feedback."""

    def __init__(self, label, turns, feedback, reason=None):
        message = f"Agent exhausted {turns} rounds for {label}"
        if reason:
            message = f"Agent stopped after {turns} round(s) for {label}: {reason}"
        super().__init__(message)
        self.turns = turns
        self.feedback = feedback


class AgentEscalated(AgentExhausted):
    """The agent's own diagnoses say this task cannot succeed within its permissions.

    Raised by a stop_when policy so the caller can route the problem to an owner
    that has the needed permission (e.g. a test reviewer) instead of spending the
    remaining rounds on edits that cannot satisfy the gate.
    """

    def __init__(self, label, turns, feedback, reason, diagnoses):
        super().__init__(label, turns, feedback, reason=reason)
        self.diagnoses = diagnoses


TEST_CAUSES = {"test_requirement_mismatch", "test_overconstraint"}


def repeated_test_blame(times=2):
    """stop_when policy for repairs whose tests are read-only.

    When the implementation agent diagnoses the (protected) test itself as wrong
    in `times` rounds, further implementation rounds cannot fix it.
    """
    def policy(diagnoses):
        blamed = [item for item in diagnoses if isinstance(item, dict) and item.get("cause") in TEST_CAUSES]
        if len(blamed) >= times:
            return (f"the repair agent diagnosed the read-only test as wrong in {len(blamed)} rounds "
                "(" + ", ".join(sorted({item["cause"] for item in blamed})) + ")")
        return None
    return policy


def _is_model_transport_error(error):
    """Identify provider/network failures that code edits cannot repair."""
    transport_types = {
        "APIConnectionError", "APITimeoutError", "ConnectError", "ConnectTimeout",
        "ReadTimeout", "WriteTimeout", "PoolTimeout", "gaierror",
    }
    seen = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in transport_types:
            return True
        current = current.__cause__ or current.__context__
    return False


# Read-ahead memory: files that earlier tasks had to request with read_file.
# Each read round resends the whole task context, so pre-supplying a file that is
# likely to be read is far cheaper than one more round trip. Two sources, in order:
#   1. same kind of task (same instructions, same output project);
#   2. project-wide: files that at least READ_AHEAD_PROJECT_TASKS different tasks of
#      any kind needed (typically how the project boots and seeds its database).
# The second matters for tasks that run in parallel: they start together, so the
# first has no same-kind history yet. Set ARC_READ_AHEAD=0 to disable.
_READ_MEMORY: dict[tuple[str, str], dict[str, set]] = {}
_PROJECT_READS: dict[str, dict[str, set]] = {}
_READ_MEMORY_LOCK = Lock()
READ_AHEAD_PROJECT_TASKS = 2
READ_AHEAD_FILES = 8
READ_AHEAD_FILE_CHARS = 16000
READ_AHEAD_TOTAL_CHARS = 40000


def _memory_key(files, instructions):
    return str(files.output), sha256(instructions.encode("utf-8")).hexdigest()[:16]


def _remember_reads(key, paths, task_id="task"):
    """Record which distinct tasks needed each path (per kind and project-wide)."""
    with _READ_MEMORY_LOCK:
        kind = _READ_MEMORY.setdefault(key, {})
        project = _PROJECT_READS.setdefault(key[0], {})
        for path in paths:
            kind.setdefault(path, set()).add(task_id)
            project.setdefault(path, set()).add(task_id)


def _read_ahead(key, files):
    """Return complete excerpts for files earlier tasks of this kind needed to read."""
    if os.getenv("ARC_READ_AHEAD", "1").strip().lower() in {"0", "false", "no", "off"}:
        return {}
    with _READ_MEMORY_LOCK:
        same_kind = sorted(_READ_MEMORY.get(key, {}).items(), key=lambda item: (-len(item[1]), item[0]))
        project = sorted(((path, tasks) for path, tasks in _PROJECT_READS.get(key[0], {}).items()
            if len(tasks) >= READ_AHEAD_PROJECT_TASKS), key=lambda item: (-len(item[1]), item[0]))
    ranked = list(dict.fromkeys(path for path, _tasks in [*same_kind, *project]))
    supplied, total = {}, 0
    for path in ranked:
        if len(supplied) >= READ_AHEAD_FILES:
            break
        if path in files.files:
            continue  # Already supplied in input.files.
        try:
            record = read_file(files, path)
        except (FileToolError, OSError, ValueError):
            continue
        size = len(record["content"])
        if size > READ_AHEAD_FILE_CHARS or total + size > READ_AHEAD_TOTAL_CHARS:
            continue
        supplied[path] = [record]
        total += size
    return supplied


def _file_query_as_reads(query):
    """Translate a misdirected context_request kind=file exact read into read_file calls."""
    calls = []
    for path in query.get("ids") or []:
        if not isinstance(path, str):
            return None
        call = {"tool": "read_file", "path": path}
        for key in ("start_line", "end_line"):
            if type(query.get(key)) is int:
                call[key] = query[key]
        calls.append(call)
    return calls or None


def run_file_agent(model, instructions: str, task: dict, files: FileTools, gate,
        max_turns: int, console, label: str, feedback=None, feedback_filter=None, context_index=None,
        allow_discovery=True, stop_when=None, reasoning=None) -> dict:
    """Run a file-editing agent; reasoning names the task kind for per-kind effort.

    Default kind is "implementation"; test-writing call sites pass "tests".
    """
    from infrastructure.shared.models.text import reasoning_for
    with reasoning_for(reasoning or "implementation"):
        return _run_file_agent(model, instructions, task, files, gate, max_turns, console, label,
            feedback, feedback_filter, context_index, allow_discovery, stop_when)


def _run_file_agent(model, instructions, task, files, gate, max_turns, console, label,
        feedback, feedback_filter, context_index, allow_discovery, stop_when) -> dict:
    """Refresh files and paths; optionally select failure-related files before repair.

    The stage-owned feedback filter may add writable sources/read-only references
    and condense check reports. It cannot change the acceptance gate or its result.
    stop_when(diagnoses) may return a reason to stop early with AgentEscalated.
    """
    if max_turns < 1:
        raise ValueError("--max-turns must be positive")
    index = context_index if allow_discovery else None
    allow_discovery = index is not None
    discovery, context_result = DiscoverySession(index), None
    memory_key = _memory_key(files, instructions)
    task_id = uuid4().hex
    read_files = _read_ahead(memory_key, files)
    if read_files:
        console.show(label + " | Read-ahead",
            "Pre-supplied files that earlier tasks of this kind read: " + str(list(read_files)))
    if feedback is not None:
        feedback = clean_feedback(feedback_filter(feedback) if feedback_filter else feedback)
    feedback = [] if feedback is None else [feedback]
    check_feedback = list(feedback)
    diagnoses = []
    for turn in range(1, max_turns + 1):
        console.show(f"{label} | Edit round {turn}/{max_turns}",
            "Generate file edits, then run the stage's compilation and test acceptance checks.")
        response, applied, checking = None, False, False
        try:
            def request_model():
                # Gates may regenerate wiring outside edit_file; discard stale excerpts too.
                for path, excerpts in list(read_files.items()):
                    target = files.path(path)
                    if not target.is_file():
                        read_files.pop(path)
                        continue
                    lines = read_source(target).splitlines(keepends=True)
                    for excerpt in excerpts:
                        content = "".join(lines[excerpt["start_line"] - 1:excerpt["end_line"]])
                        if len(lines) != excerpt["total_lines"] or content != excerpt["content"]:
                            read_files.pop(path)
                            break
                # Order keys from most to least stable so provider prefix caching
                # covers as much of each request as possible: static tool schemas,
                # then the task, then files (change after edits), then evidence
                # that grows within a round, then round-specific feedback/results.
                request = {"tools": files.schemas(),
                    "context_kinds": list(index.records()) if index is not None else [],
                    "task": files.task_state(task), "files": files.state()}
                conventions = task_conventions(files.output, task)
                if conventions:
                    request["task"]["shared_conventions"] = conventions
                if read_files:
                    request["read_files"] = read_files
                if discovery.evidence:
                    request["context_records"] = discovery.evidence
                if any(discovery.candidates.values()):
                    request["context_candidates"] = discovery.candidates
                request["feedback"] = feedback
                if context_result is not None:
                    request["context_result"] = context_result
                rules = instructions + "\n\n" + FILE_TOOL_RULES
                if allow_discovery:
                    rules += "\n\n" + DISCOVERY_RULES
                else:
                    rules += "\nSemantic discovery is disabled for this task. Use supplied evidence and read_file only."
                return model.generate_json(rules, request)
            response = request_model()
            while isinstance(response, dict):
                if "context_request" in response:
                    query = response["context_request"]
                    redirected = (_file_query_as_reads(query) if isinstance(query, dict)
                        and query.get("kind") == "file" and "ids" in query else None)
                    if redirected:
                        # Serve the exact file read the model asked for instead of
                        # rejecting it and spending an edit round on a format error.
                        # read_file still enforces this task's readable scope.
                        console.show(label + " | Context query -> read_file", json.dumps(query, ensure_ascii=False))
                        response = {"tool_calls": redirected}
                        continue
                    if not allow_discovery:
                        raise ValueError("Semantic discovery is disabled for this task; use the supplied task evidence and read_file within its allowed scope")
                    console.show(label + " | Context query", json.dumps(query, ensure_ascii=False))
                    if isinstance(query, dict) and query.get("kind") == "file" and "ids" in query:
                        raise ValueError('Use read_file for file content, for example '
                            '{"tool_calls":[{"tool":"read_file","path":"backend/src/db/initialize.ts"}]}. '
                            'context_request discovers semantic records or file candidates.')
                    else:
                        context_result = discovery.resolve(query)
                elif "tool_calls" in response and files.effect(response["tool_calls"]) == "read":
                    calls = response["tool_calls"]
                    console.show(label + " | Read files", str([call["path"] for call in calls]))
                    # Partial failures are reported, not raised: existing files are still
                    # returned and no edit round is lost on a not-yet-created path.
                    results, failed = files.read_batch(calls)
                    observation = {"tool_results": results, "tool_errors": failed}
                    _remember_reads(memory_key, [item["result"]["path"] for item in results], task_id)
                    for item in results:
                        record = item["result"]
                        excerpts = read_files.setdefault(record["path"], [])
                        # Keep distinct ranges; a wider read replaces excerpts it covers.
                        excerpts[:] = [old for old in excerpts if not (
                            record["start_line"] <= old["start_line"] and record["end_line"] >= old["end_line"])]
                        excerpts.append(record)
                    context_result = {"read_paths": [item["result"]["path"] for item in results]}
                    if failed:
                        context_result["read_errors"] = [{"path": item["path"], **item["error"]} for item in failed]
                        context_result["guidance"] = ("Paths in read_errors were not read; other paths were. "
                            "FILE_NOT_FOUND means the file does not exist yet: if it is this task's file, "
                            "create it with edit_file operation=create; otherwise use a path from "
                            "similar_existing_paths. Do not request the same missing path again.")
                    observed = discovery.observe(observation)
                    if observed.get("next_action"):
                        context_result["next_action"] = observed["next_action"]
                else:
                    break
                response = request_model()
            if isinstance(response, dict) and response.get("diagnosis"):
                console.show(label + " | Failure diagnosis", json.dumps(response["diagnosis"], ensure_ascii=False, indent=2))
                diagnoses.append(response["diagnosis"])
                reason = stop_when(diagnoses) if stop_when else None
                if reason:
                    checkpoint(files.output, task=label, action=f"Escalate after round {turn}", status="ESCALATED",
                        details={"round": turn, "reason": reason, "diagnoses": diagnoses})
                    raise AgentEscalated(label, turn, feedback, reason, list(diagnoses))
            calls = response["tool_calls"]
            results = files.execute(calls)
            for change in files.last_changes:
                read_files.pop(change["path"], None)
            context_result = {"tool_results": results}
            changes = [change for change in files.last_changes if "/tests/" in change["path"]
                and change["before"] is not None and change["before"] != change["after"]]
            if changes:
                audit = files.output / ".arc/test_repairs.jsonl"
                audit.parent.mkdir(parents=True, exist_ok=True)
                with audit.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"task": label, "turn": turn, "diagnosis": response.get("diagnosis"),
                        "feedback": feedback, "changes": changes}, ensure_ascii=False) + "\n")
            applied = True
            if files.pending_target:
                raise ValueError("Create task.file_target with edit_file before running acceptance checks")
            console.show(f"{label} | Edit round {turn}/{max_turns}",
                str([change["path"] for change in files.last_changes]))
            checkpoint(files.output, task=label, action=f"Apply file edits in round {turn}", status="PROPOSED",
                details={"round": turn, "files": [change["path"] for change in files.last_changes],
                    "diagnosis": response.get("diagnosis")})
            checking = True
            checks = gate()
            checkpoint(files.output, task=label, action=f"Accept edit round {turn}",
                details={"round": turn, "checks": checks})
            return {"turns": turn, "checks": checks}
        except (AgentEscalated, EnvironmentFailure):
            # Escalations go to their owner; machine failures go to the user.
            raise
        except (*RESPONSE_ERRORS, RuntimeError, OSError) as error:
            message = str(error)
            console.show(f"{label} | Repair feedback", message)
            rejected = (getattr(error, "feedback", {"error": message}) if applied
                else response_feedback(error, response))
            if isinstance(response, dict) and response.get("diagnosis") and isinstance(rejected, dict):
                rejected = {**rejected, "previous_diagnosis": response["diagnosis"]}
            rejected = clean_feedback(feedback_filter(rejected) if feedback_filter else rejected)
            if checking:
                check_feedback = [rejected]
                feedback = list(check_feedback)
            else:
                feedback = [*check_feedback, rejected]
            checkpoint(files.output, task=label, action=f"Reject edit round {turn}", status="REJECTED",
                details={"round": turn, "applied": applied, "feedback": feedback})
            # The model client already applies its configured transport retries.
            # Repeating the same prompt cannot repair DNS, connection, or timeout failures.
            if _is_model_transport_error(error):
                raise AgentExhausted(label, turn, feedback,
                    reason=f"model transport failed; stopping instead of retrying edit rounds ({message})") from error
    raise AgentExhausted(label, max_turns, feedback)
