"""Cross-platform process startup, UTF-8 output, npm resolution, and owned-tree cleanup."""

import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
from time import monotonic

WINDOWS = os.name == "nt"


def child_environment(overrides=None) -> dict:
    """Keep inherited settings while making Python child output consistently UTF-8."""
    return {**os.environ, **(overrides or {}), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def process_options() -> dict:
    """Create an owned process group using the host platform's supported API."""
    if WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)}
    return {"start_new_session": True}


def start_process(command, *, cwd, env=None, **options):
    """Launch an argument list without shell interpolation, preserving spaces and Unicode paths."""
    return subprocess.Popen([str(part) for part in command], cwd=cwd,
        env=child_environment(env), **process_options(), **options)


def stop_process(process) -> None:
    """Stop only the owned process tree on interruption/timeout, including browser/server children."""
    if WINDOWS:
        # Killing node.exe alone leaves its Playwright/Vite descendants running.
        subprocess.run([shutil.which("taskkill") or "taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        # The parent can exit before a descendant; clean the remaining group too.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


def _captured_text(stream) -> str:
    """Decode captured bytes as UTF-8 with universal newlines, like text-mode pipes."""
    stream.flush()
    stream.seek(0)
    text = stream.read().decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def capture_process(command, *, cwd, timeout, env=None) -> dict:
    """Capture UTF-8 output and clean the whole process tree before re-raising timeout or Ctrl+C.

    Output goes to temporary files rather than pipes and completion is judged by the
    process exit: a descendant that inherited the handles (a server or browser left by a
    test runner, common on Windows) can no longer block the capture until the timeout.
    """
    started = monotonic()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = start_process(command, cwd=cwd, env=env, stdout=out, stderr=err)
        try:
            process.wait(timeout=timeout)
        except BaseException:
            try:
                stop_process(process)
            except Exception as cleanup:  # keep the timeout/interrupt as the reported error
                print(f"[CHECK] process cleanup failed: {cleanup}", flush=True)
            raise
        if not WINDOWS:
            # The command has exited; end descendants it left in its owned process group
            # (pipes used to keep the capture waiting on them until the timeout cleanup).
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        stdout, stderr = _captured_text(out), _captured_text(err)
    elapsed = monotonic() - started
    label = " ".join([Path(str(command[0])).name, *(str(part) for part in command[1:])])
    print(f"[CHECK] {label} | {elapsed:.2f} s | exit {process.returncode}", flush=True)
    return {"returncode": process.returncode, "stdout": stdout, "stderr": stderr, "elapsed_seconds": elapsed,
        "output": (stdout + stderr)[-24000:]}


def npm_command() -> list[str]:
    """Resolve npm through its JavaScript CLI, avoiding Windows .cmd shell quoting."""
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or not npm:
        raise ValueError("Node.js and npm are required; install them and add their executables to PATH")
    node = str(Path(node).resolve())
    launcher = Path(npm).resolve()
    if launcher.suffix.lower() in {".js", ".mjs"}:
        return [node, str(launcher)]
    for directory in (launcher.parent, Path(node).resolve().parent):
        script = directory / "node_modules/npm/bin/npm-cli.js"
        if script.is_file():
            return [node, str(script)]
    if launcher.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        raise ValueError("Cannot locate npm-cli.js beside the npm/Node launcher; use a complete Node.js installation")
    # Unix installations may expose an executable script rather than a JS symlink.
    return [str(launcher)]
