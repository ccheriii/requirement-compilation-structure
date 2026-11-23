"""Independent requirement acceptance, separate from implementation-owned tests.

When the input supplies grader-style specs for a requirement (see supplied.py), those
read-only specs are its acceptance suite: no suite is generated and no test review runs.
Supplied specs always run in their own Playwright invocation, so the database is the
single fresh startup seed a grader would see. Other requirements get a generated suite
that follows a static black-box locator policy.
"""

from hashlib import sha256
import re

import yaml

from infrastructure.shared.agent.tools import FileTools
from infrastructure.shared.agent.loop import run_file_agent
from infrastructure.shared.agent.parallel import parallel_agents, run_parallel
from infrastructure.stages.frontend.browser import UNAVAILABLE_NOTE, browser_available
from infrastructure.shared.agent.conventions import task_conventions
from infrastructure.shared.agent.context import SourceFiles
from infrastructure.shared.agent.discovery import project_index
from infrastructure.shared.agent.recovery import implementation_task
from infrastructure.shared.project.entrypoints import selected_path
from infrastructure.stages.backend.context import requirement_context, related_schema, startup_references
from infrastructure.stages.requirements.contracts import CONTRACT_RULES
from infrastructure.stages.backend.testing import BackendChecks, require_green as require_backend_green
from infrastructure.stages.requirements.tree import RequirementTree
from infrastructure.stages.frontend.triggers.implementation import (scenario_context, IMPLEMENT_INSTRUCTIONS,
    LOCATOR_POLICY, editable_runtime_path, test_run_status)
from infrastructure.stages.frontend.triggers.e2e_runtime import CONFIG, FIXTURE
from infrastructure.stages.frontend.triggers.supplied import (install_supplied_specs, normalize_supplied,
    protect_workspace, requirement_specs)
from infrastructure.stages.frontend.triggers.testing import E2EChecks, test_path
from infrastructure.stages.frontend.triggers.scope import TriggerCheckFailure
from infrastructure.stages.frontend.triggers.design import api_ids
from infrastructure.stages.frontend.ui.runtime import APPLICATION_ENTRYPOINTS
from infrastructure.stages.frontend.ui.testing import FrontendChecks, require_green, test_path as ui_test_path
from infrastructure.utils.files import save_json
from infrastructure.shared.project.git import checkpoint

INSTRUCTIONS = '''Write independent final acceptance tests for ONE original requirement.
Derive expected results from original_requirement and its Given/When/Then scenarios.
Read project files as needed to understand test configuration, database initialization,
fixtures and API wiring. Source code and existing tests describe how to run the
application; they are not authority for expected business behavior. Derive assertions
from the original requirement, not the current implementation or process smoke tests.
Only test_path is writable. Reading a file does not grant permission to modify it.

Write a small Playwright suite at test_path using the supplied test/expect/database
fixture. Drive When actions through the real browser without mocks or injected state.
Treat the application as a black box: locate every control with the locator policy below.
The suite is checked statically; getByTestId, data-testid, CSS id/class/attribute selectors,
XPath, nth(), first() and last() reject it. ui_interfaces lists each page's route and visible
names/options only as hints; infer control roles from the original requirement wording,
never from the current design or implementation. When task.locator_style_reference lists
grader-style helper files, read them and follow their conventions; they are read-only and
recreated on every run, so copy the few helpers you need into test_path instead of importing them.
Assert original required visible business outcomes, not design-inferred internal labels or
every DOM attribute. Cover main success, critical rejection, correct identity/ownership, and
refresh/persistence WHEN required.
Use two distinct identities where identity isolation matters; a test that only creates
one account cannot reject an implementation returning the first account for everyone.
For required rejection-without-writes, verify the relevant persisted result is unchanged.
Prefer user-visible outcomes over unspecified cookie/hash encodings or storage strategies.
For a When action requiring one API operation, also observe its relevant method/endpoint and
reject duplicate submissions when the requirement expects one operation. Start observing
before the action, wait for its response and business completion, and exclude unrelated
page-entry calls or explicitly required retries. Do not count all requests globally.
There is no arbitrary test-count quota: cover the few important outcomes for this task.

Given state. Records named by concrete identities in a Given (for example an existing
account with a stated username) are provided by requirement-defined startup seeds that the
database fixture restores before each test: reuse them and never insert them again. When a
Given says the state was produced through another requirement (\\ref[requirement](X),
"registered an account through X", "is signed in", "has created ..."), drive that
prerequisite flow through the browser UI exactly as a user would, then perform the current
When. Do not assume an empty database. Arrange only scenario-specific absence or
conflicting Given states that no user flow can produce in the isolated test database, using
the documented database/test utilities with minimal valid records. Application sources,
shared fixtures, supplied specs, original requirements and other suites are not editable.
During generation only TypeScript and the locator policy are checked; missing application
behavior is a useful acceptance failure, never a reason to weaken assertions. Use edit_file
to create or repair only test_path under frontend/tests/e2e/acceptance.

''' + LOCATOR_POLICY

