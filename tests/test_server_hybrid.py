"""Unit tests for in-editor registry dispatch without requiring UEFN."""

from __future__ import annotations

import json
import os
import queue
import socket
import sys
import threading
import time
import types
import unittest
from unittest import mock


os.environ["UEFN_BRIDGE_NO_AUTOSTART"] = "1"
fake_unreal = sys.modules.get("unreal") or types.ModuleType("unreal")
fake_unreal.log = lambda *_: None
fake_unreal.log_error = lambda *_: None
fake_unreal.log_warning = lambda *_: None
sys.modules["unreal"] = fake_unreal

from bridge import server  # noqa: E402


class FakeAsyncResult:
    def __init__(self, *, complete=False, value="{}", error=""):
        self.is_complete = complete
        self.value = value
        self.error = error


class FakeRegistry:
    available = True
    registered = {"ValkyrieToolset.SessionToolset"}
    calls = []
    next_result = FakeAsyncResult()
    execute_error = None
    schemas = [
        {
            "name": "ValkyrieToolset.SessionToolset",
            "version": "1.0",
            "tools": [
                {"name": "GetSessionStatus", "inputSchema": {"type": "object"}},
                {"name": "UnapprovedTool", "inputSchema": {"type": "object"}},
            ],
        },
        {
            "name": "Hidden.EditorToolset",
            "version": "1.0",
            "tools": [{"name": "Dangerous", "inputSchema": {"type": "object"}}],
        },
    ]

    @classmethod
    def is_available(cls):
        return cls.available

    @classmethod
    def is_toolset_registered(cls, name):
        return name in cls.registered

    @classmethod
    def get_all_toolset_json_schemas(cls):
        return json.dumps(cls.schemas)

    @classmethod
    def execute_tool(cls, toolset_name, tool_name, json_input):
        cls.calls.append((toolset_name, tool_name, json.loads(json_input)))
        if cls.execute_error is not None:
            raise cls.execute_error
        return cls.next_result


