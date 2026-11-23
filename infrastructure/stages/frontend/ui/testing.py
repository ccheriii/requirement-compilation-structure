"""Deterministic UI existence suites and bounded browser/typecheck commands."""

from infrastructure.shared.agent.feedback import CheckFailure

import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile

from infrastructure.stages.frontend.browser import (browser_available, browser_state, is_unverified,
    set_browser_state, unverified_result)
from infrastructure.stages.frontend.ui.design import (accessible_name, accessible_role, data_symbols,
    option_names, page_elements, semantic_scope, visible)
from infrastructure.utils.processes import capture_process, npm_command

KEY_TYPES = {"page", "form", "input", "textarea", "checkbox", "select", "radiogroup", "button", "heading",
    "link", "list", "image", "alert", "table", "text"}
# The version suffix lets step 08 replace compiler-owned suites written by an older generator.
SUITE_PREFIX = "// Compiler-owned presence tests"
SUITE_HEADER = SUITE_PREFIX + " v2. Display values belong to this suite only."


def presentation_run_status(result):
    """Repair existing page runtime failures; only assertion failures establish valid RED."""
    if is_unverified(result):
        return "UNVERIFIED"
    if result["returncode"] == 0:
        require_green(result)
        return "GREEN"
    headlines = [message.split("\n", 1)[0] for message in result.get("failures", [])]
    if not result.get("errors") and not result.get("skipped") and not result.get("flaky") and result.get("failed") and any("UI_RUNTIME_ERROR" in message or "UI_BUSINESS_CALL" in message for message in headlines):
        return "REPAIR_REQUIRED"
    require_red(result)
    return "RED_CONFIRMED"


def test_path(pid: str) -> str:
    """Return a stable, filesystem-safe Playwright spec path for a page ID."""
    return f"frontend/tests/ui/{pid}.spec.ts"


def stale_compiler_suite(existing: str) -> bool:
    """Return whether an existing suite is compiler-owned but written by another generator version."""
    first = existing.lstrip("\ufeff").split("\n", 1)[0].rstrip("\r")
    return first.startswith(SUITE_PREFIX) and first != SUITE_HEADER


def static_href(prop: dict) -> str | None:
    """Return a literal application path suitable for an href assertion."""
    href = prop.get("href")
    if isinstance(href, str) and href.startswith("/") and not any(mark in href for mark in (":", "*", "{", "[")):
        return href
    return None


def accessible_contract(design: dict, pid: str) -> list[dict]:
    """List the page's functional controls with the role/name pairs that tests and graders use."""
    contract = []
    for uid, node in page_elements(design, pid).items():
        role, name = accessible_role(node), accessible_name(node)
        scope = semantic_scope(design, uid)
        if role is None or name is None or scope == "decorative":
            continue
        prop = node.get("prop") if isinstance(node.get("prop"), dict) else {}
        entry = {"id": uid, "role": "textbox (type=password, located by label)" if role == "password" else role,
            "name": name}
        for key in ("input_type", "placeholder"):
            if isinstance(prop.get(key), str):
                entry[key] = prop[key]
        if role in ("combobox", "radiogroup") and option_names(prop.get("options")):
            entry["options"] = option_names(prop.get("options"))
        if role == "link" and isinstance(prop.get("href"), str):
            entry["href"] = prop["href"]
        if "visible" in prop and prop["visible"] is not True:
            entry["visible_when"] = prop["visible"]
        if scope == "repeated":
            entry["repeated"] = True
        contract.append(entry)
    return contract


def role_assertions(role: str, name: str, prop: dict) -> list[str]:
    """Render deterministic role/name assertions for one visible functional element."""
    label = json.dumps(name, ensure_ascii=False)
    if role == "password":
        return [f"  {{ const field = page.getByLabel({label}, {{ exact: true }});",
            "    await expect(field).toHaveCount(1);",
            '    await expect(field).toHaveAttribute("type", "password"); }']
    locator = f"page.getByRole({json.dumps(role)}, {{ name: {label}, exact: true }})"
    if role in ("combobox", "radiogroup"):
        child = "option" if role == "combobox" else "radio"
        lines = [f"  {{ const control = {locator};", "    await expect(control).toHaveCount(1);"]
        for option in option_names(prop.get("options")):
            lines.append(f"    await expect(control.getByRole({json.dumps(child)}, {{ name: {json.dumps(option, ensure_ascii=False)}, exact: true }})).toHaveCount(1);")
        lines[-1] += " }"
        return lines
    if role == "link" and static_href(prop):
        return [f"  {{ const link = {locator};", "    await expect(link).toHaveCount(1);",
            f'    await expect(link).toHaveAttribute("href", {json.dumps(static_href(prop), ensure_ascii=False)}); }}']
    return [f"  await expect({locator}).toHaveCount(1);"]


