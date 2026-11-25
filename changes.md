# Changes: speed and token optimizations

These changes make the compiler faster and reduce the tokens it uses. Projected savings come from the compiler log of the last full run (`outputs/main-optimization-full/compiler.log`). A full run with all changes has since been measured; see **Results: what changed and improvement** below.

## Baseline: where time and tokens went

| Metric | Value |
| --- | --- |
| Wall time | ~59 min (3,517 s) |
| Model latency | ~47.5 min (2,850 s) |
| Model calls | 170 |
| Input tokens | 2.47M (1.79M cached) |
| Output tokens | 316k |

Findings:

- **Stage 10 dominates.** It used 1,922 s of wall time and 1.75M input tokens (71% of the total).
- **Read-only calls are expensive.** 78 calls only read files. Each one resent the full 10–48k-token context. Together they used 1.19M input tokens (48%) and 936 s of latency (33%).
- **Tool-usage mistakes consumed repair rounds.** The REQ-2 independent acceptance repair used all 10 rounds (~45k tokens each) and still failed. Most rounds failed on tool-usage mistakes, not test failures:
  - "File is not writable" with no list of writable files
  - a misused `context_request kind=file`
  - repeated identical reads
- **Failure evidence was sent two or three times per round.** `TriggerCheckFailure` built its error string from `str(CheckFailure)`. That string already contained the whole report, including a JSON copy of up to 24,000 characters, with ANSI colour codes.
- **`npm install` blocked stage 04 for 4 minutes.**
- **The 3 reference images were analysed one after another,** at ~36 s each.

## Changes

### 1. Failure feedback is sent once, without ANSI codes

- `infrastructure/shared/agent/feedback.py`
  - `CheckFailure` now keeps a short `headline`.
  - New `failure_message(error)` returns the headline, or the error text capped at 2,000 characters, with ANSI codes stripped.
  - New `strip_ansi` and `clean_feedback` remove ANSI codes; `compact_feedback` also strips them.
- `infrastructure/stages/frontend/triggers/acceptance.py` and `implementation.py`: `TriggerCheckFailure` now uses `failure_message(error)`. The full report stays attached as structured `checks`.
- `infrastructure/shared/agent/loop.py`: every feedback item sent to the model goes through `clean_feedback`.

### 2. Tool errors tell the model what to do next

- `infrastructure/shared/agent/tools/files.py`
  - `WRITE_NOT_ALLOWED` now includes `writable_paths`, plus a note when other paths may be writable by pattern.
  - `FILE_NOT_FOUND` on `read_file` now includes `similar_existing_paths`: files with the same name and close matches under `frontend/` and `backend/` sources and tests. Suggestions respect the task's readable scope.
- `infrastructure/shared/agent/loop.py`: a `context_request` with `kind=file` and `ids` was rejected before, costing a full round. The agent now serves it as `read_file` calls. `read_file` still enforces the task's readable scope.

### 3. Fewer read-only calls

- `infrastructure/shared/agent/loop.py` adds a read-ahead memory.
  - It records the files that tasks of the same kind had to read. "Same kind" means the same instructions in the same output project.
  - The next task of that kind receives those files upfront in `read_files`.
  - Limits: 6 files, 16k characters per file, 40k characters in total. Files already in `input.files` are skipped.
  - Set `ARC_READ_AHEAD=0` to turn it off.
- `infrastructure/shared/agent/tools/protocol.py`: a new **BATCH READS** rule tells the model to request every file it needs in one `tool_calls` list.

### 4. `npm install` runs in the background

- `infrastructure/shared/project/dependencies.py`
  - New `prefetch_dependencies(output_dir)` starts `npm install` in a background thread and captures its output.
  - `install_dependencies()` waits for that install first. If it succeeded and no `package.json` changed since it started, nothing more runs. If it failed, timed out or a `package.json` changed, a normal install runs.
  - New `cancel_prefetches()` stops installs that are still running.
- `infrastructure/compiler.py`
  - In full-pipeline runs, the install starts right after a GREEN stage 00, so it overlaps with stages 01–03.
  - Unfinished installs are always cancelled on exit.
  - Single `--stage` runs are unchanged.

### 5. Reference images are analysed in parallel

- `infrastructure/pipeline/requirements.py`
  - Images are analysed concurrently in a `ThreadPoolExecutor`. Each worker runs in `copy_context()`, so model logging still works.
  - Saving, checkpoints and console output stay sequential and in input order.
  - `VISUAL_CONCURRENCY` sets how many images run at once (default 4).
- `infrastructure/shared/project/git.py`: `checkpoint()` is now serialized with a lock, so concurrent work cannot collide on `.git/index.lock`.

### 6. Requests are smaller and cache better

- `infrastructure/shared/agent/loop.py` orders request keys from most to least stable: `tools`, `context_kinds`, `task`, `files`, evidence, then `feedback` and `context_result`. Provider prefix caching then covers more of each request.
- `infrastructure/shared/models/text.py`: request payloads are serialized as compact JSON, with no extra spaces.

### 7. Generated tests no longer assert an absolute starting state

The REQ-2 failure came from its acceptance test asserting that the database started with zero sessions. REQ-1's startup data legitimately adds a session, so no implementation could satisfy that test without breaking REQ-1.

- `infrastructure/shared/agent/tools/protocol.py` adds `TEST_DATA_RULES`. Tests must not assert:
  - empty tables
  - exact total row counts
  - that only certain records exist in the starting state

  Instead, they snapshot the relevant records before the action and assert the change afterwards.
- The rule is appended to all three prompts that generate tests:
  - backend HTTP tests (`backend_implementation.py`)
  - end-to-end smoke tests (`triggers/implementation.py`)
  - independent acceptance tests (`triggers/acceptance.py`)
- The rule names no domain concepts, so it applies to any set of requirements.

### 8. A repair that blames a read-only test is escalated

- `infrastructure/shared/agent/loop.py`
  - `run_file_agent` accepts an optional `stop_when(diagnoses)` policy. When the policy returns a reason, the loop raises `AgentEscalated`, a subclass of `AgentExhausted`, so existing failure handling still applies.
  - The reusable `repeated_test_blame()` policy triggers once the agent has diagnosed the test itself as wrong (`test_requirement_mismatch` or `test_overconstraint`) in 2 rounds.
