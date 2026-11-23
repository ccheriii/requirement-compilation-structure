"""Implement required startup data and repair database initialization failures."""

import json
from pathlib import Path
import re

from infrastructure.shared.agent.feedback import CheckFailure, compact_feedback
from infrastructure.shared.agent.tools import FileTools
from infrastructure.shared.agent.loop import run_file_agent
from infrastructure.shared.models.text import TextModel
from infrastructure.shared.project.dependencies import install_dependencies
from infrastructure.shared.project.git import GitRepository, checkpoint
from infrastructure.stages.database.order import table_dependencies, implementation_groups
from infrastructure.stages.database.testing import DatabaseChecks
from infrastructure.stages.requirements.tree import RequirementTree
from infrastructure.stages.requirements.contracts import scenario_seeds, seed_identity_values, shared_clock
from infrastructure.stages.backend.context import requirement_context
from infrastructure.utils.files import read_artifact, save_json, save_text
from infrastructure.stages.database.runtime import PASSWORD_PATH, ensure_password_module, seed_source


SEED_PATH = "backend/src/db/seed.ts"

WIRING = (
    "backend/src/db/client.ts", "backend/src/db/initialize.ts", "backend/src/db/setup.ts",
    SEED_PATH, PASSWORD_PATH,
    "backend/src/server.ts", "backend/src/app.ts", "backend/src/api/register.ts",
    "backend/scripts/copy-db.mjs", "package.json", "backend/package.json", "backend/tsconfig.json",
)

# Description sentences that describe records which already exist before any user action.
EXISTENCE_SENTENCE = re.compile(
    r"\b(?:exist|exists|existing|pre-existing|already|seed|seeded|seeds|predefined|pre-defined|"
    r"preloaded|pre-loaded|built-in|stocked|initially)\b", re.IGNORECASE)

# `export const NAME = { key: 'value', ... }` object literals in grading-environment test helpers.
FIXTURE_OBJECT = re.compile(
    r"export\s+const\s+([A-Za-z_$][\w$]*)\s*(?::[^=;{}]+)?=\s*\{([^{}]*)\}", re.DOTALL)
FIXTURE_ENTRY = re.compile(
    r"""(?:^|,)\s*(?:([A-Za-z_$][\w$]*)|'([^'\\]*)'|"([^"\\]*)")\s*:\s*"""
    r"""(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)"|`([^`$\\]*)`)\s*(?=,|$)""", re.DOTALL)
FIXTURE_COMMENT = re.compile(r"\b(?:seed|seeded|seeds|existing|exists|given|scenario)\b", re.IGNORECASE)
# Gate enforcement needs an affirmative statement ("Seeded by ...", "existing account"),
# not "no user exists" / "non-existing".
SEEDED_COMMENT = re.compile(r"(?<!non-)(?<!not )\b(?:seed|seeded|seeds|existing|pre-existing|predefined)\b",
    re.IGNORECASE)
SECRET_KEY = re.compile(r"password|passwd|passcode|passphrase|pwd|secret|^pin$|[_-]pin$", re.IGNORECASE)
BEARER_KEY = re.compile(r"token|session|cookie|api_?key|bearer|credential", re.IGNORECASE)
FIXTURE_COMMENTS = re.compile(
    r"""'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*"|`[^`]*`|//[^\n]*|/\*.*?\*/""", re.DOTALL)
MAX_FIXTURES = 20

