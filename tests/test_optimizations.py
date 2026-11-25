"""Regression checks for token/latency optimizations in the agent loop and pipeline."""

from pathlib import Path
from tempfile import TemporaryDirectory
import os
import threading
import time
import unittest
from unittest.mock import Mock, patch

from infrastructure.shared.agent import loop
from infrastructure.shared.agent.feedback import CheckFailure, clean_feedback, failure_message
from infrastructure.shared.agent.loop import run_file_agent
from infrastructure.shared.agent.tools import FileToolError, FileTools
from infrastructure.shared.project import dependencies
from infrastructure.utils.files import save_text


ANSI = "\x1b[2mexpect(\x1b[22m\x1b[31mreceived\x1b[39m\x1b[2m).toBe(\x1b[22m"


def call(tool, **parameters):
    return {"tool": tool, **parameters}


class FeedbackCompaction(unittest.TestCase):
    def test_failure_message_uses_headline_not_embedded_report(self):
        report = {"returncode": 1, "failures": ["x"], "output": "{" + "y" * 24000 + "}"}
        error = CheckFailure("UI checks are not GREEN", report)
        self.assertIn("y" * 100, str(error))  # The legacy string still embeds the report.
        self.assertEqual(failure_message(error), "UI checks are not GREEN")

    def test_failure_message_strips_ansi_and_bounds_plain_errors(self):
        message = failure_message(ValueError(ANSI + "z" * 5000), limit=100)
        self.assertNotIn("\x1b", message)
        self.assertLess(len(message), 200)
        self.assertIn("more characters", message)

    def test_clean_feedback_strips_nested_ansi(self):
        cleaned = clean_feedback({"checks": {"failures": [ANSI]}, "n": 1})
        self.assertEqual(cleaned, {"checks": {"failures": ["expect(received).toBe("]}, "n": 1})