- `infrastructure/shared/agent/recovery.py` now records `error_type` and the agent's `diagnoses` on a failed task.
- In `infrastructure/stages/frontend/triggers/acceptance.py`, when the repair is escalated:
  1. The repair stops early, and the application files go back to their state before the repair.
  2. An escalated test review runs. It can edit the test, but it must check each claim from the repair agent against the original requirement and keep every required business outcome. If the claims are wrong, it leaves the test unchanged.
  3. The acceptance test runs again. If it passes, the requirement is GREEN without counting any smoke suite as verified. Otherwise one final repair runs without early stopping.

### 9. Independent stages run at the same time

- `infrastructure/compiler.py` declares `OVERLAPPING_STAGES`:
  - stage 07 (UI design) runs alongside stage 06 (backend implementation)
  - stage 09 (trigger design) runs alongside stage 08 (UI implementation)
- These pairs are independent:
  - Stage 07 reads only `requirements.json`, `db_schema.json`, `backend_apis.json` and `visual_analysis.json`, and writes only `ui_design.json`.
  - Stage 09 additionally reads `ui_design.json` and `db_obligations.json`, and writes only `frontend_triggers.json`.
  - Neither reads code or reports produced by the stage it overlaps with.
- The background stage runs on a daemon thread with a copy of the logging context. The main loop waits for it before the background stage's slot in the pipeline. Stage results are recorded under a lock and kept in pipeline order in `compilation.json`.
- Overlap is used only in full-pipeline runs without `--pause`. Set `ARC_OVERLAP_STAGES=0` to turn it off.
- `infrastructure/utils/presentation/logging.py`: `console_log` used to swap the process-wide `sys.stdout` and `sys.stderr` for each stage. Two concurrent stages would have duplicated lines, and one could have written to the other's closed log. A `RoutingStream` is now installed once and sends each write to the current stage's log through a ContextVar. Threads started without a context fall back to the most recently opened stage. All stages share one output lock.

### 10. Stage 10 acceptance suites are generated concurrently

- In `infrastructure/stages/frontend/triggers/acceptance.py`, per-requirement setup is now a `prepare(rid)` helper. All missing acceptance suites are generated concurrently first. Each suite:
  - is written from the original requirements only
  - edits only its own file
  - is typechecked with its own temporary config, plus `--noEmit`
  - is not recorded in the shared implementation-path index
- Running, review and repair stay sequential, because they share the application and its database.
- A generation failure is re-raised at the same point in the loop as before, so reporting is unchanged.
- Turned off under `--pause`. Set `ARC_ACCEPTANCE_CONCURRENCY=1` for sequential generation.

### 11. Machine problems stop the pipeline instead of going to the model

- `infrastructure/shared/project/dependencies.py` adds `EnvironmentFailure`. A failed or timed-out `npm install` raises it, with a hint naming the likely cause:
  - npm cache files you don't own (with the `chown` fix)
  - other permission errors
  - network problems
  - a full disk
- `EnvironmentFailure` is never sent to a model as repair feedback:
  - the agent loop re-raises it
  - the database runtime check re-raises it
  - `TaskFailures` re-raises it
  - `Compiler._run` records the stage as FAILED, shows the hint and stops the pipeline, because later stages would hit the same problem
- Before, a broken npm cache made stage 04 spend up to 10 model rounds "repairing" code that wasn't broken.
- `READ_NOT_ALLOWED` errors now list `readable_paths`, so the model doesn't guess blindly.

### Configuration

`.env.example` documents `VISUAL_CONCURRENCY=4`, `ARC_READ_AHEAD=1`, `ARC_OVERLAP_STAGES=1`, `ARC_ACCEPTANCE_CONCURRENCY=4`, `ARC_E2E_TEST_CONCURRENCY=4`, `ARC_DESIGN_CONCURRENCY=4` and `ARC_BACKEND_TEST_CONCURRENCY=4`.

## Tests

- New `tests/test_optimizations.py` (17 tests, all passing) covers:
  - feedback headlines and ANSI stripping
  - the writable-path list and path suggestions
  - serving a `context_request` file read as `read_file`
  - read-ahead, including turning it off, and the request key order
  - falling back to a normal install after a failed or outdated background install
  - parallel, in-order image analysis
  - early escalation after repeated test blame, and no escalation after a single one
  - the test-data rule being present in all three test-generation prompts
  - stage overlap: timing, report order, artifacts, and the off switch
  - concurrent log sessions writing only to their own logs
- The existing suite has the same results as before these changes: 75 of 153 tests fail, all of them already failing beforehand. The causes are stale imports of removed modules, such as `06_design_backend_apis`.

Run the tests with:

```bash
uv run python -m unittest discover -s tests -t tests
```

## Projected impact

| Change | Projected saving |
| --- | --- |
| Background `npm install` | ~3–4 min |
| Parallel image analysis | ~70 s with 3 images |
| Read-ahead and batched reads | Part of the 936 s and 1.19M tokens spent on reads |
| Single, clean failure feedback | Several thousand tokens per repair round |
| Clearer tool errors | Fewer repair rounds lost to tool-usage mistakes, as in REQ-2 |
| Overlapping stages 06/07 and 08/09 | ~4–5 min (07 and 09 took 145 s and 136 s) |
| Concurrent acceptance-suite generation | ~3 min |
| Test-data rule and escalation | Stops repairs that cannot succeed (REQ-2 used 10 rounds × ~45k tokens) and gives them a path to GREEN |

Overall estimate before the measured run: ~59 → ~35–40 min, with 25–35% fewer input tokens.

## Results: what changed and improvement

These figures compare the full run with all changes (`outputs/optimized-run`) against the baseline run (`outputs/main-optimization-full`). Both runs used the same `input/` and the same model, `deepseek-flash`.

### What changed

| | Before (baseline run) | After (optimized run) | Improvement |
| --- | --- | --- | --- |
| Overall result | COMPLETED_WITH_FAILURES (REQ-2 failed) | **GREEN**: all 11 stages and all 4 requirements | REQ-2 fixed |
| Model calls | 170 | **95** | −44% |
| Input tokens | 2.47M | **1.06M** | −57% |
| Output tokens | 316k | **216k** | −32% |
| Reasoning tokens | 241k | **130k** | −46% |
| Read-only calls | 78 | **23** | −71% |
| Rejected edit rounds | 33 | **18** | −45% |
| Acceptance tests needing review or repair | 3 needed test review; REQ-2's repair used 10 rounds and failed | **none**: every suite passed first time | — |
| Wall time | 59 min | 75 min | slower, see below |

