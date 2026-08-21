"""Tests for the public bridge client and hybrid routing helpers."""

from __future__ import annotations

import io
import socket
import unittest
import urllib.error
from unittest import mock

from bridge.client import BridgeError, ConnectionFailed, UEFNBridge


class FakeBridge(UEFNBridge):
    def __init__(self, response: dict):
        super().__init__()
        self.response = response
        self.sent_payload = None
        self.sent_timeout = None

    def _post(self, payload: dict, timeout: float = 0) -> dict:
        self.sent_payload = payload
        self.sent_timeout = timeout
        return self.response


class ClientTests(unittest.TestCase):
    def test_non_object_success_envelope_is_outcome_unknown(self):
        bridge = FakeBridge([])
        with self.assertRaises(BridgeError) as caught:
            bridge.run("entities.create")
        self.assertTrue(caught.exception.outcome_unknown)

    def test_non_boolean_ok_is_rejected_as_ambiguous(self):
        bridge = FakeBridge({"ok": "false", "result": {"unexpected": True}})
        with self.assertRaises(BridgeError) as caught:
            bridge.run("devices.place")
        self.assertTrue(caught.exception.outcome_unknown)

    def test_non_object_http_error_body_does_not_leak_attribute_error(self):
        bridge = UEFNBridge()
        error = urllib.error.HTTPError(
            bridge.base_url,
            500,
            "Server Error",
            {"Content-Type": "application/json"},
            io.BytesIO(b"[]"),
        )
        with mock.patch.object(bridge._opener, "open", side_effect=error):
            with self.assertRaises(BridgeError) as caught:
                bridge.run("entities.create")
        self.assertTrue(caught.exception.outcome_unknown)

    def test_non_string_error_is_rejected_as_ambiguous(self):
        bridge = FakeBridge({"ok": False, "error": []})
        with self.assertRaises(BridgeError) as caught:
            bridge.run("entities.create")
        self.assertTrue(caught.exception.outcome_unknown)

    def test_non_string_http_error_is_rejected_as_ambiguous(self):
        bridge = UEFNBridge()
        error = urllib.error.HTTPError(
            bridge.base_url,
            400,
            "Bad Request",
            {"Content-Type": "application/json"},
            io.BytesIO(b'{"error":[]}'),
        )
        with mock.patch.object(bridge._opener, "open", side_effect=error):
            with self.assertRaises(BridgeError) as caught:
                bridge.run("entities.create")
        self.assertTrue(caught.exception.outcome_unknown)

    def test_timeout_after_send_has_unknown_outcome(self):
        bridge = UEFNBridge(timeout=0.01)
        with mock.patch.object(
            bridge._opener,
            "open",
            side_effect=socket.timeout("timed out"),
        ):
            with self.assertRaises(BridgeError) as caught:
                bridge.run("entities.create")
        self.assertTrue(caught.exception.outcome_unknown)

    def test_connection_reset_after_send_has_unknown_outcome(self):
        bridge = UEFNBridge()
        failure = urllib.error.URLError(ConnectionResetError("reset"))
        with mock.patch.object(bridge._opener, "open", side_effect=failure):
            with self.assertRaises(BridgeError) as caught:
                bridge.run("devices.place")
        self.assertTrue(caught.exception.outcome_unknown)

    def test_connection_refused_is_known_not_dispatched(self):
        bridge = UEFNBridge()
        failure = urllib.error.URLError(ConnectionRefusedError("refused"))
        with mock.patch.object(bridge._opener, "open", side_effect=failure):
            with self.assertRaises(ConnectionFailed) as caught:
                bridge.run("actors.list")
        self.assertFalse(caught.exception.outcome_unknown)

    def test_loopback_bridge_client_disables_environment_proxies(self):
        bridge = UEFNBridge()
        proxy_handlers = [
            handler
            for handler in bridge._opener.handlers
            if isinstance(handler, urllib.request.ProxyHandler)
        ]
        self.assertEqual(proxy_handlers, [])

    def test_run_preserves_route_metadata(self):
        bridge = FakeBridge({
            "ok": True,
            "route": "toolset_registry",
            "result": {"returnValue": "Connected"},
        })

        result = bridge.run("session.status", timeout_s=12)

        self.assertEqual(result, {"returnValue": "Connected"})
        self.assertEqual(bridge.last_route, "toolset_registry")
        self.assertEqual(bridge.sent_payload["timeout_s"], 12)

    def test_failure_exposes_unknown_outcome(self):
        bridge = FakeBridge({
            "ok": False,
            "route": "toolset_registry",
            "error": "Call timed out and may still complete",
        })

        with self.assertRaises(BridgeError) as caught:
            bridge.run("entities.create")

        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual(caught.exception.route, "toolset_registry")

    def test_call_tool_uses_hybrid_command(self):
        bridge = FakeBridge({"ok": True, "route": "python", "result": {}})

        bridge.call_tool("Example.Tools", "Read", {"path": "x"})

        self.assertEqual(bridge.sent_payload["command"], "toolsets.call")
        self.assertEqual(
            bridge.sent_payload["params"],
            {
                "toolset_name": "Example.Tools",
                "tool_name": "Read",
                "arguments": {"path": "x"},
                "allow_mcp_fallback": True,
            },
        )

    def test_planned_route(self):
        self.assertEqual(UEFNBridge.planned_route("actors.list"), "python")
        self.assertEqual(
            UEFNBridge.planned_route("session.status"),
            "toolset_registry_then_mcp",
        )
        self.assertEqual(UEFNBridge.planned_route("mcp.call"), "mcp")


if __name__ == "__main__":
    unittest.main()
