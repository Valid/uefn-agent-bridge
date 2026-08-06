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


class BridgeError(Exception):
    """Raised when a bridge command fails."""
    def __init__(self, message: str, traceback_text: str = ""):
        super().__init__(message)
        self.traceback_text = traceback_text


class ConnectionFailed(BridgeError):
    """Bridge server is not reachable."""
    pass


class UEFNBridge:
    """Send commands to the UEFN Python Bridge HTTP server."""

    def __init__(self, host: str = "127.0.0.1", port: int = 9210, timeout: float = 30.0):
        self.base_url = f"http://{host}:{port}"
        self.timeout = timeout

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
            with urllib.request.urlopen(req, timeout=request_timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.URLError as exc:
            if "refused" in str(exc).lower() or "no connection" in str(exc).lower():
                raise ConnectionFailed(
                    "Bridge not running.  Start it inside UEFN: "
                    "Tools > Execute Python Script > bridge/server.py"
                ) from exc
            raise BridgeError(f"HTTP error: {exc}") from exc
        except Exception as exc:
            if "timed out" in str(exc).lower():
                raise BridgeError(f"Request timed out after {request_timeout}s") from exc
            raise

    def run(self, command: str, *, timeout_s: float = 0, **params: Any) -> Any:
        """Execute a named bridge command with keyword params.

        Returns the 'result' dict on success; raises BridgeError on failure.
        """
        payload = {"command": command, "params": params}
        if timeout_s > 0:
            payload["timeout_s"] = timeout_s
        resp = self._post(payload, max(self.timeout, timeout_s + 5) if timeout_s > 0 else 0)
        if not resp.get("ok", False):
            raise BridgeError(
                resp.get("error", "Unknown error"),
                resp.get("traceback", ""),
            )
        return resp.get("result")

    def exec(self, code: str, timeout_s: float = 0) -> dict:
        """Run arbitrary Python code inside UEFN.  Returns {result, stdout, stderr}."""
        return self.run("exec", code=code, timeout_s=timeout_s)

    def status(self) -> dict:
        """Check bridge health and list available commands."""
        return self.run("status")

    def batch(self, commands: list[dict]) -> list[dict]:
        """Run multiple commands in a single tick.

        Each entry: {"command": "name", "params": {...}}
        """
        return self.run("batch.exec", commands=commands)

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
