"""Isolate failed implementation tasks and complete independent final checks."""

from contextlib import contextmanager
import json
import re
from uuid import uuid4

from infrastructure.shared.agent.files import EXCLUDED
from infrastructure.utils.files import save_bytes, save_json
from infrastructure.shared.project.git import checkpoint


INDEX = ".arc/implementation_paths.json"
ALL_ROOTS = ("backend/src", "backend/tests", "frontend/src", "frontend/tests")


def _snapshot(output, roots=ALL_ROOTS, exclude=(), include_index=True):
    paths = {path.relative_to(output).as_posix()
        for root in roots
        for path in (output / root).rglob("*") if path.is_file() and not path.is_symlink()
        and not any(part in EXCLUDED or part.startswith(".") for part in path.relative_to(output).parts)}
    paths = {path for path in paths if not any(path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in exclude)}
    if include_index and (output / INDEX).is_file():
        paths.add(INDEX)
    return {path: (output / path).read_bytes() for path in paths}


def _restore_index_kinds(output, before, kinds):
    """Restore only this task's kinds in the shared path index; keep others' entries."""
    index = output / INDEX
    try:
        current = json.loads(index.read_text(encoding="utf-8-sig")) if index.is_file() else {}
        previous = json.loads(before) if before is not None else {}
    except ValueError:
        return
    for kind in kinds:
        if kind in previous:
            current[kind] = previous[kind]
        else:
            current.pop(kind, None)
    save_json(index, current)


@contextmanager
def implementation_task(output, entry, console, label, *, roots=ALL_ROOTS, exclude=(), index_kinds=None):
    """Archive rejected files, restore the task baseline, and continue on acceptance failure.

    Missing prerequisites are checked before this boundary. Disk errors, interrupts,
    and unexpected compiler exceptions propagate; they are not business failures.
    roots/exclude limit the files this task may roll back, and index_kinds the
    path-index kinds it owns, so stages running concurrently on other layers
    (e.g. backend TDD beside frontend UI work) never lose each other's edits.
    """
    scoped = index_kinds is not None
    from infrastructure.shared.project.entrypoints import _INDEX_LOCK
    before = _snapshot(output, roots, exclude, include_index=not scoped)
    index_before = (output / INDEX).read_bytes() if scoped and (output / INDEX).is_file() else None
    try:
        yield
    except ValueError as error:
        after = _snapshot(output, roots, exclude, include_index=not scoped)
        changed = {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
        name = re.sub(r"[^\w.-]+", "_", label).strip("._")[:100] or "task"
        directory = output / ".arc/failures" / f"{name}.{uuid4().hex[:8]}"
        for path in sorted(changed):
            if path in after:
                save_bytes(directory / "files" / path, after[path])
        failure = {"status": "FAILED", "task": label, "phase": entry.get("status"),
            "error": str(error), "error_type": type(error).__name__, "turns": getattr(error, "turns", 0),
            "feedback": getattr(error, "feedback", None), "changed_files": sorted(changed),
            "deleted_files": sorted(changed - after.keys()),
            "archive": directory.relative_to(output).as_posix()}
        if getattr(error, "diagnoses", None):
            failure["diagnoses"] = error.diagnoses
        save_json(directory / "failure.json", failure)
        for path in sorted(changed):
            if path in before:
                save_bytes(output / path, before[path])
            else:
                (output / path).unlink(missing_ok=True)
        if scoped:
            with _INDEX_LOCK:
                _restore_index_kinds(output, index_before, index_kinds)
        entry.update(failure)
        console.show(label + " | FAILED; continuing", str(error) + "\nBaseline restored. Failure archive: " + failure["archive"])
        checkpoint(output, task=label, action="Archive failed attempt and restore task baseline",
            status="FAILED", details=failure)
    except BaseException as error:
        checkpoint(output, task=label, action="Record interrupted implementation task",
            status="INTERRUPTED" if isinstance(error, KeyboardInterrupt) else "FAILED",
            details={"error": str(error), "error_type": type(error).__name__})
        raise
    else:
        checkpoint(output, task=label, action="Publish accepted implementation task",
            status=entry.get("status", "GREEN"), details=entry)


def final_checks(console, **checks):
    """Attempt every final compiler/test/build check even if an earlier check fails."""
    results = {}
    for name, (action, accept) in checks.items():
        result = None
        try:
            result = action()
            if result.get("returncode"):
                raise ValueError(result.get("output", f"{name} failed"))
            if accept:
                accept(result)
            results[name] = {"status": "GREEN", "result": result}
        except (ValueError, RuntimeError) as error:
            results[name] = {"status": "FAILED", "result": result, "error": str(error)}
        console.show("Final verification | " + name + " | " + results[name]["status"],
            results[name].get("error") or (result or {}).get("output", "Passed"))
        checkpoint(task="final verification", action="Run " + name,
            status=results[name]["status"], details=results[name])
    return results


def complete_report(report, entries, verification):
    """Separate completion of the process from successful acceptance."""
    report["failures"] = [dict(entry) for entry in entries if entry.get("status") == "FAILED"]
    report["verification"] = verification
    report["status"] = ("COMPLETED_WITH_FAILURES" if report["failures"]
        or any(check["status"] == "FAILED" for check in verification.values()) else "GREEN")
    return report
