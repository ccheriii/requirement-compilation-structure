"""Production-mode acceptance, the way an external grader runs the generated application.

Build the workspace with ``npm run build``, start ``node backend/dist/server.js`` on a free
port with a new empty database (no compiler test environment), check that the root page and
every concrete page route serve the single-page application, then run the supplied specs
(if any) against that one origin. The server process tree is always stopped.
"""

import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from infrastructure.shared.agent.context import SourceFiles
from infrastructure.stages.frontend.browser import UNAVAILABLE_NOTE, browser_available
from infrastructure.shared.agent.discovery import project_index
from infrastructure.shared.agent.loop import run_file_agent
from infrastructure.shared.agent.recovery import implementation_task
from infrastructure.stages.frontend.triggers.e2e_runtime import PROD_BASE_URL_ENV, PROD_CONFIG, PROD_CONFIG_SOURCE
from infrastructure.stages.frontend.triggers.implementation import IMPLEMENT_INSTRUCTIONS, editable_runtime_path
from infrastructure.stages.frontend.triggers.scope import TriggerCheckFailure
from infrastructure.stages.frontend.triggers.supplied import normalize_supplied, protect_workspace
from infrastructure.stages.frontend.ui.runtime import APPLICATION_ENTRYPOINTS
from infrastructure.stages.frontend.ui.testing import parse_playwright_result
from infrastructure.utils.files import save_json, save_text
from infrastructure.utils.processes import capture_process, npm_command, process_options, stop_process

SERVER_ENTRY = "backend/dist/server.js"
PLAYWRIGHT_CLI = "node_modules/@playwright/test/cli.js"
BACKEND_ENTRYPOINTS = ("backend/src/api/register.ts", "backend/src/app.ts", "backend/src/server.ts")
BUILD_TIMEOUT = 900
TEST_TIMEOUT = 900
STARTUP_SECONDS = 60
ANSI = re.compile(r"\x1b\[[0-9;]*m")

PRODUCTION_NOTE = """Production-readiness repair. An external grader runs `npm run build` at the
workspace root, then starts `node backend/dist/server.js` with PORT and DATABASE_URL pointing to
a new empty database file, and drives the application in a browser on that single origin.
task.failure names the failing stage: build (frontend or backend compile errors), startup
(the built server crashes, does not answer /api/health, or cannot initialize and seed
requirement-defined startup data on an empty database), routes (a page route does not serve
the single-page application's index.html with its root element on direct load or reload), or
supplied_specs (authoritative read-only grader specs fail). Fix the cause at its source in the
application. Do not delete features, requirement-defined data or tests, and never edit supplied
specs: match their roles, accessible names, routes and flows. Strict-mode violations mean
duplicate accessible names on the page."""


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def concrete_routes(page_routes):
    """Return the root page plus every literal page route (parameterized patterns are skipped)."""
    routes = ["/"]
    for route in page_routes or ():
        if not isinstance(route, str) or not route.strip():
            continue
        route = route.strip()
        if not route.startswith("/"):
            route = "/" + route
        if any(marker in route for marker in (":", "{", "}", "*", "[", "?", "#")):
            continue
        if route not in routes:
            routes.append(route)
    return routes


def page_routes(ui):
    return [page.get("route") for page in (ui or {}).get("pages", {}).values() if isinstance(page, dict)]