### Why wall time went up

The model provider was much slower during the optimized run: about **33 output tokens/s**, against **111 tokens/s** in the baseline run. The design stages show this clearly, because their code is essentially unchanged:
- Stage 03 made the same 4 calls but took **486 s instead of 50 s**.
- Stage 05 made the same 4 calls but took **332 s instead of 126 s**.

At the baseline's provider speed, the optimized run's 216k output tokens would take roughly **30–35 minutes**. That is an estimate. A timing comparison only means something when both runs see similar provider speed.

### What caused each improvement

- **REQ-2 now passes, with no review or repair needed:** the test-data rule (change 7). Generated acceptance tests no longer assert an absolute starting state, such as zero sessions, which could never pass.
- **71% fewer read-only calls:** read-ahead memory and the batched-read rule (change 3). Later tasks received the files earlier tasks had to request. The log shows "Read-ahead" lines.
- **45% fewer rejected rounds:** actionable tool errors (change 2). For example, a missing-file read now suggests the right path, and the model recovered in the next round.
- **57% fewer input tokens:** mostly the factors above (fewer rounds, each resending the full context), plus failure feedback sent once without ANSI codes (change 1).
- **Speed changes that helped but are hidden by the provider slowdown:**
  - stages ran at the same time (06/07, 08/09)
  - images were analysed in parallel (stage 01 took 0.5 min)
  - acceptance suites were generated in parallel
- **The environment check proved its value:** during the first optimized attempt, a root-owned `~/.npm` cache broke `npm install`. That led to change 11, which stops the pipeline with a fix instead of making the model "repair" working code.

### Remaining inefficiency

Some rejected rounds still come from the model trying to replace a whole existing file, which the rules forbid. In stage 08 one such round cost ~61 s and 7.6k output tokens before it was rejected.

## Possible next steps

- Try `MODEL_REASONING=none` in `.env`. 76% of output tokens were reasoning (241k of 316k), so this could save 10+ minutes, but quality may drop.
- Stop a repair loop early when the same check failure repeats.
- Reduce whole-file replacement attempts, for example by catching them before the model writes the full file, or by making the rule more prominent in the prompt.
- Re-run when the provider is at normal speed to get a fair wall-time comparison.
- Run on a second, different requirements file to confirm the changes work for other kinds of webpages.

## Second attempt

Changes made after the measured optimized run, to speed up stage 10 further. The optimized run's results above do not include them; the next full run will measure them.

### 12. Stage 10 smoke tests are written in parallel up front

In the optimized run, stage 10 took 21 min. About 17 min of that was the sequential per-requirement loop (write a smoke test → run it → implement). Writing the tests alone used 435 s of model time, one requirement after another.

- `infrastructure/stages/frontend/triggers/implementation.py`
  - After the baseline check, every requirement's functional smoke suite is written concurrently.
  - The suites are written from the requirement, UI design and API contracts, not from earlier requirements' implementations.
  - The parallel gate only typechecks, so browser tests never run at the same time.
  - The sequential loop already reuses an existing suite: it runs it and implements against it. If the suite no longer compiles, the loop regenerates it with the error as feedback.
  - A failed parallel write is deferred to the sequential loop, which writes the suite as before.
  - Per-requirement setup moved into `requirement_scenario()` and `test_setup()`, shared by both paths.
  - Each worker gets its own `SourceFiles`, because feedback filtering changes its state.
- `infrastructure/shared/project/entrypoints.py`: `remember_path()` is locked, because concurrent agents update `.arc/implementation_paths.json`.
- Turned off under `--pause`. Set `ARC_E2E_TEST_CONCURRENCY=1` for sequential writing.
- Expected saving: most of the 435 s of sequential test writing. That is about 4–5 min at the optimized run's provider speed, or ~2 min at normal speed.

### 13. Stage 03 infers all requirements at once

- In `infrastructure/pipeline/db_obligations.py`, each requirement's obligation inference uses only that requirement, its steps and the finished schema, never another requirement's result. All model calls now run concurrently.
- Results are still published one at a time in requirement order, with the same failure tracking.
- In the optimized run, stage 03 used 486 s of model time across 4 sequential calls. In parallel, it takes about as long as the slowest single call.
- Turned off under `--pause`. Set `ARC_DESIGN_CONCURRENCY=1` for sequential inference.
- **Verified** on the optimized run's real artifacts, with a 0.5 s fake model: 4 requirements took 0.51 s instead of 2.0 s, and the output was identical to the real run's `db_obligations.json`.

### 14. Stage 06 API contract tests are written in parallel up front

- In `infrastructure/pipeline/backend_implementation.py`, all missing API contract suites are written concurrently before the TDD loop.
  - Each suite is written from its API contract and owning requirement only.
  - The parallel gate only typechecks.
  - Each worker gets its own `SourceFiles`.
- The TDD loop then reuses each suite. It runs its real gate in order (still confirming RED before implementation) instead of asking the model again. The model is called only if a suite no longer compiles.
- A failed parallel write is deferred to the loop, which writes the suite as before.
- In the optimized run, test writing was 352 s of model time across 9 sequential calls.
- Turned off under `--pause`. Set `ARC_BACKEND_TEST_CONCURRENCY=1` for sequential writing.

### Verification of 12 and 14 on real infrastructure

Both were run end to end on APFS clones of the optimized run's output. The generated test files were deleted, and a fake model recreated them from the originals. The typecheck, test runner, server, browser and git checkpoints were all real.

- **Stage 06:**
  - all 4 contract suites were written concurrently (peak of 4 model calls)
  - the loop reused each suite, so there were exactly 4 test calls and 4 implementation calls
  - 17 of 17 HTTP tests passed
  - all 4 index entries were present
- **Stage 10:**
  - 4 smoke suites and 4 acceptance suites were each written concurrently
  - the stage was GREEN (8 end-to-end and 9 UI tests)
  - independent acceptance was GREEN

### What cannot be parallelised, and why

- **Stages 02, 05, 07 and 09:** each requirement extends the design the previous one produced. Running them concurrently would create conflicting entities, APIs, pages or triggers. Stages 07 and 09 already overlap with 06 and 08.
- **Stage 08:** every page edits the shared `App.tsx`.
- **Implementation loops in stages 06 and 10:** every step changes the same app and database.
- **Git checkpoints:** measured at 91 ms each, about 17 s across a run's 184 checkpoints. Not worth changing.