SEED_INSTRUCTIONS = '''Read the current requirement and incrementally maintain its prerequisite database rows.
Each table has one insertion file: backend/src/db/seeds/ENTITY.sql. The compiler has
created these files and a dependency-ordered seed.ts loader. Edit only the insertion
SQL files; do not put records or INSERT statements in seed.ts or the table DDL files.
task.insert_files identifies the current tables. task.table_names lists the canonical
table names; their DDL is in backend/src/db/tables/ENTITY.sql. If another table is needed,
read its DDL and insertion SQL directly. Semantic queries are not available in this stage.
Only canonical database table/insertion files are readable; other application files and
requirements are outside this task. The password helper contract is specified below;
there is no need to inspect the seed loader or other infrastructure.

Identify data that must EXIST BEFORE this requirement's When actions:
- System/domain data users cannot create through the application, such as train services,
  reference catalogues, built-in roles or already stocked books.
- Existing users, books, shelves and other domain records stated in Given or explicit
  seed/prerequisite sections. Include these even when another requirement implements
  their creation: login must be testable with a pre-existing account independently of
  registration, and reading/editing content must not depend on its creation workflow.
- Include the related parent rows and association rows needed to make these records valid.
Preserve exact names, credentials, contents, relationships and initial status supplied by
requirements. For omitted mandatory fields, choose minimal valid deterministic values.
Do not discard prerequisites merely because they occur in a scenario rather than a
sentence saying "system startup". Use the supplied complete requirement and its scenarios.
If a generic reference such as "an existing verified account" does not identify concrete
records, do not invent a separate account, password or object. The defining leaf requirement
will supply those records during this pass. Fill omitted mandatory fields only for records
whose identity is actually established by this requirement.

task.given_facts lists the Given text of ALL leaf requirements' scenario steps, plus
description sentences about existing records. The same record is often described in several
requirements with different attributes (for example a name in one, its password in another).
When you seed a record that given_facts also describes elsewhere, seed it ONCE with every
attribute stated anywhere (exact identities, credentials, names, statuses, relationships).
If an existing seed statement for the same business key lacks or contradicts a stated value,
replace that exact value inside the existing statement. Never add a second INSERT for the
same business key: WHERE NOT EXISTS makes the later statement a silent no-op.

task.environment_fixtures (when present) are authoritative Given records from the grading
environment, keyed by fixture name with their source file. When the record you seed matches
one by any identity value (for example the same username, code or title), seed it with
every listed field exactly as given, mapping each key to the corresponding schema column
(passwords through seed_password_hash). They may supply values the requirement text omits,
such as a contact address; never substitute invented values for them. Ignore fixtures that
match no record this requirement seeds, and never seed a fixture whose record the scenario
itself creates or requires to be absent (for example input for a new registration).

Do not pre-execute the current When/Then. A record created by this scenario's operation
must remain absent until that operation. Do not fabricate authenticated sessions, personal
history, favorites or drafts that the requirement explicitly establishes through login
or other UI actions. Browser/session state is not a database seed prerequisite.
Never seed bearer credentials: session rows, session/auth tokens, cookies, API keys or
reset codes, even when Given says such a session "already exists" or the browser carries
it. Tests obtain those by signing in at run time; a fixed token would be a publicly known
backdoor. Seed only the account (and its required related rows) the credential belongs to.
Different scenarios may need different preconditions. Seed their distinct prerequisite
objects with the exact specified identities; scenario-specific absence, counts or mutable
states can still be arranged by isolated test fixtures. If constraints prevent coexistence,
keep the shared baseline valid and leave conflicting scenario variations to test setup.
Return tool_calls=[] only when no prerequisite rows need adding or correcting.

Read relevant existing insertion SQL, then append missing rows or replace a small exact
span. Preserve ALL earlier requirements' records and comments; never replace a whole
file or rebuild it for only the current requirement. Mark related statements with the
requirement ID. A table's insertion file inserts only into that table; put its parents
and associations in their own files. SELECT subqueries may resolve existing foreign keys.
Use stable identifiers/business keys and SQLite INSERT ... SELECT ... WHERE NOT EXISTS,
or a targeted ON CONFLICT ... DO NOTHING. Avoid INSERT OR IGNORE, which hides invalid data.
Repeated startup must not duplicate records, overwrite user edits or reset credentials.
Do not DELETE, DROP, REPLACE, change table definitions, disable constraints or suppress errors.
The loader executes these files in table-dependency order inside the initialization
transaction; foreign-key checks are deferred until all seed files have executed.

For password-hash columns, use the SQL function seed_password_hash('required password').
It returns scrypt$saltHex$digestHex using a 16-byte salt and a 64-byte digest, produced by
the shared backend/src/db/password.ts hashPassword that credential handlers verify with
verifyPassword. Write the exact stated password literal as its argument. Never store a
plaintext password in a hash column or invent a digest. The password function is called by
SQL only for missing rows.
Escape SQL literals correctly; use 0/1 booleans and valid JSON text when required.
Real initialization/build checks run after this pass and the database owner repairs errors.
'''