class ActionableToolErrors(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        save_text(self.root / "frontend/src/home/HomePage.tsx", "export {}\n")
        save_text(self.root / "frontend/src/other.ts", "export {}\n")

    def tearDown(self):
        self.directory.cleanup()

    def test_write_refusal_lists_writable_paths(self):
        tools = FileTools(self.root, writable=["frontend/src/other.ts"])
        with self.assertRaises(FileToolError) as failed:
            tools.execute([call("edit_file", path="frontend/src/home/HomePage.tsx",
                operation="append", new_content="//x\n")])
        self.assertEqual(failed.exception.feedback["code"], "WRITE_NOT_ALLOWED")
        self.assertEqual(failed.exception.feedback["writable_paths"], ["frontend/src/other.ts"])

    def test_missing_read_suggests_existing_path(self):
        tools = FileTools(self.root)
        with self.assertRaises(FileToolError) as failed:
            tools.execute([call("read_file", path="frontend/src/pages/HomePage.tsx")])
        self.assertEqual(failed.exception.feedback["code"], "FILE_NOT_FOUND")
        self.assertIn("frontend/src/home/HomePage.tsx", failed.exception.feedback["similar_existing_paths"])


class FewerWastedRounds(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        save_text(self.root / "backend/src/api/register.ts", "import a from './a';\nexport const routes = [\n  a,\n];\n")
        loop._READ_MEMORY.clear(); loop._PROJECT_READS.clear()

    def tearDown(self):
        self.directory.cleanup()

    def test_read_batch_with_a_not_yet_created_file_returns_the_rest_without_losing_a_round(self):
        seen = []

        def generate(_, payload):
            seen.append(payload)
            if len(seen) == 1:  # Test-first: the implementation file does not exist yet.
                return {"tool_calls": [call("read_file", path="backend/src/api/register.ts"),
                    call("read_file", path="backend/src/api/get-current-session.ts")]}
            self.assertIn("backend/src/api/register.ts", payload["read_files"])
            errors = payload["context_result"]["read_errors"]
            self.assertEqual([(item["path"], item["code"]) for item in errors],
                [("backend/src/api/get-current-session.ts", "FILE_NOT_FOUND")])
            return {"tool_calls": [call("edit_file", path="backend/src/api/get-current-session.ts",
                operation="create", new_content="export const handler = 1;\n")]}

        result = run_file_agent(Mock(generate_json=generate), "Implement", {},
            FileTools(self.root, writable=["backend/src/api/get-current-session.ts", "backend/src/api/register.ts"]),
            lambda: {"ok": True}, 3, Mock(), "impl")
        self.assertEqual(result["turns"], 1)  # Previously: the whole read failed and cost a round.

    def test_whole_file_rewrite_is_accepted_only_when_it_keeps_every_line(self):
        tools = FileTools(self.root, writable=["backend/src/api/register.ts"])
        original = (self.root / "backend/src/api/register.ts").read_text()
        added = "import a from './a';\nimport b from './b';\nexport const routes = [\n  a,\n  b,\n];\n"
        tools.execute([call("edit_file", path="backend/src/api/register.ts", operation="replace",
            old_content=original, new_content=added)])
        self.assertEqual((self.root / "backend/src/api/register.ts").read_text(), added)
        with self.assertRaises(FileToolError) as rejected:  # Drops an earlier route: still forbidden.
            tools.execute([call("edit_file", path="backend/src/api/register.ts", operation="replace",
                old_content=added, new_content="import b from './b';\nexport const routes = [\n  b,\n];\n")])
        self.assertEqual(rejected.exception.feedback["code"], "WHOLE_FILE_REPLACEMENT_NOT_ALLOWED")


class AgentLoopRoundTrips(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        save_text(self.root / "backend/src/db/seed.ts", "export const seed = 1;\n")
        save_text(self.root / "backend/src/target.ts", "export {}\n")
        loop._READ_MEMORY.clear(); loop._PROJECT_READS.clear()

    def tearDown(self):
        loop._READ_MEMORY.clear(); loop._PROJECT_READS.clear()
        self.directory.cleanup()

    def tools(self):
        return FileTools(self.root, writable=["backend/src/target.ts"])

    def test_file_context_request_is_served_as_read_instead_of_burning_a_round(self):
        payloads = []

        def generate(_, payload):
            payloads.append(payload)
            if len(payloads) == 1:
                return {"context_request": {"kind": "file", "ids": ["backend/src/db/seed.ts"]}}
            self.assertIn("backend/src/db/seed.ts", payload["read_files"])
            return {"tool_calls": [call("edit_file", path="backend/src/target.ts",
                operation="append", new_content="// done\n")]}

        result = run_file_agent(Mock(generate_json=generate), "Task kind A", {}, self.tools(),
            lambda: {"ok": True}, 3, Mock(), "redirect", allow_discovery=False)
        self.assertEqual(result["turns"], 1)
        self.assertEqual(len(payloads), 2)

    def test_reads_from_earlier_task_are_supplied_ahead_to_the_next_task_of_same_kind(self):
        def first(_, payload):
            if "read_files" not in payload:
                return {"tool_calls": [call("read_file", path="backend/src/db/seed.ts")]}
            return {"tool_calls": []}

        run_file_agent(Mock(generate_json=first), "Task kind B", {}, self.tools(),
            lambda: {}, 2, Mock(), "first")
        seen = []

        def second(_, payload):
            seen.append(payload)
            return {"tool_calls": []}

        run_file_agent(Mock(generate_json=second), "Task kind B", {}, self.tools(),
            lambda: {}, 2, Mock(), "second")
        self.assertEqual(len(seen), 1)  # No read round trip needed.
        self.assertIn("backend/src/db/seed.ts", seen[0]["read_files"])
        self.assertEqual(list(seen[0])[:2], ["tools", "context_kinds"])  # Stable prefix first.

        other = []
        run_file_agent(Mock(generate_json=lambda _, payload: other.append(payload) or {"tool_calls": []}),
            "Different task kind", {}, self.tools(), lambda: {}, 2, Mock(), "other")
        self.assertNotIn("read_files", other[0])

    def test_files_needed_by_several_task_kinds_are_supplied_to_any_new_kind(self):
        def reader(_, payload):
            if "read_files" not in payload:
                return {"tool_calls": [call("read_file", path="backend/src/db/seed.ts")]}
            return {"tool_calls": []}

        def first_payload(kind):
            seen = []
            run_file_agent(Mock(generate_json=lambda _, payload: seen.append(payload) or {"tool_calls": []}),
                kind, {}, self.tools(), lambda: {}, 2, Mock(), kind)
            return seen[0]

        run_file_agent(Mock(generate_json=reader), "Kind one", {}, self.tools(), lambda: {}, 2, Mock(), "one")
        self.assertNotIn("read_files", first_payload("Brand-new kind A"))  # One task is not enough.
        run_file_agent(Mock(generate_json=reader), "Kind two", {}, self.tools(), lambda: {}, 2, Mock(), "two")
        self.assertIn("backend/src/db/seed.ts", first_payload("Brand-new kind B")["read_files"])

    def test_read_ahead_can_be_disabled(self):
        loop._remember_reads(loop._memory_key(self.tools(), "Kind C"), ["backend/src/db/seed.ts"])
        with patch.dict(os.environ, {"ARC_READ_AHEAD": "0"}):
            self.assertEqual(loop._read_ahead(loop._memory_key(self.tools(), "Kind C"), self.tools()), {})
        self.assertIn("backend/src/db/seed.ts",
            loop._read_ahead(loop._memory_key(self.tools(), "Kind C"), self.tools()))


class Escalation(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        save_text(self.root / "backend/src/target.ts", "export {}\n")
        loop._READ_MEMORY.clear(); loop._PROJECT_READS.clear()

    def tearDown(self):
        self.directory.cleanup()

    def run_agent(self, causes, gate):
        responses = iter(causes)

        def generate(_, payload):
            cause = next(responses)
            return {"diagnosis": {"cause": cause, "reason": "r"}, "tool_calls": [call("edit_file",
                path="backend/src/target.ts", operation="append", new_content="//x\n")]}

        return run_file_agent(Mock(generate_json=generate), "Repair", {}, FileTools(self.root,
            writable=["backend/src/target.ts"]), gate, 10, Mock(), "repair",
            feedback={"failed": True}, stop_when=loop.repeated_test_blame())

    def test_repeated_test_blame_stops_early_with_diagnoses(self):
        def gate():
            raise ValueError("still failing")

        with self.assertRaises(loop.AgentEscalated) as stopped:
            self.run_agent(["test_overconstraint", "implementation", "test_requirement_mismatch"], gate)
        self.assertEqual(stopped.exception.turns, 3)  # Not all 10 rounds.
        self.assertEqual(len(stopped.exception.diagnoses), 3)
        self.assertIsInstance(stopped.exception, loop.AgentExhausted)  # Callers' failure handling still applies.

    def test_single_test_blame_does_not_stop(self):
        result = self.run_agent(["test_overconstraint"], lambda: {"ok": True})
        self.assertEqual(result["turns"], 1)

    def test_every_generated_test_prompt_forbids_absolute_startup_state(self):
        from infrastructure.pipeline.backend_implementation import TEST_INSTRUCTIONS as backend
        from infrastructure.stages.frontend.triggers.implementation import TEST_INSTRUCTIONS as smoke
        from infrastructure.stages.frontend.triggers.acceptance import INSTRUCTIONS as acceptance
        for prompt in (backend, smoke, acceptance):
            self.assertIn("TEST DATA ASSERTIONS", prompt)


class StageOverlap(unittest.TestCase):
    def run_pipeline(self, environment):
        from contextlib import nullcontext
        from infrastructure import compiler

        calls = []

        def stage(name, seconds):
            def run(*_args, **_kwargs):
                calls.append(("start", name, time.monotonic()))
                time.sleep(seconds)
                calls.append(("end", name, time.monotonic()))
                return {"status": "GREEN"}
            return run

        pipeline = (("05_design_backend_apis", stage("05", 0.0), "a5"),
            ("06_implement_backend_tdd", stage("06", 0.4), "a6"),
            ("07_design_ui", stage("07", 0.4), "a7"),
            ("08_implement_ui", stage("08", 0.0), "a8"))
        with TemporaryDirectory() as directory, patch.object(compiler, "PIPELINE", pipeline), \
                patch.object(compiler, "console_log", lambda *a, **k: nullcontext()), \
                patch.object(compiler, "checkpoint"), patch.object(compiler, "show_panel"), \
                patch.dict(os.environ, environment):
            instance = compiler.Compiler(Path(directory) / "in", Path(directory) / "out")
            started = time.monotonic()
            instance.compile()
            return instance, time.monotonic() - started

    def test_ui_design_runs_alongside_backend_implementation(self):
        instance, elapsed = self.run_pipeline({"ARC_OVERLAP_STAGES": "1"})
        self.assertLess(elapsed, 0.7)
        self.assertEqual(list(instance.report["stages"]),
            ["05_design_backend_apis", "06_implement_backend_tdd", "07_design_ui", "08_implement_ui"])
        self.assertEqual(set(instance.artifacts), {"a5", "a6", "a7", "a8"})
        self.assertEqual(instance.report["status"], "GREEN")

    def test_overlap_can_be_disabled(self):
        _instance, elapsed = self.run_pipeline({"ARC_OVERLAP_STAGES": "0"})
        self.assertGreaterEqual(elapsed, 0.8)


class ScopedRollback(unittest.TestCase):
    """A failing frontend task must not roll back backend work done concurrently."""

    def test_failed_frontend_task_keeps_concurrent_backend_edits_and_index_entries(self):
        import json as json_module
        from infrastructure.shared.agent.recovery import implementation_task
        from infrastructure.shared.project.entrypoints import remember_path

        with TemporaryDirectory() as directory:
            root = Path(directory)
            save_text(root / "frontend/src/Page.tsx", "original page\n")
            save_text(root / "backend/src/api.ts", "original api\n")
            entry = {}
            with patch("infrastructure.shared.agent.recovery.checkpoint"):
                with implementation_task(root, entry, Mock(), "page", roots=("frontend/src", "frontend/tests"),
                        index_kinds=("page",)):
                    save_text(root / "frontend/src/Page.tsx", "broken page\n")
                    remember_path(root, "page", "p1", "frontend/src/Page.tsx")
                    # Meanwhile, stage 06 (another thread in a real run) edits the backend:
                    save_text(root / "backend/src/api.ts", "new api from stage 06\n")
                    save_text(root / "backend/tests/api.test.ts", "new test\n")
                    remember_path(root, "backend_test", "API-1", "backend/tests/api.test.ts")
                    raise ValueError("page task failed")
            self.assertEqual(entry["status"], "FAILED")
            self.assertEqual((root / "frontend/src/Page.tsx").read_text(), "original page\n")  # Rolled back.
            self.assertEqual((root / "backend/src/api.ts").read_text(), "new api from stage 06\n")  # Kept.
            self.assertTrue((root / "backend/tests/api.test.ts").is_file())
            index = json_module.loads((root / ".arc/implementation_paths.json").read_text())
            self.assertEqual(index.get("backend_test"), {"API-1": "backend/tests/api.test.ts"})
            self.assertNotIn("page", index)  # This task's own registration was undone.

    def test_stage_10_rollback_leaves_acceptance_suites_alone(self):
        from infrastructure.shared.agent.recovery import implementation_task
        with TemporaryDirectory() as directory:
            root = Path(directory)
            save_text(root / "frontend/src/App.tsx", "app\n")
            with patch("infrastructure.shared.agent.recovery.checkpoint"):
                with implementation_task(root, {}, Mock(), "req", exclude=("frontend/tests/e2e/acceptance",)):
                    save_text(root / "frontend/src/App.tsx", "broken\n")
                    save_text(root / "frontend/tests/e2e/acceptance/abc.spec.ts", "written in background\n")
                    raise ValueError("requirement failed")
            self.assertEqual((root / "frontend/src/App.tsx").read_text(), "app\n")
            self.assertTrue((root / "frontend/tests/e2e/acceptance/abc.spec.ts").is_file())


class OutputRouting(unittest.TestCase):
    def test_concurrent_sessions_write_only_to_their_own_logs(self):
        import io
        import sys
        from contextvars import copy_context
        from infrastructure.utils.presentation import logging as compiler_logging

        logs = {"a": io.StringIO(), "b": io.StringIO()}
        barrier = threading.Barrier(2)

        def session(name):
            with compiler_logging._routed_output(logs[name]):
                barrier.wait()
                for _ in range(50):
                    print(f"line-{name}")

        terminal = io.StringIO()
        original = sys.stdout
        sys.stdout = terminal
        try:
            threads = [threading.Thread(target=copy_context().run, args=(session, name)) for name in logs]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            sys.stdout = original
        self.assertEqual(logs["a"].getvalue().count("line-a"), 50)
        self.assertNotIn("line-b", logs["a"].getvalue())
        self.assertNotIn("line-a", logs["b"].getvalue())
        self.assertEqual(terminal.getvalue().count("line-"), 100)  # Both still reach the terminal once.


class PerTaskReasoning(unittest.TestCase):
    def test_each_task_kind_uses_its_own_setting_and_falls_back(self):
        from contextvars import copy_context
        from concurrent.futures import ThreadPoolExecutor
        from infrastructure.shared.agent.proposals import propose_json
        from infrastructure.shared.models.text import reasoning_mode

        seen = []

        def generate(_instructions, _payload):
            seen.append(reasoning_mode("low"))
            return {"tool_calls": []}

        with TemporaryDirectory() as directory:
            tools = FileTools(Path(directory))
            model = Mock(generate_json=generate)
            with patch.dict(os.environ, {"MODEL_REASONING_TESTS": "none", "MODEL_REASONING_DESIGN": "high"}):
                run_file_agent(model, "Write tests", {}, tools, lambda: {}, 1, Mock(), "tests", reasoning="tests")
                run_file_agent(model, "Implement", {}, tools, lambda: {}, 1, Mock(), "impl")
                propose_json(model, "Design", {}, lambda value: value, Mock(), "design", 1)
                with ThreadPoolExecutor(1) as pool:  # Parallel workers keep their kind.
                    pool.submit(copy_context().run, lambda: run_file_agent(model, "Write tests", {}, tools,
                        lambda: {}, 1, Mock(), "parallel", reasoning="tests")).result()
            run_file_agent(model, "Write tests", {}, tools, lambda: {}, 1, Mock(), "unset", reasoning="tests")
        # tests -> none, implementation -> fallback (low), design -> high, parallel tests -> none, unset -> low
        self.assertEqual(seen, ["none", "low", "high", "none", "low"])

    def test_invalid_value_is_rejected(self):
        from infrastructure.shared.models.text import reasoning_for, reasoning_mode
        with patch.dict(os.environ, {"MODEL_REASONING_TESTS": "fast"}), reasoning_for("tests"):
            with self.assertRaises(ValueError):
                reasoning_mode("low")


class AdaptiveStallDeadline(unittest.TestCase):
    def setUp(self):
        from infrastructure.utils.presentation import model_io
        self.model_io = model_io
        self.saved = {kind: list(values) for kind, values in model_io._FIRST_CHUNK_TIMES.items()}
        for values in model_io._FIRST_CHUNK_TIMES.values():
            values.clear()

    def tearDown(self):
        for kind, values in self.saved.items():
            self.model_io._FIRST_CHUNK_TIMES[kind][:] = values

    def test_image_requests_get_a_longer_deadline_and_retries_double(self):
        deadline, kind = self.model_io.stall_deadline, self.model_io._request_kind
        image = {"messages": [{"role": "user", "content": [{"type": "text", "text": "x"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]}]}
        self.assertEqual(kind(image), "vision")
        self.assertEqual(kind({"messages": [{"role": "user", "content": "x"}]}), "text")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARC_STALL_FIRST_CHUNK_SECONDS", None)
            os.environ.pop("ARC_STALL_FIRST_CHUNK_SECONDS_VISION", None)
            self.assertEqual(deadline("vision", 0), 60)
            self.assertEqual([deadline("text", attempt) for attempt in range(4)], [10, 20, 40, 80])
            self.assertEqual(deadline("text", 10), self.model_io.MAX_STALL_DEADLINE)

    def test_deadline_adapts_to_slow_environments(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARC_STALL_FIRST_CHUNK_SECONDS", None)
            for seconds in (12, 14, 16):  # This environment's healthy requests start in ~14 s.
                self.model_io._record_first_chunk("text", seconds)
            self.assertEqual(self.model_io.stall_deadline("text", 0), 42)  # Not 10: no false stalls.


class StallDetection(unittest.TestCase):
    """A real local SSE server: the first request stalls, later ones stream normally."""

    def setUp(self):
        import json as json_module
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.requests = []
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json_module.loads(self.rfile.read(int(self.headers["Content-Length"])))
                test.requests.append(body)
                if not body.get("stream"):
                    payload = json_module.dumps({"id": "c1", "object": "chat.completion", "created": 0,
                        "model": "m", "choices": [{"index": 0, "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"ok": true}'}}],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if len(test.requests) <= test.header_stalls:
                    time.sleep(4)  # Queued before sending anything, even headers.
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    if len(test.requests) <= test.stalls:
                        # Queued: keep-alive comments only, then (eventually) the answer.
                        for _ in range(test.stall_beats):
                            self.wfile.write(b": keep-alive\n\n")
                            self.wfile.flush()
                            time.sleep(0.25)
                    base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m"}
                    pieces = [{"role": "assistant", "reasoning_content": "think"}, {"content": '{"ok": '},
                        {"content": "true}"}]
                    for delta in pieces:
                        chunk = {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                        self.wfile.write(b"data: " + json_module.dumps(chunk).encode() + b"\n\n")
                        self.wfile.flush()
                        time.sleep(test.chunk_gap)
                    done = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                    usage = {**base, "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 7,
                        "total_tokens": 12, "completion_tokens_details": {"reasoning_tokens": 2}}}
                    for event in (done, usage):
                        self.wfile.write(b"data: " + json_module.dumps(event).encode() + b"\n\n")
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.stalls, self.chunk_gap, self.stall_beats, self.header_stalls = 0, 0.0, 40, 0

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def model(self):
        from infrastructure.shared.models.text import TextModel
        environment = {"OPENAI_API_KEY": "test", "MODEL": "test-model", "MODEL_REASONING": "provider",
            "OPENAI_BASE_URL": f"http://127.0.0.1:{self.server.server_address[1]}/v1",
            "ARC_STALL_FIRST_CHUNK_SECONDS": "1", "ARC_STALL_IDLE_SECONDS": "1", "ARC_STALL_RETRIES": "2"}
        with patch.dict(os.environ, environment):
            return TextModel()

    def generate(self, model, **environment):
        with patch.dict(os.environ, {"ARC_STALL_FIRST_CHUNK_SECONDS": "1", "ARC_STALL_IDLE_SECONDS": "1",
                "ARC_STALL_RETRIES": "2", **environment}):
            return model.generate_json("Return JSON.", {"x": 1})

    def test_stalled_request_is_cancelled_and_resent(self):
        self.stalls = 1
        started = time.monotonic()
        self.assertEqual(self.generate(self.model()), {"ok": True})
        elapsed = time.monotonic() - started
        self.assertEqual(len(self.requests), 2)
        self.assertLess(elapsed, 5)  # The stalled request would have taken 10 s.
        self.assertTrue(self.requests[0]["stream"])

    def test_request_queued_before_headers_is_resent(self):
        self.header_stalls = 1  # The real endpoint withholds even headers while queued.
        started = time.monotonic()
        self.assertEqual(self.generate(self.model()), {"ok": True})
        self.assertLess(time.monotonic() - started, 3.5)  # Not the 4 s queue wait.
        self.assertEqual(len(self.requests), 2)

    def test_pause_mid_answer_longer_than_first_deadline_is_not_cut_off(self):
        # Seen on the benchmark runner: output started, then paused > first deadline.
        self.chunk_gap = 1.6  # first deadline 1 s, mid-answer limit 5 s.
        self.assertEqual(self.generate(self.model(), ARC_STALL_IDLE_SECONDS="5"), {"ok": True})
        self.assertEqual(len(self.requests), 1)

    def test_slow_but_streaming_generation_is_not_interrupted(self):
        self.chunk_gap = 0.6  # Total 1.8 s > 1 s deadline, but chunks keep arriving.
        self.assertEqual(self.generate(self.model()), {"ok": True})
        self.assertEqual(len(self.requests), 1)

    def test_final_attempt_waits_without_deadline(self):
        self.stalls, self.stall_beats = 1, 8  # Queued 2 s, then answers; deadline is 1 s.
        self.assertEqual(self.generate(self.model(), ARC_STALL_RETRIES="0"), {"ok": True})
        self.assertEqual(len(self.requests), 1)  # The only attempt is the last: never cancelled.

    def test_streaming_can_be_disabled(self):
        self.assertEqual(self.generate(self.model(), ARC_STREAM="0"), {"ok": True})
        self.assertNotIn("stream", self.requests[0])


class BackgroundInstall(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        save_text(self.root / "package.json", "{}\n")
        dependencies._prefetches.clear()

    def tearDown(self):
        dependencies._prefetches.clear()
        self.directory.cleanup()

    def state(self, returncode=0):
        thread = threading.Thread(target=lambda: None)
        thread.start()
        thread.join()
        return {"fingerprint": dependencies._manifest_fingerprint(self.root), "lines": ["added 1 package\n"],
            "returncode": returncode, "error": None, "process": Mock(poll=Mock(return_value=0)), "thread": thread}

    def test_successful_prefetch_skips_foreground_install(self):
        dependencies._prefetches[self.root] = self.state()
        with patch.object(dependencies, "start_process") as start:
            dependencies.install_dependencies(self.root)
        start.assert_not_called()

    def test_changed_manifest_or_failure_falls_back_to_foreground_install(self):
        for returncode, change in ((0, True), (1, False)):
            dependencies._prefetches[self.root] = self.state(returncode)
            if change:
                save_text(self.root / "frontend/package.json", '{"dependencies":{"x":"1"}}\n')
            self.assertFalse(dependencies._join_prefetch(self.root))


class ParallelImages(unittest.TestCase):
    def test_images_are_analyzed_concurrently_and_published_in_order(self):
        from infrastructure.pipeline import requirements
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input").mkdir()
            names = ["a.png", "b.png", "c.png"]
            for name in names:
                (root / "input" / name).write_bytes(b"x")
            save_text(root / "input/requirements.yaml",
                "id: REQ-0\nname: root\ndescription:\n  text: t\n  image:\n"
                + "".join(f"    - reference: {name}\n" for name in names))

            def analyze(image):
                time.sleep(0.3)
                return {"layout": image.name, "elements": [], "style": ""}

            with patch.object(requirements, "VisualModel", return_value=Mock(analyze=analyze)), \
                    patch.object(requirements, "DemoConsole"), patch.object(requirements, "checkpoint"):
                started = time.monotonic()
                requirements.explore_requirement(root / "input", root / "out/.arc")
                elapsed = time.monotonic() - started
            import json
            analysis = json.loads((root / "out/.arc/visual_analysis.json").read_text())
            self.assertEqual(list(analysis), names)
            self.assertLess(elapsed, 0.8)


if __name__ == "__main__":
    unittest.main()