def absence_assertion(role: str, name: str) -> str:
    """Render the assertion that a hidden functional element is absent from the accessibility tree."""
    label = json.dumps(name, ensure_ascii=False)
    if role == "password":
        return f"  await expect(page.getByLabel({label}, {{ exact: true }})).toHaveCount(0);"
    return f"  await expect(page.getByRole({json.dumps(role)}, {{ name: {label}, exact: true }})).toHaveCount(0);"


# Playwright exposes password inputs with the textbox role, so these roles share one name space.
TEXT_ROLES = {"textbox", "password"}


def role_key(role: str) -> str:
    """Group roles whose locators can match the same element (password fields are textboxes)."""
    return "textbox" if role in TEXT_ROLES else role


def semantic_assertions(design: dict, elements: dict, root: str, state: dict) -> list[str]:
    """Render role, name, type, option, href and alert checks for one display state."""
    shown, hidden, excluded, alerts = {}, [], set(), {"shown": 0, "hidden": 0}
    for uid, node in elements.items():
        if uid == root or not isinstance(node, dict):
            continue
        role, name = accessible_role(node), accessible_name(node)
        if semantic_scope(design, uid):
            # Repeated rows and decoration may legitimately reuse a name; never assert its absence.
            if role is not None and name is not None:
                excluded.add((role_key(role), name))
            continue
        is_shown = visible(design, uid, state)
        if node.get("type") == "alert":
            alerts["shown" if is_shown else "hidden"] += 1
            continue
        if role is None or name is None:
            continue
        prop = node.get("prop") if isinstance(node.get("prop"), dict) else {}
        if is_shown:
            shown.setdefault((role, name), []).append(prop)
        elif (role, name) not in hidden:
            hidden.append((role, name))
    lines = []
    for (role, name), props in shown.items():
        # Several shown elements with one role/name contradict the unique-name rule; a count(1)
        # check would be unsatisfiable for the declared design, so leave them to testid checks.
        if len(props) == 1:
            lines.extend(role_assertions(role, name, props[0]))
    blocked = {(role_key(role), name) for role, name in shown} | excluded
    shown_names = {name for _, name in shown}
    for role, name in hidden:
        # A label locator also matches other controls with that name; a shared role/name is covered above.
        if (role_key(role), name) in blocked or (role == "password" and name in shown_names):
            continue
        lines.append(absence_assertion(role, name))
    if alerts["shown"] == 1:
        lines.append('  await expect(page.getByRole("alert")).toBeVisible();')
    elif alerts["shown"] > 1:
        lines.append('  await expect(page.getByRole("alert").first()).toBeVisible();')
    elif alerts["hidden"]:
        lines.append('  await expect(page.getByRole("alert")).toHaveCount(0);')
    return lines


def existence_suite(design: dict, pid: str) -> str:
    """Supply test-local display data; check controls and actual application data keys."""
    page = design["pages"][pid]
    elements = page_elements(design, pid)
    variables = data_symbols(elements)
    empty_values = {"string": "", "integer": 0, "number": 0, "boolean": False,
        "array": [], "object": {}, "null": None}
    values = {}
    for name, declaration in variables.items():
        values[name] = empty_values[declaration["dtype"]]

    # Exercise conditional controls using values owned only by this generated test.
    states = [values]
    for uid in elements:
        overrides = {}
        current, seen = uid, set()
        possible = True
        while current in elements and current not in seen:
            seen.add(current)
            node = elements[current]
            rule = node.get("prop", {}).get("visible", True)
            if rule is False:
                possible = False
                break
            if isinstance(rule, dict):
                name, expected = rule["var"], rule["equals"]
                if name in overrides and overrides[name] != expected:
                    possible = False
                    break
                overrides[name] = expected
            current = node.get("parent")
        candidate = {**values, **overrides}
        if possible and candidate not in states:
            states.append(candidate)

    lines = [SUITE_HEADER,
        'import { test, expect } from "@playwright/test";', '']
    for number, state in enumerate(states, 1):
        lines.append(f'test({json.dumps(pid + " / display " + str(number) + " / UI element existence")}, async ({{ page }}) => {{')
        lines.extend([
            '  const runtimeErrors: string[] = [];',
            '  page.on("pageerror", error => runtimeErrors.push(error.message));',
            '  await page.addInitScript(testData => { (window as unknown as { __ARC_UI_TEST_DATA__: unknown }).__ARC_UI_TEST_DATA__ = testData; }, '
                + json.dumps({"page": pid, "data": state}, ensure_ascii=False) + ');',
            f'  await page.goto({json.dumps(page["route"])});',
            '  if (runtimeErrors.length) throw new Error("UI_RUNTIME_ERROR: " + runtimeErrors.join("\\n"));',
            '  try {',
        ])
        for uid, node in elements.items():
            kind = node.get("type", "page" if uid == page["root"] else "container")
            if kind not in KEY_TYPES:
                continue
            locator = f"page.getByTestId({json.dumps(uid)})"
            shown = visible(design, uid, state)
            lines.append(f"  await expect({locator}){'' if shown else '.not'}.toBeVisible();")
        # Role/name checks mirror how acceptance tests locate controls (strict, exact names).
        lines.extend(semantic_assertions(design, elements, page["root"], state))
        lines.extend([
            '  await expect.poll(() => page.evaluate(({ pageId, keys }) => {',
            '    const state = (window as unknown as { __ARC_UI_DATA__?: { page: string; data: Record<string, unknown> } }).__ARC_UI_DATA__;',
            '    return state?.page === pageId && keys.every(key => Object.prototype.hasOwnProperty.call(state.data, key));',
            '  }, ' + json.dumps({"pageId": pid, "keys": sorted(variables)}) + ')).toBe(true);',
            '  } catch (error) {',
            '    if (runtimeErrors.length) throw new Error("UI_RUNTIME_ERROR: " + runtimeErrors.join("\\n"));',
            '    throw error;',
            '  }',
            '  if (runtimeErrors.length) throw new Error("UI_RUNTIME_ERROR: " + runtimeErrors.join("\\n"));',
            '});', '',
        ])
    return "\n".join(lines)


