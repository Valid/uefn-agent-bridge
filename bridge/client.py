"""Lightweight Python client for the UEFN Python Bridge.

This runs *outside* the editor (your local machine, CI, AI agent, etc.)
and sends commands to the bridge server running inside UEFN.

Usage:
    from bridge.client import UEFNBridge

    ue = UEFNBridge()                    # connects to 127.0.0.1:9210
    print(ue.status())                   # health check
    print(ue.run("actors.list"))         # list actors
    print(ue.exec("result = 2 + 2"))     # run arbitrary python
    print(ue.run("actors.spawn", asset_path="/Engine/BasicShapes/Cube", location=[100, 0, 50]))

Docs: https://github.com/Valid/uefn-python-bridge
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Optional

try:
    from .routes import PYTHON_COMMANDS, TOOLSET_ROUTES, default_deadline_s
except ImportError:
    from routes import PYTHON_COMMANDS, TOOLSET_ROUTES, default_deadline_s


class BridgeError(Exception):
    """Raised when a bridge command fails.

    ``envelope`` is the complete failure response when the bridge sent one,
    for example the ``stdout`` and ``stderr`` of an ``exec`` that raised.
    """
    def __init__(
        self,
        message: str,
        traceback_text: str = "",
        route: str = "",
        outcome_unknown: bool = False,
        envelope: Optional[dict] = None,
    ):
        super().__init__(message)
        self.traceback_text = traceback_text
        self.route = route
        self.outcome_unknown = outcome_unknown
        self.envelope = envelope or {}


class BridgeBusy(BridgeError):
    """The bridge was busy and the request asked not to wait (``if_busy="reject"``).

    Nothing was started. ``busy`` describes what the bridge is doing.
    """
    def __init__(self, message: str, busy: dict, route: str = "", envelope: Optional[dict] = None):
        super().__init__(message, route=route, envelope=envelope)
        self.busy = busy


class ConnectionFailed(BridgeError):
    """Bridge server is not reachable."""
    pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UEFNBridge:
    """Send commands to the UEFN Python Bridge HTTP server."""

    def __init__(self, host: str = "127.0.0.1", port: int = 9210, timeout: float = 30.0):
        self.base_url = f"http://{host}:{port}"
        self.timeout = timeout
        self.last_response: Optional[dict] = None
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )

    def _post(self, payload: dict, timeout: float = 0) -> dict:
        data = json.dumps(payload).encode()
        request_timeout = timeout or self.timeout
        req = urllib.request.Request(
            self.base_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(req, timeout=request_timeout) as resp:
                decoded = json.loads(resp.read().decode())
                if not isinstance(decoded, dict):
                    raise BridgeError(
                        "Bridge returned a non-object response envelope",
                        outcome_unknown=True,
                    )
                return decoded
        except urllib.error.HTTPError as exc:
            try:
                decoded = json.loads(exc.read().decode())
                payload = decoded if isinstance(decoded, dict) else {}
            except Exception:
                payload = {}
            raw_message = payload.get("error", f"HTTP {exc.code}: {exc.reason}")
            malformed_error = not isinstance(raw_message, str)
            message = (
                f"HTTP {exc.code} returned a malformed error envelope"
                if malformed_error else raw_message
            )
            outcome_unknown = bool(payload.get("outcome_unknown")) or (
                "may still complete" in message.lower()
            ) or exc.code >= 500 or malformed_error
            traceback_text = payload.get("traceback", "")
            route = payload.get("route", "")
            route = route if isinstance(route, str) else ""
            if exc.code == 409 and isinstance(payload.get("busy"), dict) and not malformed_error:
                raise BridgeBusy(message, payload["busy"], route, payload) from exc
            raise BridgeError(
                message,
                traceback_text if isinstance(traceback_text, str) else "",
                route,
                outcome_unknown,
                payload,
            ) from exc
        except BridgeError:
            raise
        except urllib.error.URLError as exc:
            if "refused" in str(exc).lower() or "no connection" in str(exc).lower():
                raise ConnectionFailed(
                    "Bridge not running.  Start it inside UEFN: "
                    "Tools > Execute Python Script > bridge/server.py"
                ) from exc
            raise BridgeError(
                f"Bridge transport failed after the request may have been sent: {exc}",
                outcome_unknown=True,
            ) from exc
        except Exception as exc:
            if "timed out" in str(exc).lower():
                raise BridgeError(
                    f"Request timed out after {request_timeout}s",
                    outcome_unknown=True,
                ) from exc
            raise BridgeError(
                f"Bridge response failed after the request may have been sent: {exc}",
                outcome_unknown=True,
            ) from exc

    def request(
        self,
        command: str,
        *,
        timeout_s: float = 0,
        if_busy: str = "wait",
        **params: Any,
    ) -> dict:
        """Execute a command and return the complete response envelope.

        ``if_busy="reject"`` returns at once with :class:`BridgeBusy` instead of
        queueing behind a running command. Without ``timeout_s`` the bridge
        applies its own deadline (longer for session, push and build tools),
        and the client waits at least that long.
        """
        payload: dict = {"command": command, "params": params}
        if timeout_s > 0:
            payload["timeout_s"] = timeout_s
        if if_busy != "wait":
            payload["if_busy"] = if_busy
        deadline = timeout_s if timeout_s > 0 else default_deadline_s(command, params, 0)
        response_timeout = max(self.timeout, deadline + 5) if deadline > 0 else 0
        resp = self._post(payload, response_timeout)
        if not isinstance(resp, dict):
            raise BridgeError(
                "Bridge returned a non-object response envelope",
                outcome_unknown=True,
            )
        self.last_response = resp
        if not isinstance(resp.get("ok"), bool):
            raise BridgeError(
                "Bridge response envelope has no boolean 'ok' field",
                route=str(resp.get("route", "")),
                outcome_unknown=True,
            )
        route = resp.get("route", "")
        traceback_text = resp.get("traceback", "")
        if not isinstance(route, str) or not isinstance(traceback_text, str):
            raise BridgeError(
                "Bridge response envelope has invalid metadata fields",
                outcome_unknown=True,
            )
        if not resp["ok"]:
            message = resp.get("error", "Unknown error")
            if not isinstance(message, str):
                raise BridgeError(
                    "Bridge failure envelope has a non-string 'error' field",
                    route=route,
                    outcome_unknown=True,
                )
            raise BridgeError(
                message,
                traceback_text,
                route,
                bool(resp.get("outcome_unknown"))
                or "may still complete" in message.lower(),
                resp,
            )
        return resp

    def run(self, command: str, *, timeout_s: float = 0, if_busy: str = "wait", **params: Any) -> Any:
        """Execute a named bridge command with keyword params.

        Returns the 'result' dict on success; raises BridgeError on failure.
        """
        return self.request(command, timeout_s=timeout_s, if_busy=if_busy, **params).get("result")

    @property
    def last_route(self) -> str:
        """Route used by the latest response: python, toolset_registry, or mcp."""
        return (self.last_response or {}).get("route", "")

    @staticmethod
    def planned_route(command: str) -> str:
        """Explain the deterministic route without making a request."""
        if command in PYTHON_COMMANDS:
            return "python"
        if command in TOOLSET_ROUTES or command.startswith("toolsets."):
            return "toolset_registry_then_mcp"
        if command.startswith("mcp."):
            return "mcp"
        return "python"

    def exec(self, code: str, timeout_s: float = 0) -> dict:
        """Run arbitrary Python code inside UEFN.  Returns {result, stdout, stderr}.

        If the code raises, this raises BridgeError; its ``envelope`` still has
        ``stdout`` and ``stderr``.
        """
        return self.run("exec", code=code, timeout_s=timeout_s)

    def status(self) -> dict:
        """Check bridge health and list available commands."""
        return self.run("status")

    def batch(self, commands: list[dict]) -> dict:
        """Run multiple commands in a single tick.

        Each entry: {"command": "name", "params": {...}}
        """
        return self.run("batch.exec", commands=commands)

    def toolsets(self, refresh: bool = False, timeout_s: float = 30.0) -> dict:
        """List UEFN-approved native toolsets through the fastest route."""
        return self.run("toolsets.list", refresh=refresh, timeout_s=timeout_s)

    def describe_toolset(
        self,
        toolset_name: str,
        refresh: bool = False,
        timeout_s: float = 30.0,
    ) -> dict:
        """Get one UEFN-approved toolset schema."""
        return self.run(
            "toolsets.describe",
            toolset_name=toolset_name,
            refresh=refresh,
            timeout_s=timeout_s,
        )

    def call_tool(
        self,
        toolset_name: str,
        tool_name: str,
        arguments: Optional[dict] = None,
        *,
        allow_mcp_fallback: bool = True,
        timeout_s: float = 120.0,
    ) -> Any:
        """Use the local Toolset Registry first, then MCP on capability miss."""
        return self.run(
            "toolsets.call",
            toolset_name=toolset_name,
            tool_name=tool_name,
            arguments=arguments or {},
            allow_mcp_fallback=allow_mcp_fallback,
            timeout_s=timeout_s,
        )

    def mcp_call(
        self,
        toolset_name: str,
        tool_name: str,
        arguments: Optional[dict] = None,
        *,
        timeout_s: float = 120.0,
    ) -> Any:
        """Force one call through the official UEFN MCP HTTP server."""
        return self.run(
            "mcp.call",
            toolset_name=toolset_name,
            tool_name=tool_name,
            arguments=arguments or {},
            timeout_s=timeout_s,
        )

    # ── Convenience shortcuts ──────────────────────────────────────────

    def actors(self, class_filter: str = "") -> list:
        return self.run("actors.list", class_filter=class_filter).get("actors", [])

    def spawn(self, asset_path: str = "", actor_class: str = "", location: Optional[list] = None, **kw) -> dict:
        return self.run("actors.spawn", asset_path=asset_path, actor_class=actor_class, location=location, **kw)

    def assets(self, directory: str = "/Game/", **kw) -> list:
        return self.run("assets.list", directory=directory, **kw).get("assets", [])

    def level_info(self) -> dict:
        return self.run("level.info")

    def camera(self) -> dict:
        return self.run("viewport.camera")