REVIEW_INSTRUCTIONS = '''Review this independent acceptance test against the original requirement.
This is a separate test review, not implementation repair. Actual failure evidence is
provided. Diagnose test_requirement_mismatch, test_overconstraint, environment, or
implementation. Change ONLY test_path if specific original requirement or fixture evidence
shows the test is wrong. Preserve every required business outcome. If the implementation
or environment is wrong, return tool_calls=[] and explain it; the failed acceptance remains
failed. Compilation passing is enough to finish this review; you are NOT asked to turn a
business failure GREEN. Never adjust expected values just to match observed wrong behavior.
Read project files as needed to understand wiring, fixtures and failure evidence. Use
original requirements as authority for expected business behavior; source code and existing
tests do not override them. Only test_path is writable; all other files remain read-only.

Locator roles, accessible names, exact: true, input type="password", role="alert",
radiogroup/radio, combobox options, link href and URL (toHaveURL) assertions are requirement
contracts, never overconstraint. Never replace them with test ids, CSS, XPath, nth/first/last,
partial or regex names or different roles, and never delete them. If such a locator does not
resolve, is ambiguous (strict mode violation) or its assertion fails, diagnose implementation
and return tool_calls=[]. Edits that break the locator policy are rejected automatically.'''

REPAIR_NOTE = '''Independent acceptance suites (frontend/tests/e2e/acceptance) and supplied specs
(frontend/tests/e2e/supplied) are read-only for implementation repair. Preserve their
original requirement outcomes and repair the real application. Test review has already been
performed separately. Failures in supplied final regression/build checks are also repair
targets: retrieve the failing requirement and source evidence on demand, and preserve other
accepted behavior.
Contract precedence: supplied specs > generated independent acceptance suites > compiler/process
smoke tests (frontend/tests/e2e/*.spec.ts) > UI presence specs (frontend/tests/ui). When a
lower-precedence smoke or UI presence test conflicts with the role, accessible-name, route or
flow contract of a higher-precedence spec, update that lower-precedence test to the higher
contract (keeping its other coverage) instead of breaking the application contract. This is the
only case in which a smoke test's locators or roles may change.
Supplied specs run without fixtures against a fresh database seeded once at server startup:
records they use by concrete identity must come from requirement-defined startup seed data,
and flows they drive (for example sign-up, sign-in, sign-out, reload) must work in sequence in
one browser session. Strict-mode violations mean duplicate accessible names in the application.'''

BANNED_LOCATOR = re.compile(r"getByTestId|data-testid|xpath=|\.nth\(|\.first\(\)|\.last\(\)"
    r"|\.locator\(\s*['\"`][#.\[]")
CONTRACT_ASSERTION = re.compile(r"getByRole\(|getByLabel\(|getByText\(|toHaveURL\("
    r"|toHaveAttribute\(\s*['\"`]type['\"`]\s*,\s*['\"`]password")


