"""Retry model proposals when parsing or consuming their returned records fails."""

import json

from infrastructure.shared.agent.discovery import DISCOVERY_RULES, DiscoverySession
from infrastructure.shared.project.git import checkpoint


RESPONSE_ERRORS = (ValueError, KeyError, TypeError, AttributeError, IndexError)
# Model request failures (RuntimeError) are retried, but a provider that keeps failing
# after the model client's own transient backoff is not worth more paid attempts.
MAX_CONSECUTIVE_REQUEST_FAILURES = 3
REPAIR_INSTRUCTIONS = """The previous proposal was rejected. Read correction.error and correction.details
in the input and fix that failure. correction.previous_response is the rejected
answer, not an accepted example. Return a complete replacement JSON object in the
original output format, including every required top-level section. Do not return
only the unchanged portion, a partial patch, or the correction metadata itself.
DSML/XML tool-call text is forbidden. If requesting context, return the allowed
context_request JSON object directly, never invoke/parameter tags. Otherwise return
the complete stage result. invalid_response is rejected data; never copy its format."""


def request_json(model, instructions, request, **options):
    """Call model.generate_json, marking a TypeError raised by the call itself as a compiler bug.

    A signature mismatch raises TypeError in this frame before the callee runs, so its
    traceback has no deeper frame. Such errors must not be retried as bad model output.
    """
    try:
        return model.generate_json(instructions, request, **options)
    except TypeError as error:
        if error.__traceback__ is not None and error.__traceback__.tb_next is None:
            error.compiler_bug = True
        raise
    except RuntimeError as error:
        # Model clients raise RuntimeError for failed requests; tag them so callers do not
        # mistake RuntimeErrors from acceptance or checkpoints for provider failures.
        error.model_request_failed = True
        raise


def model_request_failed(error):
    """True for a RuntimeError raised by the model client's request, not by acceptance code."""
    return bool(getattr(error, "model_request_failed", False))


def compiler_bug(error):
    """True for errors that came from calling the model client incorrectly, not from its answer."""
    return bool(getattr(error, "compiler_bug", False))


def response_feedback(error, response):
    """Describe an actual response-consumption failure, without adding design rules."""
    message = str(error)
    if isinstance(error, KeyError):
        key = error.args[0] if error.args else "(unknown)"
        message = f"Missing key {key!r} while reading the model response. Return the requested complete records."
    if isinstance(error, json.JSONDecodeError):
        message += (" Output must be one valid JSON object, without DSML/XML tool-call tags, "
            "Markdown or text outside the object. For an allowed context query return "
            '{"context_request":{"kind":"<advertised kind>","query":"<needed fact>"}}. '
            "Otherwise return the stage result as JSON. Do not repeat the rejected format.")
    feedback = {"error": message, "error_type": type(error).__name__, "previous_response": response}
    if isinstance(error, json.JSONDecodeError):
        feedback["invalid_response"] = error.doc
    if hasattr(error, "feedback"):
        feedback["details"] = error.feedback
    return feedback


def propose_json(model, instructions, request, accept, console, label, max_attempts=10, context_index=None, task_complexity="high"):
    """Generate and repair a proposal within a finite total model-call budget."""
    if max_attempts < 1:
        raise ValueError("--max-attempts must be positive")
    request = dict(request)
    if context_index is not None:
        instructions += "\n\n" + DISCOVERY_RULES
        request["context_kinds"] = list(context_index.records())
    discovery = DiscoverySession(context_index) if context_index is not None else None
    request_failures = 0
    for attempt in range(1, max_attempts + 1):
        progress = f"{attempt}/{max_attempts}"
        console.show(f"{label} | Model proposal {progress}", "Generate the proposal; response-reading and obligation errors receive repair feedback.")
        response = None
        try:
            prompt = instructions if "correction" not in request else instructions + "\n\n" + REPAIR_INSTRUCTIONS
            if "correction" in request:
                correction = request["correction"]
                prompt += "\nActual failure: " + correction["error"]
                if correction.get("details"):
                    prompt += "\nRepair details:\n" + json.dumps(correction["details"], ensure_ascii=False, indent=2)
            response = request_json(model, prompt, request, task_complexity=task_complexity)
            while context_index is not None and isinstance(response, dict) and "context_request" in response:
                query = response["context_request"]
                console.show(label + " | Context query", json.dumps(query, ensure_ascii=False))
                request["context_result"] = discovery.resolve(query)
                request["context_records"] = discovery.evidence
                request["context_candidates"] = discovery.candidates
                response = request_json(model, prompt, request, task_complexity=task_complexity)
            accepted = accept(response)
            checkpoint(task=label, action=f"Accept design proposal {attempt}",
                details={"attempt": attempt, "sections": list(response) if isinstance(response, dict) else []})
            return accepted, response
        except RuntimeError as error:
            if not model_request_failed(error):
                raise  # acceptance/checkpoint failures keep their previous behaviour
            # The request itself failed (after the client's transient retries); the model
            # produced no answer, so keep any earlier correction instead of blaming it.
            request_failures += 1
            details = {"error": str(error), "error_type": type(error).__name__, "failed_attempt": attempt}
            checkpoint(task=label, action=f"Model request failed in proposal {attempt}", status="REJECTED", details=details)
            console.show(f"{label} | Model request failed", str(error))
            if attempt >= max_attempts or request_failures >= MAX_CONSECUTIVE_REQUEST_FAILURES:
                raise RuntimeError(f"{label}: model request failed {request_failures} time(s) in a row: {error}") from error
        except RESPONSE_ERRORS as error:
            if compiler_bug(error):
                raise
            request_failures = 0
            feedback = response_feedback(error, response)
            feedback["failed_attempt"] = attempt
            checkpoint(task=label, action=f"Reject design proposal {attempt}", status="REJECTED", details=feedback)
            console.show(f"{label} | Repair feedback", json.dumps(feedback, ensure_ascii=False, indent=2))
            if attempt >= max_attempts:
                raise RuntimeError(f"{label} exhausted {max_attempts} proposals: {feedback['error']}") from error
            request["correction"] = feedback
