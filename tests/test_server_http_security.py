"""HTTP boundary tests for localhost CSRF and request-size protections."""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import threading
import types
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer, ThreadingHTTPServer


os.environ["UEFN_BRIDGE_NO_AUTOSTART"] = "1"
fake_unreal = sys.modules.get("unreal") or types.ModuleType("unreal")
fake_unreal.log = getattr(fake_unreal, "log", lambda *_: None)
fake_unreal.log_error = getattr(fake_unreal, "log_error", lambda *_: None)
fake_unreal.log_warning = getattr(fake_unreal, "log_warning", lambda *_: None)
sys.modules["unreal"] = fake_unreal

from bridge import server  # noqa: E402


class HTTPBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.previous_http = server._http
        self.http = HTTPServer(("127.0.0.1", 0), server._Handler)
        self.http._uefn_bridge_accepting = True
        server._http = self.http
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.http.server_port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)
        server._http = self.previous_http

    def _open(self, request):
        try:
            response = self.opener.open(request, timeout=2)
            return response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def test_hostile_web_origin_is_rejected_before_body_dispatch(self):
        request = urllib.request.Request(
            self.url,
            data=b'{"command":"status"}',
            headers={
                "Content-Type": "text/plain",
                "Origin": "https://attacker.example",
            },
            method="POST",
        )
        status, headers, body = self._open(request)
        self.assertEqual(status, 403)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertIn("loopback", json.loads(body)["error"])

    def test_non_json_post_is_rejected(self):
        request = urllib.request.Request(
            self.url,
            data=b'{"command":"status"}',
            headers={"Content-Type": "text/plain"},
            method="POST",
        )
        status, _, _ = self._open(request)
        self.assertEqual(status, 415)

    def test_non_object_json_and_non_string_command_are_rejected(self):
        for body in (b"[]", b'{"command":[]}'):
            with self.subTest(body=body):
                request = urllib.request.Request(
                    self.url,
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                status, _, _ = self._open(request)
                self.assertEqual(status, 400)

    def test_loopback_origin_is_echoed_for_json_request(self):
        origin = "http://127.0.0.1:4173"
        request = urllib.request.Request(
            self.url,
            data=b'{"command":"status"}',
            headers={"Content-Type": "application/json", "Origin": origin},
            method="POST",
        )
        status, headers, body = self._open(request)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), origin)
        self.assertTrue(json.loads(body)["ok"])

    def test_oversized_body_is_rejected_without_reading_it(self):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.http.server_port, timeout=2
        )
        connection.request(
            "POST",
            "/",
            body=b"",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(server.MAX_REQUEST_BYTES + 1),
            },
        )
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 413)

    def test_stale_server_cannot_enqueue_after_stop_begins(self):
        first_check = threading.Event()
        calls = []
        original_put = server._work_queue.put
        original_check = server._Handler._server_accepts_dispatch
        original_accepting = self.http._uefn_bridge_accepting
        check_count = {"value": 0}

        def recording_put(item):
            calls.append(item)

        def tracked_check(handler):
            result = original_check(handler)
            check_count["value"] += 1
            if check_count["value"] == 1:
                first_check.set()
            return result

        payload = b'{"command":"actors.list"}'
        headers = (
            "POST / HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.http.server_port}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        connection = socket.create_connection(
            ("127.0.0.1", self.http.server_port), timeout=2
        )
        replacement = None
        replacement_thread = None
        try:
            server._work_queue.put = recording_put
            server._Handler._server_accepts_dispatch = tracked_check
            # Let the handler pass its entry check, then hold it in body read.
            connection.sendall(headers + payload[:1])
            self.assertTrue(first_check.wait(1.0))
            with server._http_lifecycle_lock:
                self.http._uefn_bridge_accepting = False

            # A replacement generation must remain responsive while the stale
            # handler is still blocked on its incomplete body.
            replacement = ThreadingHTTPServer(
                ("127.0.0.1", 0), server._Handler
            )
            replacement.daemon_threads = True
            replacement._uefn_bridge_accepting = True
            server._http = replacement
            replacement_thread = threading.Thread(
                target=replacement.serve_forever, daemon=True
            )
            replacement_thread.start()
            new_url = f"http://127.0.0.1:{replacement.server_port}"
            new_request = urllib.request.Request(
                new_url,
                data=b'{"command":"status"}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            new_status, _, new_body = self._open(new_request)
            self.assertEqual(new_status, 200)
            self.assertTrue(json.loads(new_body)["ok"])

            connection.sendall(payload[1:])
            response = http.client.HTTPResponse(connection)
            response.begin()
            body = response.read()
            status = response.status
        finally:
            connection.close()
            if replacement is not None:
                replacement.shutdown()
                replacement.server_close()
            if replacement_thread is not None:
                replacement_thread.join(timeout=2)
            server._http = self.http
            server._work_queue.put = original_put
            server._Handler._server_accepts_dispatch = original_check
            self.http._uefn_bridge_accepting = original_accepting

        self.assertEqual(status, 503)
        self.assertEqual(calls, [])
        self.assertGreaterEqual(check_count["value"], 2)
        self.assertIn("stopping", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()
