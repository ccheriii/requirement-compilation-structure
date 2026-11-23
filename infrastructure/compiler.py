"""Run all compilation passes or one selected pass in the same pipeline."""

from pathlib import Path
import shutil
import time
import traceback

from infrastructure.pipeline.project_initialization import project_initialization
from infrastructure.pipeline.requirements import explore_requirement
from infrastructure.pipeline.db_schema import build_db_schema
from infrastructure.pipeline.db_obligations import infer_db_obligations
from infrastructure.pipeline.database import implement_db
from infrastructure.pipeline.backend_apis import design_backend_apis
from infrastructure.pipeline.backend_implementation import implement_backend_tdd
from infrastructure.pipeline.ui_design import design_frontend_ui
from infrastructure.pipeline.ui_implementation import implement_frontend_ui
from infrastructure.pipeline.frontend_triggers import design_frontend_triggers
from infrastructure.pipeline.frontend_implementation import implement_frontend_triggers
from infrastructure.utils.presentation.console import show_panel
from infrastructure.utils.presentation.logging import console_log
from infrastructure.utils.files import save_json
from infrastructure.shared.project.git import checkpoint


PIPELINE = (
    ("00_project_initialization", project_initialization, "project_files"),
    ("01_explore_requirement", explore_requirement, "requirements"),
    ("02_build_db_schema", build_db_schema, "db_schema"),
    ("03_infer_db_obligations", infer_db_obligations, "db_obligations"),
    ("04_implement_db", implement_db, "db_implementations"),
    ("05_design_backend_apis", design_backend_apis, "backend_apis"),
    ("06_implement_backend_tdd", implement_backend_tdd, "backend_checks"),
    ("07_design_ui", design_frontend_ui, "ui_design"),
    ("08_implement_ui", implement_frontend_ui, "ui_checks"),
    ("09_design_frontend_triggers", design_frontend_triggers, "frontend_triggers"),
    ("10_implement_frontend_tdd", implement_frontend_triggers, "frontend_e2e"),
)

# Kept when a full compilation starts over in an existing output directory: installed
# packages make reruns fast, and git history/logs are audit evidence, not generated state.
PRESERVED_NAMES = {".git", "node_modules", "package-lock.json", "compiler.log", "model_calls.jsonl"}


def _remove(path: Path) -> bool:
    """Delete a file or tree, retrying briefly for Windows/OneDrive locks; report failure instead of raising."""
    for attempt in range(5):
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            return True
        except OSError:
            time.sleep(0.2 * (attempt + 1))
    return False


def reset_output(output_dir: Path) -> list[str]:
    """Remove generated sources and artifacts from a previous compilation.

    Stage 00 deliberately never overwrites existing files, so without this a full rerun
    inherits stale handlers, routes, tests and .arc path records from earlier designs.
    Returns workspace-relative paths that could not be removed (for example a locked DB).
    """
    leftovers = []

    def clean(directory: Path) -> None:
        for child in directory.iterdir():
            if child.name in PRESERVED_NAMES:
                continue
            if child.is_dir() and not child.is_symlink() and any(
                    (child / name).exists() for name in ("node_modules",)):
                clean(child)  # workspace folders such as frontend/ keep their node_modules
                continue
            if not _remove(child):
                leftovers.append(child.relative_to(output_dir).as_posix())

    if output_dir.is_dir():
        clean(output_dir)
    return leftovers


