"""Run all compilation passes or one selected pass in the same pipeline."""

from contextvars import copy_context
import os
from pathlib import Path
import queue
from threading import RLock, Thread
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
from infrastructure.shared.project.dependencies import EnvironmentFailure, cancel_prefetches, prefetch_dependencies
from infrastructure.stages.frontend.ui.runtime import prepare_frontend


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

# What each stage actually needs (verified per stage); a stage not listed needs
# the previous one. In full runs a stage starts as soon as its needs are done:
#   06 backend TDD  ─┐  (edits backend/ only)
#   07 UI design ─┬─ 08 UI implementation (edits frontend/ only) ─┐
#                 └─ 09 trigger design (writes .arc only) ─────────┴─ 10
# 07 reads requirements/schema/backend_apis/visual analysis; 08 reads ui_design;
# 09 reads ui_design and db_obligations. None reads 06's code or reports, and
# 06/08 keep failure recovery to their own layer (implementation_task roots).
# Disable with ARC_OVERLAP_STAGES=0; never used with --pause or --stage.
STAGE_DEPENDENCIES = {
    "06_implement_backend_tdd": ("05_design_backend_apis",),
    "07_design_ui": ("05_design_backend_apis",),
    "08_implement_ui": ("07_design_ui",),
    "09_design_frontend_triggers": ("07_design_ui",),
    "10_implement_frontend_tdd": ("06_implement_backend_tdd", "08_implement_ui", "09_design_frontend_triggers"),
}
STAGE_ORDER = {name: index for index, (name, _, _) in enumerate(PIPELINE)}


def stage_needs(name, ordered):
    """Stages that must finish before name starts, among the ordered stages being run."""
    needs = STAGE_DEPENDENCIES.get(name)
    if needs is None:
        index = ordered.index(name)
        needs = (ordered[index - 1],) if index > 0 else ()
    return {need for need in needs if need in ordered}