The remaining time is mostly model output speed. The levers left are `MODEL_REASONING=none` or a faster model, and both trade off quality.

### Second attempt: how to verify

In the next full run, look for "Writing N API contract suite(s)…" in stage 06 and "Writing N functional smoke suite(s)…" in stage 10. Stage 03 should finish in about the time of its slowest single call. Compare stage 10's wall time with the optimized run's 21 min, keeping in mind that provider speed affects the result.

### Second attempt: results

Measured on `outputs/optimized-run-3`, against the original run (`main-optimization-full`) and the first optimized run (`optimized-run`).

| | Original | 1st optimized | 2nd attempt |
| --- | --- | --- | --- |
| Result | COMPLETED_WITH_FAILURES | GREEN | **GREEN** |
| Independent acceptance | REQ-2 failed | all 4 pass | **all 4 pass** |
| Wall time | 58.7 min | 74.8 min | **64.3 min** |
| Model calls | 170 | 95 | 112 |
| Input tokens | 2.47M | 1.06M | 1.20M |
| Output tokens | 316k | 216k | **198k** |
| Reasoning tokens | 241k | 130k | 130k |
| Read-only calls | 78 | 23 | 41 |
| Rejected edit rounds | 33 | 18 | **16** |
| Model time (sum of all calls) | 47.5 min | 109.5 min | 98.8 min |
| Stalled calls | 0 | 28 | 38 |
| Model time lost to stalls | 0 | 60.2 min | 55.9 min |
| Healthy generation speed | 115 tok/s | 72 tok/s | 77 tok/s |

Stage wall times (min):

| Stage | Original | 1st optimized | 2nd attempt |
| --- | --- | --- | --- |
| 01 | 1.8 | 0.5 | 2.4 |
| 02 | 0.5 | 1.8 | 3.7 |
| 03 | 0.8 | 8.1 | **2.8** |
| 04 | 5.0 | 1.8 | 3.7 |
| 05 | 2.1 | 5.5 | 5.1 |
| 06 (07 runs alongside) | 6.5 | 15.9 | **13.1** |
| 07 | 2.4 | 9.1 | 5.0 |
| 08 (09 runs alongside) | 5.3 | 19.8 | **5.7** |
| 09 | 2.3 | 19.2 | 8.0 |
| 10 | 32.0 | 21.3 | 25.6 |

**What improved**
- **10.5 min faster than the first optimized run**, even with more stalls (38, against 28).
- **Overlap paid off:** 98.8 min of model time fit into 64.3 min of wall time, through parallel calls and overlapping stages.
- **Stage 03: 8.1 → 2.8 min** (change 13).
- **Stage 06: 15.9 → 13.1 min** (change 14).
- **Stages 08/09:** about 20 → 8 min, mostly because fewer requests stalled in those stages this time.
- **Output tokens and rejected rounds both fell,** and every requirement still passed independent acceptance.

**What got worse**
- **Stage 10: 21.3 → 25.6 min.** Writing the smoke tests in parallel did work (4 calls, 66 s). But read-only calls in stage 10 rose from 13 to 23, costing 1,282 s, 6 of them stalled.
- **Read-only calls overall: 23 → 41**, mostly in stages 06 and 10. Parallel test writers start together, so the read-ahead memory (which learns from finished tasks of the same type) had nothing to supply, and each writer fetched the same files itself. That is also why input tokens rose slightly. Change 17 addresses this.
- **Against the original run:** the provider was much worse during this run. Stalls cost ~56 min of model time (the original had none), and even healthy calls ran at 77 tok/s against 115. Change 15 targets the stalls.

## Third attempt

These changes came after the second attempt's run had started. That run uses the code as it was when it started, so it does not include them. The next full run will measure them.

### 15. Stalled model requests are detected and resent

**Why runs became slow.** The provider sometimes leaves a request queued before it generates anything. Measured on the real endpoint:
- **Healthy requests:** the first chunk arrives in 1.6–2.2 s, or ~3.5 s for an uncached 31k-token prompt.
- **Stalled requests:** nothing for 15–100+ s, not even response headers, then the whole answer in under a second.
- **No long silences mid-answer:** once an answer starts, it streams steadily.

In the optimized run, **28 stalled calls lost ~3,600 s of model time (55%)**. Run 1 had none. Stalls also happened in stages that make one call at a time, so concurrency does not cause them.

**What changed** in `infrastructure/utils/presentation/model_io.py`:
- Responses are streamed. A worker thread reads the stream while the caller enforces deadlines. The deadline also covers the wait for headers, through a client copy whose read timeout equals the deadline.
- No output within `ARC_STALL_FIRST_CHUNK_SECONDS` (default **10 s**) means the connection is shut down (`socket.shutdown`, which reliably wakes a blocked read) and the request is resent, up to `ARC_STALL_RETRIES` (default **5**) times. Silence of 60 s after an answer has started (`ARC_STALL_IDLE_SECONDS`) is also a stall.
- The final attempt has no deadline, so a slow but working provider still finishes.
- A healthy long generation keeps streaming and is never interrupted.
- SDK retries are off for attempts with a deadline. `_complete()` retries 5xx, 429 and connection errors itself, with backoff.
- Results are reassembled into the usual response (content, `finish_reason`, usage including reasoning tokens). Logs and audit records still contain the full output.
- Compatibility:
  - If a provider rejects streaming, plain requests are used.
  - If a provider ignores `stream: true` and returns JSON, that body is used as is (no second request) and streaming is turned off for that provider.
  - `ARC_STREAM=0` turns all of this off.

**Verified:**
- **Local SSE server, 5 tests:**
  - a stall before headers and a stall after headers were both resent
  - a slow streaming generation was not interrupted
  - the last attempt waited
  - the off switch works
- **Real endpoint, 8 calls:** mean **14.8 s** and worst **34.7 s** per call, against 64–102 s per stalled call before.
- **Existing test changes:**
  - `test_image_data_is_full_in_logs_and_absent_from_terminal` now sets aside `stream`/`stream_options` before comparing the logged wire body.
  - The already-failing `test_console_is_bounded_but_panels_and_model_io_are_complete` now errors earlier, because it supplies exactly three clock readings and streaming reads the clock more often.

### 16. Per-task reasoning effort

