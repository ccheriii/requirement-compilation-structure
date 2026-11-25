"""Install the generated workspace's npm dependencies before each TDD run."""

from hashlib import sha256
import json
from pathlib import Path
import subprocess
from threading import Lock, RLock, Thread

from infrastructure.utils.processes import npm_command, start_process, stop_process


INSTALL_TIMEOUT = 600


class EnvironmentFailure(RuntimeError):
    """The machine, not the generated code, is broken (npm permissions, network, ...).

    Model repair cannot fix this, so it is never sent to an agent as repair
    feedback; the compiler stops the pipeline with an actionable message.
    """


def environment_hint(lines) -> str:
    """Name the likely machine-level cause of a failed npm install, if recognizable."""
    text = "".join(lines)
    if ("EACCES" in text or "EPERM" in text) and ("_cacache" in text or ".npm" in text):
        return ("Your npm cache contains files you do not own (usually left by an earlier `sudo npm`). "
            "Fix it once with: sudo chown -R $(id -u):$(id -g) ~/.npm   then re-run the compiler.")
    if "EACCES" in text or "EPERM" in text:
        return "npm hit a file permission error; check ownership of the output directory and ~/.npm."
    if any(code in text for code in ("ENOTFOUND", "EAI_AGAIN", "ETIMEDOUT", "ECONNRESET", "ECONNREFUSED")):
        return "npm could not reach the package registry; check the network, proxy or registry settings."
    if "ENOSPC" in text:
        return "The disk is full; free some space and re-run the compiler."
    return ""


MANIFESTS = ("package.json", "backend/package.json", "frontend/package.json")
# Only these manifest fields change what npm install produces; scripts do not.
DEPENDENCY_FIELDS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies",
    "workspaces", "overrides", "packageManager", "name", "version")

# Background installs started right after scaffolding, keyed by output directory.
_prefetches: dict[Path, dict] = {}
_prefetch_lock = Lock()
# Stages may run concurrently: never run two npm installs in one project at once,
# and skip installs whose dependency declarations were already installed.
_install_lock = RLock()
_installed: dict[Path, str] = {}


def _manifest_fingerprint(output_dir: Path) -> str:
    """Identify the dependency declarations an install was based on."""
    digest = sha256()
    for name in MANIFESTS:
        path = output_dir / name
        raw = path.read_bytes() if path.is_file() else b"<missing>"
        try:
            manifest = json.loads(raw)
            relevant = json.dumps({key: manifest.get(key) for key in DEPENDENCY_FIELDS}, sort_keys=True).encode()
        except ValueError:
            relevant = raw
        digest.update(name.encode() + b"\0" + relevant + b"\0")
    return digest.hexdigest()


def prefetch_dependencies(output_dir: Path) -> bool:
    """Start npm install in the background so it overlaps with model-only stages.

    The first install takes minutes, but nothing needs node_modules until the
    database runtime check in stage 04. install_dependencies() joins this run and
    re-installs only if it failed or any package.json changed meanwhile.
    """
    output_dir = Path(output_dir).resolve()
    if not (output_dir / "package.json").is_file():
        return False
    with _prefetch_lock:
        if output_dir in _prefetches:
            return False
        state = {"fingerprint": _manifest_fingerprint(output_dir), "lines": [], "returncode": None, "error": None}
        process = start_process(
            [*npm_command(), "install", "--include=dev", "--no-audit", "--no-fund"],
            cwd=output_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )

        def collect():
            try:
                for line in process.stdout:
                    state["lines"].append(line)
                state["returncode"] = process.wait()
            except BaseException as error:  # Reported when the install is joined.
                state["error"] = error

        thread = Thread(target=collect, name="npm-install-prefetch", daemon=True)
        thread.start()
        state.update(process=process, thread=thread)
        _prefetches[output_dir] = state
    print(f"Started npm install in the background for {output_dir}; it overlaps with the design stages.", flush=True)
    return True


def cancel_prefetches() -> None:
    """Stop unfinished background installs (interrupted or partial pipeline runs)."""
    with _prefetch_lock:
        states = list(_prefetches.values())
        _prefetches.clear()
    for state in states:
        if state["process"].poll() is None:
            stop_process(state["process"])
        state["thread"].join(timeout=5)


def _join_prefetch(output_dir: Path) -> bool:
    """Wait for a background install; True when it already satisfies the current manifests."""
    with _prefetch_lock:
        state = _prefetches.pop(output_dir, None)
    if state is None:
        return False
    if state["thread"].is_alive():
        print("Waiting for the background npm install to finish...", flush=True)
    state["thread"].join(timeout=INSTALL_TIMEOUT)
    if state["thread"].is_alive():
        stop_process(state["process"])
        state["thread"].join(timeout=5)
        print("Background npm install timed out; retrying in the foreground.", flush=True)
        return False
    print("".join(state["lines"]), end="", flush=True)
    if state["error"] is not None or state["returncode"] != 0:
        print(f"Background npm install failed (exit {state['returncode']}); retrying in the foreground.", flush=True)
        return False
    if state["fingerprint"] != _manifest_fingerprint(output_dir):
        print("package.json changed after the background install started; re-running npm install.", flush=True)
        return False
    print("Background npm install completed; dependencies are current.", flush=True)
    return True


def install_dependencies(output_dir: Path) -> None:
    """Run npm install in the generated workspace, stream its output, and fail on timeout or nonzero exit."""
    if not (output_dir / "package.json").is_file():
        raise ValueError("Run main.py --stage 00 before installing project dependencies")
    output_dir = Path(output_dir).resolve()
    with _install_lock:
        if _join_prefetch(output_dir):
            _installed[output_dir] = _manifest_fingerprint(output_dir)
            return
        fingerprint = _manifest_fingerprint(output_dir)
        if _installed.get(output_dir) == fingerprint and (output_dir / "node_modules").is_dir():
            print("npm dependencies already installed for the current package.json declarations; skipping.", flush=True)
            return
        _install(output_dir)
        _installed[output_dir] = fingerprint


def _install(output_dir: Path) -> None:
    command = npm_command()
    print(f"Installing dependencies in {output_dir}: npm install --include=dev --no-audit --no-fund", flush=True)
    with start_process(
        [*command, "install", "--include=dev", "--no-audit", "--no-fund"],
        cwd=output_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    ) as process:
        # Forward child output through Python so the console log also receives it.
        lines = []

        def forward_output():
            """Forward npm output through Python so both the terminal and compiler.log receive it."""
            for line in process.stdout:
                lines.append(line)
                print(line, end="", flush=True)

        reader = Thread(target=forward_output, daemon=True)
        reader.start()
        try:
            process.wait(timeout=INSTALL_TIMEOUT)
        except subprocess.TimeoutExpired as error:
            stop_process(process)
            raise EnvironmentFailure(f"npm install timed out after {INSTALL_TIMEOUT} seconds. "
                + (environment_hint(lines) or "Check the network and npm registry.")) from error
        except BaseException:
            stop_process(process)
            raise
        finally:
            reader.join()
        if process.returncode:
            hint = environment_hint(lines)
            raise EnvironmentFailure(f"npm install failed with exit code {process.returncode}; see the npm output above."
                + (" " + hint if hint else ""))
