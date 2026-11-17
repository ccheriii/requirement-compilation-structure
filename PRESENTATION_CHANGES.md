# Project Changes and Improvements

## Comparison basis

This summary compares the repository's initial Git commit (`init structure`) with
the current `dev` working tree. The latest schema-consistency changes are still
uncommitted. The saved local test reports are evidence about a local run, not an
ARC-Bench score or a controlled before-and-after comparison.

## Short presentation summary

The project started as a set of separate scripts that transformed requirements
into a web application. It has evolved into an integrated, 11-stage compiler
pipeline with structured intermediate artifacts, validation, generated tests,
and repair loops. Recent work targets recurring cross-stage mismatches: required
database fields omitted by creation flows, guessed field names in test fixtures,
and mismatched API/test expectations.

## What changed from the initial version

| Area | Initial version | Current direction and improvement |
|---|---|---|
| Execution | Separate numbered scripts and standalone checks. | A single `main.py` / `Compiler` pipeline runs all 11 stages or one selected stage. It records stage results and continues after a stage failure so later passes can still provide evidence. |
| Intermediate artifacts | Stage outputs were less centralized. | Requirements, schema, obligations, API contracts, UI designs, test results, and compilation status are stored under the generated output's `.arc/` directory. This makes failures easier to trace to a stage. |
| Requirement-to-code flow | The project already had the core staged idea. | The pipeline makes the sequence explicit: requirements → schema → read/write obligations → database → APIs → backend tests and implementation → UI → browser interactions and tests. |
| Validation | Basic implementation and test support existed. | The current workflow includes schema/obligation checks, database checks, API test-driven implementation, UI checks, frontend E2E tests, and independent acceptance tests. |
| Agent reliability | Simpler file-generation and model interaction flow. | Shared agent tooling adds controlled file reads/edits, contextual retrieval, feedback and repair loops, and Git checkpoints to make generation more traceable and recoverable. |
| Deployment and portability | The initial version included a generated React/Express/SQLite template. | Later changes improve the command-line workflow, environment setup, platform support, and generated application deployment. |
| Cross-stage schema consistency | A field added by a later requirement could be absent from earlier associations for the same entity. | The current uncommitted change backfills newly required non-null fields into earlier requirement associations that already use the entity. Prompts also emphasize required writes, canonical names, and schema-valid fixtures. |

## Recent failure patterns addressed

The local repair history contains examples of:

- A required `Session.created_at` field missing from generated session fixtures.
- Guessed consent-field names such as `agreement_accepted` instead of the
  canonical `agreement_consent`.
- Tests waiting for `/api/session` when the declared endpoint was `/api/me`.
- Test fixtures attempting to insert records already created by database seeds.
- Registration form keys not matching the backend API contract.

The latest edits aim to prevent or reduce recurrence by propagating required
schema fields and making the generation instructions refer to actual schema,
DDL, and API contracts. These are targeted improvements; they do not prove that
all such errors are eliminated.

## Local results available to present

The saved `outputs/demo/.arc/` reports show a local run with:

- Compilation status: **GREEN** across the 11 pipeline stages.
- Backend API tests: **20/20 passing** in the final backend test report.
- Frontend report: **8 functional E2E tests and 7 UI tests**, with no final failures.
- Independent acceptance: **12/12 passing** in the final recorded results.
- During acceptance, earlier attempts had failures that were repaired; for
  example, the first REQ-1 attempt had 3 failing tests and REQ-4 had 2. This
  shows both the value of the repair loop and the fact that initial generations
  still need correction.

These are local results from saved artifacts. They are not a controlled
before-and-after measurement, and they are not the official ARC-Bench score.
Do not claim a measured improvement in ARC-Bench pass rate or cost without
comparing equivalent submissions and recording the platform results.

## Recent before-and-after comparison

An earlier saved run reported `COMPLETED_WITH_FAILURES` in backend stage 06.
The failing homepage-session case expected HTTP 500 when a database lookup
failed, but the generated handler returned HTTP 200 with a signed-out response.
The handler was updated to distinguish an actual lookup error (500) from a
missing or invalid session (200 signed out). The targeted session API tests
then passed **5/5**.