- `infrastructure/shared/models/text.py` adds task kinds: `design`, `implementation` and `tests`. Each text call uses `MODEL_REASONING_<KIND>` when set, otherwise `MODEL_REASONING`.
  - `propose_json` calls (stages 02, 03, 05, 07, 09) are `design`.
  - `run_file_agent` calls are `implementation` by default (implementation, repairs, test reviews).
  - The five test-writing call sites pass `reasoning="tests"`: stage 06 contract tests, stage 10 smoke tests and acceptance suites, parallel and sequential.
  - Image analysis already had `VISUAL_REASONING`.
- The kind is a ContextVar, so parallel workers keep it.
- Unknown values are rejected. With nothing set, behaviour is unchanged.
- **Enabled in `.env`:** `VISUAL_REASONING=none` and `MODEL_REASONING_TESTS=none`. Design, implementation and repairs keep `low`.
- In the optimized run, test writing and image analysis used ~39k reasoning tokens. Whether test quality holds without reasoning has to be confirmed by a full run. If acceptance or repair rounds increase, set them back to `low`.
- **Verified:**
  - Unit test: each kind picks up its own setting, falls back correctly and survives a thread pool.
  - Real endpoint: `none` sends `thinking: disabled` and returns 0 reasoning tokens with valid JSON; `low` returned 72 reasoning tokens for the same prompt.

### 17. Project-wide read-ahead memory

- **Problem:** in the second attempt, the same project files were fetched again and again by every kind of agent:
  - `backend/src/db/initialize.ts`, `db/seed.ts`, `db/seeds/*.sql`, `db/tables/*.sql`
  - `server.ts`, `app.ts`, `api/register.ts`

  The read-ahead memory was keyed by task type, so stage 10 test writers could not benefit from stage 06 having read exactly these files. Parallel writers of the same type start together, so they had no history either.
- **What changed** in `infrastructure/shared/agent/loop.py`:
  - Reads are recorded per distinct task (not per call), both per task type and project-wide.
  - Pre-supplied files come first from the same task type, then from any file at least 2 different tasks of any type needed.
  - Limits: 8 files (was 6), 16k characters per file, 40k characters in total.
  - `ARC_READ_AHEAD=0` still disables it.
- **Expected effect:** fewer read-only round trips in stage 10 and in later tasks of every stage. Each avoided round saves resending the full 10–48k-token context. Stage 06's first parallel writers still start without history, because no earlier stage's agents read those files.
- **Verified:** a unit test checks that one task's read is not enough, and that a file read by two different task types is supplied to a brand-new type.

### 18. Dependency-based stage scheduling and background acceptance writing

In the second attempt, the 06–09 block took 21 min and stage 10 took 25.6 min:
- 4.0 min writing smoke tests
- 11.6 min implementing requirements one at a time
- 5.7 min writing acceptance tests
- 4.3 min running acceptance and review

Much of that waiting was ordering, not necessity.

**Scheduling** (`infrastructure/compiler.py`): the fixed stage pairs are replaced by `STAGE_DEPENDENCIES`, and each stage starts as soon as the stages it actually needs have finished:

```
06 backend TDD  ─────────────────────────────────┐   (edits backend/ only)
07 UI design ─┬─ 08 UI implementation ───────────┼─ 10   (08 edits frontend/ only)
              └─ 09 trigger design ──────────────┘       (09 writes .arc only)
```

- **What each stage reads (checked per stage):**
  - 07 reads requirements, schema, `backend_apis` and visual analysis
  - 08 reads `ui_design`
  - 09 reads `ui_design` and `db_obligations`
  - none of them reads stage 06's code or reports
- **Before:** `max(06, 07) + max(08, 09)` = 13.1 + 8.0 = ~21 min (second-attempt times).
- **Now:** `max(06, 07 + max(08, 09))` = max(13.1, 5.0 + 8.0) = ~13 min.
- Stages without overlap, `--pause` runs and single `--stage` runs still go one at a time on the main thread. `ARC_OVERLAP_STAGES=0` turns overlap off.

**Making 06 and 08 safe to run together:**
- **Rollback limited to each layer** (`infrastructure/shared/agent/recovery.py`). `implementation_task` takes `roots`, `exclude` and `index_kinds`. A failed task restores only files under its roots and only its own entries in `.arc/implementation_paths.json`:
  - stage 06 uses backend roots, with `api`/`backend_test` index entries
  - stage 08 uses frontend roots, with `page` index entries
  - stage 10 excludes `frontend/tests/e2e/acceptance`
- **Stage 08's final build** now builds only the `@arc/frontend` workspace (`FrontendChecks.build_frontend`). Backend builds are verified in stages 06 and 10, so a half-finished backend edit from 06 can't fail it.
- **npm installs** (`infrastructure/shared/project/dependencies.py`) never run concurrently in one project. An install is skipped when the dependency declarations are unchanged; the fingerprint now covers dependency fields only, so a scripts change from stage 10 no longer forces a reinstall.
- **Stage 08's dependency preparation** (`prepare_frontend`, which is deterministic) now runs right after stage 00, before the background install. That install then already includes Tailwind and Playwright, which removes the separate ~1-minute install stage 08 used to do.

**Background acceptance writing** (`infrastructure/stages/frontend/triggers/acceptance.py`, `implementation.py`, `pipeline/frontend_implementation.py`):
- Acceptance writing moved into `AcceptanceSuites`, which can start early and collect results later.
- `run_trigger_tdd(on_ready=...)` starts it once the runtime, fixtures and parallel smoke tests exist. From then on it runs alongside the sequential implementation loop instead of after it.
- Writers are daemon threads, limited to `ARC_ACCEPTANCE_CONCURRENCY`. Running, review and repair stay sequential.

**Verified on real infrastructure** (APFS clones of `optimized-run-3`, fake model, real typecheck, test runners, servers, rollback and git):
- **Stages 06 and 08 through the real scheduler:** they overlapped for 21 s. Both were GREEN, stage 06 recreated all 4 contract suites, and all backend and page index entries were present.
- **Stage 10:** the 4 smoke suites started at 10.3 s and the 4 acceptance suites at 12.6 s, before the implementation loop's first requirement. Stage 10 and independent acceptance were both GREEN, and every file matched the original.
- **Unit tests:**
  - A failing frontend task rolls back its page and its own index entry, but keeps a concurrent backend edit and `backend_test` entry. With the old unscoped rollback, the backend edit was lost; this was checked.
  - A stage 10 rollback leaves acceptance suites alone.
- **Expected effect** (second-attempt times): about −8 min on the 06–09 block, and up to −5.7 min in stage 10, because acceptance writing is hidden under the implementation loop. That's about 13 min in total, on top of stall detection (15).