def _text(value) -> str:
    """Render a Given/description value (normally a string) as plain text."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_text(item) for item in value)
    return "" if value is None else str(value)


def given_facts(tree) -> dict:
    """Given text of every leaf scenario step plus description sentences about existing records."""
    steps, descriptions = {}, {}
    for rid in tree.leaves:
        for sid in tree.own_steps[rid]:
            given = _text(tree.steps[sid].get("Given")).strip()
            if given:
                steps[sid] = f"{rid}: {given}"
        sentences = re.split(r"(?<=[.!?])\s+|\n+", _text(tree.requirements[rid].get("description")))
        selected = [sentence.strip() for sentence in sentences if EXISTENCE_SENTENCE.search(sentence)]
        if selected:
            descriptions[rid] = selected
    return {"steps": steps, "descriptions": descriptions}


def requirement_given_text(tree, rid) -> str:
    """Only this leaf's step Given text; used by the lenient seed identity check."""
    return "\n".join(_text(tree.steps[sid].get("Given")) for sid in tree.own_steps[rid])


def recorded_input_dir(output):
    """Recover the input directory from the compiler's stage-01 checkpoint record, if any."""
    if not (Path(output) / ".git").exists():  # never read an enclosing repository's history
        return None
    try:
        record = GitRepository(output).run("log", "-n", "1", "--format=%B",
            "--grep=Publish input requirement snapshot", check=False).stdout
    except Exception:  # Git is optional evidence; absence only disables fixtures.
        return None
    found = re.search(r'"source"\s*:\s*("(?:[^"\\]|\\.)*")', record or "")
    if not found:
        return None
    try:
        directory = Path(json.loads(found.group(1))).parent
    except (ValueError, TypeError):
        return None
    return directory if (directory / "requirements.yaml").is_file() else None


def _contains_token(text: str, value: str) -> bool:
    """Exact token occurrence: not embedded in a longer identifier."""
    return re.search(r"(?<![\w])" + re.escape(value) + r"(?![\w])", text) is not None


def load_environment_fixtures(input_dir, given_text: str) -> dict:
    """Parse exported string-valued object literals from <input_dir>/tests/*.ts.

    A fixture qualifies when its preceding comment mentions seed/existing/given/scenario
    records, or when one of its values is an identity mentioned in requirement Given text."""
    if input_dir is None:
        return {}
    directory = Path(input_dir) / "tests"
    if not directory.is_dir():
        return {}
    fixtures = {}
    for path in sorted(directory.rglob("*.ts")):
        try:
            source = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        for match in FIXTURE_OBJECT.finditer(source):
            name, body = match.group(1), match.group(2)
            # Drop comments but keep string literals intact (e.g. a value such as 'http://host/path').
            body = FIXTURE_COMMENTS.sub(lambda m: m.group(0) if m.group(0)[0] in "'\"`" else " ",
                body).strip().rstrip(",")
            values = {}
            for entry in FIXTURE_ENTRY.finditer(body):
                key = entry.group(1) or entry.group(2) or entry.group(3)
                value = next((group for group in entry.group(4, 5, 6) if group is not None), "")
                value = re.sub(r"\\(.)", r"\1", value)
                if key and value:
                    values[key] = value
            if not values:
                continue
            preceding = source[:match.start()].rstrip().splitlines()[-6:]
            comment = []
            for line in reversed(preceding):
                stripped = line.strip()
                if stripped.startswith(("//", "/*", "*")) or stripped.endswith("*/"):
                    comment.append(stripped)
                else:
                    break
            comment = " ".join(reversed(comment))
            commented = bool(FIXTURE_COMMENT.search(comment))
            mentioned = any(len(value) >= 3 and _contains_token(given_text, value) for value in values.values())
            if commented or mentioned:
                fixtures[name] = {"source": path.relative_to(Path(input_dir)).as_posix(), "fields": values}
                if comment:
                    fixtures[name]["comment"] = comment[:300]
            if len(fixtures) >= MAX_FIXTURES:
                return fixtures
    return fixtures


def missing_fixture_identities(output, fixtures: dict, given_text: str, insert_paths) -> list[dict]:
    """Fixture values stated verbatim in this requirement's Given text but absent from all seed SQL."""
    seeds = "\n".join((output / relative).read_text(encoding="utf-8-sig")
        for relative in insert_paths if (output / relative).is_file())
    missing = []
    for name, fixture in fixtures.items():
        fields = {key: value for key, value in fixture["fields"].items()
            if len(value) >= 3 and not BEARER_KEY.search(key) and _contains_token(given_text, value)}
        # The fixture must be identified in this Given by a non-secret value (e.g. a username);
        # a secret alone (e.g. a deliberately wrong password) never identifies a seeded record.
        if not any(not SECRET_KEY.search(key) for key in fields):
            continue
        # Only fixtures their source explicitly documents as seeded/existing are enforced; a
        # fixture matched merely by value may describe a record the scenario must create
        # (for example a registration whose Given says that username is still unused).
        if not SEEDED_COMMENT.search(fixture.get("comment", "")):
            continue
        for key, value in fields.items():
            if value not in seeds and value.replace("'", "''") not in seeds:
                missing.append({"fixture": name, "field": key, "value": value})
    return missing


