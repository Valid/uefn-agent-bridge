"""Busy reporting, fail-fast requests, slow-tool deadlines and exec failures."""

from __future__ import annotations

import contextlib
import json
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

from bridge import routes, server  # noqa: E402
from bridge.client import BridgeBusy, BridgeError, UEFNBridge  # noqa: E402


class DefaultDeadlineTests(unittest.TestCase):
    def test_slow_aliases_get_their_own_deadline(self):
        self.assertEqual(routes.default_deadline_s("session.start"), 600.0)
        self.assertEqual(routes.default_deadline_s("session.push_changes"), 600.0)
        self.assertEqual(routes.default_deadline_s("verse.build"), 600.0)
        self.assertEqual(routes.default_deadline_s("session.start_game"), 180.0)

    def test_generic_calls_naming_a_slow_tool_match_the_alias(self):
        params = {"toolset_name": "ValkyrieToolset.SessionToolset", "tool_name": "StartSession"}
        self.assertEqual(routes.default_deadline_s("toolsets.call", params), 600.0)
        self.assertEqual(routes.default_deadline_s("mcp.call", params), 600.0)

    def test_qualified_tool_names_get_the_same_deadline(self):
        params = {
            "toolset_name": "ValkyrieToolset.SessionToolset",
            "tool_name": "ValkyrieToolset.SessionToolset.StartSession",
        }
        self.assertEqual(routes.default_deadline_s("toolsets.call", params), 600.0)
        mismatched = dict(params, tool_name="ValkyrieToolset.VerseToolset.StartSession")
        self.assertEqual(routes.default_deadline_s("toolsets.call", mismatched, 30.0), 30.0)

    def test_everything_else_keeps_the_fallback(self):
        self.assertEqual(routes.default_deadline_s("actors.list", {}, 30.0), 30.0)
        self.assertEqual(routes.default_deadline_s("session.status", {}, 30.0), 30.0)
        odd = {"toolset_name": ["not", "a", "name"], "tool_name": {"x": 1}}
        self.assertEqual(routes.default_deadline_s("toolsets.call", odd, 30.0), 30.0)