class HybridServerTests(unittest.TestCase):
    def setUp(self):
        server.unreal.ToolsetRegistry = FakeRegistry
        FakeRegistry.available = True
        FakeRegistry.registered = {"ValkyrieToolset.SessionToolset"}
        FakeRegistry.calls = []
        FakeRegistry.next_result = FakeAsyncResult()
        FakeRegistry.execute_error = None
        server._toolset_schema_cache = None
        server._pending_tool_calls.clear()
        server._history.clear()
        server._mcp_client = None
        with server._results_lock:
            server._results.clear()
            server._abandoned_requests.clear()
        while True:
            try:
                server._work_queue.get_nowait()
            except queue.Empty:
                break

    def test_schema_discovery_filters_hidden_registry_toolsets(self):
        schemas = server._get_local_toolset_schemas()
        self.assertEqual(
            [item["name"] for item in schemas],
            ["ValkyrieToolset.SessionToolset"],
        )
        self.assertEqual(
            [tool["name"] for tool in schemas[0]["tools"]],
            ["GetSessionStatus"],
        )

    def test_allowed_tool_starts_locally_and_normalizes_name(self):
        pending = server._start_local_tool_call(
            "ValkyrieToolset.SessionToolset",
            "ValkyrieToolset.SessionToolset.GetSessionStatus",
            {},
        )
        self.assertIsInstance(pending, server._PendingRegistryCall)
        self.assertEqual(
            FakeRegistry.calls,
            [("ValkyrieToolset.SessionToolset", "GetSessionStatus", {})],
        )

    def test_unscoped_save_is_refused_before_any_dispatch(self):
        class RecordingClient:
            calls = []

            def call_tool(self, *args, **kwargs):
                self.calls.append(args)

        client = RecordingClient()
        server._mcp_client = client
        asset_tools = "editor_toolset.toolsets.asset.AssetTools"
        with self.assertRaises(ValueError):
            server._cmd_toolsets_call(asset_tools, "save_assets", {"asset_paths": []})
        with self.assertRaises(ValueError):
            server._cmd_mcp_call(asset_tools, "save_assets", {})
        self.assertEqual(FakeRegistry.calls, [])
        self.assertEqual(client.calls, [])

        server._cmd_mcp_call(asset_tools, "save_assets", {}, allow_save_all=True)
        self.assertEqual(len(client.calls), 1)

    def test_hidden_toolset_is_sent_to_policy_enforcing_mcp(self):
        result = server._start_local_tool_call(
            "Hidden.EditorToolset", "Dangerous", {}
        )
        self.assertIsInstance(result, server._MCPFallback)
        self.assertEqual(FakeRegistry.calls, [])

    def test_unapproved_tool_in_visible_toolset_never_executes_locally(self):
        result = server._start_local_tool_call(
            "ValkyrieToolset.SessionToolset", "UnapprovedTool", {}
        )
        self.assertIsInstance(result, server._MCPFallback)
        self.assertEqual(FakeRegistry.calls, [])

    def test_malformed_local_schema_fails_over_before_dispatch(self):
        original = FakeRegistry.schemas
        try:
            FakeRegistry.schemas = [
                {
                    "name": "ValkyrieToolset.SessionToolset",
                    "tools": None,
                }
            ]
            result = server._cmd_toolsets_list(refresh=True)
        finally:
            FakeRegistry.schemas = original
        self.assertIsInstance(result, server._MCPFallback)
        self.assertEqual(FakeRegistry.calls, [])

    def test_real_tool_error_is_not_retried(self):
        pending = server._PendingRegistryCall(
            FakeAsyncResult(complete=True, error="Invalid property value"),
            "ValkyrieToolset.SessionToolset",
            "StartSession",
            {},
        )
        outcome = pending.poll()
        self.assertIsInstance(outcome, dict)
        self.assertFalse(outcome["ok"])
        self.assertNotIsInstance(outcome, server._MCPFallback)

    def test_post_dispatch_capability_error_is_not_retried(self):
        pending = server._PendingRegistryCall(
            FakeAsyncResult(complete=True, error="Toolset is not registered"),
            "ValkyrieToolset.SessionToolset",
            "StartSession",
            {},
        )
        outcome = pending.poll()
        self.assertIsInstance(outcome, dict)
        self.assertFalse(outcome["ok"])
        self.assertNotIsInstance(outcome, server._MCPFallback)

    def test_execute_exception_is_unknown_and_never_falls_back(self):
        FakeRegistry.execute_error = RuntimeError("dispatch transport failed")
        with self.assertRaises(server._RegistryOutcomeUnknown) as caught:
            server._start_local_tool_call(
                "ValkyrieToolset.SessionToolset",
                "GetSessionStatus",
                {},
            )
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual(len(FakeRegistry.calls), 1)

    def test_async_result_property_failure_is_outcome_unknown(self):
        class BrokenAsyncResult:
            @property
            def is_complete(self):
                raise RuntimeError("poll failed")

        FakeRegistry.next_result = BrokenAsyncResult()
        pending = server._start_local_tool_call(
            "ValkyrieToolset.SessionToolset",
            "GetSessionStatus",
            {},
        )
        outcome = pending.poll()
        self.assertFalse(outcome["ok"])
        self.assertTrue(outcome["outcome_unknown"])

    def test_timeout_is_outcome_unknown_and_not_retried(self):
        pending = server._PendingRegistryCall(
            FakeAsyncResult(complete=False),
            "ValkyrieToolset.SessionToolset",
            "StartSession",
            {},
        )
        pending.deadline_mono = time.monotonic() - 1
        outcome = pending.poll()
        self.assertFalse(outcome["ok"])
        self.assertTrue(outcome["outcome_unknown"])
        self.assertIn("not retried", outcome["error"])

    def test_malformed_post_dispatch_result_has_unknown_outcome(self):
        pending = server._PendingRegistryCall(
            FakeAsyncResult(complete=True, value="not json"),
            "ValkyrieToolset.SessionToolset",
            "StartSession",
            {},
        )
        outcome = pending.poll()
        self.assertFalse(outcome["ok"])
        self.assertTrue(outcome["outcome_unknown"])

    def test_tick_polls_registry_result_without_busy_waiting(self):
        FakeRegistry.next_result = FakeAsyncResult(
            complete=True,
            value=json.dumps({"returnValue": "Connected"}),
        )
        rid = "test-request"
        server._work_queue.put((
            rid,
            "session.status",
            {},
            time.monotonic() + 5,
        ))

        server._tick(0.0)

        with server._results_lock:
            response = server._results.pop(rid)
        self.assertTrue(response["ok"])
        self.assertEqual(response["route"], "toolset_registry")
        self.assertEqual(response["result"]["returnValue"], "Connected")

    def test_tick_does_not_overlap_pending_registry_calls(self):
        first_result = FakeAsyncResult(complete=False)
        FakeRegistry.next_result = first_result
        deadline = time.monotonic() + 5
        server._work_queue.put(("first", "session.status", {}, deadline))
        server._work_queue.put(("second", "session.status", {}, deadline))

        server._tick(0.0)

        self.assertEqual(len(FakeRegistry.calls), 1)
        self.assertEqual(set(server._pending_tool_calls), {"first"})
        self.assertEqual(server._work_queue.qsize(), 1)

        first_result.is_complete = True
        first_result.value = json.dumps({"returnValue": "Connected"})
        server._tick(0.0)
        self.assertEqual(len(FakeRegistry.calls), 2)

    def test_stop_cancellation_prevents_queued_work_surviving_restart(self):
        server._tick_health = 4
        server._work_queue.put((
            "queued-request",
            "actors.delete",
            {"targets": ["Example"]},
            time.monotonic() + 30,
        ))
        pending = server._PendingRegistryCall(
            FakeAsyncResult(complete=False),
            "ValkyrieToolset.SessionToolset",
            "StartSession",
            {},
        )
        pending.command = "session.start"
        server._pending_tool_calls["pending-request"] = pending

        server._cancel_dispatch_state()

        self.assertTrue(server._work_queue.empty())
        self.assertEqual(server._pending_tool_calls, {})
        self.assertEqual(server._tick_health, 0)
        with server._results_lock:
            queued = server._results.pop("queued-request")
            active = server._results.pop("pending-request")
        self.assertFalse(queued["ok"])
        self.assertNotIn("outcome_unknown", queued)
        self.assertTrue(active["outcome_unknown"])

    def test_shutdown_and_reload_are_not_http_thread_commands(self):
        self.assertNotIn("shutdown", server._HTTP_THREAD_COMMANDS)
        self.assertNotIn("reload", server._HTTP_THREAD_COMMANDS)

    def test_editor_thread_reset_does_not_block_on_mcp_delete(self):
        closed = threading.Event()

        class SlowClient:
            def close(self, timeout=None):
                time.sleep(0.1)
                closed.set()

        server._mcp_client = SlowClient()
        started = time.monotonic()
        server._reset_mcp_client(close_async=True)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.05)
        self.assertTrue(closed.wait(1.0))

    def test_invalid_mcp_configuration_preserves_live_client(self):
        class ExistingClient:
            def __init__(self):
                self.closed = False

            def close(self, timeout=None):
                self.closed = True

        class RejectingClient:
            def __init__(self, **_kwargs):
                raise ValueError("invalid endpoint")

        existing = ExistingClient()
        original_url = server._mcp_url
        server._mcp_client = existing
        try:
            with mock.patch.object(
                server, "_mcp_client_type", return_value=RejectingClient
            ):
                with self.assertRaises(ValueError):
                    server._reset_mcp_client("http://bad.example/mcp")
            self.assertIs(server._mcp_client, existing)
            self.assertEqual(server._mcp_url, original_url)
            self.assertFalse(existing.closed)
        finally:
            server._mcp_client = None

    def test_dead_server_restart_cleans_stale_tick_and_queue(self):
        class OldServer:
            _uefn_bridge_accepting = True

            def __init__(self):
                self.closed = False

            def server_close(self):
                self.closed = True

        class DeadThread:
            @staticmethod
            def is_alive():
                return False

        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        old_server = OldServer()
        server._http = old_server
        server._http_thread = DeadThread()
        server._tick_handle = "old-tick"
        server._active_port = 9999
        server._work_queue.put((
            "stale-request",
            "actors.list",
            {},
            time.monotonic() + 30,
        ))
        unregistered = []

        def unregister(handle):
            unregistered.append(handle)

        try:
            with mock.patch.object(server, "_disable_background_throttle"), \
                 mock.patch.object(
                     server.unreal,
                     "unregister_slate_post_tick_callback",
                     side_effect=unregister,
                     create=True,
                 ), \
                 mock.patch.object(
                     server.unreal,
                     "register_slate_post_tick_callback",
                     return_value="new-tick",
                     create=True,
                 ):
                self.assertEqual(server.start(port=port, mode="tick"), port)
                self.assertFalse(old_server._uefn_bridge_accepting)
                self.assertTrue(old_server.closed)
                self.assertIn("old-tick", unregistered)
                self.assertTrue(server._work_queue.empty())
                server.stop()
                self.assertIn("new-tick", unregistered)
        finally:
            if server._http is not None:
                server.stop()


if __name__ == "__main__":
    unittest.main()
