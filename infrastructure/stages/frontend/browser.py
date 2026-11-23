"""Whether Playwright's Chromium can run here, and the result used when it cannot.

Hosted runners (minimal Linux containers) often lack Chromium's OS libraries. Browser
checks then fail for environmental reasons, and treating them as application failures
makes every UI/trigger task exhaust its repairs, leaving pages without behavior. When no
browser can launch, compilation continues in a degraded mode: browser suites are reported
UNVERIFIED (never GREEN), so implementation agents still run and are gated by TypeScript,
backend HTTP tests and the production build instead.
"""

_state = {"available": None, "detail": ""}

UNAVAILABLE_NOTE = ("Browser checks were not run: Playwright Chromium cannot launch in this environment. "
    "Behavior is unverified; implement it from the requirement, design and API contracts.")


def browser_state() -> dict:
    """Current probe result: available is None (not probed), True or False."""
    return dict(_state)


def set_browser_state(available: bool, detail: str = "") -> None:
    _state.update(available=bool(available), detail=detail[-4000:])


def browser_available() -> bool:
    """False only after a probe proved that Chromium cannot launch."""
    return _state["available"] is not False


def unverified_result(files=()) -> dict:
    """Result for a browser suite that could not run. returncode None is neither pass nor fail;
    callers must treat it as unverified, never as GREEN evidence."""
    return {"returncode": None, "unverified": True, "passed": 0, "failed": 0, "skipped": 0,
        "errors": [], "failures": [], "files": {}, "selected": list(files),
        "output": UNAVAILABLE_NOTE + ("\n" + _state["detail"] if _state["detail"] else "")}


def is_unverified(result) -> bool:
    return isinstance(result, dict) and bool(result.get("unverified"))