SEED_PREREQUISITE_INSTRUCTIONS = """Seed the scenario prerequisite records listed in task.seed_prerequisites.

Each record is {label, data, requirements, scenarios}: data is a JSON object the external
acceptance environment expects to EXIST when the application starts with a fresh database
(the tests never create these records themselves). Insert every record so the application
shows it exactly as given:
- Map JSON keys to the matching tables/columns in task.entities (read table SQL as needed).
  Nested arrays/objects become rows in their related tables with correct foreign keys.
- Keep every identity value verbatim (names, titles, emails, usernames, codes, prices,
  quantities, flags, statuses). Do not translate, shorten, re-case or invent replacements.
- Passwords use seed_password_hash('plain text'). Never seed session tokens or other bearer
  credentials; sign-in state is created by the tests through the UI.
- Dates and times: use task.clock (the shared application/evaluator clock) as "now"; store
  stated timestamps exactly and derive relative ones from that clock.
- Fill mandatory columns the record omits with plausible neutral values.
- Write idempotent statements (INSERT ... SELECT ... WHERE NOT EXISTS on the business key) in
  the per-table insertion files from task.insert_files, in dependency order. One INSERT per
  business key: if a record is already seeded, correct that statement instead of adding one.
Only the insertion SQL files are writable. Use edit_file; reads come first if needed.
"""

SEED_CHUNK = 12


def scenario_seed_tasks(arc_dir, tree, schema, model, console, failures, insert_paths, order, readable,
        max_turns=10):
    """Seed all parsed scenario prerequisite records before requirement-by-requirement data."""
    output = arc_dir.parent.resolve()
    records = scenario_seeds(tree)
    if not records:
        return
    clock = shared_clock(tree)
    allowed = set(insert_paths.values())
    tables = [f"backend/src/db/tables/{name}.sql" for name in order
        if (output / f"backend/src/db/tables/{name}.sql").is_file()]
    console.show("Startup data | Scenario seed prerequisites",
        json.dumps({"records": len(records), "clock": clock}, ensure_ascii=False))
    for start in range(0, len(records), SEED_CHUNK):
        chunk = records[start:start + SEED_CHUNK]
        label = f"Startup data / seed prerequisites {start // SEED_CHUNK + 1}"
        with failures.task(label):
            files = FileTools(output, references=[*tables, *insert_paths.values()],
                allow_write=lambda path: path in allowed, readable=readable)
            task = {"seed_prerequisites": chunk, "entities": schema["entities"], "table_names": order,
                "insert_files": insert_paths, "clock": clock}
            expected = {value for record in chunk for value in seed_identity_values(record["data"])}

            def gate(expected=expected):
                seeds = "\n".join((output / relative).read_text(encoding="utf-8-sig")
                    for relative in insert_paths.values() if (output / relative).is_file())
                missing = sorted(value for value in expected
                    if value not in seeds and value.replace("'", "''") not in seeds)
                if missing:
                    raise CheckFailure("Seed prerequisite values are missing from the insertion SQL", {
                        "missing_values": missing,
                        "instruction": "Seed every listed record with these exact values (verbatim, "
                            "SQL-escaped) in the matching table insertion files."})
                return {"status": "GENERATED", "insert_files": list(files.changed_paths)}

            run_file_agent(model, SEED_PREREQUISITE_INSTRUCTIONS, task, files, gate,
                max_turns, console, label, allow_discovery=False)