### Third attempt: how to verify

- **Stall detection:** "[MODEL STALLED]" panels show stalled requests being resent. Count them with `grep -c "MODEL STALLED" compiler.log`. Compare model time per call with the optimized run, where 28 calls lost ~3,600 s.
- **Project-wide read-ahead:** count read-only calls with `grep -c "| Read files (full content)" compiler.log`. The second attempt had 41.
- **Scheduling:** look for "Starting 08_implement_ui alongside: 06_implement_backend_tdd" and "Writing N independent acceptance suite(s) in the background". Compare the 06–09 block (21 min in the second attempt) and stage 10 (25.6 min).
- **Per-task reasoning:** compare the reasoning tokens used for test writing and image analysis with earlier runs. Also check that acceptance results and repair rounds did not get worse. If they did, set `MODEL_REASONING_TESTS=low` and `VISUAL_REASONING=low` in `.env`.

### Third attempt: results

Measured on `outputs/optimized-run-4` (changes 15–18), with test writing and image analysis set to `low` reasoning. The "main branch" column is `main-optimization-full`, which ran on the `main` branch (`1a625ad`), a separate line of work from the Nov 5 (12/12) version on `dev`. All optimized runs build on `dev`.

| | main branch | 1st optimized | 2nd attempt | **3rd attempt** |
| --- | --- | --- | --- | --- |
| Result | COMPLETED_WITH_FAILURES | GREEN | GREEN | **GREEN** |
| Independent acceptance | 3/4 | 4/4 | 4/4 | **4/4** |
| Wall time | 58.7 min | 74.8 min | 64.3 min | **26.8 min** |
| Reached stage 10 at | 26.7 min | 53.5 min | 38.7 min | **12.2 min** |
| Stage 10 | 32.0 min | 21.3 min | 25.6 min | **14.6 min** |
| Model calls | 170 | 95 | 112 | 91 |
| Input tokens | 2.47M | 1.06M | 1.20M | 1.14M |
| Output tokens | 316k | 216k | 198k | 211k |
| Read-only calls | 78 | 23 | 41 | 26 |
| Rejected edit rounds | 33 | 18 | 16 | **13** |
| Model time (sum of all calls) | 47.5 min | 109.5 min | 98.8 min | **57.2 min** |
| Calls still stalled > 45 s | 0 | 28 | 38 | **8** |
| Stalled requests cancelled and resent | — | — | — | 48 |

Stage wall times (min):

| Stage | main branch | 1st optimized | 2nd attempt | 3rd attempt |
| --- | --- | --- | --- | --- |
| 01 | 1.8 | 0.5 | 2.4 | 0.7 |
| 02 | 0.5 | 1.8 | 3.7 | 0.5 |
| 03 | 0.8 | 8.1 | 2.8 | 0.7 |
| 04 | 5.0 | 1.8 | 3.7 | 0.7 |
| 05 | 2.1 | 5.5 | 5.1 | 1.7 |
| 06 | 6.5 | 15.9 | 13.1 | 5.3 |
| 07 | 2.4 | 9.1 | 5.0 | 3.2 |
| 08 | 5.3 | 19.8 | 5.7 | 4.6 |
| 09 | 2.3 | 19.2 | 8.0 | 3.3 |
| 10 | 32.0 | 21.3 | 25.6 | 14.6 |

**What made the difference**
- **Stall detection (15):** 48 stalled requests were cancelled and resent, and only 8 calls still took over 45 s, against 38 in the second attempt. Total model time fell from 98.8 to 57.2 min with a similar number of calls.
- **Scheduling (18):** the 06–09 block finished 7.9 min after stage 05, against 21.0 min in the second attempt. Stage 08 started alongside stage 06, and stage 08 no longer ran its own npm install.
- **Background acceptance writing (18):** stage 10 took 14.6 min, against 25.6.
- **Project-wide read-ahead (17):** read-only calls fell from 41 to 26.
- **Quality held:** all 11 stages GREEN, all 4 requirements passed independent acceptance, and rejected edit rounds were the lowest of any run (13).

**Caveats**
- **Provider luck:** the provider may simply have been in a better state during this run, and some of the gain is that. The design stages (02–05), whose code barely changed, were also faster than in the second attempt.
- **One small app:** this is a single run on the 4-requirement `input/`. The real test is ARC-bench BookStack (12 tests), where the Nov 5 version passed 12/12 in 75 min.

## Fourth attempt

Fixes found when the third-attempt version first ran on ARC-bench (BookStack).

### 19. Adaptive stall deadlines

**Problem.** On the benchmark runner, all four image analyses in stage 01 hit the 10 s stall deadline on every attempt, in lockstep. They were cancelled and resent 5 times each before the final attempt, which waits without a deadline. Real queueing is random; this pattern was systematic. Image requests upload hundreds of KB and need the image processed before the first output arrives. Measured against the real endpoint, healthy image requests start answering after **18–20 s**. A fixed 10 s deadline therefore cancelled healthy requests: ~50 s and 4 duplicate requests per image. It would do the same to text requests in any environment where they start more slowly than locally.

**What changed** (`infrastructure/utils/presentation/model_io.py`):
- **Per-kind base deadline:** `ARC_STALL_FIRST_CHUNK_SECONDS` (default 10 s) for text and `ARC_STALL_FIRST_CHUNK_SECONDS_VISION` (default **60 s**) for requests containing images.
- **Learns from the run:** the time to first output of each successful request is recorded per kind (last 50). The deadline is at least **3× the observed median** once 3 samples exist, so a slower environment raises it automatically.
- **Doubles per retry:** 10 → 20 → 40 → 80 s, capped at 180 s. A wrong first guess stops cancelling healthy requests after one or two tries.
- The final attempt still waits without a deadline, and `ARC_STREAM=0` still turns stall detection off.

**Verified:**
- Unit tests: image vs text detection, the 60 s image base, doubling and the cap, and adaptation (healthy starts of 12–16 s give a 42 s deadline instead of 10 s).
- Real endpoint: 3 reference images analysed concurrently, healthy first output at 18–20 s, **0 resends** (the old rule would have cancelled every one), all results valid.
- The submission zip was rebuilt and re-checked: no `.env`, the API key appears in no file, `main.py` and `requirements.txt` are at the root.

### 20. A pause mid-answer no longer cuts off a healthy response