def require_green(result: dict) -> None:
    """Require passing actual browser assertions with no reported runtime errors.

    An unverified result (no launchable browser) is not a failure the application can fix;
    gates accept it and rely on their other checks. It is never reported as GREEN."""
    if is_unverified(result):
        return
    if result["returncode"] or result.get("errors") or not result.get("passed") or result.get("failed"):
        raise CheckFailure("UI checks are not GREEN", result)


def require_red(result: dict) -> None:
    """Require missing-component assertion failures rather than browser, server, or application runtime failures."""
    errors = result.get("failures", [])
    excluded = ("UI_RUNTIME_ERROR", "UI_BUSINESS_CALL", "ERR_", "browserType.launch", "page.goto:", "SyntaxError", "ReferenceError", "Cannot find module")
    # Code frames can contain the literal runtime-guard text from adjacent lines;
    # classify the actual error headline rather than those source snippets.
    headlines = [re.sub(r"\x1b\[[0-9;]*m", "", message).split("\n", 1)[0] for message in errors]
    if not result["returncode"] or not result.get("failed") or result.get("errors") or not errors or any(not re.search(r"expect\(|AssertionError", message) or any(word in message for word in excluded) for message in headlines):
        raise ValueError(f"Expected missing-component assertion RED, not infrastructure/runtime failure: {result}")


def parse_playwright_result(result: dict, include_files: bool = False) -> dict:
    """Parse the Playwright JSON reporter into counts, failures, runtime errors, and optional per-file totals."""
    try:
        report = json.loads(result.pop("stdout"))
    except (ValueError, KeyError) as error:
        raise ValueError(f"Playwright did not return its JSON report: {result['output']}") from error
    failures = []
    files = {}

    def visit(suite):
        """Accumulate nested Playwright suite/spec results into the normalized browser-check report."""
        for spec in suite.get("specs", []):
            filename = spec.get("file", suite.get("file", ""))
            files[filename] = files.get(filename, 0) + len(spec.get("tests", []))
            for test in spec.get("tests", []):
                for run in test.get("results", []):
                    if run.get("status") not in {"passed", "skipped"}:
                        failures.append("\n".join(item.get("message", "") for item in run.get("errors", [])) + "\nTest file: " + filename)
        for child in suite.get("suites", []):
            visit(child)

    for suite in report.get("suites", []):
        visit(suite)
    stats = report.get("stats", {})
    if isinstance(stats.get("duration"), (int, float)):
        result["test_seconds"] = stats["duration"] / 1000
    result.update({"passed": stats.get("expected", 0), "failed": stats.get("unexpected", 0),
        "skipped": stats.get("skipped", 0), "flaky": stats.get("flaky", 0),
        "errors": report.get("errors", []), "failures": failures})
    if include_files:
        result["files"] = files
    result["output"] = json.dumps({key: value for key, value in result.items() if key != "output"}, ensure_ascii=False, indent=2)[-24000:]
    return result


