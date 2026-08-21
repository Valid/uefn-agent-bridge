from __future__ import annotations

import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bridge.mcp_client import (
    MCPConfigurationError,
    MCPJSONRPCError,
    MCPOutcomeUnknownError,
    MCPTimeoutError,
    MCPToolError,
    PROTOCOL_VERSION,
    UnrealMCPClient,
    _looks_like_invalid_session,
    _parse_sse_stream,
    _parse_toolset_list_result,
)


_DROP_CONNECTION = object()


def _json_body(payload):
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _rpc_result(request_id, result):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    }


def _sse_body(payload):
    notification = {
        "jsonrpc": "2.0",
        "method": "notifications/progress",
        "params": {"progressToken": "test", "progress": 1},
    }
    return (
        ": keepalive\n\n"
        f"event: message\ndata: {json.dumps(notification)}\n\n"
        f"event: message\ndata: {json.dumps(payload)}\n\n"
    ).encode("utf-8")


class _Scenario:
    def __init__(self):
        self.records = []
        self.session_count = 0
        self.sse_methods = set()
        self.jsonrpc_error_method = None
        self.jsonrpc_error_message = "Bad request"
        self.jsonrpc_invalid_method_once = None
        self.jsonrpc_invalid_used = False
        self.invalid_method_once = None
        self.invalid_used = False
        self.malformed_call_once = False
        self.truncated_call_once = False
        self.drop_call_once = False
        self.delay_call_once = 0.0
        self.delay_initialize_once = 0.0
        self.tool_execution_count = 0

    def _response(self, request_id, result, method, extra_headers=None):
        payload = _rpc_result(request_id, result)
        headers = dict(extra_headers or {})
        if method in self.sse_methods:
            headers["Content-Type"] = "text/event-stream"
            return 200, headers, _sse_body(payload)
        headers["Content-Type"] = "application/json"
        return 200, headers, _json_body(payload)

    def dispatch(self, record):
        self.records.append(record)
        payload = record["payload"]
        method = payload.get("method")
        request_id = payload.get("id")

        if method == "initialize":
            if self.delay_initialize_once:
                delay = self.delay_initialize_once
                self.delay_initialize_once = 0.0
                time.sleep(delay)
            self.session_count += 1
            session_id = f"session-{self.session_count}"
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "unreal-mcp", "version": "test"},
            }
            return self._response(
                request_id,
                result,
                method,
                {"Mcp-Session-Id": session_id},
            )

        if method == "notifications/initialized":
            return 202, {"Content-Type": "application/json"}, b""

        if self.invalid_method_once == method and not self.invalid_used:
            self.invalid_used = True
            return 404, {"Content-Type": "text/plain"}, b"Invalid MCP session"

        if self.jsonrpc_error_method == method:
            error = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32602,
                    "message": self.jsonrpc_error_message,
                    "data": {"field": "value"},
                },
            }
            return 200, {"Content-Type": "application/json"}, _json_body(error)

        if (
            self.jsonrpc_invalid_method_once == method
            and not self.jsonrpc_invalid_used
        ):
            self.jsonrpc_invalid_used = True
            error = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32001,
                    "message": "Invalid MCP session",
                },
            }
            return 200, {"Content-Type": "application/json"}, _json_body(error)

        if method == "ping":
            return self._response(request_id, {}, method)

        if method == "tools/list":
            tools = [
                {"name": "list_toolsets", "inputSchema": {"type": "object"}},
                {"name": "describe_toolset", "inputSchema": {"type": "object"}},
                {"name": "call_tool", "inputSchema": {"type": "object"}},
            ]
            return self._response(request_id, {"tools": tools}, method)

        if method == "tools/call":
            meta_name = payload["params"]["name"]
            arguments = payload["params"]["arguments"]
            if meta_name == "call_tool":
                if self.delay_call_once:
                    delay = self.delay_call_once
                    self.delay_call_once = 0.0
                    time.sleep(delay)
                if self.drop_call_once:
                    self.drop_call_once = False
                    return _DROP_CONNECTION
                if self.malformed_call_once:
                    self.malformed_call_once = False
                    return 200, {"Content-Type": "application/json"}, b'{"jsonrpc":'
                if self.truncated_call_once:
                    self.truncated_call_once = False
                    return (
                        200,
                        {
                            "Content-Type": "application/json",
                            "X-Test-Declared-Length": "1000",
                        },
                        b'{"jsonrpc":"2.0"',
                    )

                self.tool_execution_count += 1
                tool_name = arguments["tool_name"]
                if tool_name == "fail":
                    result = {
                        "isError": True,
                        "content": [{"type": "text", "text": "Editor rejected it"}],
                    }
                elif tool_name == "plain_result":
                    result = {
                        "isError": False,
                        "content": [{"type": "text", "text": "plain text"}],
                    }
                elif tool_name == "multi_result":
                    result = {
                        "isError": False,
                        "content": [
                            {"type": "text", "text": '{"partial":true}'},
                            {"type": "text", "text": "second block"},
                        ],
                    }
                else:
                    nested = {
                        "toolset_name": arguments["toolset_name"],
                        "tool_name": tool_name,
                        "arguments": arguments["arguments"],
                    }
                    result = {
                        "isError": False,
                        "content": [{"type": "text", "text": json.dumps(nested)}],
                    }
            elif meta_name == "list_toolsets":
                result = {
                    "isError": False,
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "toolsets": [
                                        {
                                            "name": "editor.scene.SceneTools",
                                            "description": "Scene operations",
                                        }
                                    ]
                                }
                            ),
                        }
                    ],
                }
            elif meta_name == "describe_toolset":
                result = {
                    "isError": False,
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "toolset_name": arguments["toolset_name"],
                                    "tools": [{"name": "get_current_level"}],
                                }
                            ),
                        }
                    ],
                }
            else:
                raise AssertionError(f"Unexpected meta-tool {meta_name}")
            return self._response(request_id, result, method)

        raise AssertionError(f"Unexpected MCP method {method}")


