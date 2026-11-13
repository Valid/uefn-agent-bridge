"""exec's quiet budget, and late results kept as jobs for callers that timed out."""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock


os.environ["UEFN_BRIDGE_NO_AUTOSTART"] = "1"
fake_unreal = sys.modules.get("unreal") or types.ModuleType("unreal")
fake_unreal.log = getattr(fake_unreal, "log", lambda *_: None)
fake_unreal.log_error = getattr(fake_unreal, "log_error", lambda *_: None)
fake_unreal.log_warning = getattr(fake_unreal, "log_warning", lambda *_: None)
sys.modules["unreal"] = fake_unreal

from bridge import server  # noqa: E402
from bridge.client import BridgeError, UEFNBridge  # noqa: E402


def run_exec(code: str, **params):
    return server._execute_and_respond("exec-test", "exec", {"code": code, **params})


class ExecBudgetTests(unittest.TestCase):
    def tearDown(self):
        server._set_editor_command()

    def test_a_python_loop_with_no_work_is_stopped(self):
        started = time.monotonic()
        response = run_exec("print('started')\nwhile True:\n    pass", budget_s=1)
        elapsed = time.monotonic() - started
        self.assertFalse(response["ok"])
        self.assertLess(elapsed, 4.0)
        self.assertIn("exec was stopped", response["error"])
        self.assertIn("budget_s", response["error"])
        self.assertEqual(response["stdout"], "started\n")
        self.assertEqual(response["stopped_by_budget"]["budget_s"], 1.0)
        self.assertGreaterEqual(response["stopped_by_budget"]["quiet_s"], 1.0)
        self.assertIsNone(sys.getprofile(), "the profile hook is removed")
        self.assertIsNone(server._exec_guard)
        time.sleep(0.6)  # a stop must never land after the command returned

    def test_the_next_exec_after_a_stop_runs_normally(self):
        # On CPython 3.11, clearing a fired stop with SetAsyncExc(NULL) left the
        # eval breaker set and the next exec spun forever in its first call.
        for _ in range(3):
            self.assertFalse(run_exec("while True:\n    pass", budget_s=1)["ok"])
            box = {}
            worker = threading.Thread(
                target=lambda: box.update(r=run_exec("import threading\nresult = threading.active_count()")),
                daemon=True,
            )
            worker.start()
            worker.join(10)
            self.assertFalse(worker.is_alive(), "the exec after a stop hung")
            self.assertTrue(box["r"]["ok"], box["r"])

    def test_except_exception_does_not_swallow_the_stop(self):
        response = run_exec(
            "try:\n    while True:\n        pass\nexcept Exception:\n    print('caught')",
            budget_s=1,
        )
        self.assertFalse(response["ok"])
        self.assertNotIn("caught", response["stdout"])

    def test_prints_keep_a_long_script_alive(self):
        code = (
            "import time\nend = time.monotonic() + 2.5\nn = 0\n"
            "while time.monotonic() < end:\n"
            "    n += 1\n"
            "    if n % 20000 == 0:\n        print(n)\n"
            "result = 'finished'"
        )
        response = run_exec(code, budget_s=1)
        self.assertTrue(response["ok"], response.get("error"))
        self.assertEqual(response["result"]["result"], "finished")

    def test_keep_alive_keeps_a_long_script_alive(self):
        code = (
            "import time\nend = time.monotonic() + 2.5\n"
            "while time.monotonic() < end:\n    keep_alive()\n"
            "result = 'finished'"
        )
        response = run_exec(code, budget_s=1)
        self.assertTrue(response["ok"], response.get("error"))

    def test_time_inside_one_long_native_call_never_counts(self):
        response = run_exec("import time\ntime.sleep(2.5)\nresult = 'finished'", budget_s=1)
        self.assertTrue(response["ok"], response.get("error"))

    def test_editor_calls_keep_a_long_script_alive(self):
        code = (
            "import math, time\nend = time.monotonic() + 2.5\n"
            "while time.monotonic() < end:\n    math.sqrt(2)\n"
            "result = 'finished'"
        )
        with mock.patch.object(server, "_is_unreal_callable", lambda fn: fn is math.sqrt):
            alive = run_exec(code, budget_s=1)
        self.assertTrue(alive["ok"], alive.get("error"))
        # The same loop without editor calls is only Python and short natives: stopped.
        stopped = run_exec(code, budget_s=1)
        self.assertFalse(stopped["ok"])
        self.assertIn("exec was stopped", stopped["error"])

    def test_budget_zero_turns_it_off_and_bad_values_fail(self):
        off = run_exec("import sys\nresult = sys.getprofile() is None", budget_s=0)
        self.assertEqual(off["result"]["result"], True)
        on = run_exec("import sys\nresult = sys.getprofile() is None")
        self.assertEqual(on["result"]["result"], False)
        bad = run_exec("result = 1", budget_s="soon")
        self.assertFalse(bad["ok"])
        self.assertIn("budget_s", bad["error"])

    def test_syntax_errors_and_raising_scripts_still_report_as_before(self):
        syntax = run_exec("def (:")
        self.assertFalse(syntax["ok"])
        self.assertIn("SyntaxError", syntax["error"])
        raised = run_exec("raise ValueError('boom')")
        self.assertEqual(raised["error"], "exec raised ValueError: boom")
        self.assertNotIn("stopped_by_budget", raised)

    def test_activity_shows_how_long_a_running_exec_has_been_quiet(self):
        seen = {}

        def editor():
            server._set_editor_command("exec")
            run_exec(
                "import time\nend = time.monotonic() + 1.0\n"
                "while time.monotonic() < end:\n    pass",
                budget_s=30,
            )
            server._set_editor_command()

        thread = threading.Thread(target=editor, daemon=True)
        thread.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not seen:
            editor_command = server._activity_snapshot()["editor_command"]
            if editor_command and "exec" in editor_command:
                seen.update(editor_command["exec"])
            time.sleep(0.05)
        thread.join(timeout=5)
        self.assertEqual(seen["budget_s"], 30.0)
        self.assertIn("quiet_s", seen)

    def test_the_stop_only_lands_in_the_script_not_in_bridge_code(self):
        guard = server._ExecGuard(1.0)
        self.assertFalse(guard._script_running(), "this test's own frames are not the script")


