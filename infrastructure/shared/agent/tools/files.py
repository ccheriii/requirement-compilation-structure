"""Workspace-relative file reading and exact local editing operations."""

from difflib import get_close_matches
from pathlib import PurePosixPath

from infrastructure.shared.agent.files import EXCLUDED
from infrastructure.utils.files import normalize_newlines, read_source
from infrastructure.utils.paths import check_path_case, relative_path


class FileToolError(ValueError):
    def __init__(self, code, message, path=None, **details):
        super().__init__(message)
        self.feedback = {"code": code, "message": message, **details}
        if path is not None:
            self.feedback["path"] = path


def workspace_path(root, path):
    """Translate a portable model path into a native path inside the project."""
    parts = relative_path(path).parts
    if any(part.startswith(".") or part in EXCLUDED for part in parts) or parts[:2] == ("backend", "data"):
        raise FileToolError("INVALID_PATH", "Use a project source/configuration path", path)
    check_path_case(root, path)
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise FileToolError("INVALID_PATH", "Symlinks are not allowed", path)
    if not current.resolve().is_relative_to(root.resolve()) or current.is_dir():
        raise FileToolError("INVALID_PATH", "Use a file inside the working directory", path)
    return current


SUGGESTION_ROOTS = ("frontend/src", "frontend/tests", "backend/src", "backend/tests")


def similar_paths(files, path, limit=5):
    """Suggest existing project paths close to a missing one, so one retry can succeed.

    A bare "file does not exist" made models guess again, spending a whole round
    (and the full context) per guess.
    """
    candidates = set(getattr(files, "files", ()))
    for root in SUGGESTION_ROOTS:
        directory = files.output / root
        if not directory.is_dir():
            continue
        for item in directory.rglob("*"):
            relative = item.relative_to(files.output)
            if item.is_file() and not any(part.startswith(".") or part in EXCLUDED for part in relative.parts):
                candidates.add(relative.as_posix())
    if files.readable is not None:
        candidates &= set(files.readable)
    name = PurePosixPath(path).name
    same_name = sorted(candidate for candidate in candidates if PurePosixPath(candidate).name == name)
    close = get_close_matches(path, sorted(candidates), n=limit, cutoff=0.6)
    return list(dict.fromkeys([*same_name, *close]))[:limit]


def writable_listing(files, limit=40):
    """Explicitly writable paths for this task; pattern-based permissions are described in words."""
    paths = sorted(files.writable)
    listing = {"writable_paths": paths[:limit]}
    if len(paths) > limit:
        listing["more_writable_paths"] = len(paths) - limit
    if files.allow_write is not None:
        listing["note"] = ("Other application source paths may also be writable; input.files[path].writable "
            "and read_file results report writability. Compiler-owned fixtures and tests stay read-only.")
    return listing


def keeps_every_line(original, updated):
    """True if every non-blank original line still appears in updated, in the same order."""
    remaining = iter(line.rstrip() for line in updated.splitlines())
    return all(any(line == candidate for candidate in remaining)
        for line in (line.rstrip() for line in original.splitlines()) if line.strip())


def read_file(files, path, start_line=1, end_line=None):
    """Read the requested lines only; reading never expands the pinned source context."""
    target = files.path(path)
    if files.readable is not None and path not in files.readable:
        readable = sorted(files.readable)
        raise FileToolError("READ_NOT_ALLOWED", "This task cannot read this file; read only readable_paths",
            path, readable_paths=readable[:40], **({"more_readable_paths": len(readable) - 40} if len(readable) > 40 else {}))
    if not target.is_file():
        details = {}
        suggestions = similar_paths(files, path)
        if suggestions:
            details["similar_existing_paths"] = suggestions
        raise FileToolError("FILE_NOT_FOUND", "File does not exist; use edit_file/create for a new file"
            + ("; did you mean one of similar_existing_paths?" if suggestions else ""), path, **details)
    if type(start_line) is not int or start_line < 1 or (end_line is not None and
            (type(end_line) is not int or end_line < start_line)):
        raise FileToolError("INVALID_LINE_RANGE", "Use positive, inclusive line numbers", path)
    lines = read_source(target).splitlines(keepends=True)
    last = min(end_line if end_line is not None else len(lines), len(lines))
    return {"path": path, "content": "".join(lines[start_line - 1:last]), "start_line": start_line,
        "end_line": last, "total_lines": len(lines), "writable": files.can_write(path)}


def edit_file(files, path, operation, new_content, old_content=""):
    """Stage a create, unique exact replacement, prepend or append in the current batch."""
    files.path(path)  # Reject unsafe paths before attempting task-specific adaptation.
    requested = path
    if operation == "create" and files.pending_target and files._target_path is None:
        path = files.target.adapt(path)
        files.path(path)
        files._target_path = path
    if not files.can_write(path) and path != files._target_path:
        raise FileToolError("WRITE_NOT_ALLOWED", "File is not writable for this task. Edit one of "
            "writable_paths instead, or fix the behavior from the writable side.", path, **writable_listing(files))
    if not isinstance(new_content, str) or not isinstance(old_content, str):
        raise FileToolError("INVALID_ARGUMENTS", "old_content and new_content must be strings", path)
    new_content, old_content = normalize_newlines(new_content), normalize_newlines(old_content)
    target = files.path(path)
    current = files._pending.get(path, read_source(target) if target.is_file() else None)
    if operation == "create":
        if current is not None:
            raise FileToolError("FILE_ALREADY_EXISTS", "Read the existing file and use replace", path)
        if old_content:
            raise FileToolError("INVALID_ARGUMENTS", "create requires empty old_content", path)
        updated = new_content
    elif operation in {"replace", "prepend", "append"}:
        if current is None:
            raise FileToolError("FILE_NOT_FOUND", "Use create for a missing file", path)
        if operation == "replace":
            if not old_content:
                raise FileToolError("INVALID_ARGUMENTS", "replace requires nonempty exact old_content", path)
            if current.strip() and old_content.strip() == current.strip() and not keeps_every_line(current, new_content):
                # A full rewrite that keeps every existing line (pure additions) is safe;
                # one that drops or changes earlier lines is what this rule prevents.
                raise FileToolError("WHOLE_FILE_REPLACEMENT_NOT_ALLOWED",
                    "Incremental compilation forbids replacing a whole existing file in a way that removes or "
                    "changes existing lines. Keep every existing line (additions only), or edit a smaller unique "
                    "span, preserving earlier requirements, registrations and state.", path)
            first = current.find(old_content)
            count = 0 if first < 0 else (2 if current.find(old_content, first + 1) >= 0 else 1)
            if count != 1:
                raise FileToolError("OLD_CONTENT_NOT_FOUND" if count == 0 else "OLD_CONTENT_NOT_UNIQUE",
                    f"Expected exactly one old_content match, found {count}; read the relevant lines and retry", path)
            updated = current.replace(old_content, new_content, 1)
        else:
            if old_content:
                raise FileToolError("INVALID_ARGUMENTS", "Boundary insertion requires empty old_content", path)
            updated = new_content + current if operation == "prepend" else current + new_content
    else:
        raise FileToolError("INVALID_OPERATION", "Use create, replace, prepend or append", path)
    files._pending[path] = updated
    result = {"path": path, "operation": operation}
    if path != requested:
        result["requested_path"] = requested
        result["notice"] = "Adapted to this task's required destination; use path in future calls and imports"
    return result