def _get(url, timeout=10):
    """Return (status, content type, body text) without raising on HTTP error statuses."""
    request = urllib.request.Request(url, headers={"Accept": "text/html,application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read(400000).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        body = error.read(20000).decode("utf-8", "replace") if error.fp else ""
        return error.code, error.headers.get("Content-Type", "") if error.headers else "", body


def _tail(path, size=6000):
    try:
        return Path(path).read_bytes()[-size:].decode("utf-8", "replace")
    except OSError:
        return ""


def _wait_for_health(base, process, seconds=STARTUP_SECONDS):
    deadline = time.monotonic() + seconds
    last = "no response"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Production server exited with code {process.returncode} before answering /api/health")
        try:
            status, _type, body = _get(base + "/api/health", timeout=2)
            if status == 200:
                return
            last = f"status {status}: {body[:300]}"
        except (OSError, ValueError) as error:
            last = str(error)
        time.sleep(0.5)
    raise RuntimeError(f"Production server did not answer {base}/api/health within {seconds}s ({last})")


def _has_root(body):
    lowered = body.lower()
    return any(marker in lowered for marker in ('id="root"', "id='root'", "id=root"))


def production_check(output, page_routes=(), supplied_specs=None, *, build=True):
    """Build, start the built server on a fresh database, probe pages, run supplied specs.

    Returns {"returncode", "stage", "output", "passed", "failed", "failures", "routes", ...};
    returncode 0 means every step passed. Never raises for application failures."""
    output = Path(output).resolve()
    supplied = normalize_supplied(supplied_specs)
    result = {"returncode": 1, "stage": "build", "output": "", "passed": 0, "failed": 0,
        "failures": [], "routes": {}}

    def fail(message, **extra):
        result.update(returncode=1, output=message[-24000:], failures=[message[-8000:]], **extra)
        return result

    if build:
        try:
            built = capture_process([*npm_command(), "run", "build"], cwd=output, timeout=BUILD_TIMEOUT)
        except subprocess.TimeoutExpired:
            return fail(f"npm run build timed out after {BUILD_TIMEOUT}s")
        except (OSError, ValueError) as error:
            return fail(f"npm run build could not start: {error}")
        if built["returncode"]:
            return fail("npm run build failed:\n" + built["output"])
    result["stage"] = "startup"
    if not (output / SERVER_ENTRY).is_file():
        return fail(f"{SERVER_ENTRY} does not exist after the build")
    node = shutil.which("node")
    if not node:
        return fail("Node.js is required for the production check")
    directory = Path(tempfile.mkdtemp(prefix="arc-prod-"))
    log_path = directory / "server.log"
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    # The grader's environment: no compiler test variables, a fresh database file.
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("ARC_")}
    env.update(PORT=str(port), DATABASE_URL=str(directory / "app.db"))
    server, log = None, None
    try:
        log = open(log_path, "wb")
        server = subprocess.Popen([node, SERVER_ENTRY], cwd=output, env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, **process_options())
        _wait_for_health(base, server)
        result["stage"] = "routes"
        problems = []
        for route in concrete_routes(page_routes):
            try:
                status, content_type, body = _get(base + route)
            except (OSError, ValueError) as error:
                status, content_type, body = None, "", str(error)
            ok = status == 200 and "html" in content_type.lower() and _has_root(body)
            result["routes"][route] = {"status": status, "content_type": content_type, "ok": ok}
            if not ok:
                problems.append(f"GET {route} returned {status} ({content_type or 'no content type'}); "
                    f"expected 200 HTML containing the root element. Body: {body[:300]}")
        if problems:
            return fail("Page routes are not served by the production server:\n" + "\n".join(problems))
        if supplied["all_specs"] and not browser_available():
            # Build and HTTP route checks above still ran; specs need a launchable browser.
            result["supplied_specs"] = "UNVERIFIED: " + UNAVAILABLE_NOTE
        elif supplied["all_specs"]:
            result["stage"] = "supplied_specs"
            if not (output / PLAYWRIGHT_CLI).is_file():
                return fail(f"{PLAYWRIGHT_CLI} is missing; install workspace dependencies")
            save_text(output / PROD_CONFIG, PROD_CONFIG_SOURCE)
            print("Production acceptance | built server + fresh database | " + ", ".join(supplied["all_specs"]), flush=True)
            try:
                # Name the installed specs explicitly: only compiler-installed supplied
                # files run here, even if another file ever lands in that directory.
                run = capture_process([node, PLAYWRIGHT_CLI, "test", "--config", PROD_CONFIG,
                    *supplied["all_specs"]], cwd=output, timeout=TEST_TIMEOUT, env={PROD_BASE_URL_ENV: base})
            except subprocess.TimeoutExpired:
                return fail(f"Supplied specs timed out after {TEST_TIMEOUT}s")
            try:
                parsed = parse_playwright_result(run, include_files=True)
            except ValueError as error:
                return fail(str(error))
            result.update({key: parsed.get(key) for key in ("passed", "failed", "skipped", "flaky", "errors", "files")})
            result["failures"] = parsed.get("failures", [])
            result["output"] = parsed.get("output", "")
            if parsed["returncode"] or parsed.get("failed") or parsed.get("errors") or parsed.get("flaky"):
                result["returncode"] = 1
                if not result["failures"]:
                    result["failures"] = [str(item) for item in parsed.get("errors") or []] or [result["output"][-4000:]]
                return result
        result.update(returncode=0, stage="complete", output=(f"Production server OK; routes {list(result['routes'])}; "
            f"supplied specs passed={result['passed']} failed={result['failed']}"))
        return result
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        return fail(f"{type(error).__name__}: {error}")
    finally:
        if server is not None:
            try:
                stop_process(server)
            except Exception:  # cleanup must never mask the check result
                try:
                    server.kill()
                except OSError:
                    pass
        if log is not None:
            log.close()
        result["server_log"] = _tail(log_path)
        shutil.rmtree(directory, ignore_errors=True)


def compact_production(result):
    """Small, failure-focused view of a production check for reports and repair feedback."""
    keys = ("returncode", "stage", "passed", "failed", "skipped", "flaky", "routes")
    value = {key: result.get(key) for key in keys if key in result}
    value["failures"] = [ANSI.sub("", str(item))[:4000] for item in (result.get("failures") or [])[:10]]
    if not value["failures"]:
        value["output"] = ANSI.sub("", str(result.get("output", "")))[-6000:]
    if result.get("returncode") and result.get("server_log"):
        value["server_log"] = ANSI.sub("", result["server_log"])[-4000:]
    return value


def require_production(result):
    if result.get("returncode"):
        raise TriggerCheckFailure("Production acceptance failed at stage " + str(result.get("stage")),
            compact_production(result))
    return result


def run_production_acceptance(arc_dir, ui, supplied, model, console, max_turns=10):
    """Run the production check; on failure run ONE bounded repair agent gated by it.

    Returns a report entry; never raises for check, model or repair failures."""
    output = arc_dir.parent.resolve()
    supplied = normalize_supplied(supplied)
    routes = page_routes(ui)
    entry = {"status": "CHECKING", "supplied_specs": supplied["all_specs"], "routes": concrete_routes(routes)}
    try:
        console.show("Production acceptance", "Build, start node " + SERVER_ENTRY + " on a fresh database, "
            "check page routes and run supplied specs against the built application.")
        first = production_check(output, routes, supplied)
        entry["initial"] = compact_production(first)
        if not first["returncode"]:
            entry.update(status="GREEN", result=compact_production(first))
        else:
            entry["status"] = "REPAIRING"
            selection = SourceFiles(output, ui)
            backend_selection = SourceFiles(output, root="backend/src", tests_root="backend/tests")
            context_index = project_index(output,
                kinds=("schema", "api", "page", "ui_element", "ui_data", "trigger", "requirement",
                    "test_requirement", "convention", "file"),
                file_roots=("frontend/src", "frontend/tests", "backend/src", "backend/tests"))
            references = [path for path in [*supplied["all_specs"], *supplied["helpers"], "package.json",
                "frontend/package.json", "frontend/tsconfig.json", "frontend/vite.config.ts",
                "frontend/index.html", "backend/package.json", "backend/tsconfig.json"] if (output / path).is_file()]
            workspace = selection.workspace(paths=APPLICATION_ENTRYPOINTS, references=references)
            workspace.allow_write = lambda relative: editable_runtime_path(output, relative)
            backend_selection.include(workspace, [path for path in BACKEND_ENTRYPOINTS if (output / path).is_file()])
            protect_workspace(workspace)

            def gate():
                return require_production(production_check(output, routes, supplied))

            def feedback(value):
                return selection.feedback(workspace, backend_selection.feedback(workspace, value))

            task = {"task": "production_acceptance", "failure": compact_production(first),
                "page_routes": entry["routes"], "supplied_acceptance_specs": supplied["all_specs"],
                "production_command": "npm run build; node " + SERVER_ENTRY + " (PORT and DATABASE_URL set, empty database)",
                "application_entrypoints": list(APPLICATION_ENTRYPOINTS),
                "backend_entrypoints": {"registry": BACKEND_ENTRYPOINTS[0], "app": BACKEND_ENTRYPOINTS[1],
                    "server": BACKEND_ENTRYPOINTS[2]}}
            label = "Production acceptance / Repair"
            with implementation_task(output, entry, console, label):
                try:
                    repair = run_file_agent(model, IMPLEMENT_INSTRUCTIONS + "\n\n" + PRODUCTION_NOTE, task,
                        workspace, gate, max_turns * 2, console, label,
                        feedback={"error": "Production acceptance failed at stage " + str(first.get("stage")),
                            "checks": compact_production(first)},
                        feedback_filter=feedback, context_index=context_index)
                except RuntimeError as error:
                    # implementation_task restores the baseline only for ValueError/OSError; a
                    # model/runtime failure must not leave a half-applied final repair behind.
                    raise ValueError(f"Production repair stopped: {error}") from error
                entry.update(status="GREEN", repair_turns=repair["turns"],
                    result=compact_production(repair["checks"]), changed_files=sorted(workspace.changed_paths))
            if entry["status"] != "GREEN":
                entry["status"] = "FAILED"
    except Exception as error:  # an optional final check must never crash the stage
        entry.update(status="FAILED", error=f"{type(error).__name__}: {error}"[:4000])
    try:
        save_json(arc_dir / "production_acceptance.json", entry)
    except OSError:
        pass
    console.show("Production acceptance | " + entry["status"],
        entry.get("error") or str((entry.get("result") or entry.get("initial") or {}).get("output", ""))[:2000] or "Completed")
    return entry