class _Editor:
    """Runs server._tick on a thread, as the editor's game thread would."""

    def __init__(self):
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop.is_set():
            server._tick(0.0)
            time.sleep(0.01)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=5)


class JobTests(unittest.TestCase):
    def setUp(self):
        self.previous_http = server._http
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server._Handler)
        self.http.daemon_threads = True
        self.http._uefn_bridge_accepting = True
        server._http = self.http
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.http.server_port
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)
        server._http = self.previous_http
        server._commands.pop("test.slow", None)
        server._set_editor_command()
        server._pending_tool_calls.clear()
        server._outlived_tool_calls.clear()
        with server._results_lock:
            server._jobs.clear()
            server._abandoned_requests.clear()
            server._rid_jobs.clear()
            server._results.clear()
        while not server._work_queue.empty():
            server._work_queue.get_nowait()

    def post(self, payload: dict):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def test_a_command_that_outlives_its_deadline_keeps_its_result(self):
        def slow():
            time.sleep(1.5)
            return {"spawned": 3}

        server._commands["test.slow"] = slow
        with _Editor():
            status, body = self.post({"command": "test.slow", "timeout_s": 1, "job_id": "spawn-1"})
            self.assertEqual(status, 504)
            self.assertTrue(body["outcome_unknown"])
            self.assertEqual(body["job_id"], "spawn-1")
            self.assertIn("job.result", body["error"])
            status, job = self.post(
                {"command": "job.result", "params": {"job_id": "spawn-1", "wait_s": 5}}
            )
        self.assertEqual(status, 200)
        self.assertEqual(job["result"]["state"], "done")
        self.assertEqual(job["result"]["command"], "test.slow")
        self.assertEqual(job["result"]["response"]["result"], {"spawned": 3})
        _, listed = self.post({"command": "job.result"})
        self.assertEqual([j["job_id"] for j in listed["result"]["jobs"]], ["spawn-1"])
        self.assertNotIn("response", listed["result"]["jobs"][0])

    def test_a_named_job_keeps_its_result_even_when_answered_in_time(self):
        # A caller that stops listening early (its own watchdog) can still collect it.
        server._commands["test.slow"] = lambda: {"placed": 1}
        with _Editor():
            status, body = self.post({"command": "test.slow", "job_id": "place-1"})
        self.assertEqual((status, body["ok"]), (200, True))
        job = server._cmd_job_result("place-1")
        self.assertEqual((job["state"], job["response"]["result"]), ("done", {"placed": 1}))

    def test_a_huge_result_is_not_kept_whole(self):
        with server._results_lock:
            job_id = server._open_job_locked("big-1", "exec")
            server._finish_job_locked(job_id, "done", {"ok": True, "result": "x" * (2 << 20)})
        job = server._cmd_job_result("big-1")
        self.assertGreater(job["response"]["result_too_large"], server.JOB_MAX_RESULT_BYTES)
        self.assertTrue(job["response"]["ok"])

    def test_without_a_job_id_the_bridge_names_one(self):
        server._commands["test.slow"] = lambda: time.sleep(1.5) or {}
        with _Editor():
            status, body = self.post({"command": "test.slow", "timeout_s": 1})
            self.assertEqual(status, 504)
            _, job = self.post(
                {"command": "job.result", "params": {"job_id": body["job_id"], "wait_s": 5}}
            )
        self.assertEqual(job["result"]["state"], "done")

    def test_a_running_job_reports_running_with_activity(self):
        server._commands["test.slow"] = lambda: time.sleep(2.0) or {}
        with _Editor():
            self.post({"command": "test.slow", "timeout_s": 1, "job_id": "slow-1"})
            _, job = self.post({"command": "job.result", "params": {"job_id": "slow-1"}})
            self.assertEqual(job["result"]["state"], "running")
            self.assertIn("activity", job["result"])
            self.post({"command": "job.result", "params": {"job_id": "slow-1", "wait_s": 5}})

    def test_a_named_job_waiting_its_turn_is_known_and_can_expire_unstarted(self):
        release = threading.Event()
        server._commands["test.slow"] = lambda: release.wait(5) and {"first": True}
        replies = {}

        def send(name, payload):
            replies[name] = self.post(payload)

        with _Editor():
            first = threading.Thread(target=send, args=("first", {"command": "test.slow", "timeout_s": 5}))
            first.start()
            time.sleep(0.3)
            second = threading.Thread(
                target=send,
                args=("second", {"command": "actors.list", "timeout_s": 1, "job_id": "behind-1"}),
            )
            second.start()
            time.sleep(0.3)
            waiting = server._cmd_job_result("behind-1")
            self.assertEqual(waiting["state"], "running")
            self.assertTrue(waiting["waiting_for_turn"])
            second.join(5)
            release.set()
            first.join(5)
        self.assertIn("expired before serialized dispatch", replies["second"][1]["error"])
        job = server._cmd_job_result("behind-1")
        self.assertEqual(job["state"], "not_started")
        self.assertNotIn("waiting_for_turn", job)

    def test_a_job_id_still_running_is_refused_and_keeps_the_first_result(self):
        server._commands["test.slow"] = lambda: time.sleep(1.5) or {"spawned": 1}
        with _Editor():
            status, _ = self.post({"command": "test.slow", "timeout_s": 1, "job_id": "dup-1"})
            self.assertEqual(status, 504)
            status, again = self.post(
                {"command": "test.slow", "job_id": "dup-1", "if_busy": "reject"}
            )
            self.assertEqual(status, 409)
            self.assertIn("still running", again["error"])
            job = server._cmd_job_result("dup-1", wait_s=5)
        self.assertEqual((job["state"], job["response"]["result"]), ("done", {"spawned": 1}))

    def test_a_command_the_editor_never_reached_is_not_started(self):
        with server._results_lock:
            server._abandoned_requests["queued-1"] = server._open_job_locked("queued-1", "actors.list")
        server._work_queue.put(("queued-1", "actors.list", {}, time.monotonic() + 5))
        server._tick(0.0)
        job = server._cmd_job_result("queued-1")
        self.assertEqual(job["state"], "not_started")
        self.assertIn("never started", job["response"]["error"])

    def test_a_native_tool_that_outlives_its_caller_keeps_its_final_result(self):
        class NativeResult:
            is_complete, error, value = False, "", '{"pushed": true}'

        native = NativeResult()
        call = server._PendingRegistryCall(native, "ValkyrieToolset.SessionToolset", "PushChanges", {})
        call.command, call.deadline_mono = "session.push_changes", time.monotonic() - 1
        server._pending_tool_calls["push-rid"] = call
        server._rid_jobs["push-rid"] = "push-1"
        server._poll_pending_tool_calls()
        with server._results_lock:
            answer = server._results.pop("push-rid")
        self.assertTrue(answer["outcome_unknown"])
        self.assertEqual(answer["job_id"], "push-1")
        self.assertIn("job.result", answer["error"])
        self.assertEqual(server._cmd_job_result("push-1")["state"], "running")
        native.is_complete = True
        server._poll_pending_tool_calls()
        job = server._cmd_job_result("push-1")
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["response"], {"ok": True, "route": "toolset_registry",
                                           "result": {"pushed": True}})

    def test_a_native_tool_done_as_its_deadline_passes_gives_its_real_result(self):
        class NativeResult:
            is_complete, error, value = True, "", '{"built": true}'

        call = server._PendingRegistryCall(NativeResult(), "T", "Build", {})
        call.command, call.deadline_mono = "verse.build", time.monotonic() - 1
        server._pending_tool_calls["build-rid"] = call
        server._poll_pending_tool_calls()
        with server._results_lock:
            answer = server._results.pop("build-rid")
        self.assertEqual(answer["result"], {"built": True})

    def test_a_bridge_stop_ends_outlived_jobs_as_unknown(self):
        class NativeResult:
            is_complete, error, value = False, "", "{}"

        call = server._PendingRegistryCall(NativeResult(), "T", "StartSession", {})
        call.command, call.deadline_mono = "session.start", time.monotonic() - 1
        server._pending_tool_calls["start-rid"] = call
        server._poll_pending_tool_calls()
        server._cancel_dispatch_state()
        job = server._cmd_job_result("start-rid")
        self.assertEqual(job["state"], "unknown")
        self.assertTrue(job["response"]["outcome_unknown"])

    def test_unknown_and_invalid_job_ids(self):
        status, body = self.post({"command": "job.result", "params": {"job_id": "nope"}})
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertIn("No job 'nope'", body["error"])
        status, body = self.post({"command": "actors.list", "job_id": "has spaces"})
        self.assertEqual(status, 400)
        self.assertIn("job_id", body["error"])

    def test_old_jobs_are_pruned(self):
        with server._results_lock:
            for index in range(server.JOB_CAP + 5):
                job_id = server._open_job_locked(f"r{index}", "exec")
                server._finish_job_locked(job_id, "done", {"ok": True})
            self.assertEqual(len(server._jobs), server.JOB_CAP)
            self.assertNotIn("r0", server._jobs)
            server._jobs["r10"]["finished"] = time.monotonic() - server.JOB_KEEP_S - 1
            server._prune_jobs_locked()
            self.assertNotIn("r10", server._jobs)


class ClientJobTests(unittest.TestCase):
    def test_a_timeout_error_carries_the_job_id(self):
        client = UEFNBridge()
        envelope = {"ok": False, "route": "python", "outcome_unknown": True,
                    "job_id": "j-1", "error": "'exec' timed out (1s)."}
        with mock.patch.object(client, "_post", return_value=envelope), \
                self.assertRaises(BridgeError) as caught:
            client.exec("while True: pass", budget_s=5)
        self.assertEqual(caught.exception.job_id, "j-1")

    def test_job_result_and_track_as_are_sent(self):
        client = UEFNBridge()
        sent = []

        def fake_post(payload, timeout=0):
            sent.append((payload, timeout))
            return {"ok": True, "route": "python", "result": {"state": "done"}}

        with mock.patch.object(client, "_post", side_effect=fake_post):
            client.job_result("j-1", wait_s=30)
            client.run("actors.list", track_as="mine")
        self.assertEqual(sent[0][0]["params"], {"job_id": "j-1", "wait_s": 30})
        self.assertGreaterEqual(sent[0][1], 35)
        self.assertEqual(sent[1][0]["job_id"], "mine")


if __name__ == "__main__":
    unittest.main()