The compiler prompts were also changed to reduce under-testing in future
compilations:

| Area | Before | After |
|---|---|---|
| Backend test generation | `max_tests` and “critical rejection” wording could steer generation toward a small set of happy-path and rejection cases. | `max_tests` is a suite-size target, not a hard cap. Prompts ask for each distinct requirement/API contract branch, required state invariants, and explicitly defined input classes, without a Cartesian product of fields. |
| Frontend E2E generation | Guidance favored one successful workflow and at most one additional critical case. | Guidance asks for each distinct specified user-visible outcome, including applicable rejection, identity, and persistence behavior; it says not to omit a required scenario to meet a test-count target. |
| Backend implementation | Error paths could be converted into success-shaped responses. | Instructions explicitly require declared error status/body/headers and prohibit silently treating database or lookup failures as success. |

These are prompt-level guardrails, not a guarantee against every possible
external test. They improve the instructions for later compilations; they did
not cause or alter the already-generated app's test results. The generated
app's handler fix and the prompt changes are separate changes.

## Complete current change inventory

This section records the complete relevant change set visible in the worktree
when this summary was updated. Several schema/fixture changes were already
uncommitted in the worktree before the later prompt-coverage edits in this
conversation; they are included here for completeness and should not be
attributed to those later edits.

### Schema evolution and cross-stage consistency

- `infrastructure/stages/database/schema.py` adds
  `backfill_required_field_associations`. When a field is newly introduced or
  becomes non-nullable, it adds that required field to earlier requirement
  associations that already use the same entity. It does not alter unrelated
  entity associations or the original input schema.
- `infrastructure/pipeline/db_schema.py` invokes the backfill after applying
  each schema patch. Its instructions now caution against inventing timestamps
  and require all creation flows for a shared entity to account for newly
  required fields.
- `infrastructure/pipeline/db_obligations.py` tells obligation inference to
  include every required non-null field for record creation, including
  generated timestamps and foreign keys, and to apply that to each creation
  operation for a shared entity.
- `infrastructure/pipeline/backend_apis.py` applies the same rule to API write
  sets and postconditions, while requiring canonical field names.
- `tests/test_schema_evolution.py` adds coverage for backfilling required
  fields into earlier requirements and updates tests to import the actual
  pipeline modules rather than numeric stage-script module names.

### Database fixture and acceptance-test reliability

- `infrastructure/stages/database/implementation.py` now instructs seed
  generation to use only columns in the current DDL and to populate every
  `NOT NULL` column without a SQL default, using valid deterministic values.
- `infrastructure/stages/frontend/triggers/acceptance.py` now requires
  independent acceptance tests to inspect the supplied schema and actual DDL,
  use exact column names, and provide required values such as
  `Session.created_at` in fixture inserts.
- `infrastructure/pipeline/backend_implementation.py` includes instructions
  for selecting the exact existing test path, using typed database helpers,
  avoiding duplicate seeded fixtures, and exercising required API branches.
  Its latest additions make `max_tests` a size target and require coverage of
  distinct contract branches and specified invariants.

### API error handling and browser test coverage

- The generated app's
  `outputs/demo/backend/src/api/homepage-session.ts` and
  `outputs/demo/backend/src/api/get-home-session.ts` were changed so an
  unexpected database lookup error returns 500 and a signed-out body, while a
  missing or invalid cookie remains a normal 200 signed-out response. These
  files are under the ignored `outputs/` directory, so this generated-app fix
  does not appear in the repository's tracked Git diff.
- `infrastructure/pipeline/backend_implementation.py` now tells the
  implementation agent to honor API-declared error status/body/headers and not
  turn database errors into success-shaped responses. Backend test-generation
  guidance now treats `max_tests` as a target rather than a hard ceiling.
- `infrastructure/stages/frontend/triggers/implementation.py` now asks E2E
  test generation to cover each distinct specified user-visible outcome,
  including relevant rejection, identity, and persistence cases, without
  forcing an exhaustive Cartesian product.