**Problem.** Also seen on the benchmark runner, in stage 02: `no streamed output within the read deadline; cancelling and resending`, 35 s into an attempt that had already started answering. To catch requests queued before any response headers, attempts with a deadline used a network read timeout equal to the first-output deadline (10 s). A read timeout applies to *every* read, so any pause of more than 10 s between chunks killed an answer that was being generated. The work was discarded and the request resent. The mid-answer limit was meant to be 60 s.

**What changed** (`_stream_completion` in `infrastructure/utils/presentation/model_io.py`):
- The request itself and the stream both run on a worker thread. The calling thread enforces the first-output deadline from the moment the request is sent, so waiting for headers counts, and enforces the mid-answer limit once output has started.
- The socket read timeout is now the mid-answer limit (`ARC_STALL_IDLE_SECONDS`, 60 s), never the first-output deadline.
- If the caller gives up before headers arrive, the worker shuts the connection down as soon as the call returns. Otherwise the socket is shut down at once.
- A non-streamed JSON reply and a rejected `stream_options` are still handled, now inside the worker.

**Verified:**
- New local-server test: output starts, then pauses 1.6 s with a 1 s first-output deadline and a 5 s mid-answer limit. One request, not cut off. The other 5 stall tests (queued before headers, queued after headers, slow streaming, final attempt, off switch) still pass.
- Real endpoint, during a slow period (healthy text requests needed ~9 s to start): 6 text calls and 3 concurrent image analyses, **1 resend in 10 requests**. The text deadline adapted from 10 s to ~27 s after 3 samples, and an 18.7 s call that started promptly was not interrupted.
- Submission zip rebuilt and re-checked.

### Fourth attempt: run status (stopped, no result)

The local run `outputs/optimized-run-5` was **stopped by hand in stage 05**, after ~23 min. Stages 00–04 were GREEN. It produced no final result, so it says nothing about pass rate or total time.

**Why it was stopped:** the model gateway was heavily overloaded at the time. With the same code and input as the third attempt:

| Stages 01–05 | 3rd attempt (`optimized-run-4`) | 4th attempt (`optimized-run-5`) |
| --- | --- | --- |
| Generation speed | 78 tok/s | **18 tok/s** |
| Slowest call | 42 s | **382 s** (6,199 tokens at 16 tok/s) |
| Stalled requests resent | — | 3 |

Requests were not stuck: only 3 stalled, and stall detection resent them. The model was generating very slowly. A network check on the running process showed data still arriving on its single connection at roughly 4 tok/s during a stage 05 proposal. Stall detection cannot speed this up, because a slow but working answer is not resent.

**Gateway check during the slowdown** (one identical ~600-token request per model):

| Model | Result |
| --- | --- |
| `deepseek-flash` | first output 17.7 s, then 13 tok/s |
| `minimax-m3` | first output 12.0 s, then 28 tok/s |
| `glm-5.3-flash` | no response within 120 s |
| `deepseek-v4-flash` | 403: "Model is not in the team allowed list" |
| `qwen3.6-flash` | 403: "用户被冻结" ("user is frozen"), to raise with the organisers |

**Takeaways**
- **Gateway load decides wall time.** Across today's runs the same code saw 13–115 tok/s, so timing comparisons are only meaningful between runs made under similar gateway conditions. Calls, tokens, rejected rounds and pass/fail don't depend on provider speed.
- **Check the gateway before a timed run:** a ~400-token request should show its first output in under ~5 s and run at over 50 tok/s.
- **Next:**
  - re-run the fourth attempt when the gateway is healthy
  - confirm with the organisers which models the team may use, and what the "user is frozen" error means
  - diagnose the 11/35 result on the full BookStack from that run's `.arc` reports

### Fourth attempt: results

Measured on `outputs/optimized-run-6`, a re-run of the fourth attempt (changes 15–20, with test writing and image analysis at `low` reasoning). It started once a gateway check showed a healthy gateway: first output after 2.6 s, 146 tok/s.

| | 2nd attempt | 3rd attempt | 4th attempt |
| --- | --- | --- | --- |
| Result | GREEN | GREEN | GREEN |
| Independent acceptance | 4/4 | 4/4 | 4/4 |
| Wall time | 64.3 min | 26.8 min | 45.4 min |
| Reached stage 10 at | 38.7 min | 12.2 min | 24.5 min |
| Stage 10 | 25.6 min | 14.6 min | 20.9 min |
| Model calls | 112 | 91 | 114 |
| Input tokens | 1.20M | 1.14M | 1.48M |
| Output tokens | 198k | 211k | 239k |
| Read-only calls | 41 | 26 | 27 |
| Rejected edit rounds | 16 | 13 | 36 |
| …from reading a missing file | 7 | 8 | 26 |
| …from whole-file replacement | 3 | 1 | 5 |
| Model time (sum of calls) | 98.8 min | 57.2 min | 102.1 min |
| Average generation speed | 33 tok/s | 62 tok/s | 39 tok/s |

Stage wall times (min):

| Stage | 2nd attempt | 3rd attempt | 4th attempt |
| --- | --- | --- | --- |
| 01 | 2.4 | 0.7 | 1.4 |
| 02 | 3.7 | 0.5 | 1.1 |
| 03 | 2.8 | 0.7 | 1.0 |
| 04 | 3.7 | 0.7 | 0.9 |
| 05 | 5.1 | 1.7 | 2.2 |
| 06 | 13.1 | 5.3 | 11.8 |
| 07 | 5.0 | 3.2 | 6.0 |
| 08 | 5.7 | 4.6 | 11.8 |
| 09 | 8.0 | 3.3 | 5.8 |
| 10 | 25.6 | 14.6 | 20.9 |

**What the numbers say**
- **Quality held:** GREEN, with all 4 requirements passing independent acceptance.
- **Slower than the third attempt (45.4 against 26.8 min), for two reasons:**
  1. **Gateway speed:** the gateway was fast at the check but averaged **39 tok/s** over the run, against 62 in the third attempt. Total model time was 102 against 57 min.
  2. **Rejected rounds:** **36**, against 13. **26** came from the model reading a file that did not exist; in each case one missing path failed the whole read and cost a round. Five came from rejected whole-file replacements. That accounts for most of the extra calls (114 against 91) and input tokens (1.48M against 1.14M). The worst hit were stage 10 acceptance writing and review (REQ-1 took 9 generation rounds, REQ-4 5 generation and 5 review rounds) and stages 06 and 08 (11.8 min each).
