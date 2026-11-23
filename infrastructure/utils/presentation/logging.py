"""Mirror step output to the terminal and an append-only workspace log."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
import sys
import json
import re
from threading import RLock
import traceback
from contextvars import ContextVar
from time import monotonic

from infrastructure.utils.presentation.stages import stage_label


_session = ContextVar("compiler_log_session", default=None)


def session_info():
    """Return the current output directory, stage, and log lock, when configured."""
    return _session.get()


def full_detail(title: str, body: str) -> None:
    """Keep full panel content in the log without flooding the terminal."""
    session = _session.get()
    if session:
        with session["lock"]:
            session["log"].write(f"\n--- {title} (full content) ---\n{body}\n")
            session["log"].flush()


class TeeStream:
    """Stream wrapper that mirrors output to the terminal and the step log."""
    def __init__(self, terminal, log, lock) -> None:
        """Bind a terminal and log file using one lock so streamed child output remains readable."""
        self.terminal, self.log, self.lock = terminal, log, lock

    def write(self, text: str) -> int:
        """Mirror text to the terminal and log, flushing both so headless viewers stream it live."""
        with self.lock:
            try:
                self.terminal.write(text)
                self.terminal.flush()
            except (OSError, ValueError):
                pass  # a closed or broken viewer pipe must never stop compilation
            self.log.write(re.sub(r"\x1b\[[0-9;]*m", "", text))
            self.log.flush()
        return len(text)

    def flush(self) -> None:
        """Flush both destinations under the shared output lock."""
        with self.lock:
            try:
                self.terminal.flush()
            except (OSError, ValueError):
                pass
            self.log.flush()

    def __getattr__(self, name):
        """Delegate stream capabilities such as isatty to the original terminal."""
        if name == "terminal":
            raise AttributeError(name)
        return getattr(self.terminal, name)


class MirrorStream:
    """Process-wide stdout that also copies every write to a second live stream (stderr)."""
    def __init__(self, primary, mirror) -> None:
        self.primary, self.mirror, self.lock = primary, mirror, RLock()

    def write(self, text: str) -> int:
        with self.lock:
            for stream in (self.primary, self.mirror):
                try:
                    stream.write(text)
                    stream.flush()
                except (OSError, ValueError):
                    pass  # one broken viewer must not stop compilation or the other stream
        return len(text)

    def flush(self) -> None:
        with self.lock:
            for stream in (self.primary, self.mirror):
                try:
                    stream.flush()
                except (OSError, ValueError):
                    pass

    def __getattr__(self, name):
        """Delegate isatty, fileno, encoding and similar capabilities to the real stdout."""
        if name == "primary":
            raise AttributeError(name)
        return getattr(self.primary, name)


def configure_console() -> None:
    """Use UTF-8 and line buffering for real terminal/redirected streams.

    A piped stdout is block-buffered by default, so a hosted runner's log view would
    only receive output in 8 KB bursts (or at exit); line buffering streams every line."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            except (OSError, ValueError):
                stream.reconfigure(encoding="utf-8", errors="replace")


def _same_stream(first, second) -> bool:
    """True when two standard streams write to the same file or pipe (e.g. 2>&1)."""
    import os
    try:
        a, b = os.fstat(first.fileno()), os.fstat(second.fileno())
    except (AttributeError, OSError, ValueError):
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino) and a.st_ino != 0


def live_mirror(stdout, stderr):
    """Return the stream that should also receive console output, or None.

    Hosted runners (no TTY) may display only stderr, where Python's logging writes.
    Headless runs therefore mirror console output to stderr too, unless both streams
    already reach the same destination or stderr is an interactive terminal.
    ARC_LOG_STDERR=0 disables the mirror; ARC_LOG_STDERR=1 forces it."""
    import os
    setting = os.getenv("ARC_LOG_STDERR", "").strip().lower()
    if setting in {"0", "false", "no", "off"}:
        return None
    if setting in {"1", "true", "yes", "on"}:
        return stderr
    try:
        if stdout.isatty() or stderr.isatty():
            return None
    except (AttributeError, ValueError):
        return None
    return None if _same_stream(stdout, stderr) else stderr


def install_live_mirror() -> bool:
    """Mirror all console output to stderr for headless runs (see live_mirror); idempotent."""
    if isinstance(sys.stdout, MirrorStream):
        return True
    mirror = live_mirror(sys.stdout, sys.stderr)
    if mirror is None:
        return False
    sys.stdout = MirrorStream(sys.stdout, mirror)
    return True


@contextmanager
def console_log(output_dir: Path, *, debug_model_io: bool = False, model_log: bool = True, stage: str | None = None):
    """Mirror a pass to compiler.log; explicit stage names support in-process compilation."""
    from infrastructure.utils.presentation.console import show_panel, show_dashboard
    from infrastructure.utils.presentation.artifacts import ARTIFACTS
    from infrastructure.shared.project.git import initialize_repository, checkpoint
    output_dir.mkdir(parents=True, exist_ok=True)
    initialize_repository(output_dir)
    configure_console()
    with (output_dir / "compiler.log").open("a", encoding="utf-8", newline="\n") as log:
        lock = RLock()
        stdout = TeeStream(sys.stdout, log, lock)
        stderr = TeeStream(sys.stderr, log, lock)
        with redirect_stdout(stdout), redirect_stderr(stderr):
            timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            script = stage or Path(sys.argv[0]).name
            artifacts = {}
            for kind in ARTIFACTS:
                try:
                    value = json.loads((output_dir / ".arc" / (kind + ".json")).read_text(encoding="utf-8-sig"))
                    if isinstance(value, dict):
                        artifacts[kind] = value
                except (OSError, ValueError):
                    continue
            token = _session.set({"output": output_dir, "stage": stage_label(script), "lock": lock, "log": log,
                "debug_model_io": debug_model_io, "model_log": model_log, "artifacts": artifacts, "model_stats": {}})
            started = monotonic()
            model_logs = f"Full model I/O: {output_dir / 'model_calls.jsonl'}" if model_log else "Detailed model I/O logs: disabled"
            show_panel(f"[START] {stage_label(script)}", f"{timestamp}\nScript: {script}\nLogs: {output_dir / 'compiler.log'}\n{model_logs}")
            show_dashboard(stage_label(script), artifacts)
            outcome, failure = "COMPLETED", None
            checkpoint(task="stage", action="Begin stage execution", status="RUNNING", details={"script": script})
            try:
                yield
            except BaseException as error:
                outcome = "INTERRUPTED" if isinstance(error, KeyboardInterrupt) else "FAILED"
                failure = {"error": str(error), "error_type": type(error).__name__}
                if not isinstance(error, SystemExit):
                    traceback.print_exc(file=log)
                show_panel(f"[FAILED] {stage_label(script)}", f"Stopped after {monotonic() - started:.1f}s", tone="error")
                raise
            else:
                show_panel(f"[DONE] {stage_label(script)}", f"Completed in {monotonic() - started:.1f}s", tone="success")
            finally:
                checkpoint(task="stage", action="Finish stage execution", status=outcome,
                    details={"script": script, "error": failure, "seconds": round(monotonic() - started, 2)})
                stdout.flush()
                stderr.flush()
                _session.reset(token)