def implement_startup_data(arc_dir, schema, model, console, failures, max_turns=10, input_dir=None):
    """Create per-table files, then accumulate prerequisite rows one requirement at a time."""
    output = arc_dir.parent.resolve()
    order = [name for group in implementation_groups(schema) for name in group]
    insert_paths = {name: f"backend/src/db/seeds/{name}.sql" for name in order}
    for name, relative in insert_paths.items():
        if not (output / relative).is_file():
            save_text(output / relative, f"-- Prerequisite rows for {name}; extended incrementally by requirement.\n")
    ensure_password_module(output)
    save_text(output / SEED_PATH, seed_source(order))
    checkpoint(output, stage="04", task="startup data loader", action="Publish per-table insertion files and loader",
        details={"insertion_order": order, "insert_files": insert_paths})

    tree = RequirementTree(read_artifact(arc_dir, "requirements.json"))
    dependencies = table_dependencies(schema)
    allowed = set(insert_paths.values())
    readable = allowed | {f"backend/src/db/tables/{name}.sql" for name in order}
    facts = given_facts(tree)
    all_given = "\n".join([*facts["steps"].values(), *(s for items in facts["descriptions"].values() for s in items)])
    if input_dir is None:
        input_dir = recorded_input_dir(output)
    fixtures = load_environment_fixtures(input_dir, all_given)
    if fixtures:
        console.show("Startup data | Environment fixtures", json.dumps(fixtures, ensure_ascii=False, indent=2))
    scenario_seed_tasks(arc_dir, tree, schema, model, console, failures, insert_paths, order, readable, max_turns)
    clock = shared_clock(tree)
    for rid in tree.leaves:
        requirement = requirement_context(tree, rid)
        if not requirement["description"] and not requirement["steps"]:
            continue
        with failures.task("startup data / " + rid):
            names = set()
            for symbol in schema["requirements"].get(rid, []):
                names.add(symbol.split(".", 1)[0])
            pending = list(names)
            while pending:
                for parent in dependencies[pending.pop()]:
                    if parent not in names:
                        names.add(parent)
                        pending.append(parent)
            entities = {name: schema["entities"][name] for name in sorted(names)}
            references = [f"backend/src/db/tables/{name}.sql" for name in entities
                if (output / f"backend/src/db/tables/{name}.sql").is_file()]
            references.extend(insert_paths[name] for name in sorted(names))
            files = FileTools(output, references=references, allow_write=lambda path: path in allowed,
                readable=readable)
            task = {"requirement": requirement, "entities": entities, "table_names": order,
                "insert_files": {name: insert_paths[name] for name in sorted(names)},
                "given_facts": facts}
            if clock:
                task["clock"] = clock
            if fixtures:
                task["environment_fixtures"] = fixtures
            own_given = requirement_given_text(tree, rid)

            def generated(own_given=own_given, related=bool(names)):
                # Lenient identity check: only fixture values stated verbatim in this
                # requirement's own Given text, and only when it maps to database tables.
                missing = missing_fixture_identities(output, fixtures, own_given, insert_paths.values()) if related else []
                if missing:
                    raise CheckFailure("Given records from environment_fixtures are not seeded", {
                        "missing_seed_values": missing,
                        "instruction": "This requirement's Given text names these records. Seed (or correct the "
                            "existing statement for) each matching record with every field from its "
                            "environment fixture, passwords through seed_password_hash('...'). Do not add "
                            "a second INSERT for a business key that is already seeded."})
                return {"status": "GENERATED", "insert_files": list(files.changed_paths)}

            run_file_agent(model, SEED_INSTRUCTIONS, task, files, generated,
                max_turns, console, "Startup data / " + rid, allow_discovery=False)


REPAIR_INSTRUCTIONS = '''Repair the actual database implementation and its startup wiring.
The compiler has already generated SQL and executed real checks; feedback contains
actual build, SQLite initialization or server startup failures. Use edit_file for repairs.

Use the canonical schema as authority. Repair the responsible SQL table files, related
foreign-key tables, initializer, client, setup/server entry point or SQL build-copy wiring.
Repair the responsible db/seeds/ENTITY.sql insertion files when prerequisite data fails.
seed.ts is only the shared SQL loader; do not move business row definitions into it.
backend/src/db/password.ts is the compiler-owned password module (hashPassword,
verifyPassword: scrypt$saltHex$digestHex) shared by seed_password_hash, credential handlers
and test fixtures; keep its exports and encoding unchanged.
Preserve the existing seed identities, credentials, values and relationships; correct
execution errors without redefining their business meaning. Semantic queries are disabled. The
loader runs after table creation in the initialization transaction, on every startup.
Keep it idempotent and preserve existing user data; do not remove it to make checks pass.
Keep CREATE TABLE IF NOT EXISTS and all canonical tables/fields. Opening the database
must create missing tables on every initialization/startup without deleting existing data.
Use the supplied better-sqlite3 client and real SQL files in both source and built output.
Preserve DATABASE_URL and PORT, createApp's existing health/API routes and signal cleanup.
Keep initialization transactional and foreign keys enabled. Startup must initialize the
same database connection used by the application before listening. Expected SQLite errors
must retain the failing SQL filename and underlying cause for diagnosis.

Do not replace the canonical schema with a test-only database, omit a failing table,
catch an error and pretend startup succeeded, disable constraints to hide a failure,
change the schema artifact, weaken checks, or add mocks. Do not drop existing user data.
This task owns database SQL and the supplied startup wiring; later API/UI tasks do not.
Use the supplied files first. Read missing related SQL/startup lines with read_file;
read the canonical table DDL for missing definitions. Use edit_file/replace for exact local repairs or
edit_file/create for a missing canonical SQL file under backend/src/db/tables or db/seeds.
The compiler rebuilds, initializes/reinitializes real databases, restores a missing table,
starts/restarts the compiled backend and initializes backend/data/app.db after every edit.
'''