- **Changes 19 and 20 target the benchmark runner** (image deadlines and pauses mid-answer). Locally there were few stalls, so this run mostly confirms they cause no harm. Their effect has to be judged on ARC-bench.
- **Change 21 (fifth attempt) addresses exactly the 26 missing-file and 5 whole-file rejections.** The fifth attempt's run should show far fewer rejected rounds. That count doesn't depend on gateway speed, so it is the fairest comparison.

## Fifth attempt

Changes made while the fourth attempt's re-run (`outputs/optimized-run-6`) was in progress. That run uses the code as it was when it started, so it does not include them.

### 21. Fewer rounds lost to tool-usage mistakes in implementation tasks

**Problem.** In the fourth-attempt re-run, `API-get-current-session / Implementation` (stage 06) lost 4 rounds before it could work properly:

| Round | Model action | Outcome |
| --- | --- | --- |
| 1 | rewrote the whole of `register.ts` | rejected: whole-file replacement |
| 2 | read `register.ts` + `get-current-session.ts` | failed: `get-current-session.ts` does not exist yet |
| 3 | whole-file rewrite again | rejected |
| 4 | the same two reads again | failed again |

Two causes:
1. **Test-first means the implementation file does not exist yet.** The model tries to read the file it is meant to create. One missing path failed the whole read batch, so even `register.ts`, which existed, was not returned, and the round was lost. The old special case only forgave this when the target's path was already known. For a brand-new API it isn't, so the model has to guess the conventional name.
2. **Every whole-file replacement was rejected,** including the common safe case: rewriting `register.ts` with every existing line kept and one route added.

**What changed**
- **`FileTools.read_batch()`** (`infrastructure/shared/agent/tools/__init__.py`) runs reads one at a time. Existing files are returned. A failed read is reported in `context_result.read_errors`, with its error code and `similar_existing_paths`, plus guidance: create it if it is this task's file, otherwise use a suggested path, and don't request the same missing path again. No edit round is used, and repeating the same missing read is still caught by the existing "same evidence repeatedly" guard. This replaces the narrower missing-target special case in `infrastructure/shared/agent/loop.py`.
- **`keeps_every_line()`** (`infrastructure/shared/agent/tools/files.py`): a whole-file `replace` is accepted when every non-blank existing line still appears, in order (pure additions). A replacement that removes or changes an earlier line is still rejected, with a clearer message. The prompt rule in `protocol.py` now says the same.
- **Scope:** these apply to every file-editing agent (stages 04, 06, 08, 10 and repairs).

**Verified**
- Agent-loop test: a read batch of an existing file plus a not-yet-created one returns the existing file and a `FILE_NOT_FOUND` entry. The model then creates the file, and the task finishes in **1 round**; before, the batch failed and cost a round.
- File-tool test: an additions-only rewrite of `register.ts` is applied, and a rewrite that drops an earlier route is still rejected.
- All 32 tests pass. The existing suite has no new failures; the one already-failing clock-mocking test is unchanged.

### Fifth attempt: results

Measured on `outputs/optimized-run-7` (changes 15–21, with test writing and image analysis at `low` reasoning), compared with the third and fourth attempts on the same 4-requirement input.

| | 3rd attempt | 4th attempt | 5th attempt |
| --- | --- | --- | --- |
| Result | GREEN | GREEN | GREEN |
| Independent acceptance | 4/4 | 4/4 | 4/4 |
| Acceptance rounds (generation + review + repair) | 10 | 25 | 4 |
| Wall time | 26.8 min | 45.4 min | 48.5 min |
| Reached stage 10 at | 12.2 min | 24.5 min | 27.2 min |
| Stage 10 | 14.6 min | 20.9 min | 21.3 min |
| Model calls | 91 | 114 | 86 |
| Input tokens (cached) | 1.14M (64%) | 1.48M (65%) | 0.83M (48%) |
| Output tokens | 211k | 239k | 191k |
| Read-only calls | 26 | 27 | 29 |
| Rejected edit rounds | 13 | 36 | 2 |
| …from reading a missing file | 8 | 26 | 0 |
| …from whole-file replacement | 1 | 5 | 1 |
| Model time (sum of calls) | 57.2 min | 102.1 min | 105.6 min |
| Average generation speed | 62 tok/s | 39 tok/s | 30 tok/s |

Stage wall times (min):

| Stage | 3rd attempt | 4th attempt | 5th attempt |
| --- | --- | --- | --- |
| 01 | 0.7 | 1.4 | 0.8 |
| 02 | 0.5 | 1.1 | 1.0 |
| 03 | 0.7 | 1.0 | 1.9 |
| 04 | 0.7 | 0.9 | 1.9 |
| 05 | 1.7 | 2.2 | 3.5 |
| 06 | 5.3 | 11.8 | 9.8 |
| 07 | 3.2 | 6.0 | 8.0 |
| 08 | 4.6 | 11.8 | 7.2 |
| 09 | 3.3 | 5.8 | 10.0 |
| 10 | 14.6 | 20.9 | 21.3 |

**What change 21 did**
- **Rejected edit rounds: 36 → 2.** Rejections from reading a missing file fell from **26 to 0**: reads with a not-yet-created path now return the files that exist and report the missing one, without losing a round. Whole-file rejections fell from 5 to 1.
- **The lowest cost of any run:** 86 model calls (against 114), **0.83M input tokens** (against 1.48M, **−44%**) and 191k output tokens (against 239k).
- **Acceptance rounds: 25 → 4.** All 4 acceptance suites were written in a single round each, with no test review or repair needed.
- **Quality held:** GREEN, with 4/4 independent acceptance.

**Why wall time didn't improve (48.5 min)**
- **The gateway was the slowest of the three runs:** **30 tok/s** on average, against 39 and 62. Total model time was about the same as the fourth attempt (105.6 against 102.1 min), even though this run made 25% fewer calls and produced 20% fewer output tokens.
- **The cache hit rate fell (48% against 65%),** as expected: fewer repeated rounds means fewer resends of an already-cached prompt. Uncached input stayed about the same (~0.43M against ~0.52M), while total input dropped sharply.

**Conclusion:** change 21 removed the main source of wasted rounds. Measures that don't depend on gateway speed (calls, tokens, rejected rounds, acceptance rounds) are the best of any run so far. Wall time in these local runs is dominated by gateway speed (30–62 tok/s across the last three runs), so it can't show the improvement on its own.