- `tests/test_prompt_coverage.py` adds two checks to ensure these backend and
  frontend prompt requirements remain present.

### Workspace and attribution notes

- The repository status also contains untracked `.DS_Store` files and
  `yx_agent1.zip`. They are workspace files, not compiler or test changes, and
  are not included in the functional change inventory above.
- The generated application and `.arc` reports are local output artifacts;
  the reports provide evidence for the run metrics and test counts above but
  are not tracked source changes.
- The prompt-only edits affect future compilations. The latest local GREEN
  run is evidence of this output's tests, not evidence that the later prompt
  wording caused those results or that all external tests will pass.

The latest saved run in `outputs/demo/.arc/compilation.json` is **GREEN** for
all 11 stages. Its final reports record:

| Final suite | Cases passed |
|---|---:|
| Backend API | 20/20 |
| UI | 7/7 |
| Functional E2E | 8/8 |
| Independent requirement acceptance | 12/12 |
| **Distinct cases in these final reports** | **47/47** |

The 47 count adds the distinct cases represented in the four final reports;
it does not count the same suites' repeated regression executions during the
pipeline.

### Latest run resources

The latest `outputs/demo/compiler.log` records about **18 minutes 12 seconds**
of summed stage elapsed time and **424,227 reported tokens** across 47 model
calls:

- Input: **321,740 tokens**, including **160,128 cached input tokens**.
- Output: **102,487 tokens**.
- Summed model round-trip latency: about **16 minutes 21 seconds**. This is
   included in stage elapsed time, not additional wall-clock time.

| Stage | Elapsed | Reported tokens |
|---|---:|---:|
| 00 Initialize project | 0.2 s | 0 |
| 01 Read requirements and analyze images | 1m 52s | 9,280 |
| 02 Design database schema | 27s | 13,087 |
| 03 Infer database obligations | 1m 09s | 18,111 |
| 04 Implement database | 40s | 31,713 |
| 05 Design backend APIs | 2m 20s | 40,435 |
| 06 Implement backend and test | 5m 22s | 224,980 |
| 07 Design UI and frontend data | 2m 53s | 45,825 |
| 08 Implement UI and test | 15s | 0 |
| 09 Design frontend triggers | 2m 12s | 40,796 |
| 10 Implement triggers and E2E test | 1m 04s | 0 |
| **Total** | **18m 12s** | **424,227** |

Reported tokens are provider-reported usage, not a measure of output quality.
Stages with zero model calls still take time for compilation, browser tests,
builds, and other local work.

## Suggested 6-slide, 6–10 minute presentation

1. **Goal and challenge** — Turn structured requirements and reference assets
   into a working full-stack app; explain why a single LLM code-generation step
   is difficult to validate.
2. **Initial version** — Separate scripts, existing React/Express/SQLite
   template, and the original staged-generation concept.
3. **What evolved** — Integrated 11-stage runner, `.arc` artifacts, targeted
   stage runs, explicit reports, checkpoints, and recovery.
4. **Reliability improvements** — Schema/obligation/API consistency, clearer
   branch-coverage prompts, and database, backend, UI, E2E, and independent
   acceptance checks.
5. **Evidence and remaining limitations** — Present the local 47/47 final
   cases, 18m 12s run, and token totals; explain that several first attempts
   needed repair and distinguish local results from ARC-Bench results.
6. **Next steps** — Run a fresh compilation from a clean output directory,
   submit the same smoke task before/after changes, classify platform failures,
   then compare pass rate, runtime, and LLM cost.

## Suggested closing statement

“We improved observability and recovery, fixed a concrete error-path mismatch,
and strengthened prompts so required scenario branches are less likely to be
omitted. The latest local run passed all 47 cases represented in its final
reports in about 18 minutes, using about 424 thousand reported tokens. This is
not a guarantee that every generated app will pass, nor a controlled
before-and-after or ARC-Bench comparison; that still requires equivalent
benchmark runs.”