class FrontendChecks:
    """Compiler-owned browser installation, TypeScript, Playwright, and workspace build commands."""
    def __init__(self, output: Path, timeout: int = 600) -> None:
        """Bind browser/build checks to the generated workspace and locate Node.js and npm."""
        self.output, self.timeout = output.resolve(), timeout
        self.node = shutil.which("node")
        if not self.node:
            raise ValueError("Node.js is required for UI checks")
        self.node = str(Path(self.node).resolve())

    def execute(self, args: list[str], timeout: int | None = None, env: dict | None = None, executable: str | None = None) -> dict:
        """Run a fixed check command, capture its output, and stop its process group on timeout or interruption."""
        try:
            return capture_process([executable or self.node, *args], cwd=self.output,
                timeout=timeout or self.timeout, env=env)
        except subprocess.TimeoutExpired as error:
            raise ValueError(f"Frontend check timed out after {timeout or self.timeout}s") from error

    def install_browser(self) -> None:
        """Install Chromium and prove it launches; never fatal (see stages/frontend/browser.py).

        On Linux a failed launch is retried after `install --with-deps`, which installs the
        OS libraries when the runner has root (typical for containers). Probed once per process."""
        if browser_state()["available"] is not None:
            return
        if os.getenv("ARC_DISABLE_BROWSER", "").strip() == "1":
            # Exercise the degraded (no browser) path deliberately, e.g. to test it locally.
            set_browser_state(False, "ARC_DISABLE_BROWSER=1")
            print("WARNING: browser checks disabled by ARC_DISABLE_BROWSER=1.", flush=True)
            return
        cli = "node_modules/@playwright/test/cli.js"
        try:
            installed = self.execute([cli, "install", "chromium"], timeout=600)
            print(installed["output"][-4000:], flush=True)
        except ValueError as error:
            print(f"Playwright Chromium installation did not finish: {error}", flush=True)
        probe = self.probe_browser()
        if probe["returncode"] and sys.platform.startswith("linux"):
            print("Chromium cannot launch; installing its OS libraries (playwright install --with-deps chromium).",
                flush=True)
            try:
                deps = self.execute([cli, "install", "--with-deps", "chromium"], timeout=900)
                print(deps["output"][-4000:], flush=True)
            except ValueError as error:
                print(f"Installing Chromium OS libraries did not finish: {error}", flush=True)
            probe = self.probe_browser()
        if probe["returncode"]:
            set_browser_state(False, probe["output"])
            print("WARNING: Playwright Chromium cannot launch here. Continuing without browser checks: "
                "UI and trigger tasks are gated by TypeScript, backend tests and the build.\n"
                + probe["output"][-2000:], flush=True)
        else:
            set_browser_state(True)
            print("Playwright Chromium launch check: OK", flush=True)

    def probe_browser(self) -> dict:
        """Launch and close headless Chromium once with the workspace's Playwright."""
        script = ("const { chromium } = require('@playwright/test');"
            "chromium.launch().then(b => b.close()).then(() => console.log('chromium ok'))"
            ".catch(e => { console.error(String(e && e.message || e)); process.exit(1); });")
        try:
            return self.execute(["-e", script], timeout=120)
        except ValueError as error:
            return {"returncode": 1, "output": str(error)}

    def typecheck(self, files: list[str], *, test_roots=("frontend/tests/ui",), source_files=("src/**/*.ts", "src/**/*.tsx")) -> dict:
        """Compile selected frontend/test roots with a temporary TypeScript configuration, then remove that configuration."""
        includes = list(source_files)
        for relative in files:
            path = (self.output / relative).resolve()
            if not any(path.is_relative_to(self.output / root) for root in test_roots):
                raise ValueError("Typecheck only the supplied frontend test roots")
            includes.append(path.relative_to(self.output / "frontend").as_posix())
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", suffix=".json", prefix=".tsconfig-ui-", dir=self.output / "frontend", delete=False) as config:
            path = Path(config.name)
            json.dump({"extends": "./tsconfig.json", "compilerOptions": {"types": ["vite/client", "node"], "forceConsistentCasingInFileNames": True}, "include": includes}, config)
        try:
            result = self.execute(["node_modules/typescript/bin/tsc", "--noEmit", "-p", str(path)])
            result.pop("stdout", None)
            return result
        finally:
            path.unlink(missing_ok=True)

    def tests(self, files: list[str]) -> dict:
        """Run selected UI-existence specs with Playwright and normalize the reporter output."""
        if not files:
            raise ValueError("Do not run an empty UI suite")
        if not browser_available():
            return unverified_result(files)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        env = {**os.environ, "ARC_UI_PORT": str(port)}
        result = self.execute(["node_modules/@playwright/test/cli.js", "test", "--config", "frontend/playwright.ui.config.ts", *files], env=env)
        return parse_playwright_result(result)

    def build(self) -> dict:
        """Run the generated workspace's frontend and backend build command and return the captured result."""
        command = npm_command()
        return self.execute([*command[1:], "run", "build"], executable=command[0])