class _FakeMCPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        record = {
            "http_method": "POST",
            "headers": {key: value for key, value in self.headers.items()},
            "payload": payload,
        }
        response = self.server.scenario.dispatch(record)
        if response is _DROP_CONNECTION:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            return
        status, headers, body = response
        self.send_response(status)
        declared_length = headers.pop("X-Test-Declared-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", declared_length)
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def do_DELETE(self):
        record = {
            "http_method": "DELETE",
            "headers": {key: value for key, value in self.headers.items()},
            "payload": None,
        }
        self.server.scenario.records.append(record)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def log_message(self, format, *args):
        pass


class UnrealMCPClientTests(unittest.TestCase):
    def test_sse_keepalives_cannot_extend_wall_clock_deadline(self):
        class KeepaliveStream:
            def readline(self):
                time.sleep(0.02)
                return b": keepalive\n"

        started = time.monotonic()
        with self.assertRaises(MCPTimeoutError):
            _parse_sse_stream(
                KeepaliveStream(),
                expected_id=1,
                deadline=time.monotonic() + 0.05,
            )
        self.assertLess(time.monotonic() - started, 0.15)

    def test_loopback_mcp_client_disables_environment_proxies(self):
        client = UnrealMCPClient(endpoint="http://127.0.0.1:1/mcp")
        proxy_handlers = [
            handler
            for handler in client._opener.handlers
            if isinstance(handler, urllib.request.ProxyHandler)
        ]
        self.assertEqual(proxy_handlers, [])

    def test_http_domain_session_error_is_not_an_mcp_session_rejection(self):
        self.assertFalse(
            _looks_like_invalid_session(400, b"Play session not found")
        )
        self.assertFalse(
            _looks_like_invalid_session(400, b"Play session ID not found")
        )
        self.assertTrue(
            _looks_like_invalid_session(400, b"Invalid MCP session ID")
        )

    def test_tools_list_changed_notification_invalidates_discovery_cache(self):
        client = UnrealMCPClient(endpoint="http://127.0.0.1:1/mcp")
        client._tools_cache = [{"name": "cached"}]
        client._toolsets_cached = True
        client._toolsets_cache = {"toolsets": ["cached"]}
        client._toolset_description_cache["cached"] = {"tools": []}
        result = client._response_result(
            [
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/tools/list_changed",
                },
                {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}},
            ],
            7,
        )
        self.assertEqual(result, {"ok": True})
        self.assertIsNone(client._tools_cache)
        self.assertFalse(client._toolsets_cached)
        self.assertEqual(client._toolset_description_cache, {})

    def test_markdown_toolset_list_is_normalized(self):
        result = _parse_toolset_list_result({
            "content": [{
                "type": "text",
                "text": (
                    "- Example.First: First description\n"
                    "  continues here.\n"
                    "- Example.Second: Second description\n"
                ),
            }]
        })
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["toolsets"][0], {
            "name": "Example.First",
            "description": "First description continues here.",
        })

    def setUp(self):
        self.scenario = _Scenario()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeMCPHandler)
        self.server.daemon_threads = True
        self.server.scenario = self.scenario
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.endpoint = f"http://{host}:{port}/mcp"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _client(self, timeout=1.0):
        return UnrealMCPClient(self.endpoint, timeout=timeout)

    def _records_for_method(self, method):
        return [
            record
            for record in self.scenario.records
            if record["payload"] and record["payload"].get("method") == method
        ]

    def _meta_records(self, name):
        return [
            record
            for record in self._records_for_method("tools/call")
            if record["payload"]["params"]["name"] == name
        ]

    def test_lazy_session_lifecycle_headers_and_delete(self):
        client = self._client()
        self.assertEqual([], self.scenario.records)

        tools = client.list_tools()
        self.assertEqual(
            ["list_toolsets", "describe_toolset", "call_tool"],
            [tool["name"] for tool in tools],
        )
        self.assertEqual("session-1", client.session_id)
        self.assertTrue(client.initialized)
        self.assertEqual("unreal-mcp", client.server_info["name"])

        methods = [
            record["payload"].get("method")
            for record in self.scenario.records
            if record["payload"]
        ]
        self.assertEqual(
            ["initialize", "notifications/initialized", "tools/list"],
            methods,
        )
        initialize_record = self._records_for_method("initialize")[0]
        initialized_record = self._records_for_method("notifications/initialized")[0]
        tools_record = self._records_for_method("tools/list")[0]
        self.assertNotIn("Mcp-Session-Id", initialize_record["headers"])
        self.assertEqual("session-1", initialized_record["headers"]["Mcp-Session-Id"])
        self.assertEqual("session-1", tools_record["headers"]["Mcp-Session-Id"])
        self.assertEqual(
            PROTOCOL_VERSION,
            tools_record["headers"]["Mcp-Protocol-Version"],
        )
        for record in self.scenario.records:
            self.assertNotIn("Origin", record["headers"])

        client.close()
        delete_record = self.scenario.records[-1]
        self.assertEqual("DELETE", delete_record["http_method"])
        self.assertEqual("session-1", delete_record["headers"]["Mcp-Session-Id"])
        self.assertIsNone(client.session_id)
        self.assertFalse(client.initialized)

    def test_discovery_results_are_cached_per_session(self):
        client = self._client()
        first_tools = client.list_tools()
        first_tools.append({"name": "mutated"})
        self.assertEqual(3, len(client.list_tools()))

        self.assertEqual(client.list_toolsets(), client.list_toolsets())
        name = "editor.scene.SceneTools"
        self.assertEqual(
            client.describe_toolset(name),
            client.describe_toolset(name),
        )
        self.assertEqual(1, len(self._records_for_method("tools/list")))
        self.assertEqual(1, len(self._meta_records("list_toolsets")))
        self.assertEqual(1, len(self._meta_records("describe_toolset")))

        client.list_toolsets(refresh=True)
        client.describe_toolset(name, refresh=True)
        self.assertEqual(2, len(self._meta_records("list_toolsets")))
        self.assertEqual(2, len(self._meta_records("describe_toolset")))

    def test_call_tool_keeps_exact_toolset_and_sends_short_tool_name(self):
        client = self._client()
        toolset = "editor_toolset.toolsets.scene.SceneTools"
        result = client.call_tool(
            toolset,
            f"{toolset}.get_current_level",
            {"include_hidden": False},
        )
        self.assertEqual(toolset, result["toolset_name"])
        self.assertEqual("get_current_level", result["tool_name"])
        self.assertEqual({"include_hidden": False}, result["arguments"])

        record = self._meta_records("call_tool")[0]
        envelope = record["payload"]["params"]["arguments"]
        self.assertEqual(toolset, envelope["toolset_name"])
        self.assertEqual("get_current_level", envelope["tool_name"])
        self.assertEqual({"include_hidden": False}, envelope["arguments"])

    def test_qualified_tool_name_must_match_exact_toolset_prefix(self):
        client = self._client()
        with self.assertRaises(MCPConfigurationError):
            client.call_tool(
                "editor.session.SessionTools",
                "other.session.SessionTools.StartSession",
            )
        with self.assertRaises(MCPConfigurationError):
            client.call_tool(
                "editor.session.SessionTools",
                "editor.session.SessionTools.Nested.StartSession",
            )
        self.assertEqual([], self.scenario.records)

    def test_nested_json_is_parsed_but_plain_and_multi_content_are_preserved(self):
        client = self._client()
        toolset = "example.Toolset"
        parsed = client.call_tool(toolset, "json_result")
        self.assertEqual("json_result", parsed["tool_name"])

        plain = client.call_tool(toolset, "plain_result")
        self.assertEqual("plain text", plain["content"][0]["text"])
        multi = client.call_tool(toolset, "multi_result")
        self.assertEqual(2, len(multi["content"]))

    def test_sse_response_can_precede_result_with_a_notification(self):
        self.scenario.sse_methods.add("tools/list")
        client = self._client()
        tools = client.list_tools()
        self.assertEqual(3, len(tools))

    def test_jsonrpc_and_tool_errors_are_typed(self):
        self.scenario.jsonrpc_error_method = "tools/list"
        client = self._client()
        with self.assertRaises(MCPJSONRPCError) as rpc_context:
            client.list_tools()
        self.assertEqual(-32602, rpc_context.exception.code)
        self.assertEqual({"field": "value"}, rpc_context.exception.data)
        self.assertEqual(1, len(self._records_for_method("tools/list")))

        self.scenario.jsonrpc_error_method = None
        with self.assertRaises(MCPToolError) as tool_context:
            client.call_tool("example.Toolset", "fail")
        self.assertIn("Editor rejected it", str(tool_context.exception))

    def test_explicit_invalid_session_reinitializes_and_replays_once(self):
        self.scenario.invalid_method_once = "tools/list"
        client = self._client()
        tools = client.list_tools()
        self.assertEqual(3, len(tools))
        self.assertEqual(2, self.scenario.session_count)
        self.assertEqual(2, len(self._records_for_method("tools/list")))
        self.assertEqual("session-2", client.session_id)

        request_headers = [
            record["headers"]["Mcp-Session-Id"]
            for record in self._records_for_method("tools/list")
        ]
        self.assertEqual(["session-1", "session-2"], request_headers)

    def test_jsonrpc_invalid_session_reinitializes_and_replays_once(self):
        self.scenario.jsonrpc_invalid_method_once = "tools/list"
        client = self._client()
        tools = client.list_tools()
        self.assertEqual(3, len(tools))
        self.assertEqual(2, self.scenario.session_count)
        self.assertEqual(2, len(self._records_for_method("tools/list")))

    def test_domain_session_error_is_not_mistaken_for_mcp_session_rejection(self):
        self.scenario.jsonrpc_error_method = "tools/call"
        self.scenario.jsonrpc_error_message = "Play session ID not found"
        client = self._client()
        with self.assertRaises(MCPJSONRPCError):
            client.call_tool("example.SessionTools", "start_session")
        self.assertEqual(1, len(self._meta_records("call_tool")))
        self.assertEqual(1, self.scenario.session_count)

    def test_timeouts_must_be_positive_and_finite(self):
        for value in (float("nan"), float("inf"), float("-inf"), 0.0, -1.0):
            with self.subTest(value=value):
                with self.assertRaises(MCPConfigurationError):
                    UnrealMCPClient(self.endpoint, timeout=value)

        client = self._client()
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(operation_timeout=value):
                with self.assertRaises(MCPConfigurationError):
                    client.initialize(timeout=value)
        self.assertEqual([], self.scenario.records)

    def test_invalid_session_clears_discovery_caches(self):
        client = self._client()
        client.list_toolsets()
        self.assertEqual(1, len(self._meta_records("list_toolsets")))

        self.scenario.invalid_method_once = "tools/list"
        client.list_tools(refresh=True)
        client.list_toolsets()
        self.assertEqual(2, len(self._meta_records("list_toolsets")))

    def test_call_tool_replays_only_after_explicit_invalid_session(self):
        self.scenario.invalid_method_once = "tools/call"
        client = self._client()
        result = client.call_tool("example.Toolset", "read_value")
        self.assertEqual("read_value", result["tool_name"])
        self.assertEqual(2, self.scenario.session_count)
        self.assertEqual(2, len(self._meta_records("call_tool")))
        self.assertEqual(1, self.scenario.tool_execution_count)

    def test_mutating_call_malformed_response_is_outcome_unknown_and_not_retried(self):
        self.scenario.malformed_call_once = True
        client = self._client()
        with self.assertRaises(MCPOutcomeUnknownError) as context:
            client.call_tool("example.Toolset", "mutate")
        self.assertTrue(context.exception.outcome_unknown)
        self.assertEqual(1, len(self._meta_records("call_tool")))
        self.assertEqual(1, self.scenario.session_count)

    def test_mutating_call_truncated_response_is_outcome_unknown_and_not_retried(self):
        self.scenario.truncated_call_once = True
        client = self._client()
        with self.assertRaises(MCPOutcomeUnknownError):
            client.call_tool("example.Toolset", "mutate")
        self.assertEqual(1, len(self._meta_records("call_tool")))
        self.assertEqual(1, self.scenario.session_count)

    def test_retry_safe_call_can_opt_in_to_one_ambiguous_retry(self):
        self.scenario.malformed_call_once = True
        client = self._client()
        result = client.call_tool(
            "example.Toolset",
            "read_value",
            retry_safe=True,
        )
        self.assertEqual("read_value", result["tool_name"])
        self.assertEqual(2, len(self._meta_records("call_tool")))
        self.assertEqual(2, self.scenario.session_count)
        self.assertEqual(1, self.scenario.tool_execution_count)

    def test_mutating_call_connection_drop_is_outcome_unknown_and_not_retried(self):
        self.scenario.drop_call_once = True
        client = self._client()
        with self.assertRaises(MCPOutcomeUnknownError):
            client.call_tool("example.Toolset", "mutate")
        self.assertEqual(1, len(self._meta_records("call_tool")))
        self.assertEqual(1, self.scenario.session_count)

    def test_mutating_call_timeout_is_outcome_unknown_and_not_retried(self):
        self.scenario.delay_call_once = 0.2
        client = self._client(timeout=0.05)
        with self.assertRaises(MCPOutcomeUnknownError):
            client.call_tool("example.Toolset", "mutate")
        time.sleep(0.25)
        self.assertEqual(1, len(self._meta_records("call_tool")))
        self.assertEqual(1, self.scenario.session_count)

    def test_http_error_body_timeout_preserves_outcome_unknown(self):
        client = self._client()
        client.initialize()

        class TimeoutBody:
            def read(self, _size=-1):
                raise socket.timeout("timed out reading error body")

            read1 = read

            def close(self):
                pass

        class ErrorOpener:
            def open(self, _request, timeout=None):
                raise urllib.error.HTTPError(
                    self.endpoint,
                    500,
                    "Server Error",
                    {"Content-Type": "text/plain"},
                    TimeoutBody(),
                )

        opener = ErrorOpener()
        opener.endpoint = self.endpoint
        client._opener = opener

        with self.assertRaises(MCPOutcomeUnknownError) as context:
            client.call_tool("example.Toolset", "mutate")
        self.assertTrue(context.exception.outcome_unknown)

    def test_one_deadline_covers_initialize_and_tool_call(self):
        self.scenario.delay_initialize_once = 0.06
        self.scenario.delay_call_once = 0.06
        client = self._client(timeout=0.1)
        started = time.monotonic()
        with self.assertRaises(MCPOutcomeUnknownError):
            client.call_tool("example.Toolset", "mutate")
        self.assertLess(time.monotonic() - started, 0.16)

    def test_read_only_discovery_may_retry_an_ambiguous_failure(self):
        original_response = self.scenario._response
        failures = {"count": 0}

        def malformed_once(request_id, result, method, extra_headers=None):
            if method == "tools/list" and failures["count"] == 0:
                failures["count"] += 1
                return 200, {"Content-Type": "application/json"}, b'{"jsonrpc":'
            return original_response(request_id, result, method, extra_headers)

        self.scenario._response = malformed_once
        client = self._client()
        tools = client.list_tools()
        self.assertEqual(3, len(tools))
        self.assertEqual(2, len(self._records_for_method("tools/list")))
        self.assertEqual(2, self.scenario.session_count)

    def test_remote_urls_are_rejected_by_default(self):
        with self.assertRaises(MCPConfigurationError):
            UnrealMCPClient("http://example.com/mcp")
        with self.assertRaises(ValueError):
            UnrealMCPClient("http://192.168.1.20:8000/mcp")

        client = UnrealMCPClient("https://example.com/mcp", allow_remote=True)
        self.assertEqual("https://example.com/mcp", client.endpoint)
        self.assertEqual(client.endpoint, client.url)


if __name__ == "__main__":
    unittest.main()