def _code(source):
    """Drop comments so explanatory text does not trigger the static locator lint."""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("//"))


def locator_lint(source):
    """Return banned locator constructs found in a generated acceptance suite."""
    return sorted({match.group(0) for match in BANNED_LOCATOR.finditer(_code(source))})


def contract_assertions(source):
    """Count role/label/text/URL/password contract assertions in a suite."""
    return len(CONTRACT_ASSERTION.findall(_code(source)))


def lint_message(found):
    return ("Acceptance tests must locate controls as a black-box grader would: getByRole(role, "
        "{ name, exact: true }), getByLabel(name, { exact: true }) for password fields and "
        "getByText(value, { exact: true }) for displayed values, from the page root. Remove: "
        + ", ".join(found))


def require_acceptance(result):
    require_green(result)
    if result.get('skipped') or result.get('flaky'):
        raise ValueError('Independent acceptance includes skipped or unstable tests')


def acceptance_path(rid):
    return 'frontend/tests/e2e/acceptance/' + sha256(rid.encode()).hexdigest()[:12] + '.spec.ts'


def original_requirements(input_dir):
    """Read the original YAML again; no generated design is used as the test oracle."""
    path = input_dir / 'requirements.yaml'
    if not path.is_file():
        raise ValueError('Independent acceptance requires original input/requirements.yaml')
    return RequirementTree(yaml.safe_load(path.read_text(encoding='utf-8-sig')))


def _option_names(options):
    names = []
    for option in options if isinstance(options, list) else []:
        if isinstance(option, dict):
            option = option.get('label') or option.get('text') or option.get('value')
        if isinstance(option, (str, int, float)) and str(option).strip():
            names.append(str(option))
    return names


def visible_interfaces(page_subtrees):
    """Project pages to their route and visible names/options only.

    Element types, input types, ids and data bindings are design decisions; the
    independent oracle must infer roles from the requirement instead of copying them."""
    interfaces = {}
    for pid, page in page_subtrees.items():
        names = []
        for element in (page.get('elements') or {}).values():
            prop = element.get('prop') if isinstance(element, dict) else None
            if not isinstance(prop, dict) or prop.get('decorative'):
                continue
            name = next((prop[key] for key in ('label', 'text') if isinstance(prop.get(key), str)
                and prop[key].strip()), None)
            if not name:
                continue
            item = {'name': name}
            options = _option_names(prop.get('options'))
            if options:
                item['options'] = options
            if item not in names:
                names.append(item)
        interfaces[pid] = {'route': page.get('route'), 'visible_names': names}
    return interfaces


def _unique(paths):
    return list(dict.fromkeys(paths))