class _LiveHandlerTestCase(unittest.TestCase):
    """Runs the real request handler on an ephemeral port, without an editor."""

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
        server._set_dispatch_turn()
        server._set_editor_command()
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
            with self.opener.open(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    @contextlib.contextmanager
    def turn_held_by(self, command: str):
        server._request_serial_lock.acquire()
        server._set_dispatch_turn(command)
        try:
            yield
        finally:
            server._set_dispatch_turn()
            server._request_serial_lock.release()


class BusyTests(_LiveHandlerTestCase):
    def test_reject_returns_409_with_what_holds_the_turn(self):
        with self.turn_held_by("verse.build"):
            status, body = self.post({"command": "actors.list", "if_busy": "reject"})
        self.assertEqual(status, 409)
        self.assertFalse(body["ok"])
        self.assertNotIn("outcome_unknown", body)
        self.assertEqual(body["busy"]["dispatch_turn"]["command"], "verse.build")
        self.assertTrue(server._work_queue.empty(), "a rejected request must not be queued")

    def test_reject_counts_editor_work_whose_caller_timed_out(self):
        # The turn is free, but the editor is still running an abandoned command.
        server._set_editor_command("exec")
        status, body = self.post({"command": "actors.list", "if_busy": "reject"})
        self.assertEqual(status, 409)
        self.assertEqual(body["busy"]["editor_command"]["command"], "exec")
        server._set_editor_command()
        server._work_queue.put(("abandoned-1", "exec", {}, time.monotonic() + 5))
        status, _ = self.post({"command": "actors.list", "if_busy": "reject"})
        self.assertEqual(status, 409)
        self.assertEqual(server._work_queue.qsize(), 1, "nothing new was queued")
        self.assertTrue(server._request_serial_lock.acquire(blocking=False), "the turn was released")
        server._request_serial_lock.release()

    def test_reject_on_an_idle_bridge_dispatches_normally(self):
        status, body = self.post({"command": "status", "if_busy": "reject"})
        self.assertEqual((status, body["ok"]), (200, True))

    def test_invalid_if_busy_is_a_400(self):
        status, body = self.post({"command": "actors.list", "if_busy": "later"})
        self.assertEqual(status, 400)
        self.assertIn("if_busy", body["error"])

    def test_status_stays_answerable_and_counts_waiting_requests(self):
        with self.turn_held_by("session.push_changes"):
            waiter = threading.Thread(
                target=self.post, args=({"command": "actors.list", "timeout_s": 1},), daemon=True
            )
            waiter.start()
            deadline = time.monotonic() + 2
            activity = {}
            while time.monotonic() < deadline:
                status, body = self.post({"command": "status"})
                activity = body["result"]["activity"]
                if activity["waiting"] == 1:
                    break
                time.sleep(0.02)
            self.assertEqual(status, 200)
            self.assertEqual(activity["waiting"], 1)
            self.assertEqual(activity["dispatch_turn"]["command"], "session.push_changes")
        waiter.join(timeout=5)
        self.assertEqual(server._activity_snapshot()["waiting"], 0)

    def test_health_get_reports_activity(self):
        with self.opener.open(f"http://127.0.0.1:{self.port}/", timeout=5) as response:
            body = json.loads(response.read())
        self.assertIn("activity", body)
        self.assertIn("editor_tick_age_s", body["activity"])
        # Clients find the bridge by this identity field, so it keeps the original name.
        self.assertEqual((body["bridge"], body["name"]), ("uefn-python-bridge", "uefn-agent-bridge"))

    def test_client_raises_bridge_busy(self):
        client = UEFNBridge(port=self.port, timeout=5)
        with self.turn_held_by("verse.build"), self.assertRaises(BridgeBusy) as caught:
            client.run("actors.list", if_busy="reject")
        self.assertEqual(caught.exception.busy["dispatch_turn"]["command"], "verse.build")
        self.assertFalse(caught.exception.outcome_unknown)


class DeadlineSelectionTests(_LiveHandlerTestCase):
    def deadline_used(self, payload: dict) -> float:
        seen = []

        def refuse_turn(timeout_s):
            seen.append(timeout_s)
            return False

        with mock.patch.object(server, "_wait_for_turn", side_effect=refuse_turn):
            self.post(payload)
        return seen[0]

    def test_session_start_without_timeout_waits_ten_minutes(self):
        self.assertAlmostEqual(self.deadline_used({"command": "session.start"}), 600.0, delta=1.0)

    def test_ordinary_command_keeps_thirty_seconds(self):
        self.assertAlmostEqual(self.deadline_used({"command": "actors.list"}), 30.0, delta=1.0)

    def test_explicit_timeout_wins(self):
        self.assertAlmostEqual(
            self.deadline_used({"command": "session.start", "timeout_s": 12}), 12.0, delta=1.0
        )

    def test_client_waits_as_long_as_the_bridge_deadline(self):
        client = UEFNBridge(timeout=30)
        sent = {}

        def fake_post(payload, timeout=0):
            sent["timeout"] = timeout
            return {"ok": True, "route": "toolset_registry", "result": {}}

        with mock.patch.object(client, "_post", side_effect=fake_post):
            client.run("session.start")
        self.assertGreaterEqual(sent["timeout"], 600.0)


class EditorActivityTests(unittest.TestCase):
    def tearDown(self):
        server._set_editor_command()
        server._commands.pop("test.observe", None)

    def test_tick_reports_the_running_command_and_clears_it(self):
        seen = {}

        def observe():
            seen.update(server._activity_snapshot())
            return {}

        server._commands["test.observe"] = observe
        rid = "observe-1"
        server._work_queue.put((rid, "test.observe", {}, time.monotonic() + 5))
        server._tick(0.0)
        with server._results_lock:
            response = server._results.pop(rid)
        self.assertTrue(response["ok"])
        self.assertEqual(seen["editor_command"]["command"], "test.observe")
        self.assertEqual(seen["editor_command"]["state"], "running")
        after = server._activity_snapshot()
        self.assertIsNone(after["editor_command"])
        self.assertLess(after["editor_tick_age_s"], 1.0)

    def test_native_tool_that_outlives_its_deadline_stays_busy_until_it_finishes(self):
        class NativeResult:
            is_complete, error, value = False, "", "{}"

        native = NativeResult()
        call = server._PendingRegistryCall(native, "ValkyrieToolset.SessionToolset", "StartSession", {})
        call.command, call.deadline_mono = "session.start", time.monotonic() - 1
        server._pending_tool_calls["late-1"] = call
        try:
            server._poll_pending_tool_calls()
            with server._results_lock:
                answer = server._results.pop("late-1")
            self.assertTrue(answer["outcome_unknown"], "the caller hears it timed out")
            editor = server._activity_snapshot()["editor_command"]
            self.assertEqual((editor["command"], editor["state"]), ("session.start", "outlived_caller"))
            self.assertTrue(server._editor_work_outstanding())
            native.is_complete = True
            server._poll_pending_tool_calls()
            self.assertIsNone(server._activity_snapshot()["editor_command"])
            self.assertFalse(server._editor_work_outstanding())
        finally:
            server._pending_tool_calls.clear()
            server._outlived_tool_calls.clear()
            server._set_editor_command()

    def test_tick_age_grows_while_the_editor_does_not_tick(self):
        with mock.patch.object(server, "_last_tick_mono", time.monotonic() - 42.0):
            self.assertGreaterEqual(server._activity_snapshot()["editor_tick_age_s"], 42.0)


class ExecFailureTests(unittest.TestCase):
    def test_raising_script_fails_with_its_output(self):
        response = server._execute_and_respond(
            "exec-1", "exec", {"code": "print('before')\nresult = 7\nraise ValueError('boom')"}
        )
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "exec raised ValueError: boom")
        self.assertEqual(response["stdout"], "before\n")
        self.assertEqual(response["result"], 7)
        self.assertIn("ValueError: boom", response["stderr"])
        self.assertIn("ValueError: boom", response["traceback"])
        self.assertEqual(response["route"], "python")

    def test_clean_script_still_succeeds(self):
        response = server._execute_and_respond("exec-2", "exec", {"code": "result = 2 + 2"})
        self.assertEqual((response["ok"], response["result"]["result"]), (True, 4))

    def test_batch_entry_keeps_exec_output(self):
        with mock.patch.object(server.unreal, "ScopedEditorTransaction",
                               lambda _name: contextlib.nullcontext(), create=True):
            batch = server._cmd_batch_exec([
                {"command": "exec", "params": {"code": "print('x')\nraise KeyError('k')"}},
            ])
        entry = batch["results"][0]
        self.assertEqual((entry["index"], entry["success"]), (0, False))
        self.assertEqual(entry["stdout"], "x\n")
        self.assertIn("KeyError", entry["error"])

    def test_client_error_carries_the_failure_envelope(self):
        client = UEFNBridge()
        envelope = {"ok": False, "route": "python", "error": "exec raised ValueError: boom",
                    "stdout": "before\n", "stderr": "", "traceback": "", "result": None}
        with mock.patch.object(client, "_post", return_value=envelope), \
                self.assertRaises(BridgeError) as caught:
            client.exec("raise ValueError('boom')")
        self.assertEqual(caught.exception.envelope["stdout"], "before\n")


if __name__ == "__main__":
    unittest.main()