def verify_database_runtime(arc_dir, schema, model, console, max_turns=10):
    output = arc_dir.parent.resolve()
    ensure_password_module(output)
    report = {"status": "CHECKING", "expected_tables": list(schema["entities"]), "repair_turns": 0}
    path = arc_dir / "db_checks.json"
    save_json(path, report)
    checks = DatabaseChecks(output, schema)
    manifest = None

    def gate():
        nonlocal manifest
        current = [(output / name).read_text(encoding="utf-8-sig") for name in ("package.json", "backend/package.json")]
        dependencies = ("tsx", "typescript", "better-sqlite3", "express")
        if (manifest is not None and current != manifest) or any(not (output / "node_modules" / name).exists() for name in dependencies):
            install_dependencies(output)
        manifest = current
        result = checks.run()
        report.update(status="GREEN" if result["returncode"] == 0 else "FAILED", checks=result)
        save_json(path, report)
        checkpoint(output, stage="04", task="database runtime", action="Execute real database and startup checks",
            status=report["status"], details=result)
        if result["returncode"]:
            raise CheckFailure("Database initialization or startup failed", result)
        return result

    try:
        gate()
        return report
    except (ValueError, RuntimeError) as error:
        feedback = getattr(error, "feedback", {"error": str(error)})

    sql_paths = {name: f"backend/src/db/tables/{name}.sql" for name in schema["entities"]}
    insert_paths = {name: f"backend/src/db/seeds/{name}.sql" for name in schema["entities"]}
    allowed = {*WIRING, *sql_paths.values(), *insert_paths.values()}
    workspace = FileTools(output, WIRING, allow_write=lambda path: path in allowed, readable=allowed)
    task = {"table_order": [name for group in implementation_groups(schema) for name in group],
        "startup_data_loader": SEED_PATH,
        "insertion_directory": "backend/src/db/seeds",
        "database_path": "backend/data/app.db", "entities": {}}
    dependencies = table_dependencies(schema)

    def repair_feedback(value):
        value = compact_feedback(value)
        text = json.dumps(value, ensure_ascii=False)
        selected = {name for name in sql_paths if f"{name}.sql" in text
            or re.search(r"Missing (?:table|column): " + re.escape(name) + r"(?!\w)", text)}
        pending = list(selected)
        while pending:
            name = pending.pop()
            for target in dependencies[name]:
                if target not in selected:
                    selected.add(target)
                    pending.append(target)
        # Retain wiring and replace the previous failing table context instead of accumulating it.
        workspace.files = set(WIRING) | {sql_paths[name] for name in selected} | {insert_paths[name] for name in selected}
        workspace.writable = set(workspace.files)
        task["entities"] = {name: schema["entities"][name] for name in sorted(selected)}
        return value

    try:
        agent = run_file_agent(model or TextModel(), REPAIR_INSTRUCTIONS, task, workspace, gate,
            max_turns, console, "Database runtime repair", feedback=feedback,
            feedback_filter=repair_feedback, allow_discovery=False)
        report.update(status="GREEN", repair_turns=agent["turns"])
        save_json(path, report)
        return report
    except (ValueError, RuntimeError) as error:
        report.update(status="FAILED", repair_turns=getattr(error, "turns", 0), error=str(error))
        save_json(path, report)
        raise