def run_independent_acceptance(input_dir, arc_dir, schema, backend, ui, design, model, console,
        max_turns=10, *, process_report, supplied=None):
    output = arc_dir.parent.resolve()
    tree = original_requirements(input_dir)
    if supplied is None:
        supplied = install_supplied_specs(input_dir, output, list(tree.leaves))
    supplied = normalize_supplied(supplied)
    helpers = list(supplied['helpers'])
    leaves = list(tree.leaves)
    checks = E2EChecks(output)
    ui_checks = FrontendChecks(output)
    backend_checks = BackendChecks(output)
    selection = SourceFiles(output, ui)
    backend_selection = SourceFiles(output, root='backend/src', tests_root='backend/tests')
    context_index = project_index(output,
        kinds=('schema', 'api', 'page', 'ui_element', 'ui_data', 'trigger', 'requirement',
            'test_requirement', 'convention', 'file'),
        file_roots=('frontend/src', 'frontend/tests', 'backend/src', 'backend/tests'))
    accepted = []
    verified_process = set()
    pending_verification = any(check['status'] == 'FAILED'
        for check in process_report.get('verification', {}).values())
    report = {'status': 'RUNNING', 'requirements': {},
        'supplied': {'specs': supplied['all_specs'], 'unmapped': supplied['unmapped'], 'helpers': helpers}}
    if not browser_available():
        # Nothing can execute acceptance suites; repairing against unrun tests would only churn code.
        report.update(status='UNVERIFIED', reason=UNAVAILABLE_NOTE,
            requirements={rid: {'status': 'UNVERIFIED'} for rid in leaves})
        save_json(arc_dir / 'independent_acceptance.json', report)
        console.show('Independent acceptance | UNVERIFIED', UNAVAILABLE_NOTE)
        return report
    save_json(arc_dir / 'independent_acceptance.json', report)

    def acceptance_task(rid, rid_specs, path):
        """Requirement-grounded generation/repair inputs, independent of other requirements."""
        context = scenario_context(ui, design, rid)
        aids = set(backend['requirements'].get(rid, []))
        for trigger in context['triggers'].values():
            aids.update(api_ids(trigger))
        apis = {aid: backend['apis'][aid] for aid in aids}
        task = {'original_requirement': requirement_context(tree, rid),
            'test_path': rid_specs[0] if rid_specs else path,
            'ui_interfaces': visible_interfaces(context['page_subtrees']),
            'api_interfaces': {aid: {key: value for key, value in api.items()
                if key in ('signature', 'request', 'response')} for aid, api in apis.items()},
            'schema': related_schema(schema, apis.values())}
        return context, aids, apis, task

    def generation_inputs(rid, path, task):
        """Workspace and compile/lint gate for writing one generated acceptance suite."""
        if helpers:
            task['locator_style_reference'] = {'files': helpers,
                'note': 'Grader-style helpers (read-only). Follow their locator conventions.'}
        # Exact readable paths: guessed names (e.g. BookDetail.tsx for Book.tsx) cost a model round.
        task['application_files'] = sorted(path.relative_to(output).as_posix()
            for path in (output / 'frontend/src').rglob('*') if path.suffix in ('.ts', '.tsx') and path.is_file())
        workspace = FileTools.for_target(output, 'acceptance_test', rid, fixed_path=path,
            references=[FIXTURE, 'backend/tests/support/context.ts', *helpers,
                CONFIG, *startup_references(output)])

        def compile_suite():
            if not (output / path).is_file():
                raise ValueError('Create the supplied acceptance test_path')
            found = locator_lint((output / path).read_text(encoding='utf-8-sig', errors='replace'))
            if found:
                raise ValueError(lint_message(found))
            result = checks.typecheck([path], full_source=False)
            if result['returncode']:
                raise ValueError(result['output'])
            return result
        return workspace, compile_suite

    def pregenerate(rid):
        """Write one generated suite. Its gate is compile + lint only and its workspace
        writes only its own fixed path, so suites for different requirements are
        independent and can be written concurrently before the sequential run/repair loop."""
        path = acceptance_path(rid)
        target = output / path
        if target.is_file() and locator_lint(target.read_text(encoding='utf-8-sig', errors='replace')):
            target.unlink()
        if target.is_file():
            return None
        _context, _aids, _apis, task = acceptance_task(rid, [], path)
        if not task['ui_interfaces']:
            return None
        task['shared_conventions'] = task_conventions(output, {'requirement': task['original_requirement']})
        workspace, compile_suite = generation_inputs(rid, path, task)
        try:
            agent = run_file_agent(model, INSTRUCTIONS, task, workspace, compile_suite,
                max_turns, console, 'Independent acceptance / ' + rid + ' / Generation')
        except BaseException:
            # Leave no half-written suite: the sequential loop regenerates it from scratch.
            target.unlink(missing_ok=True)
            raise
        return agent['turns']

    generated_ahead = {}
    pending_generation = [rid for rid in leaves if not requirement_specs(supplied, rid)]
    if len(pending_generation) > 1 and parallel_agents() > 1:
        console.show('Independent acceptance | Parallel generation',
            f'Writing {len(pending_generation)} independent acceptance suites with up to '
            f'{parallel_agents()} concurrent agents; running and repairing stay sequential.')
        for rid, outcome in run_parallel(pregenerate, pending_generation).items():
            if isinstance(outcome, BaseException):
                # The sequential loop regenerates this suite with its normal gate.
                generated_ahead[rid] = {'error': str(outcome)[:2000]}
            elif outcome is not None:
                generated_ahead[rid] = {'turns': outcome}
        checkpoint(output, stage="10", task="independent acceptance",
            action="Generate independent acceptance suites in parallel",
            details={'generated': sorted(rid for rid, value in generated_ahead.items() if 'turns' in value),
                'failed': sorted(rid for rid, value in generated_ahead.items() if 'error' in value)})

    for position, rid in enumerate(leaves):
        path = acceptance_path(rid)
        rid_specs = requirement_specs(supplied, rid)
        # Specs not mapped to one requirement may span several; run them once all exist.
        extra_specs = list(supplied['unmapped']) if position == len(leaves) - 1 else []
        own_supplied = _unique([*rid_specs, *extra_specs])
        entry = ({'status': 'RUNNING_SUPPLIED', 'source': 'supplied', 'test_path': rid_specs[0],
                'test_paths': rid_specs} if rid_specs else
            {'status': 'GENERATING', 'source': 'generated', 'test_path': path})
        report['requirements'][rid] = entry
        label = 'Independent acceptance / ' + rid
        try:
            context, aids, apis, task = acceptance_task(rid, rid_specs, path)
            if rid_specs:
                task['supplied_acceptance_specs'] = rid_specs
            elif not task['ui_interfaces']:
                entry.update(status='FAILED', error='No UI interface available for independent requirement acceptance; not verified')
                continue
            task['shared_conventions'] = task_conventions(output, {'requirement': task['original_requirement']})
            result, supplied_result = None, None
            if own_supplied:
                supplied_result = checks.tests(own_supplied)
                entry['supplied_result'] = supplied_result
            if rid_specs:
                result = supplied_result
                entry['result'] = result
            else:
                workspace, compile_suite = generation_inputs(rid, path, task)
                if rid in generated_ahead:
                    entry['parallel_generation'] = generated_ahead[rid]
                    if 'turns' in generated_ahead[rid]:
                        entry['generation_turns'] = generated_ahead[rid]['turns']
                target = output / path
                if target.is_file() and locator_lint(target.read_text(encoding='utf-8-sig', errors='replace')):
                    # A suite from an older run that breaks the locator policy is regenerated.
                    target.unlink()
                    entry['regenerated'] = 'locator_policy'
                if not target.is_file():
                    agent = run_file_agent(model, INSTRUCTIONS, task, workspace, compile_suite,
                        max_turns, console, label + ' / Generation')
                    entry['generation_turns'] = agent['turns']
                result = checks.tests([path])
                entry["result"] = result
                try:
                    require_acceptance(result)
                except ValueError:
                    # A separate, requirement-grounded review may correct the test, but its
                    # gate intentionally does not demand behavioral GREEN.
                    entry['initial_failure'] = result
                    save_json(arc_dir / 'independent_acceptance.json', report)
                    checkpoint(output, stage="10", task=rid, action="Record independent acceptance before test review",
                        status="FAILED", details={"test_path": path, "checks": result})
                    original = target.read_bytes()
                    entry['contract_assertions'] = {'before_review': contract_assertions(
                        original.decode('utf-8', errors='replace'))}

                    # The review gate is compile + locator lint only: a correct review may
                    # remove an invented assertion, so assertion counts are recorded, not gated.
                    try:
                        review = run_file_agent(model, REVIEW_INSTRUCTIONS, task, workspace, compile_suite,
                            max_turns, console, label + ' / Test review', feedback=result)
                        entry['review_turns'] = review['turns']
                        entry['contract_assertions']['after_review'] = contract_assertions(
                            target.read_text(encoding='utf-8-sig', errors='replace'))
                    except (ValueError, RuntimeError) as error:
                        # A rejected review never weakens the suite: restore it and repair the app.
                        target.write_bytes(original)
                        entry['review_error'] = str(error)[:2000]
                    result = checks.tests([path])
                    entry["result"] = result
            if (test_run_status(result) != 'GREEN' or pending_verification
                    or (supplied_result is not None and test_run_status(supplied_result) != 'GREEN')):
                entry['status'] = 'REPAIRING'
                process_ids = [key for key, value in process_report['requirements'].items()
                    if pending_verification or value.get('status') == 'GREEN' or key == rid]
                process_files = [test_path(key, output) for key in process_ids
                    if (output / test_path(key, output)).is_file()]
                gate_keys = [*accepted, rid]
                supplied_files = _unique([spec for key in gate_keys for spec in requirement_specs(supplied, key)]
                    + extra_specs)
                acceptance_files = [acceptance_path(key) for key in gate_keys
                    if not requirement_specs(supplied, key) and (output / acceptance_path(key)).is_file()]
                ui_files = [ui_test_path(pid) for pid in ui['pages']
                    if (output / ui_test_path(pid)).is_file()]
                repair_task = {**task, **context, 'requirement': task['original_requirement'],
                    'backend_apis': apis, 'target_trigger_ids': sorted(context['triggers']),
                    'repair_reason': 'final_verification_and_acceptance' if pending_verification else 'independent_acceptance',
                    'contract_precedence': ['supplied specs', 'independent acceptance', 'smoke tests', 'UI presence tests'],
                    'application_entrypoints': list(APPLICATION_ENTRYPOINTS),
                    'backend_entrypoints': {'registry': 'backend/src/api/register.ts',
                        'app': 'backend/src/app.ts', 'server': 'backend/src/server.ts'}}
                if supplied_files:
                    repair_task['supplied_acceptance_specs'] = supplied_files
                own_smoke = test_path(rid, output)
                repair_workspace = selection.workspace(
                    paths=selection.paths('page', context['page_subtrees']),
                    references=[*(rid_specs or [path]), *extra_specs, *helpers,
                        *([own_smoke] if (output / own_smoke).is_file() else []),
                        *ui_files, FIXTURE, 'frontend/src/ui-runtime.ts',
                        *APPLICATION_ENTRYPOINTS, 'frontend/package.json', 'frontend/tsconfig.json',
                        *startup_references(output)])
                conflict_tests = set(process_files) | set(ui_files)
                repair_workspace.allow_write = lambda relative: (editable_runtime_path(output, relative)
                    or relative in conflict_tests)
                backend_selection.include(repair_workspace, backend_selection.paths('api', aids))
                protect_workspace(repair_workspace)

                def repair_gate():
                    evidence = {}
                    try:
                        if supplied_files:
                            # Own invocation: one fresh database seeded at server startup.
                            evidence['supplied'] = checks.tests(supplied_files)
                            require_acceptance(evidence['supplied'])
                        evidence['typecheck'] = checks.typecheck([*process_files, *acceptance_files])
                        if evidence['typecheck']['returncode']:
                            raise ValueError(evidence['typecheck']['output'])
                        if acceptance_files:
                            evidence['acceptance'] = checks.tests(acceptance_files)
                            require_acceptance(evidence['acceptance'])
                        if process_files:
                            evidence['smoke'] = checks.tests(process_files)
                            require_acceptance(evidence['smoke'])
                        if ui_files:
                            evidence['ui'] = ui_checks.tests(ui_files)
                            require_green(evidence['ui'])
                        if pending_verification or any(relative.startswith('backend/') for relative in repair_workspace.changed_paths):
                            evidence['backend_typecheck'] = backend_checks.typecheck()
                            if evidence['backend_typecheck']['returncode']:
                                raise ValueError(evidence['backend_typecheck']['output'])
                            suites = [selected for aid in backend['apis']
                                if (selected := selected_path(output, 'backend_test', aid))
                                and (output / selected).is_file()]
                            if suites:
                                evidence['backend'] = backend_checks.tests(suites)
                                require_backend_green(evidence['backend'])
                        evidence['build'] = checks.build()
                        if evidence['build']['returncode']:
                            raise ValueError(evidence['build']['output'])
                    except (ValueError, RuntimeError) as error:
                        raise TriggerCheckFailure(str(error), evidence) from error
                    return evidence

                def repair_feedback(value):
                    return selection.feedback(repair_workspace, backend_selection.feedback(repair_workspace, value))

                initial_feedback = {'acceptance': result,
                    'verification': process_report.get('verification', {}) if pending_verification else {}}
                if supplied_result is not None and supplied_result is not result:
                    initial_feedback['supplied'] = supplied_result
                with implementation_task(output, entry, console, label + ' / Repair'):
                    repair = run_file_agent(model, IMPLEMENT_INSTRUCTIONS + '\n\n' + REPAIR_NOTE,
                        repair_task, repair_workspace, repair_gate, max_turns * 2, console,
                        label + ' / Repair', feedback=initial_feedback,
                        feedback_filter=repair_feedback,
                        context_index=context_index)
                    checks_run = repair['checks']
                    final_key = next((key for key in (('supplied', 'acceptance') if rid_specs
                        else ('acceptance', 'supplied', 'smoke')) if key in checks_run), None)
                    if final_key is None:
                        raise ValueError('Repair gate ran no acceptance suite for this requirement')
                    entry.update(status='GREEN', repair_turns=repair['turns'],
                        result=checks_run[final_key], repair_checks=checks_run)
                    if 'supplied' in repair['checks']:
                        entry['supplied_result'] = repair['checks']['supplied']
                if entry['status'] == 'FAILED':
                    continue
                result = entry['result']
                supplied_result = entry.get('supplied_result')
                verified_process.update(key for key in process_ids if test_path(key, output) in process_files)
                pending_verification = False
            require_acceptance(result)
            if supplied_result is not None:
                require_acceptance(supplied_result)
            process_entry = process_report['requirements'].get(rid)
            if process_entry and process_entry.get('status') == 'FAILED' and rid in verified_process:
                process_entry['previous_failure'] = dict(process_entry)
                process_entry.update(status='GREEN', recovery='independent_acceptance_repair')
                save_json(arc_dir / 'frontend_e2e.json', process_report)
            entry.update(status='GREEN', result=result)
            accepted.append(rid)
        except (ValueError, RuntimeError) as error:
            entry.update(status='FAILED', error=str(error))
        finally:
            save_json(arc_dir / 'independent_acceptance.json', report)
            checkpoint(output, stage="10", task=rid, action="Publish independent requirement acceptance",
                status=entry['status'], details=entry)
            console.show(label + ' | ' + entry['status'], entry.get('error', 'Real acceptance checks completed'))
    report['status'] = 'GREEN' if report['requirements'] and all(
        entry['status'] == 'GREEN' for entry in report['requirements'].values()) else 'COMPLETED_WITH_FAILURES'
    save_json(arc_dir / 'independent_acceptance.json', report)
    checkpoint(output, stage="10", task="independent acceptance", action="Publish final independent acceptance report",
        status=report['status'], details=report)
    return report


# Acceptance suites use the declared role/name contracts verbatim, like the external grader.
INSTRUCTIONS = INSTRUCTIONS + "\n\n" + CONTRACT_RULES + (
    "Locate every contracted element with getByRole(ROLE, {name: NAME, exact: true}) exactly as declared.\n")
REVIEW_INSTRUCTIONS = REVIEW_INSTRUCTIONS + ("\n\nrequirement.role_contracts are requirement contracts: "
    "never weaken or replace a contracted locator.")
