"""Run the input acceptance specs against a generated application, the way a grader would.

Usage: uv run scripts/acceptance.py OUTPUT_DIR [--tests input/tests] [--skip-build]

Builds the generated app, starts the production server (backend serving frontend/dist)
on a free port with a fresh temporary database, and runs the Playwright specs from the
input tests directory against it. Prints a pass/fail summary and exits non-zero on failure.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CONFIG = """import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests", outputDir: "./results", workers: 1, fullyParallel: false, retries: 0,
  timeout: 10000, expect: { timeout: 5000 },  // arc-bench allows 10s per test
  reporter: [["list"], ["json", { outputFile: "./report.json" }]],
  use: { baseURL: process.env.GRADE_BASE_URL, browserName: "chromium", headless: true },
});
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run(command, cwd, env=None):
    print("$", " ".join(command), flush=True)
    return subprocess.run(command, cwd=cwd, env=env, shell=os.name == "nt", check=False)


def wait_for(url: str, process, seconds=60) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Server exited with code {process.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status < 500:
                    return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"Server did not respond at {url}")


def stop(process) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
    else:
        process.terminate()
    try:
        process.wait(10)
    except subprocess.TimeoutExpired:
        process.kill()


def summarize(report: dict) -> tuple[list, list]:
    passed, failed = [], []

    def walk(suite, prefix=""):
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                results = test.get("results", [])
                status = results[-1].get("status") if results else "skipped"
                name = f"{prefix}{spec.get('title')}"
                if status == "passed":
                    passed.append(name)
                else:
                    error = next((r.get("error", {}).get("message", "") for r in results if r.get("error")), "")
                    failed.append((name, status, error))
        for child in suite.get("suites", []):
            walk(child, prefix + (child.get("file") + " > " if child.get("file") and not prefix else ""))

    for suite in report.get("suites", []):
        walk(suite, (suite.get("file") or suite.get("title", "")) + " > ")
    return passed, failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("output", type=Path)
    parser.add_argument("--tests", type=Path, default=ROOT / "input" / "tests")
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    output, tests = args.output.resolve(), args.tests.resolve()
    if not (output / "node_modules" / "@playwright" / "test").is_dir():
        if run(["npm", "install", "--no-audit", "--no-fund", "-D", "@playwright/test"], output).returncode:
            return 2
    if not args.skip_build and run(["npm", "run", "build"], output).returncode:
        print("BUILD FAILED")
        return 2
    grade = output / ".grade"
    shutil.rmtree(grade, ignore_errors=True)
    shutil.copytree(tests, grade / "tests")
    (grade / "playwright.config.ts").write_text(CONFIG, encoding="utf-8")
    database_dir = Path(tempfile.mkdtemp(prefix="arc-grade-"))
    port = free_port()
    env = {**os.environ, "PORT": str(port), "DATABASE_URL": str(database_dir / "app.db")}
    server = subprocess.Popen(["node", "backend/dist/server.js"], cwd=output, env=env)
    try:
        wait_for(f"http://127.0.0.1:{port}/", server)
        test_env = {**os.environ, "GRADE_BASE_URL": f"http://127.0.0.1:{port}"}
        run(["node", "node_modules/@playwright/test/cli.js", "test", "--config", ".grade/playwright.config.ts"],
            output, test_env)
    finally:
        stop(server)
        shutil.rmtree(database_dir, ignore_errors=True)
    report_path = grade / "report.json"
    if not report_path.exists():
        print("NO REPORT")
        return 2
    passed, failed = summarize(json.loads(report_path.read_text(encoding="utf-8")))
    print(f"\nACCEPTANCE: {len(passed)} passed, {len(failed)} failed")
    for name, status, error in failed:
        print(f"  FAIL [{status}] {name}\n    {error.splitlines()[0] if error else ''}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
