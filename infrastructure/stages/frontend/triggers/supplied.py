"""Externally supplied acceptance specs: discovery, requirement mapping and installation.

An input directory may contain grader-style Playwright specs under <input_dir>/tests.
They are authoritative acceptance contracts. The compiler copies every TypeScript file
(specs and their helpers, keeping the relative layout) into a read-only directory of the
generated application on every run. Specs map to requirements through a
``// requirement: <RID>`` comment near the top of the file or a ``<RID>.spec.ts`` name.
"""

from pathlib import Path
import re
import shutil

from infrastructure.shared.agent.files import protected_test_path

SUPPLIED_DIR = "frontend/tests/e2e/supplied"
REQUIREMENT_PATTERN = re.compile(r"requirement:\s*([A-Za-z0-9_.:-]+)", re.IGNORECASE)
SPEC_SUFFIXES = (".spec.ts", ".test.ts", ".spec.mts", ".test.mts")
COPIED_SUFFIXES = {".ts", ".mts", ".cts"}
HEADER_LINES = 15
IGNORED_PARTS = {"node_modules", "dist", "build", "coverage", "test-results", "playwright-report"}


def empty_supplied():
    """Return the no-supplied-specs value used by every consumer."""
    return {"by_requirement": {}, "unmapped": [], "helpers": [], "all_specs": []}


def _tests_dir(input_dir):
    return Path(input_dir) / "tests"


def _discover(tests):
    """Return input-relative TypeScript files below the tests directory."""
    if not tests.is_dir():
        return []
    files = []
    for path in sorted(tests.rglob("*")):
        relative = path.relative_to(tests)
        if (path.is_file() and not path.is_symlink() and path.suffix in COPIED_SUFFIXES
                and not any(part.startswith(".") or part in IGNORED_PARTS for part in relative.parts)):
            files.append(relative.as_posix())
    return files


def _is_spec(relative):
    return relative.endswith(SPEC_SUFFIXES)


def _spec_stem(relative):
    name = relative.rsplit("/", 1)[-1]
    for suffix in SPEC_SUFFIXES:
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return name


def _resolve_id(candidate, requirement_ids):
    """Match a declared requirement id against known ids, tolerating trailing punctuation."""
    options = [candidate, candidate.rstrip(".:-")]
    if not requirement_ids:
        return options[-1] or None
    for option in options:
        if option in requirement_ids:
            return option
    lowered = {rid.lower(): rid for rid in requirement_ids}
    for option in options:
        if option.lower() in lowered:
            return lowered[option.lower()]
    return None


def _spec_requirements(path, relative, requirement_ids):
    """Map one spec to requirements by its header comment, else by its filename stem."""
    try:
        header = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()[:HEADER_LINES]
    except OSError:
        header = []
    found = []
    for line in header:
        for candidate in REQUIREMENT_PATTERN.findall(line):
            rid = _resolve_id(candidate, requirement_ids)
            if rid and rid not in found:
                found.append(rid)
    if not found:
        rid = _resolve_id(_spec_stem(relative), requirement_ids)
        if rid and (not requirement_ids or rid in requirement_ids):
            found.append(rid)
    return found


def supplied_specs(input_dir, requirement_ids=()):
    """Discover <input_dir>/tests/**/*.ts and map each spec to requirements.

    Paths are relative to <input_dir>/tests. Returns
    {"by_requirement": {rid: [specs]}, "unmapped": [...], "helpers": [...], "all_specs": [...]}.
    """
    tests = _tests_dir(input_dir)
    known = list(requirement_ids or ())
    result = empty_supplied()
    for relative in _discover(tests):
        if not _is_spec(relative):
            result["helpers"].append(relative)
            continue
        result["all_specs"].append(relative)
        rids = _spec_requirements(tests / relative, relative, known)
        if not rids:
            result["unmapped"].append(relative)
        for rid in rids:
            result["by_requirement"].setdefault(rid, []).append(relative)
    return result


def install_supplied_specs(input_dir, output, requirement_ids=()):
    """Copy every supplied TypeScript file into frontend/tests/e2e/supplied (recreated each run).

    Returns the supplied_specs() structure with output-relative paths. When the input has
    no tests directory, any stale installed copy is removed and the empty value returned.
    """
    output = Path(output)
    destination = output / SUPPLIED_DIR
    if destination.exists():
        shutil.rmtree(destination)
    discovered = supplied_specs(input_dir, requirement_ids)
    tests = _tests_dir(input_dir)
    copied = set()
    for relative in [*discovered["helpers"], *discovered["all_specs"]]:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tests / relative, target)
        copied.add(relative)
    if not copied:
        return empty_supplied()

    def installed(relative):
        return f"{SUPPLIED_DIR}/{relative}"

    return {"by_requirement": {rid: [installed(path) for path in paths]
            for rid, paths in discovered["by_requirement"].items()},
        "unmapped": [installed(path) for path in discovered["unmapped"]],
        "helpers": [installed(path) for path in discovered["helpers"]],
        "all_specs": [installed(path) for path in discovered["all_specs"]]}


def normalize_supplied(supplied):
    """Accept None or a partial structure from older callers."""
    value = empty_supplied()
    if isinstance(supplied, dict):
        for key in value:
            if isinstance(supplied.get(key), type(value[key])):
                value[key] = supplied[key]
    return value


def requirement_specs(supplied, rid):
    """Return the supplied spec paths that define acceptance for one requirement."""
    return list(normalize_supplied(supplied)["by_requirement"].get(rid, []))


def has_supplied(supplied):
    return bool(normalize_supplied(supplied)["all_specs"])


def protect_workspace(workspace):
    """Make supplied specs/helpers and acceptance suites unwritable in an agent workspace.

    Shared source selection may mark any frontend/tests spec as a writable generated
    suite; this per-instance guard keeps compiler-installed contracts read-only.
    """
    original = workspace.can_write

    def can_write(path):
        return not protected_test_path(path) and original(path)

    workspace.can_write = can_write
    return workspace