class Compiler:
    """Own shared paths, configuration, and results for one compilation."""

    def __init__(self, input_dir: Path, output_dir: Path, model_log: bool = False,
            port: int = 3000, pause: bool = False, keep_output: bool = False) -> None:
        if not 1 <= port <= 65535:
            raise ValueError("Backend port must be between 1 and 65535")
        self.input_dir = Path(input_dir).expanduser().resolve()
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.arc_dir = self.output_dir / ".arc"
        self.model_log = model_log
        self.port = port
        self.pause = pause
        self.keep_output = keep_output
        self.artifacts: dict[str, object] = {}
        self.current_stage: str | None = None
        self.report = {"status": "NOT_STARTED", "stages": {}}

    def _run(self, stage: str, function, *arguments, **options):
        """Attempt every pass; keep failures explicit without aborting later passes."""
        self.current_stage = stage
        try:
            with console_log(self.output_dir, stage=stage,
                    model_log=self.model_log, debug_model_io=self.model_log):
                result = function(*arguments, **options)
        except KeyboardInterrupt as error:
            self.report["status"] = "INTERRUPTED"
            self.report["stages"][stage] = {"status": "INTERRUPTED", "error": str(error)}
            save_json(self.arc_dir / "compilation.json", self.report)
            checkpoint(self.output_dir, stage=stage, task="stage report", action="Record interrupted pass",
                status="INTERRUPTED", details=self.report["stages"][stage])
            raise
        except Exception as error:
            result = {"status": "FAILED", "error": str(error),
                "error_type": type(error).__name__, "traceback": traceback.format_exc()}
            if getattr(error, "failures", None):
                result.update(status="COMPLETED_WITH_FAILURES", failures=error.failures)
            show_panel("[FAILED; continuing] " + stage,
                result["error"] + "\nAccepted artifacts remain available. Attempt the next pass.", tone="error")
        status = result.get("status", "GREEN") if isinstance(result, dict) else "GREEN"
        self.report["stages"][stage] = {"status": status}
        if isinstance(result, dict):
            for key in ("failures", "verification", "error", "error_type", "traceback"):
                if key in result:
                    self.report["stages"][stage][key] = result[key]
        save_json(self.arc_dir / "compilation.json", self.report)
        checkpoint(self.output_dir, stage=stage, task="stage report", action="Publish pass result",
            status=status, details=self.report["stages"][stage])
        return result

    def compile(self, stage: str | None = None) -> Path:
        """Execute every pass, or only the selected pass using existing artifacts."""
        passes = PIPELINE
        if stage is not None:
            passes = ()
            for entry in PIPELINE:
                if entry[0] == stage:
                    passes = (entry,)
                    break
            if not passes:
                raise ValueError(f"Unknown stage: {stage}. Use a full stage name such as 06_implement_backend_tdd.")
        self.artifacts.clear()
        self.report = {"status": "RUNNING", "stages": {}}
        if stage is None and not self.keep_output and (self.arc_dir.exists() or (self.output_dir / "backend").exists()):
            leftovers = reset_output(self.output_dir)
            show_panel("Requirement compiler | Fresh output",
                "Removed generated sources and .arc artifacts from the previous compilation "
                "(kept node_modules, package-lock.json, git history and logs). Use --keep-output to reuse them."
                + ("\nCould not remove (locked?): " + ", ".join(leftovers) if leftovers else ""),
                tone="error" if leftovers else "info")
        show_panel("Requirement compiler | Pipeline",
            f"Input: {self.input_dir}\nOutput: {self.output_dir}\nPasses: {len(passes)}\nDetailed model logs: {'enabled' if self.model_log else 'disabled'}")
        for name, function, artifact in passes:
            if function is project_initialization:
                result = self._run(name, function, self.output_dir, port=self.port, pause=self.pause)
            elif function in (explore_requirement, design_frontend_ui, implement_frontend_ui,
                    design_frontend_triggers, implement_frontend_triggers):
                result = self._run(name, function, self.input_dir, self.arc_dir, pause=self.pause)
            elif function is implement_db:
                result = self._run(name, function, self.arc_dir, pause=self.pause, input_dir=self.input_dir)
            else:
                result = self._run(name, function, self.arc_dir, pause=self.pause)
            self.artifacts[artifact] = result
        self.current_stage = None
        incomplete = [name for name, result in self.report["stages"].items()
            if result["status"] in {"FAILED", "COMPLETED_WITH_FAILURES"}]
        self.report["status"] = "COMPLETED_WITH_FAILURES" if incomplete else "GREEN"
        save_json(self.arc_dir / "compilation.json", self.report)
        checkpoint(self.output_dir, stage="compiler", task="pipeline", action="Publish final compilation report",
            status=self.report["status"], details={"stages": self.report["stages"], "incomplete_stages": incomplete})
        show_panel("[DONE] Requirement compilation | " + self.report["status"],
            f"Application: {self.output_dir}\nReport: {self.arc_dir / 'compilation.json'}"
            + ("\nIncomplete stages: " + ", ".join(incomplete) if incomplete else ""),
            tone="error" if incomplete else "success")
        return self.output_dir
