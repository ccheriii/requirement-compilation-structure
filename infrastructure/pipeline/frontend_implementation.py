"""Step 10: connect frontend triggers to backend APIs through functional smoke tests."""

from pathlib import Path

from infrastructure.stages.frontend.triggers.context import load_e2e_context
from infrastructure.stages.frontend.triggers.implementation import run_trigger_tdd, verify_frontend
from infrastructure.stages.frontend.triggers.acceptance import run_independent_acceptance
from infrastructure.stages.frontend.triggers.production import run_production_acceptance
from infrastructure.stages.frontend.triggers.supplied import empty_supplied, install_supplied_specs
from infrastructure.shared.models.text import TextModel
from infrastructure.utils.presentation.console import DemoConsole
from infrastructure.utils.files import save_json


def prepare_supplied_specs(input_dir: Path, output: Path, requirement_ids) -> dict:
    """Install <input_dir>/tests specs as read-only authoritative acceptance; never fatal."""
    try:
        supplied = install_supplied_specs(input_dir, output, list(requirement_ids))
    except OSError as error:
        print(f"Supplied acceptance specs could not be installed; continuing without them: {error}", flush=True)
        return empty_supplied()
    if supplied["all_specs"]:
        print("Supplied acceptance specs: " + ", ".join(supplied["all_specs"])
            + (f" (unmapped: {', '.join(supplied['unmapped'])})" if supplied["unmapped"] else ""), flush=True)
    return supplied


def implement_frontend_triggers(input_dir: Path, arc_dir: Path, pause: bool = False,
        max_turns: int = 10, max_tests: int = 2) -> dict:
    if min(max_turns, max_tests) < 1:
        raise ValueError("--max-turns and --max-tests must be positive")
    tree, schema, backend, ui, interactions, images, contexts = load_e2e_context(arc_dir, input_dir)
    report = {"status": "PREPARING", "requirements": {rid: {"status": "NO_DIRECT_TRIGGERS", "triggers": []}
        for rid in tree.requirements if tree.children[rid]}}
    save_json(arc_dir / "frontend_e2e.json", report)
    try:
        if not ui["pages"]:
            report.update({"status": "NO_UI", "summary": {"requirements": 0, "triggers": 0, "e2e_tests": 0, "ui_tests": 0, "failed": 0}})
            save_json(arc_dir / "frontend_e2e.json", report)
            return report
        model = TextModel()
        supplied = prepare_supplied_specs(input_dir, arc_dir.parent.resolve(), tree.leaves)
        result = run_trigger_tdd(arc_dir, tree, schema, backend, ui, interactions, images,
            contexts, model, DemoConsole(pause), report, max_turns, max_tests, supplied=supplied)
        acceptance = run_independent_acceptance(input_dir, arc_dir, schema, backend, ui, interactions,
            model, DemoConsole(pause), max_turns, process_report=result, supplied=supplied)
        if any(entry.get("repair_checks") for entry in acceptance["requirements"].values()):
            result = verify_frontend(arc_dir, tree, backend, ui, DemoConsole(pause), result)
        # Grader-equivalent run: production build, built server, fresh database, supplied specs.
        production = run_production_acceptance(arc_dir, ui, supplied, model, DemoConsole(pause), max_turns)
        if production.get("status") == "GREEN" and production.get("changed_files"):
            try:
                result = verify_frontend(arc_dir, tree, backend, ui, DemoConsole(pause), result)
            except (ValueError, RuntimeError, OSError) as error:
                production["verify_error"] = str(error)
        result["verification"]["independent_acceptance"] = {"status": "GREEN" if acceptance["status"] == "GREEN" else "FAILED",
            "report": ".arc/independent_acceptance.json"}
        result["verification"]["production_acceptance"] = {
            "status": "GREEN" if production.get("status") == "GREEN" else "FAILED",
            "report": ".arc/production_acceptance.json",
            **({"error": production["error"]} if production.get("error") else {})}
        if acceptance["status"] != "GREEN" or production.get("status") != "GREEN":
            result["status"] = "COMPLETED_WITH_FAILURES"
        save_json(arc_dir / "frontend_e2e.json", result)
        print(f"Frontend functional smoke {result['status']}: {result['summary']}\nReport: {arc_dir / 'frontend_e2e.json'}", flush=True)
        return result
    except BaseException as error:
        report.update({"status": "FAILED", "error": str(error)})
        for requirement in report["requirements"].values():
            if requirement["status"] not in {"GREEN", "NO_TRIGGERS", "NO_DIRECT_TRIGGERS"}:
                requirement["status"] = "FAILED"
        save_json(arc_dir / "frontend_e2e.json", report)
        raise