class Compiler:
    """Own shared paths, configuration, and results for one compilation."""

    def __init__(self, input_dir: Path, output_dir: Path, model_log: bool = False,
            port: int = 3000, pause: bool = False) -> None:
        if not 1 <= port <= 65535:
            raise ValueError("Backend port must be between 1 and 65535")
        self.input_dir = Path(input_dir).expanduser().resolve()
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.arc_dir = self.output_dir / ".arc"
        self.model_log = model_log
        self.port = port
        self.pause = pause
        self.artifacts: dict[str, object] = {}
        self.current_stage: str | None = None
        self.report = {"status": "NOT_STARTED", "stages": {}}
        self._report_lock = RLock()

    def _record(self, stage, entry):
        """Store a stage result in pipeline order and persist the report (thread-safe)."""
        with self._report_lock:
            stages = {**self.report["stages"], stage: entry}
            self.report["stages"] = dict(sorted(stages.items(), key=lambda item: STAGE_ORDER.get(item[0], len(STAGE_ORDER))))
            save_json(self.arc_dir / "compilation.json", self.report)

    def _run(self, stage: str, function, *arguments, **options):
        """Attempt every pass; keep failures explicit without aborting later passes."""
        self.current_stage = stage
        try:
            with console_log(self.output_dir, stage=stage,
                    model_log=self.model_log, debug_model_io=self.model_log):
                result = function(*arguments, **options)
        except KeyboardInterrupt as error:
            self.report["status"] = "INTERRUPTED"
            self._record(stage, {"status": "INTERRUPTED", "error": str(error)})
            checkpoint(self.output_dir, stage=stage, task="stage report", action="Record interrupted pass",
                status="INTERRUPTED", details=self.report["stages"][stage])
            raise
        except EnvironmentFailure as error:
            # Later passes would hit the same machine problem; stop with the fix.
            entry = {"status": "FAILED", "error": str(error), "error_type": type(error).__name__}
            self.report["status"] = "FAILED"
            self._record(stage, entry)
            show_panel("[STOPPED] " + stage + " | environment problem", str(error), tone="error")
            checkpoint(self.output_dir, stage=stage, task="stage report", action="Stop on environment failure",
                status="FAILED", details=entry)
            raise
        except Exception as error:
            result = {"status": "FAILED", "error": str(error),
                "error_type": type(error).__name__, "traceback": traceback.format_exc()}
            if getattr(error, "failures", None):
                result.update(status="COMPLETED_WITH_FAILURES", failures=error.failures)
            show_panel("[FAILED; continuing] " + stage,
                result["error"] + "\nAccepted artifacts remain available. Attempt the next pass.", tone="error")
        status = result.get("status", "GREEN") if isinstance(result, dict) else "GREEN"
        entry = {"status": status}
        if isinstance(result, dict):
            for key in ("failures", "verification", "error", "error_type", "traceback"):
                if key in result:
                    entry[key] = result[key]
        self._record(stage, entry)
        checkpoint(self.output_dir, stage=stage, task="stage report", action="Publish pass result",
            status=status, details=entry)
        return result

    def _run_pass(self, name, function):
        if function is project_initialization:
            return self._run(name, function, self.output_dir, port=self.port, pause=self.pause)
        if function in (explore_requirement, design_frontend_ui, implement_frontend_ui,
                design_frontend_triggers, implement_frontend_triggers):
            return self._run(name, function, self.input_dir, self.arc_dir, pause=self.pause)
        return self._run(name, function, self.arc_dir, pause=self.pause)

    def _after_initialization(self, multiple):
        """In multi-stage runs, prepare dependency manifests and install them in the background."""
        if not multiple or self.report["stages"].get("00_project_initialization", {}).get("status") != "GREEN":
            return
        try:
            # Stage 08's (deterministic) preparation adds frontend dependencies; doing it
            # now lets the background install cover them instead of a second install later.
            prepare_frontend(self.output_dir)
        except (OSError, ValueError) as error:
            print(f"Frontend preparation deferred to stage 08: {error}", flush=True)
        try:
            prefetch_dependencies(self.output_dir)
        except (OSError, ValueError) as error:
            print(f"Background npm install not started: {error}", flush=True)

    def _run_concurrently(self, passes):
        """Start each pass as soon as the passes it needs have finished."""
        ordered = [name for name, _, _ in passes]
        entries = {name: (function, artifact) for name, function, artifact in passes}
        pending, running, done = [name for name, _, _ in passes], set(), set()
        finished = queue.Queue()

        def work(name):
            try:
                finished.put((name, self._run_pass(name, entries[name][0]), None))
            except BaseException as error:  # Re-raised on the main thread.
                finished.put((name, None, error))

        while pending or running:
            for name in list(pending):
                if stage_needs(name, ordered) <= done:
                    pending.remove(name)
                    running.add(name)
                    if running - {name}:
                        print(f"Starting {name} alongside: {', '.join(sorted(running - {name}))}", flush=True)
                    Thread(target=copy_context().run, args=(work, name), name=f"stage-{name}", daemon=True).start()
            while True:
                try:
                    name, result, error = finished.get(timeout=0.5)
                    break
                except queue.Empty:
                    continue
            running.discard(name)
            if error is not None:
                raise error
            done.add(name)
            self.artifacts[entries[name][1]] = result
            if entries[name][0] is project_initialization:
                self._after_initialization(len(passes) > 1)

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
        show_panel("Requirement compiler | Pipeline",
            f"Input: {self.input_dir}\nOutput: {self.output_dir}\nPasses: {len(passes)}\nDetailed model logs: {'enabled' if self.model_log else 'disabled'}")
        overlap = (len(passes) > 1 and not self.pause
            and os.getenv("ARC_OVERLAP_STAGES", "1").strip().lower() not in {"0", "false", "no", "off"})
        try:
            if overlap:
                self._run_concurrently(passes)
            else:
                for name, function, artifact in passes:
                    self.artifacts[artifact] = self._run_pass(name, function)
                    if function is project_initialization:
                        # Overlap the slow first npm install with the model-only design
                        # stages; stage 04 joins it before touching node_modules.
                        self._after_initialization(len(passes) > 1)
        finally:
            # After an interrupt, running stages are daemon threads and must not block exit.
            cancel_prefetches()
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
