"""UEFN Python Bridge - HTTP server that runs inside the UEFN editor.

Provides a local REST API so external tools (AI agents, scripts, CI pipelines)
can execute Python inside the editor.  All `unreal.*` calls are routed to the
main editor thread via Slate tick callbacks, keeping things thread-safe.

Start manually:
    Tools > Execute Python Script > select this file

Or auto-start:
    Copy bridge/ folder + startup.py to <Project>/Content/Python/

Docs & source: https://github.com/Valid/uefn-python-bridge
"""

from __future__ import annotations

import io
import ipaddress
import json
import os
import re
import tempfile

# Store the script path early. __file__ may be absent in Execute Python Script.
try:
    _SCRIPT_PATH = os.path.abspath(__file__)
except NameError:
    _SCRIPT_PATH = ""  # will be set by _discover_script_path()
import queue
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional

# Execute Python Script does not consistently add the script directory to
# sys.path. Resolve it before importing sibling bridge modules.
if not _SCRIPT_PATH:
    import inspect
    for fi in inspect.stack():
        if fi.filename and "server.py" in fi.filename and os.path.isfile(fi.filename):
            _SCRIPT_PATH = os.path.abspath(fi.filename)
            break
    if not _SCRIPT_PATH:
        for candidate in [
            os.path.join(
                os.path.expanduser("~"),
                ".uefn-copilot",
                "uefn-python-bridge",
                "bridge",
                "server.py",
            ),
        ]:
            if os.path.isfile(candidate):
                _SCRIPT_PATH = candidate
                break
if _SCRIPT_PATH:
    _BRIDGE_DIR = os.path.dirname(_SCRIPT_PATH)
    if _BRIDGE_DIR not in sys.path:
        sys.path.insert(0, _BRIDGE_DIR)

import unreal

try:
    from .routes import (
        TOOLSET_ROUTES,
        UEFN_EXPOSED_TOOLSETS,
        default_deadline_s,
        is_tool_allowed,
        short_tool_name,
    )
    from .uefn_tool_policy import UEFN_TOOL_POLICY_REVISION
except ImportError:
    # Execute Python Script loads this file outside the bridge package.
    from routes import (  # type: ignore
        TOOLSET_ROUTES,
        UEFN_EXPOSED_TOOLSETS,
        default_deadline_s,
        is_tool_allowed,
        short_tool_name,
    )
    from uefn_tool_policy import UEFN_TOOL_POLICY_REVISION  # type: ignore

# ── Configuration ──────────────────────────────────────────────────────────

PORT_RANGE_START = 9210
PORT_RANGE_END = 9220
WORKER_BATCH_SIZE = 8          # max commands per tick
REQUEST_TIMEOUT_S = 30.0
MCP_REQUEST_TIMEOUT_S = 120.0
MAX_REQUEST_BYTES = 16 * 1024 * 1024
POLL_SLEEP_S = 0.015           # ~66 Hz polling
HISTORY_CAP = 500              # max entries in command history
LOG_CAP = 300                  # max log ring entries
VERSION = "0.4.0"
DEFAULT_MCP_URL = os.environ.get(
    "UEFN_MCP_URL", "http://127.0.0.1:8000/mcp"
)

# ── Runtime state ──────────────────────────────────────────────────────────

_http: Optional[HTTPServer] = None
_http_thread: Optional[threading.Thread] = None
_tick_handle: Optional[object] = None
_active_port: int = 0

_work_queue: queue.Queue = queue.Queue()
_results: Dict[str, Any] = {}
_results_lock = threading.Lock()
_pending_tool_calls: Dict[str, Any] = {}
_abandoned_requests: set[str] = set()

_log_lines: List[str] = []
_history: List[dict] = []

_mcp_url: str = DEFAULT_MCP_URL
_mcp_client: Any = None
_mcp_client_lock = threading.Lock()
_toolset_schema_cache: Optional[List[dict]] = None
_request_serial_lock = threading.Lock()
_http_lifecycle_lock = threading.Lock()

# ── Logging ────────────────────────────────────────────────────────────────


def _log(msg: str, level: str = "info") -> None:
    ts = time.strftime("%H:%M:%S")
    entry = f"[Bridge {ts}] {msg}"
    _log_lines.append(entry)
    while len(_log_lines) > LOG_CAP:
        _log_lines.pop(0)
    {"error": unreal.log_error, "warn": unreal.log_warning}.get(
        level, unreal.log
    )(entry)


# ── Serialisation helpers ──────────────────────────────────────────────────


def to_json_safe(obj: Any) -> Any:
    """Recursively convert Unreal objects into JSON-friendly Python types."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [to_json_safe(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): to_json_safe(v) for k, v in obj.items()}
    # Geometry
    if isinstance(obj, unreal.Vector):
        return {"x": obj.x, "y": obj.y, "z": obj.z}
    if isinstance(obj, unreal.Rotator):
        return {"pitch": obj.pitch, "yaw": obj.yaw, "roll": obj.roll}
    if isinstance(obj, unreal.Vector2D):
        return {"x": obj.x, "y": obj.y}
    if isinstance(obj, unreal.Transform):
        return {
            "translation": to_json_safe(obj.translation),
            "rotation": to_json_safe(obj.rotation.rotator()),
            "scale": to_json_safe(obj.scale3d),
        }
    # Colour
    if isinstance(obj, (unreal.LinearColor, unreal.Color)):
        return {"r": obj.r, "g": obj.g, "b": obj.b, "a": obj.a}
    # Assets
    if isinstance(obj, unreal.AssetData):
        info: Dict[str, str] = {
            "asset_name": str(obj.asset_name),
            "package_name": str(obj.package_name),
            "package_path": str(obj.package_path),
        }
        if hasattr(obj, "asset_class_path"):
            info["asset_class"] = str(obj.asset_class_path.asset_name)
        elif hasattr(obj, "asset_class"):
            info["asset_class"] = str(obj.asset_class)
        return info
    # Generic unreal objects
    for attr in ("get_path_name", "get_name"):
        fn = getattr(obj, attr, None)
        if fn:
            return str(fn())
    try:
        return str(obj)
    except Exception:
        return repr(obj)


def _rotator(value: Any) -> unreal.Rotator:
    """Build a rotation from the bridge's wire format.

    Rotations arrive as ``[pitch, yaw, roll]`` in degrees, or as the
    ``{"pitch", "yaw", "roll"}`` object that responses return. The positional
    order of ``unreal.Rotator`` is (roll, pitch, yaw), so it is only ever
    constructed by keyword here.
    """
    if isinstance(value, dict):
        unknown = sorted(set(value) - {"pitch", "yaw", "roll"})
        if unknown:
            raise ValueError(f"rotation has unknown keys {unknown}; expected pitch, yaw, roll")
        pitch, yaw, roll = (float(value.get(key, 0.0)) for key in ("pitch", "yaw", "roll"))
    elif isinstance(value, (list, tuple)) and len(value) == 3:
        pitch, yaw, roll = (float(component) for component in value)
    else:
        raise ValueError(
            'rotation must be [pitch, yaw, roll] or {"pitch": ..., "yaw": ..., "roll": ...}'
        )
    return unreal.Rotator(roll=roll, pitch=pitch, yaw=yaw)


def actor_summary(actor: unreal.Actor) -> dict:
    """Return a lightweight dict describing an actor."""
    return {
        "name": actor.get_name(),
        "label": actor.get_actor_label(),
        "class": actor.get_class().get_name(),
        "path": actor.get_path_name(),
        "location": to_json_safe(actor.get_actor_location()),
        "rotation": to_json_safe(actor.get_actor_rotation()),
        "scale": to_json_safe(actor.get_actor_scale3d()),
    }


# ── Command registry ──────────────────────────────────────────────────────

_commands: Dict[str, Callable] = {}


def command(name: str):
    """Register a callable as a bridge command."""
    def _wrap(fn: Callable):
        _commands[name] = fn
        return fn
    return _wrap


class _CommandFailed(RuntimeError):
    """A command failure that carries extra fields for the response envelope."""

    def __init__(self, message: str, **fields: Any) -> None:
        super().__init__(message)
        self.fields = fields


def run_command(name: str, params: dict) -> dict:
    """Look up and execute a registered command (must be called on main thread)."""
    fn = _commands.get(name)
    if fn is None:
        raise KeyError(f"Unknown command '{name}'. Available: {sorted(_commands)}")
    return fn(**params)


def _schedule_editor_callback(
    callback: Callable[[], None],
    delay_s: float = 0.2,
) -> None:
    """Run a callback on a later editor tick after an HTTP response can flush."""
    earliest = time.monotonic() + delay_s
    handle_box: Dict[str, Any] = {"fired": False}

    def _deferred(_dt: float) -> None:
        if handle_box["fired"] or time.monotonic() < earliest:
            return
        handle_box["fired"] = True
        handle = handle_box.pop("handle", None)
        if handle is not None:
            try:
                unreal.unregister_slate_post_tick_callback(handle)
            except Exception:
                pass
        try:
            callback()
        except Exception as exc:
            _log(f"Deferred editor callback failed: {exc}", "error")
            _log(traceback.format_exc(), "error")

    handle_box["handle"] = unreal.register_slate_post_tick_callback(_deferred)


# -- Hybrid Toolset Registry / MCP routing ----------------------------------


class _LocalRegistryUnavailable(RuntimeError):
    """The embedded Toolset Registry cannot service a requested capability."""


class _RegistryOutcomeUnknown(RuntimeError):
    """A registry dispatch failed after execution may have started."""

    outcome_unknown = True


class _MCPFallback:
    """Signal the HTTP worker to run an operation through upstream UEFN MCP."""

    def __init__(
        self,
        operation: str,
        *,
        toolset_name: str = "",
        tool_name: str = "",
        arguments: Optional[dict] = None,
        reason: str = "",
    ) -> None:
        self.operation = operation
        self.toolset_name = toolset_name
        self.tool_name = tool_name
        self.arguments = arguments or {}
        self.reason = reason
        self.command = ""
        self.started_mono = time.monotonic()


class _PendingRegistryCall:
    """A native tool invocation that must be polled on later editor ticks."""

    def __init__(
        self,
        async_result: Any,
        toolset_name: str,
        tool_name: str,
        arguments: dict,
    ) -> None:
        self.async_result = async_result
        self.toolset_name = toolset_name
        self.tool_name = tool_name
        self.arguments = arguments
        self.command = ""
        self.started_mono = time.monotonic()
        self.deadline_mono = 0.0

    def poll(self) -> Optional[Any]:
        """Return a response/fallback when complete, otherwise None."""
        if self.deadline_mono and time.monotonic() >= self.deadline_mono:
            return {
                "ok": False,
                "route": "toolset_registry",
                "outcome_unknown": True,
                "error": (
                    f"Toolset call '{self.toolset_name}.{self.tool_name}' timed out. "
                    "It was not retried through MCP because the local mutation may "
                    "still complete."
                ),
            }

        try:
            is_complete = bool(self.async_result.is_complete)
        except Exception as exc:
            return {
                "ok": False,
                "route": "toolset_registry",
                "outcome_unknown": True,
                "error": f"Could not poll Toolset Registry result: {exc}",
            }
        if not is_complete:
            return None

        try:
            error = self.async_result.error
        except Exception as exc:
            return {
                "ok": False,
                "route": "toolset_registry",
                "outcome_unknown": True,
                "error": f"Could not read Toolset Registry error state: {exc}",
            }
        if error:
            error_text = str(error)
            return {
                "ok": False,
                "route": "toolset_registry",
                "error": error_text,
            }

        try:
            value = self.async_result.value
            parsed = json.loads(value) if isinstance(value, str) else to_json_safe(value)
        except Exception as exc:
            return {
                "ok": False,
                "route": "toolset_registry",
                "outcome_unknown": True,
                "error": (
                    f"{self.toolset_name}.{self.tool_name} returned invalid JSON: {exc}"
                ),
            }
        return {"ok": True, "route": "toolset_registry", "result": parsed}


def _registry() -> Any:
    registry = getattr(unreal, "ToolsetRegistry", None)
    if registry is None:
        raise _LocalRegistryUnavailable("unreal.ToolsetRegistry is not exposed")
    try:
        if not registry.is_available():
            raise _LocalRegistryUnavailable("Toolset Registry is not available")
    except AttributeError as exc:
        raise _LocalRegistryUnavailable(
            "Toolset Registry availability API is not exposed"
        ) from exc
    return registry


def _parse_toolset_schemas(raw: Any) -> List[dict]:
    """Normalize all observed registry schema container shapes."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Toolset Registry returned invalid schema JSON: {exc}") from exc

    if isinstance(raw, dict):
        if "name" in raw and "tools" in raw:
            raw = [raw]
        elif isinstance(raw.get("toolsets"), list):
            raw = raw["toolsets"]
        else:
            raw = list(raw.values())
    elif not isinstance(raw, (list, tuple)):
        try:
            raw = list(raw)
        except TypeError as exc:
            raise ValueError(
                f"Unexpected Toolset Registry schema container: {type(raw).__name__}"
            ) from exc

    schemas: List[dict] = []
    for item in raw:
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid toolset schema JSON: {exc}") from exc
        if isinstance(item, dict) and item.get("name"):
            schemas.append(item)
    return schemas


def _get_local_toolset_schemas(refresh: bool = False) -> List[dict]:
    """Read and cache native schemas. Must be called on the editor thread."""
    global _toolset_schema_cache
    if _toolset_schema_cache is not None and not refresh:
        return _toolset_schema_cache
    registry = _registry()
    getter = getattr(registry, "get_all_toolset_json_schemas", None)
    if not callable(getter):
        raise _LocalRegistryUnavailable(
            "Toolset Registry schema discovery API is not exposed"
        )
    try:
        discovered = _parse_toolset_schemas(getter())
    except Exception as exc:
        raise _LocalRegistryUnavailable(
            f"Toolset Registry schema discovery failed safely: {exc}"
        ) from exc
    filtered: List[dict] = []
    for item in discovered:
        toolset_name = item.get("name")
        if toolset_name not in UEFN_EXPOSED_TOOLSETS:
            continue
        tools = item.get("tools")
        if not isinstance(tools, list):
            raise _LocalRegistryUnavailable(
                f"Toolset '{toolset_name}' returned a malformed tools schema"
            )
        visible = dict(item)
        visible["tools"] = [
            tool for tool in tools
            if isinstance(tool, dict)
            and is_tool_allowed(toolset_name, tool.get("name", ""))
        ]
        filtered.append(visible)
    _toolset_schema_cache = filtered
    return _toolset_schema_cache


def _start_local_tool_call(
    toolset_name: str,
    tool_name: str,
    arguments: Optional[dict] = None,
    allow_mcp_fallback: bool = True,
) -> Any:
    """Start a native registry call without blocking the editor thread."""
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise TypeError("Tool arguments must be an object")
    try:
        encoded_arguments = json.dumps(arguments, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Tool arguments are not valid JSON: {exc}") from exc
    tool_name = short_tool_name(toolset_name, tool_name)
    try:
        if tool_name is None or not is_tool_allowed(toolset_name, tool_name):
            raise _LocalRegistryUnavailable(
                f"Tool '{toolset_name}.{tool_name or '<invalid>'}' is not approved "
                "for UEFN local routing"
            )
        schemas = _get_local_toolset_schemas()
        described = next(
            (item for item in schemas if item.get("name") == toolset_name),
            None,
        )
        if described is None or not any(
            short_tool_name(toolset_name, tool.get("name", "")) == tool_name
            for tool in described.get("tools", [])
            if isinstance(tool, dict)
        ):
            raise _LocalRegistryUnavailable(
                f"Tool '{toolset_name}.{tool_name}' is not exposed by the "
                "current UEFN Toolset Registry"
            )
        registry = _registry()
        is_registered = getattr(registry, "is_toolset_registered", None)
        if callable(is_registered) and not is_registered(toolset_name):
            raise _LocalRegistryUnavailable(
                f"Toolset '{toolset_name}' is not registered"
            )
    except _LocalRegistryUnavailable as exc:
        if allow_mcp_fallback:
            return _MCPFallback(
                "call",
                toolset_name=toolset_name,
                tool_name=tool_name,
                arguments=arguments,
                reason=str(exc),
            )
        raise
    except Exception as exc:
        if allow_mcp_fallback:
            return _MCPFallback(
                "call",
                toolset_name=toolset_name,
                tool_name=tool_name,
                arguments=arguments,
                reason=str(exc),
            )
        raise

    # execute_tool is the dispatch boundary. Nothing after this point may
    # switch transports because a mutation may already have started.
    try:
        async_result = registry.execute_tool(
            toolset_name, tool_name, encoded_arguments
        )
    except Exception as exc:
        raise _RegistryOutcomeUnknown(
            f"Toolset Registry dispatch for '{toolset_name}.{tool_name}' failed; "
            "the operation was not retried because it may still complete: "
            f"{exc}"
        ) from exc
    return _PendingRegistryCall(
        async_result, toolset_name, tool_name, arguments
    )


@command("toolsets.list")
def _cmd_toolsets_list(refresh: bool = False) -> Any:
    """List native editor toolsets through the fast in-process registry."""
    try:
        schemas = _get_local_toolset_schemas(refresh=refresh)
    except _LocalRegistryUnavailable as exc:
        return _MCPFallback("list", reason=str(exc))
    toolsets = [
        {
            "name": item.get("name", ""),
            "version": item.get("version", ""),
            "description": item.get("description", ""),
            "tool_count": len(item.get("tools", [])),
        }
        for item in schemas
    ]
    return {"toolsets": toolsets, "count": len(toolsets)}


@command("toolsets.describe")
def _cmd_toolsets_describe(toolset_name: str, refresh: bool = False) -> Any:
    """Return one native toolset's complete JSON schema."""
    try:
        schemas = _get_local_toolset_schemas(refresh=refresh)
    except _LocalRegistryUnavailable as exc:
        return _MCPFallback(
            "describe", toolset_name=toolset_name, reason=str(exc)
        )
    for schema in schemas:
        if schema.get("name") == toolset_name:
            return schema
    return _MCPFallback(
        "describe",
        toolset_name=toolset_name,
        reason=f"Toolset '{toolset_name}' is not registered locally",
    )


@command("toolsets.call")
def _cmd_toolsets_call(
    toolset_name: str,
    tool_name: str,
    arguments: Optional[dict] = None,
    allow_mcp_fallback: bool = True,
) -> Any:
    """Call a tool locally, falling back to UEFN MCP only if unavailable."""
    return _start_local_tool_call(
        toolset_name,
        tool_name,
        arguments,
        allow_mcp_fallback=allow_mcp_fallback,
    )


def _make_toolset_route(toolset_name: str, tool_name: str) -> Callable:
    def _route(allow_mcp_fallback: bool = True, **arguments: Any) -> Any:
        return _start_local_tool_call(
            toolset_name,
            tool_name,
            arguments,
            allow_mcp_fallback=allow_mcp_fallback,
        )

    _route.__name__ = f"_route_{tool_name}"
    _route.__doc__ = (
        f"Hybrid alias for {toolset_name}.{tool_name}. "
        "Uses the local registry before MCP."
    )
    return _route


for _alias, (_toolset_name, _tool_name) in TOOLSET_ROUTES.items():
    if _alias in _commands:
        raise RuntimeError(f"Hybrid route collides with command '{_alias}'")
    command(_alias)(_make_toolset_route(_toolset_name, _tool_name))


def _mcp_client_type() -> Any:
    try:
        from .mcp_client import UnrealMCPClient
    except ImportError:
        from mcp_client import UnrealMCPClient
    return UnrealMCPClient


def _get_mcp_client() -> Any:
    global _mcp_client
    with _mcp_client_lock:
        if _mcp_client is None:
            _mcp_client = _mcp_client_type()(
                endpoint=_mcp_url,
                timeout=MCP_REQUEST_TIMEOUT_S,
            )
        return _mcp_client


def _close_mcp_client_quietly(client: Any) -> None:
    try:
        client.close(timeout=3.0)
    except Exception:
        pass


def _close_mcp_client_serialized_quietly(client: Any) -> None:
    """Serialize shutdown DELETE with every upstream MCP request."""
    with _request_serial_lock:
        _close_mcp_client_quietly(client)


def _reset_mcp_client(url: str = "", close_async: bool = False) -> None:
    global _mcp_client, _mcp_url
    candidate = None
    if url:
        # Validate before changing the live client or endpoint.
        candidate = _mcp_client_type()(
            endpoint=url,
            timeout=MCP_REQUEST_TIMEOUT_S,
        )
    with _mcp_client_lock:
        previous = _mcp_client
        _mcp_client = candidate
        if candidate is not None:
            _mcp_url = candidate.endpoint
    if previous is not None:
        if close_async:
            threading.Thread(
                target=_close_mcp_client_serialized_quietly,
                args=(previous,),
                daemon=True,
            ).start()
        else:
            _close_mcp_client_quietly(previous)


def _execute_mcp_fallback(
    fallback: _MCPFallback,
    timeout_s: float = MCP_REQUEST_TIMEOUT_S,
) -> dict:
    """Run an upstream MCP request from the bridge HTTP worker thread."""
    try:
        client = _get_mcp_client()
        if fallback.operation == "list":
            result = client.list_toolsets(timeout=timeout_s)
        elif fallback.operation == "describe":
            result = client.describe_toolset(
                fallback.toolset_name, timeout=timeout_s
            )
        elif fallback.operation == "call":
            result = client.call_tool(
                fallback.toolset_name,
                fallback.tool_name,
                fallback.arguments,
                timeout=timeout_s,
            )
        else:
            raise ValueError(f"Unknown MCP fallback operation '{fallback.operation}'")
        response = {"ok": True, "route": "mcp", "result": result}
        if fallback.reason:
            response["fallback_reason"] = fallback.reason
        return response
    except Exception as exc:
        response = {
            "ok": False,
            "route": "mcp",
            "error": f"UEFN MCP fallback failed: {exc}",
        }
        if getattr(exc, "outcome_unknown", False):
            response["outcome_unknown"] = True
        if fallback.reason:
            response["fallback_reason"] = fallback.reason
        return response


@command("mcp.status")
def _cmd_mcp_status(_timeout_s: float = MCP_REQUEST_TIMEOUT_S) -> dict:
    """Initialize the upstream MCP session and report its negotiated state."""
    client = _get_mcp_client()
    result = client.initialize(timeout=_timeout_s)
    return {"endpoint": client.endpoint, "server": result}


@command("mcp.list_toolsets")
def _cmd_mcp_list_toolsets(
    refresh: bool = False,
    _timeout_s: float = MCP_REQUEST_TIMEOUT_S,
) -> Any:
    return _get_mcp_client().list_toolsets(
        refresh=refresh, timeout=_timeout_s
    )


@command("mcp.describe_toolset")
def _cmd_mcp_describe_toolset(
    toolset_name: str,
    refresh: bool = False,
    _timeout_s: float = MCP_REQUEST_TIMEOUT_S,
) -> Any:
    return _get_mcp_client().describe_toolset(
        toolset_name, refresh=refresh, timeout=_timeout_s
    )


@command("mcp.call")
def _cmd_mcp_call(
    toolset_name: str,
    tool_name: str,
    arguments: Optional[dict] = None,
    _timeout_s: float = MCP_REQUEST_TIMEOUT_S,
) -> Any:
    return _get_mcp_client().call_tool(
        toolset_name, tool_name, arguments, timeout=_timeout_s
    )


@command("mcp.configure")
def _cmd_mcp_configure(url: str) -> dict:
    """Set a loopback UEFN MCP endpoint and clear the current session."""
    _reset_mcp_client(url)
    return {"endpoint": _mcp_url}


@command("mcp.reset")
def _cmd_mcp_reset() -> dict:
    """Discard the upstream MCP session so the next call reinitializes it."""
    _reset_mcp_client()
    return {"endpoint": _mcp_url, "reset": True}


# ── Built-in commands ──────────────────────────────────────────────────────
# Each command returns a plain dict; the bridge JSON-encodes it for the caller.


@command("status")
def _cmd_status() -> dict:
    return {
        "bridge_version": VERSION,
        "python": sys.version.split()[0],
        "port": _active_port,
        "uptime_s": round(time.monotonic() - _start_mono, 1) if _start_mono else 0,
        "commands": sorted(_commands),
        "activity": _activity_snapshot(),
        "routing": {
            "strategy": ["python", "toolset_registry", "mcp"],
            "toolset_aliases": sorted(TOOLSET_ROUTES),
            "tool_policy_revision": UEFN_TOOL_POLICY_REVISION,
            "mcp_endpoint": _mcp_url,
        },
    }


@command("exec")
def _cmd_exec(code: str, transaction: str = "") -> dict:
    """Run arbitrary Python inside the editor.  Assign to `result` to return
    a value; use print() for stdout.

    If ``transaction`` is provided, the code runs inside an
    ``unreal.ScopedEditorTransaction`` so the entire operation is a single
    undo entry in the editor. The namespace includes ``clip_get``, ``clip_set``,
    ``export_t3d``, ``t3d_parse``, and ``find_actor`` for Verse device edits.

    If the code raises, the command fails (``ok: false``) and the response
    still carries ``result``, ``stdout`` and ``stderr``. Changes the code made
    before the exception are not rolled back.
    """
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    ns: Dict[str, Any] = {"__builtins__": __builtins__, "unreal": unreal, "result": None}
    # convenience subsystem refs
    for alias, cls_name in [
        ("actors", "EditorActorSubsystem"),
        ("assets", "EditorAssetSubsystem"),
        ("levels", "LevelEditorSubsystem"),
    ]:
        try:
            ns[alias] = unreal.get_editor_subsystem(getattr(unreal, cls_name))
        except Exception:
            pass
    ns.update({"clip_get": _clip_get, "clip_set": _clip_set, "export_t3d": _export_t3d_impl, "t3d_parse": _t3d_parse, "find_actor": _find_actor})
    raised: Optional[BaseException] = None
    script_traceback = ""
    try:
        sys.stdout, sys.stderr = out, err
        if transaction:
            with unreal.ScopedEditorTransaction(transaction):
                exec(code, ns)
        else:
            exec(code, ns)
    except Exception as exc:
        raised = exc
        script_traceback = traceback.format_exc()
        err.write(script_traceback)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    output = {
        "result": to_json_safe(ns.get("result")),
        "stdout": out.getvalue(),
        "stderr": err.getvalue(),
    }
    if raised is not None:
        raise _CommandFailed(
            f"exec raised {type(raised).__name__}: {raised}",
            traceback=script_traceback,
            **output,
        )
    return output


@command("log")
def _cmd_log(tail: int = 80) -> dict:
    return {"lines": _log_lines[-tail:]}


@command("history")
def _cmd_history(tail: int = 30) -> dict:
    return {"entries": _history[-tail:]}


@command("shutdown")
def _cmd_shutdown() -> dict:
    """Stop the bridge server.  Re-run the py command to start again."""
    _schedule_editor_callback(stop)
    return {"message": "Shutting down..."}


@command("reload")
def _cmd_reload() -> dict:
    """Stop the server, re-read the script from disk, and restart."""
    script_path = _SCRIPT_PATH
    if not script_path or not os.path.isfile(script_path):
        return {"error": f"Cannot find script path (got: {script_path!r}). Restart manually."}

    def _do_reload():
        stop()
        _log("Reloading from disk...")
        with open(script_path, "r") as f:
            code = f.read()
        exec(code, {"__file__": script_path, "__name__": "__reloaded__", "__builtins__": __builtins__})

    _schedule_editor_callback(_do_reload)
    return {"message": f"Reloading from {script_path}..."}


# ── Actor commands ─────────────────────────────────────────────────────────


def _get_actor_sub():
    return unreal.get_editor_subsystem(unreal.EditorActorSubsystem)


def _find_actor(identifier: str) -> unreal.Actor:
    """Resolve an actor by path, label, or internal name."""
    for a in _get_actor_sub().get_all_level_actors():
        if a.get_path_name() == identifier or a.get_actor_label() == identifier or a.get_name() == identifier:
            return a
    raise ValueError(f"Actor not found: {identifier}")


@command("actors.list")
def _cmd_actors_list(class_filter: str = "") -> dict:
    actors = _get_actor_sub().get_all_level_actors()
    if class_filter:
        actors = [a for a in actors if a.get_class().get_name() == class_filter]
    return {"actors": [actor_summary(a) for a in actors], "count": len(actors)}


@command("actors.selected")
def _cmd_actors_selected() -> dict:
    actors = _get_actor_sub().get_selected_level_actors()
    return {"actors": [actor_summary(a) for a in actors], "count": len(actors)}


@command("actors.spawn")
def _cmd_actors_spawn(
    asset_path: str = "",
    actor_class: str = "",
    class_name: str = "",
    location: Optional[List[float]] = None,
    rotation: Optional[List[float]] = None,
    label: str = "",
    transaction: str = "",
) -> dict:
    def _do():
        loc = unreal.Vector(*(location or [0, 0, 0]))
        rot = _rotator(rotation if rotation is not None else [0, 0, 0])
        actor = None
        # Accept class_name as alias for actor_class
        cls_name = actor_class or class_name
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        if asset_path:
            asset = unreal.load_asset(asset_path)
            if not asset:
                raise ValueError(f"Asset not found: {asset_path}")
            actor = sub.spawn_actor_from_object(asset, loc, rot)
        elif cls_name:
            cls = getattr(unreal, cls_name, None)
            if cls is None:
                raise ValueError(f"Class not found: {cls_name}")
            actor = sub.spawn_actor_from_class(cls, loc, rot)
        else:
            raise ValueError("Provide asset_path or actor_class")
        if actor is None:
            raise RuntimeError("Spawn failed")
        if label:
            actor.set_actor_label(label)
        return {"actor": actor_summary(actor)}
    txn = transaction or f"Spawn {actor_class or asset_path}"
    with unreal.ScopedEditorTransaction(txn):
        return _do()


@command("actors.duplicate")
def _cmd_actors_duplicate(
    source: str,
    location: Optional[List[float]] = None,
    rotation: Optional[List[float]] = None,
    label: str = "",
    transaction: str = "",
) -> dict:
    """Duplicate an existing actor (preserves mesh, materials, properties).
    source: label or path of actor to duplicate."""
    def _do():
        src = _find_actor(source)
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        # Duplicate via EditorActorSubsystem
        dup = sub.duplicate_actor(src)
        if dup is None:
            raise RuntimeError(f"Failed to duplicate {source}")
        if location:
            dup.set_actor_location(unreal.Vector(*location), False, False)
        if rotation:
            dup.set_actor_rotation(_rotator(rotation), False)
        if label:
            dup.set_actor_label(label)
        return {"actor": actor_summary(dup)}
    txn = transaction or f"Duplicate {source}"
    with unreal.ScopedEditorTransaction(txn):
        return _do()


@command("actors.delete")
def _cmd_actors_delete(targets: List[str], transaction: str = "") -> dict:
    txn = transaction or f"Delete {len(targets)} actor(s)"
    with unreal.ScopedEditorTransaction(txn):
        sub = _get_actor_sub()
        all_actors = sub.get_all_level_actors()
        removed: List[str] = []
        failed: List[dict] = []
        for t in targets:
            actor = next(
                (a for a in all_actors if a.get_path_name() == t or a.get_actor_label() == t),
                None,
            )
            if actor is None:
                failed.append({"target": t, "reason": "not found"})
            elif sub.destroy_actor(actor):
                removed.append(t)
            else:
                # destroy_actor reports failure by return value, not by raising.
                failed.append({"target": t, "reason": "destroy_actor returned False"})
    return {"deleted": removed, "count": len(removed), "failed": failed}


@command("actors.transform")
def _cmd_actors_transform(
    target: str,
    location: Optional[List[float]] = None,
    rotation: Optional[List[float]] = None,
    scale: Optional[List[float]] = None,
    transaction: str = "",
) -> dict:
    txn = transaction or f"Transform {target}"
    with unreal.ScopedEditorTransaction(txn):
        actor = _find_actor(target)
        if location is not None:
            actor.set_actor_location(unreal.Vector(*location), False, False)
        if rotation is not None:
            actor.set_actor_rotation(_rotator(rotation), False)
        if scale is not None:
            actor.set_actor_scale3d(unreal.Vector(*scale))
    return {"actor": actor_summary(actor)}


@command("actors.properties")
def _cmd_actors_properties(target: str, properties: List[str]) -> dict:
    actor = _find_actor(target)
    vals = {}
    for p in properties:
        try:
            vals[p] = to_json_safe(actor.get_editor_property(p))
        except Exception as exc:
            vals[p] = f"<error: {exc}>"
    return {"target": target, "properties": vals}


@command("actors.set_color")
def _cmd_actors_set_color(target: str, color: List[float], transaction: str = "") -> dict:
    """Set the color of an actor by creating a MaterialInstanceConstant and applying it.
    color is [r, g, b, a] with 0.0-1.0 range."""
    txn = transaction or f"Set color on {target}"
    with unreal.ScopedEditorTransaction(txn):
        actor = _find_actor(target)
        
        # Create unique material name from color
        color_hex = "".join(f"{int(c*255):02x}" for c in color[:3])
        mat_name = f"MI_{color_hex}"
        mat_path = f"/Game/Materials/{mat_name}"
        
        # Reuse existing material if same color was already created
        mic = None
        if unreal.EditorAssetLibrary.does_asset_exist(mat_path):
            mic = unreal.load_asset(mat_path)
        else:
            parent = unreal.load_asset("/Engine/EditorShapes/Materials/M_ShapeMaster")
            factory = unreal.MaterialInstanceConstantFactoryNew_BaseMaterial()
            asset_tools = unreal.AssetToolsHelpers.get_asset_tools()
            mic = asset_tools.create_asset(mat_name, "/Game/Materials", None, factory)
            mic.set_editor_property("parent", parent)
            
            param = unreal.VectorParameterValue()
            param.parameter_info = unreal.MaterialParameterInfo(name="ShapeColor")
            param.parameter_value = unreal.LinearColor(r=color[0], g=color[1], b=color[2], a=color[3] if len(color) > 3 else 1.0)
            mic.set_editor_property("vector_parameter_values", [param])
        
        # Apply to all material slots on all mesh components
        applied = 0
        for comp in actor.get_components_by_class(unreal.StaticMeshComponent):
            for i in range(comp.get_num_materials()):
                comp.set_material(i, mic)
                applied += 1
    
    return {"target": target, "color": color, "material": mat_path, "slots_applied": applied}


@command("actors.set_property")
def _cmd_actors_set_property(target: str, property_name: str, value: Any, transaction: str = "") -> dict:
    txn = transaction or f"Set {property_name} on {target}"
    with unreal.ScopedEditorTransaction(txn):
        actor = _find_actor(target)
        actor.set_editor_property(property_name, value)
    return {"target": target, "property": property_name, "value": to_json_safe(value)}


# ── Asset commands ─────────────────────────────────────────────────────────


@command("assets.list")
def _cmd_assets_list(
    directory: str = "/Game/",
    recursive: bool = True,
    class_filter: str = "",
) -> dict:
    paths = unreal.EditorAssetLibrary.list_assets(directory, recursive=recursive)
    if class_filter:
        filtered = []
        for p in paths:
            data = unreal.EditorAssetLibrary.find_asset_data(p)
            if data:
                cls = str(data.asset_class_path.asset_name) if hasattr(data, "asset_class_path") else str(getattr(data, "asset_class", ""))
                if cls == class_filter:
                    filtered.append(str(p))
        paths = filtered
    else:
        paths = [str(p) for p in paths]
    return {"assets": paths, "count": len(paths)}


@command("assets.info")
def _cmd_assets_info(path: str) -> dict:
    data = unreal.EditorAssetLibrary.find_asset_data(path)
    if data is None:
        raise ValueError(f"Asset not found: {path}")
    return {"asset": to_json_safe(data)}


@command("assets.exists")
def _cmd_assets_exists(path: str) -> dict:
    return {"exists": unreal.EditorAssetLibrary.does_asset_exist(path), "path": path}


@command("assets.rename")
def _cmd_assets_rename(old_path: str, new_path: str) -> dict:
    ok = unreal.EditorAssetLibrary.rename_asset(old_path, new_path)
    return {"success": ok, "old_path": old_path, "new_path": new_path}


@command("assets.duplicate")
def _cmd_assets_duplicate(source: str, destination: str) -> dict:
    result = unreal.EditorAssetLibrary.duplicate_asset(source, destination)
    return {"success": result is not None, "source": source, "destination": destination}


@command("assets.delete")
def _cmd_assets_delete(path: str) -> dict:
    ok = unreal.EditorAssetLibrary.delete_asset(path)
    return {"success": ok, "path": path}


@command("assets.save")
def _cmd_assets_save(path: str) -> dict:
    ok = unreal.EditorAssetLibrary.save_asset(path)
    return {"success": ok, "path": path}


@command("assets.search")
def _cmd_assets_search(
    query: str = "",
    class_name: str = "",
    directory: str = "/Game/",
    recursive: bool = True,
    limit: int = 25,
    spawnable_only: bool = True,
) -> dict:
    """Search assets by keyword. Returns spawnable BlueprintGeneratedClass assets by default.
    query: keyword to search in asset names (case-insensitive, supports multiple words)
    spawnable_only: if True, only return BlueprintGeneratedClass (props you can spawn)
    """
    reg = unreal.AssetRegistryHelpers.get_asset_registry()
    assets = reg.get_assets_by_path(directory, recursive=recursive)
    
    keywords = [w.lower() for w in query.split()] if query else []
    matches = []
    
    for a in assets:
        name = str(a.asset_name).lower()
        cls = str(a.asset_class_path.asset_name) if hasattr(a, "asset_class_path") else str(a.asset_class)
        
        # Filter by class if specified
        if class_name and class_name.lower() not in cls.lower():
            continue
        
        # Filter to spawnable props by default
        if spawnable_only and "BlueprintGeneratedClass" not in cls:
            continue
        
        # Match all keywords
        if keywords and not all(kw in name or kw in str(a.package_name).lower() for kw in keywords):
            continue
        
        path = str(a.package_name)
        matches.append({
            "name": str(a.asset_name),
            "path": path,
            "spawn_path": f"{path}.{str(a.asset_name)}",
            "class": cls,
        })
        if len(matches) >= limit:
            break
    
    return {"matches": matches, "count": len(matches), "query": query}


@command("assets.selected")
def _cmd_assets_selected() -> dict:
    sel = unreal.EditorUtilityLibrary.get_selected_assets()
    return {"assets": [to_json_safe(a) for a in sel], "count": len(sel)}


@command("assets.import_task")
def _cmd_assets_import(
    source_file: str,
    destination_path: str,
    replace_existing: bool = True,
    automated: bool = True,
    save: bool = True,
) -> dict:
    task = unreal.AssetImportTask()
    task.filename = source_file
    task.destination_path = destination_path
    task.replace_existing = replace_existing
    task.automated = automated
    task.save = save
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    imported = [str(p) for p in task.imported_object_paths] if hasattr(task, "imported_object_paths") else []
    return {"imported": imported, "source": source_file}


# ── Level commands ─────────────────────────────────────────────────────────


@command("level.info")
def _cmd_level_info() -> dict:
    world = unreal.EditorLevelLibrary.get_editor_world()
    actors = _get_actor_sub().get_all_level_actors()
    return {
        "world_name": world.get_name() if world else "None",
        "actor_count": len(actors),
    }


@command("level.save")
def _cmd_level_save() -> dict:
    ok = unreal.EditorLevelLibrary.save_current_level()
    return {"success": ok}


# ── Viewport commands ──────────────────────────────────────────────────────


@command("viewport.camera")
def _cmd_viewport_camera() -> dict:
    loc, rot = unreal.EditorLevelLibrary.get_level_viewport_camera_info()
    return {"location": to_json_safe(loc), "rotation": to_json_safe(rot)}


@command("viewport.set_camera")
def _cmd_viewport_set_camera(
    location: Optional[List[float]] = None,
    rotation: Optional[List[float]] = None,
) -> dict:
    cur_loc, cur_rot = unreal.EditorLevelLibrary.get_level_viewport_camera_info()
    loc = unreal.Vector(*location) if location else cur_loc
    rot = _rotator(rotation) if rotation else cur_rot
    unreal.EditorLevelLibrary.set_level_viewport_camera_info(loc, rot)
    return {"location": to_json_safe(loc), "rotation": to_json_safe(rot)}


# ── Material commands ──────────────────────────────────────────────────────


@command("materials.create_instance")
def _cmd_materials_create_instance(
    parent_path: str,
    instance_name: str,
    destination: str = "/Game/Materials",
    scalar_params: Optional[Dict[str, float]] = None,
    vector_params: Optional[Dict[str, List[float]]] = None,
    texture_params: Optional[Dict[str, str]] = None,
) -> dict:
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    factory = unreal.MaterialInstanceConstantFactoryNew()
    mi = tools.create_asset(instance_name, destination, unreal.MaterialInstanceConstant, factory)
    if not mi:
        raise RuntimeError(f"Failed to create material instance '{instance_name}'")
    parent = unreal.EditorAssetLibrary.load_asset(parent_path)
    if parent:
        mi.set_editor_property("parent", parent)
    if scalar_params:
        for k, v in scalar_params.items():
            unreal.MaterialEditingLibrary.set_material_instance_scalar_parameter_value(mi, k, v)
    if vector_params:
        for k, v in vector_params.items():
            col = unreal.LinearColor(*v) if len(v) == 4 else unreal.LinearColor(v[0], v[1], v[2], 1.0)
            unreal.MaterialEditingLibrary.set_material_instance_vector_parameter_value(mi, k, col)
    if texture_params:
        for k, v in texture_params.items():
            tex = unreal.EditorAssetLibrary.load_asset(v)
            if tex:
                unreal.MaterialEditingLibrary.set_material_instance_texture_parameter_value(mi, k, tex)
    return {"path": str(mi.get_path_name())}


# ── Verse device commands ──────────────────────────────────────────────────

_clip_ready = False


def _clip_init() -> None:
    global _clip_ready, ctypes, u32, k32, CF_UNICODETEXT, GMEM_MOVEABLE
    if _clip_ready:
        return
    import ctypes
    from ctypes import wintypes
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.GetClipboardData.restype = wintypes.HANDLE
    u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    u32.SetClipboardData.restype = wintypes.HANDLE
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    _clip_ready = True


def _open_clip_retry():
    _clip_init()
    for _ in range(40):
        if u32.OpenClipboard(0):
            return True
        time.sleep(0.05)
    return False


def _clip_get():
    _clip_init()
    if not _open_clip_retry():
        raise RuntimeError("OpenClipboard failed")
    try:
        h = u32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = k32.GlobalLock(h)
        try:
            return ctypes.wstring_at(p)
        finally:
            k32.GlobalUnlock(h)
    finally:
        u32.CloseClipboard()


def _clip_set(text):
    _clip_init()
    buf = ctypes.create_unicode_buffer(text)
    h = k32.GlobalAlloc(GMEM_MOVEABLE, ctypes.sizeof(buf))
    p = k32.GlobalLock(h)
    ctypes.memmove(p, buf, ctypes.sizeof(buf))
    k32.GlobalUnlock(h)
    if not _open_clip_retry():
        raise RuntimeError("OpenClipboard failed for set")
    try:
        u32.EmptyClipboard()
        if not u32.SetClipboardData(CF_UNICODETEXT, h):
            raise RuntimeError("SetClipboardData failed")
    finally:
        u32.CloseClipboard()


def _export_t3d_impl(actor, filename: str = "") -> str:
    sub = _get_actor_sub()
    previous_selection = sub.get_selected_level_actors()
    path = os.path.abspath(filename or os.path.join(
        tempfile.gettempdir(), f"uefn_t3d_{actor.get_name()}.t3d"
    )).replace("\\", "/")
    try:
        sub.set_selected_level_actors([actor])
        task = unreal.AssetExportTask()
        task.object = unreal.get_editor_subsystem(
            unreal.UnrealEditorSubsystem
        ).get_editor_world()
        task.filename = path
        task.selected = True
        task.automated = True
        task.replace_identical = True
        task.prompt = False
        unreal.Exporter.run_asset_export_task(task)
    finally:
        sub.set_selected_level_actors(previous_selection)
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        raise RuntimeError(f"T3D export failed or produced an empty file: {path}")
    return path


def _t3d_parse(text) -> list[dict]:
    blocks = []
    stack = []
    for line_index, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("Begin Object"):
            class_match = re.search(r"Class=([^\s]+)", stripped)
            name_match = re.search(r'Name="([^"]+)"', stripped)
            path_match = re.search(r'ExportPath="([^"]+)"', stripped)
            blocks.append({
                "class": class_match.group(1) if class_match else "",
                "name": name_match.group(1) if name_match else "",
                "export_path": path_match.group(1) if path_match else "",
                "start": line_index,
                "end": None,
                "parent": stack[-1] if stack else None,
            })
            stack.append(len(blocks) - 1)
        elif stripped.startswith("End Object") and stack:
            blocks[stack.pop()]["end"] = line_index
    return blocks


@command("verse.find_device")
def _cmd_verse_find_device(query: str = "", class_filter: str = "VerseDevice_C") -> dict:
    devices = []
    query_lower = query.lower()
    for actor in _get_actor_sub().get_all_level_actors():
        class_name = actor.get_class().get_name()
        if class_filter and class_name != class_filter:
            continue
        if query and actor.get_name() != query and query_lower not in actor.get_actor_label().lower():
            continue
        try:
            folder = str(actor.get_folder_path())
        except Exception:
            folder = ""
        devices.append({
            "name": actor.get_name(),
            "label": actor.get_actor_label(),
            "class": class_name,
            "path": actor.get_path_name(),
            "folder": folder,
        })
    return {"devices": devices, "count": len(devices)}


@command("verse.export_t3d")
def _cmd_verse_export_t3d(target: str, filename: str = "") -> dict:
    """Export one device. Parse the file programmatically, never dump raw T3D
    at the user; this is the only reliable read path for Verse strings and enums.
    """
    actor = _find_actor(target)
    path = _export_t3d_impl(actor, filename)
    return {"file": path, "size_bytes": os.path.getsize(path), "target": actor.get_name()}


@command("verse.get_properties")
def _cmd_verse_get_properties(path: str, properties: List[str]) -> dict:
    """Read subobject properties. VerseStringProperty fields always error here
    by design; read them via verse.export_t3d instead.
    """
    obj = unreal.load_object(None, path)
    if obj is None:
        raise ValueError(f"Object not found: {path}")
    values = {}
    for property_name in properties:
        try:
            values[property_name] = to_json_safe(obj.get_editor_property(property_name))
        except Exception as exc:
            values[property_name] = f"<error: {exc}>"
    return {"path": path, "class": obj.get_class().get_name(), "properties": values}


@command("verse.set_property")
def _cmd_verse_set_property(
    path: str,
    property_name: str,
    value: Any = None,
    is_object_ref: bool = False,
    transaction: str = "",
) -> dict:
    """Write numeric, object-reference, and object-reference-array properties.
    Verse strings, enums, and options need the clipboard T3D roundtrip via exec;
    see RULES.md.
    """
    obj = unreal.load_object(None, path)
    if obj is None:
        raise ValueError(f"Object not found: {path}")
    resolved_value = value
    if is_object_ref:
        resolved_value = unreal.load_object(None, value)
        if resolved_value is None:
            try:
                resolved_value = _find_actor(value)
            except Exception:
                resolved_value = None
        if resolved_value is None:
            raise ValueError(f"Reference not found: {value}")
    with unreal.ScopedEditorTransaction(transaction or f"Set {property_name}"):
        obj.modify()
        obj.set_editor_property(property_name, resolved_value)
    return {"path": path, "property": property_name, "value": to_json_safe(resolved_value)}


# ── Batch / utility commands ──────────────────────────────────────────────


@command("batch.exec")
def _cmd_batch_exec(commands: List[dict], transaction: str = "") -> dict:
    """Execute multiple commands in sequence within a single undo transaction."""
    # Coerce transaction to string (AI sometimes passes bool)
    txn = str(transaction) if transaction and not isinstance(transaction, str) else (transaction or f"Batch: {len(commands)} commands")
    results = []
    with unreal.ScopedEditorTransaction(txn):
        for i, item in enumerate(commands):
            name = item.get("command", "")
            params = item.get("params", {})
            params.pop("transaction", None)  # avoid nested transactions
            if (
                name.startswith("mcp.")
                or name.startswith("toolsets.")
                or name in TOOLSET_ROUTES
            ):
                results.append({
                    "index": i,
                    "success": False,
                    "error": (
                        f"'{name}' is asynchronous and cannot run inside "
                        "batch.exec. Call it separately."
                    ),
                })
                continue
            try:
                r = run_command(name, params)
                results.append({"index": i, "success": True, "result": r})
            except Exception as exc:
                failure = {"index": i, "success": False, "error": str(exc)}
                failure.update({
                    key: value for key, value in getattr(exc, "fields", {}).items()
                    if key not in failure
                })
                results.append(failure)
    return {"results": results, "count": len(results)}


@command("undo")
def _cmd_undo() -> dict:
    ok = unreal.EditorLoadingAndSavingUtils.unload_unused_assets if hasattr(unreal, "GEditor") else False
    # GEditor access varies; use the transaction system
    try:
        unreal.SystemLibrary.execute_console_command(None, "TRANSACTION UNDO")
        return {"success": True}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


@command("redo")
def _cmd_redo() -> dict:
    try:
        unreal.SystemLibrary.execute_console_command(None, "TRANSACTION REDO")
        return {"success": True}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


@command("fix_throttle")
def _cmd_fix_throttle() -> dict:
    """Disable 'Use Less CPU when in Background'; callable at any time."""
    _disable_background_throttle()
    return {"success": True}


@command("store.install")
def _cmd_store_install(slug: str, version: str = "latest") -> dict:
    """Download and install a script from uefn.sh into the current project.

    Args:
        slug: Script slug from uefn.sh (e.g. 'advanced-matchmaking')
        version: Version string to install, or 'latest' for the newest
    """
    import urllib.request
    import urllib.error
    import tempfile
    import zipfile

    STORE_API = "https://uefn.sh/api/bridge/install"

    # Get the project content directory
    project = unreal.ValkyrieProjectLibrary.get_main_project()
    content_dir = unreal.ValkyrieProjectLibrary.get_project_content_dir(project)
    if not content_dir:
        return {"ok": False, "error": "Could not determine project content directory"}

    # Request download URL from uefn.sh
    _log(f"[store] Requesting install: {slug} v{version}")
    req_data = json.dumps({"slug": slug, "version": version}).encode()
    req = urllib.request.Request(
        STORE_API,
        data=req_data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode())
            return {"ok": False, "error": err.get("error", f"HTTP {e.code}")}
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

    download_url = data.get("url")
    script_name = data.get("name", slug)
    version_str = data.get("version", version)
    if not download_url:
        return {"ok": False, "error": "No download URL returned"}

    # Download the zip
    _log(f"[store] Downloading {script_name} v{version_str}...")
    tmp_path = os.path.join(tempfile.gettempdir(), f"uefn_store_{slug}.zip")
    try:
        urllib.request.urlretrieve(download_url, tmp_path)
    except Exception as e:
        return {"ok": False, "error": f"Download failed: {e}"}

    # Extract to project content dir
    dest_folder = os.path.join(content_dir, "Scripts", slug)
    os.makedirs(dest_folder, exist_ok=True)

    try:
        with zipfile.ZipFile(tmp_path, "r") as zf:
            zf.extractall(dest_folder)
        file_count = len(os.listdir(dest_folder))
    except Exception as e:
        return {"ok": False, "error": f"Extraction failed: {e}"}
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    _log(f"[store] Installed {script_name} v{version_str} → {dest_folder} ({file_count} files)")
    return {
        "ok": True,
        "script": script_name,
        "version": version_str,
        "folder": dest_folder,
        "files": file_count,
    }


# ── HTTP layer ─────────────────────────────────────────────────────────────


class _Handler(BaseHTTPRequestHandler):
    """Minimal REST handler.  GET / for health; POST / to run commands."""

    def do_GET(self) -> None:
        if not self._origin_is_allowed():
            self._respond(403, json.dumps({
                "ok": False,
                "error": "Cross-origin requests must originate from loopback",
            }).encode())
            return
        body = json.dumps({
            "bridge": "uefn-python-bridge",
            "version": VERSION,
            "port": _active_port,
            "commands": sorted(_commands),
            "activity": _activity_snapshot(),
        }).encode()
        self._respond(200, body)

    def do_POST(self) -> None:
        # ThreadingHTTPServer lets shutdown proceed while a handler is slow.
        # Do not hold a process-global lock while reading an untrusted body.
        # Actual editor work and MCP network dispatch are serialized later.
        if not self._server_accepts_dispatch():
            self._respond_stopping()
            return
        self._handle_post()

    def _handle_post(self) -> None:
        if not self._origin_is_allowed():
            self._respond(403, json.dumps({
                "ok": False,
                "error": "Cross-origin requests must originate from loopback",
            }).encode())
            return
        content_type = self.headers.get("Content-Type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            self._respond(415, json.dumps({
                "ok": False,
                "error": "Content-Type must be application/json",
            }).encode())
            return
        if self.headers.get("Transfer-Encoding"):
            self._respond(400, json.dumps({
                "ok": False,
                "error": "Transfer-Encoding is not supported",
            }).encode())
            return
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._respond(411, json.dumps({
                "ok": False,
                "error": "A valid Content-Length header is required",
            }).encode())
            return
        if content_length < 1:
            self._respond(400, json.dumps({
                "ok": False,
                "error": "Request body must not be empty",
            }).encode())
            return
        if content_length > MAX_REQUEST_BYTES:
            self._respond(413, json.dumps({
                "ok": False,
                "error": f"Request body exceeds {MAX_REQUEST_BYTES} bytes",
            }).encode())
            return
        try:
            raw = self.rfile.read(content_length)
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            self._respond(400, json.dumps({"ok": False, "error": f"Bad JSON: {exc}"}).encode())
            return

        if not isinstance(payload, dict):
            self._respond(400, json.dumps({
                "ok": False,
                "error": "Request JSON must be an object",
            }).encode())
            return

        cmd = payload.get("command", "")
        params = payload.get("params", {})
        timeout_s = payload.get("timeout_s")
        if_busy = payload.get("if_busy", "wait")
        if not isinstance(cmd, str) or not cmd:
            self._respond(400, json.dumps({
                "ok": False,
                "error": "'command' must be a non-empty string",
            }).encode())
            return
        if not isinstance(params, dict):
            self._respond(400, json.dumps({"ok": False, "error": "'params' must be an object"}).encode())
            return
        if if_busy not in ("wait", "reject"):
            self._respond(400, json.dumps({
                "ok": False,
                "error": "'if_busy' must be 'wait' or 'reject'",
            }).encode())
            return
        # Without an explicit timeout_s, slow native tools (session start, push,
        # Verse build) get a deadline that fits them; everything else gets 30 s.
        request_timeout_s = (
            max(1.0, min(600.0, float(timeout_s)))
            if isinstance(timeout_s, (int, float))
            else default_deadline_s(cmd, params, REQUEST_TIMEOUT_S)
        )

        rid = uuid.uuid4().hex
        deadline = time.monotonic() + request_timeout_s

        # Pure status data can remain responsive while an editor tool is active.
        if cmd in _UNSERIALIZED_HTTP_COMMANDS:
            if not self._reserve_dispatch():
                self._respond_stopping()
                return
            resp = _execute_and_respond(rid, cmd, dict(params))
            self._respond(200, json.dumps(resp).encode())
            return

        if if_busy == "reject":
            # Fail fast instead of queueing: nothing was started.
            if not _request_serial_lock.acquire(blocking=False):
                self._respond(409, json.dumps({
                    "ok": False,
                    "route": _command_route(cmd),
                    "busy": _activity_snapshot(),
                    "error": f"'{cmd}' was not started because the bridge is busy",
                }).encode())
                return
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not _wait_for_turn(remaining):
                self._respond(200, json.dumps({
                    "ok": False,
                    "route": _command_route(cmd),
                    "error": f"'{cmd}' expired before serialized dispatch",
                }).encode())
                return
        _set_dispatch_turn(cmd)
        try:
            if not self._reserve_dispatch():
                status, resp = 503, {
                    "ok": False,
                    "error": "Bridge server is stopping",
                }
            else:
                status, resp = self._dispatch_serialized(
                    rid, cmd, params, deadline, request_timeout_s
                )
        finally:
            _set_dispatch_turn()
            _request_serial_lock.release()
        try:
            self._respond(status, json.dumps(resp).encode())
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _dispatch_serialized(
        self,
        rid: str,
        cmd: str,
        params: dict,
        deadline: float,
        request_timeout_s: float,
    ) -> tuple[int, dict]:
        """Run one editor or MCP command while the global dispatch lock is held."""
        if cmd in _HTTP_THREAD_COMMANDS:
            direct_params = dict(params)
            if cmd in _MCP_NETWORK_COMMANDS:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return 200, {
                        "ok": False,
                        "route": "mcp",
                        "error": f"'{cmd}' expired before MCP dispatch",
                    }
                direct_params.setdefault("_timeout_s", remaining)
            resp = _execute_and_respond(rid, cmd, direct_params)
            if isinstance(resp, (_MCPFallback, _PendingRegistryCall)):
                resp = {
                    "ok": False,
                    "route": "python",
                    "error": f"'{cmd}' returned an invalid deferred result",
                }
            return 200, resp

        # Unreal-API commands run on the main (game) thread via the tick queue.
        if not self._enqueue_if_active((rid, cmd, params, deadline)):
            return 503, {
                "ok": False,
                "error": "Bridge server is stopping",
            }

        while time.monotonic() < deadline:
            completed = None
            with _results_lock:
                if rid in _results:
                    completed = _results.pop(rid)
            if completed is not None:
                if isinstance(completed, _MCPFallback):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        resp = {
                            "ok": False,
                            "route": "toolset_registry",
                            "error": (
                                f"'{completed.command or cmd}' exhausted its "
                                "deadline before MCP fallback dispatch"
                            ),
                        }
                    else:
                        if not self._reserve_dispatch():
                            resp = {
                                "ok": False,
                                "route": "mcp",
                                "error": "Bridge server stopped before MCP dispatch",
                            }
                        else:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                resp = {
                                    "ok": False,
                                    "route": "mcp",
                                    "error": (
                                        f"'{completed.command or cmd}' exhausted "
                                        "its deadline before MCP dispatch"
                                    ),
                                }
                            else:
                                resp = _execute_mcp_fallback(
                                    completed,
                                    timeout_s=remaining,
                                )
                    _record_history(
                        completed.command or cmd,
                        completed.started_mono,
                        resp,
                    )
                else:
                    resp = completed
                return 200, resp
            time.sleep(POLL_SLEEP_S)
        with _results_lock:
            _abandoned_requests.add(rid)
            _results.pop(rid, None)
        return 504, {
            "ok": False,
            "route": _command_route(cmd),
            "outcome_unknown": True,
            "error": (
                f"'{cmd}' timed out ({request_timeout_s}s). The operation may "
                "still complete if it had already started; it was not retried."
            ),
        }

    def _server_accepts_dispatch(self) -> bool:
        return (
            self.server is _http
            and bool(getattr(self.server, "_uefn_bridge_accepting", False))
        )

    def _reserve_dispatch(self) -> bool:
        """Commit a worker-thread command before stop marks this server stale."""
        with _http_lifecycle_lock:
            return self._server_accepts_dispatch()

    def _enqueue_if_active(self, item: tuple) -> bool:
        """Atomically validate this server generation and publish editor work."""
        with _http_lifecycle_lock:
            if not self._server_accepts_dispatch():
                return False
            _work_queue.put(item)
            return True

    def _respond_stopping(self) -> None:
        self._respond(503, json.dumps({
            "ok": False,
            "error": "Bridge server is stopping",
        }).encode())

    # Allow cross-origin for web-based tools
    def do_OPTIONS(self) -> None:
        if not self._origin_is_allowed():
            self._respond(403, json.dumps({
                "ok": False,
                "error": "Cross-origin requests must originate from loopback",
            }).encode())
            return
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def _respond(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin and self._origin_is_allowed():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _origin_is_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            parsed = urllib.parse.urlsplit(origin)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                return False
            host = parsed.hostname.rstrip(".").lower()
            if host == "localhost":
                return True
            return ipaddress.ip_address(host).is_loopback
        except (TypeError, ValueError):
            return False

    def log_message(self, *_: Any) -> None:
        pass  # silence default stderr spam


# ── Command execution ──────────────────────────────────────────────────────

_start_mono: float = 0
_dispatch_mode: str = "tick"
_tick_health: int = 0           # incremented by tick callback

# What the bridge is doing, readable from HTTP threads while a command runs.
_activity_lock = threading.Lock()
_last_tick_mono: float = 0.0          # when the editor last ran _tick
_editor_command: Optional[dict] = None   # {"command", "state", "since"} on the editor thread
_dispatch_turn: Optional[dict] = None    # {"command", "since"}: the request whose turn it is
_waiting_requests: int = 0            # requests queued behind that turn


def _set_editor_command(command: str = "", state: str = "running", since: float = 0.0) -> None:
    global _editor_command
    with _activity_lock:
        _editor_command = (
            {"command": command, "state": state, "since": since or time.monotonic()}
            if command else None
        )


def _set_dispatch_turn(command: str = "") -> None:
    global _dispatch_turn
    with _activity_lock:
        _dispatch_turn = {"command": command, "since": time.monotonic()} if command else None


def _wait_for_turn(timeout_s: float) -> bool:
    """Wait for the one-at-a-time dispatch turn, counted in ``waiting``."""
    global _waiting_requests
    with _activity_lock:
        _waiting_requests += 1
    try:
        return _request_serial_lock.acquire(timeout=timeout_s)
    finally:
        with _activity_lock:
            _waiting_requests -= 1


def _activity_snapshot() -> dict:
    """Report busy versus stalled without waiting for the editor.

    ``editor_tick_age_s`` grows while the editor's game thread is blocked. If
    ``editor_command`` is set, a bridge command is the reason; if it is empty,
    something outside the bridge holds the editor (a modal dialog, a build,
    a hang). ``dispatch_turn`` is the request being served one at a time, and
    ``waiting`` counts the requests queued behind it.
    """
    now = time.monotonic()
    with _activity_lock:
        editor = dict(_editor_command) if _editor_command else None
        turn = dict(_dispatch_turn) if _dispatch_turn else None
        waiting = _waiting_requests
        last_tick = _last_tick_mono
    if editor:
        editor = {"command": editor["command"], "state": editor["state"],
                  "seconds": round(now - editor["since"], 1)}
    if turn:
        turn = {"command": turn["command"], "seconds": round(now - turn["since"], 1)}
    return {
        "editor_command": editor,
        "dispatch_turn": turn,
        "waiting": waiting,
        "editor_tick_age_s": round(now - last_tick, 2) if last_tick else None,
    }

# Commands that are safe on the HTTP worker because they make no unreal calls.
_HTTP_THREAD_COMMANDS = frozenset({
    "status",
    "log",
    "history",
    "mcp.status",
    "mcp.list_toolsets",
    "mcp.describe_toolset",
    "mcp.call",
    "mcp.configure",
    "mcp.reset",
})
_MCP_NETWORK_COMMANDS = frozenset({
    "mcp.status",
    "mcp.list_toolsets",
    "mcp.describe_toolset",
    "mcp.call",
})
_UNSERIALIZED_HTTP_COMMANDS = frozenset({
    "status",
    "log",
    "history",
})


def _command_route(cmd: str) -> str:
    if cmd.startswith("mcp."):
        return "mcp"
    if cmd.startswith("toolsets.") or cmd in TOOLSET_ROUTES:
        return "toolset_registry"
    return "python"


def _record_history(cmd: str, started_mono: float, resp: dict) -> None:
    elapsed_ms = round((time.monotonic() - started_mono) * 1000, 1)
    entry = {
        "command": cmd,
        "route": resp.get("route", _command_route(cmd)),
        "elapsed_ms": elapsed_ms,
        "ok": resp.get("ok", False),
    }
    if resp.get("fallback_reason"):
        entry["fallback_reason"] = resp["fallback_reason"]
    _history.append(entry)
    while len(_history) > HISTORY_CAP:
        _history.pop(0)


def _execute_and_respond(rid: str, cmd: str, params: dict) -> Any:
    """Execute a command and return the response dict.  Thread-safe logging."""
    t0 = time.monotonic()
    try:
        data = run_command(cmd, params)
        if isinstance(data, (_PendingRegistryCall, _MCPFallback)):
            data.command = cmd
            data.started_mono = t0
            return data
        resp = {"ok": True, "route": _command_route(cmd), "result": data}
    except Exception as exc:
        _log(f"Command '{cmd}' failed: {exc}", "error")
        resp = {
            "ok": False,
            "route": _command_route(cmd),
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        if getattr(exc, "outcome_unknown", False):
            resp["outcome_unknown"] = True
        resp.update({
            key: value for key, value in getattr(exc, "fields", {}).items()
            if key not in ("ok", "route", "error")
        })
    _record_history(cmd, t0, resp)
    return resp


def _publish_result(rid: str, result: Any) -> None:
    """Publish a response unless its HTTP caller has already timed out."""
    with _results_lock:
        if rid in _abandoned_requests:
            _abandoned_requests.remove(rid)
            return
        _results[rid] = result


def _poll_pending_tool_calls() -> None:
    for rid, pending in list(_pending_tool_calls.items()):
        outcome = pending.poll()
        if outcome is None:
            continue
        del _pending_tool_calls[rid]
        if not _pending_tool_calls:
            _set_editor_command()
        if isinstance(outcome, _MCPFallback):
            outcome.command = pending.command
            outcome.started_mono = pending.started_mono
        else:
            _record_history(pending.command, pending.started_mono, outcome)
        _publish_result(rid, outcome)


def _cancel_dispatch_state() -> None:
    """Cancel work that must not survive a bridge stop/restart."""
    global _tick_health
    cancellations: Dict[str, dict] = {}
    while True:
        try:
            rid, cmd, _params, _deadline = _work_queue.get_nowait()
        except queue.Empty:
            break
        cancellations[rid] = {
            "ok": False,
            "route": _command_route(cmd),
            "error": f"'{cmd}' was cancelled before editor dispatch",
        }

    for rid, pending in list(_pending_tool_calls.items()):
        cancellations[rid] = {
            "ok": False,
            "route": "toolset_registry",
            "outcome_unknown": True,
            "error": (
                f"'{pending.command}' was pending when the bridge stopped; "
                "the operation may still complete and was not retried"
            ),
        }
    _pending_tool_calls.clear()
    _set_editor_command()
    _tick_health = 0

    with _results_lock:
        for rid, response in cancellations.items():
            if rid in _abandoned_requests:
                _abandoned_requests.remove(rid)
            else:
                _results[rid] = response


def _tick(dt: float) -> None:
    """Drain the work queue on the editor's main thread (if tick dispatch is active)."""
    global _tick_health, _last_tick_mono
    _tick_health += 1
    with _activity_lock:
        _last_tick_mono = time.monotonic()
    _poll_pending_tool_calls()
    if _pending_tool_calls:
        return
    done = 0
    while not _work_queue.empty() and done < WORKER_BATCH_SIZE:
        try:
            rid, cmd, params, deadline = _work_queue.get_nowait()
        except queue.Empty:
            break
        with _results_lock:
            if rid in _abandoned_requests:
                _abandoned_requests.remove(rid)
                done += 1
                continue
        if time.monotonic() >= deadline:
            _publish_result(rid, {
                "ok": False,
                "route": _command_route(cmd),
                "error": f"'{cmd}' expired before it reached the editor thread",
            })
            done += 1
            continue
        _set_editor_command(cmd)
        try:
            response = _execute_and_respond(rid, cmd, params)
        finally:
            _set_editor_command()
        if isinstance(response, _PendingRegistryCall):
            response.deadline_mono = deadline
            _pending_tool_calls[rid] = response
            _set_editor_command(cmd, "waiting_on_tool", response.started_mono)
            done += 1
            break
        else:
            _publish_result(rid, response)
        done += 1
    # Some registry tools complete synchronously. Pick those up this tick.
    _poll_pending_tool_calls()


# ── Start / Stop ───────────────────────────────────────────────────────────


def _pick_port() -> int:
    for port in range(PORT_RANGE_START, PORT_RANGE_END + 1):
        # First check if something is already listening
        test = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        test.settimeout(0.5)
        try:
            test.connect(("127.0.0.1", port))
            test.close()
            continue  # Port in use by a live server
        except (ConnectionRefusedError, OSError, TimeoutError):
            test.close()

        # Try to bind WITHOUT SO_REUSEADDR to avoid ghost binds
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", port))
            s.close()
            return port
        except OSError:
            continue
    raise RuntimeError(f"No free port in {PORT_RANGE_START}-{PORT_RANGE_END}")


def _disable_background_throttle() -> None:
    """Disable 'Use Less CPU when in Background' so tick callbacks fire
    even when UEFN is not the focused window."""
    success = False

    # Method 1: EditorPerformanceSettings CDO
    try:
        settings = unreal.get_default_object(unreal.EditorPerformanceSettings)
        if settings.get_editor_property("use_less_cpu_when_in_background"):
            settings.set_editor_property("use_less_cpu_when_in_background", False)
            _log("Disabled 'Use Less CPU when in Background' via EditorPerformanceSettings")
            success = True
    except Exception as e:
        _log(f"EditorPerformanceSettings method failed: {e}")

    # Method 2: Direct CVar access
    if not success:
        try:
            cvar = unreal.ConsoleVariable.find("t.IdleWhenNotForeground")
            if cvar:
                cvar.set_int(0)
                _log("Disabled background throttle via CVar")
                success = True
        except Exception:
            pass

    # Method 3: Console command with available world context approaches
    if not success:
        for world_ctx in [None]:
            try:
                unreal.SystemLibrary.execute_console_command(
                    world_ctx, "t.IdleWhenNotForeground 0"
                )
                _log("Disabled background throttle via console command")
                success = True
                break
            except Exception:
                pass

    # Method 4: Keep-alive ticker if we cannot disable throttle.
    # ensure ticks fire by periodically requesting focus
    if not success:
        _log("WARNING: Could not disable background CPU throttle. "
             "Manually uncheck: Edit → Editor Preferences → General → "
             "Performance → 'Use Less CPU when in Background'")


def _unregister_tick_callback() -> None:
    """Forget and best-effort unregister the current Slate callback."""
    global _tick_handle
    handle = _tick_handle
    _tick_handle = None
    if handle is None:
        return
    try:
        unreal.unregister_slate_post_tick_callback(handle)
    except Exception as exc:
        _log(f"Tick callback cleanup failed: {exc}", "warn")


def start(port: int = 0, mode: str = "auto") -> int:
    """Start the bridge.  Returns the bound port.

    Args:
        port: Port to bind (0 = auto-detect).
        mode: Deprecated compatibility parameter. Editor work always uses a
            Slate tick callback so Unreal APIs stay on the game thread.
    """
    global _http, _http_thread, _tick_handle, _active_port, _start_mono
    global _dispatch_mode, _toolset_schema_cache

    if mode not in ("auto", "direct", "tick"):
        raise ValueError("mode must be 'auto', 'direct', or 'tick'")

    if _http is not None:
        # Check if previous server is actually alive
        if _http_thread and _http_thread.is_alive():
            _log(f"Already running on :{_active_port}", "warn")
            return _active_port
        else:
            _log("Previous server died; cleaning up and restarting")
            with _http_lifecycle_lock:
                _http._uefn_bridge_accepting = False
                _cancel_dispatch_state()
            _unregister_tick_callback()
            try:
                _http.server_close()
            except Exception:
                pass
            _http = None
            _http_thread = None
            _active_port = 0
            _toolset_schema_cache = None

    _disable_background_throttle()

    port = port or _pick_port()
    # Override allow_reuse_address to prevent ghost binds
    HTTPServer.allow_reuse_address = False
    http_server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    http_server.daemon_threads = True
    http_server._uefn_bridge_accepting = True
    _http = http_server
    _active_port = port
    _start_mono = time.monotonic()

    # The tick callback is mandatory. A listening server without it would
    # accept editor commands that can only time out in the queue.
    try:
        _tick_handle = unreal.register_slate_post_tick_callback(_tick)
    except Exception as exc:
        http_server._uefn_bridge_accepting = False
        http_server.server_close()
        _http = None
        _http_thread = None
        _tick_handle = None
        _active_port = 0
        _start_mono = 0.0
        _log(f"Tick callback registration failed: {exc}", "error")
        raise RuntimeError(
            "Cannot start bridge without a Slate tick callback"
        ) from exc
    _dispatch_mode = "tick"
    if mode == "direct":
        _log("Dispatch mode 'direct' is deprecated; using safe tick dispatch", "warn")

    def _serve(server_instance: ThreadingHTTPServer = http_server):
        try:
            _log("HTTP server thread started")
            server_instance.serve_forever(poll_interval=0.05)
            _log("HTTP server thread exited normally")
        except Exception as exc:
            _log(f"HTTP server crashed: {exc}", "error")
            import traceback
            _log(traceback.format_exc(), "error")

    _http_thread = threading.Thread(target=_serve, daemon=True)
    _http_thread.start()
    _log("Dispatch: tick (game thread)")

    _log(f"Bridge v{VERSION} listening on http://127.0.0.1:{port}")
    _log(f"{len(_commands)} commands registered")
    return port


def stop() -> None:
    """Shut the bridge down cleanly."""
    global _http, _http_thread, _tick_handle, _active_port, _toolset_schema_cache

    if _http is None:
        _unregister_tick_callback()
        _cancel_dispatch_state()
        _toolset_schema_cache = None
        _reset_mcp_client(close_async=True)
        _log("Not running", "warn")
        return

    # Mark this server generation stale and drain its editor queue atomically.
    # A handler still reading a request body must not enqueue after this point.
    with _http_lifecycle_lock:
        _http._uefn_bridge_accepting = False
        _cancel_dispatch_state()
    _unregister_tick_callback()
    if _http_thread and _http_thread.is_alive():
        _http.shutdown()
    _http.server_close()
    if _http_thread and _http_thread.is_alive():
        _http_thread.join(timeout=1)

    _log(f"Stopped (was :{_active_port})")
    _http = None
    _http_thread = None
    _active_port = 0
    _toolset_schema_cache = None
    # Waiting handlers consume cancellation results. Late editor results use
    # abandoned-request markers, so neither collection is cleared here.
    _reset_mcp_client(close_async=True)


def restart(port: int = 0) -> int:
    stop()
    time.sleep(0.3)
    return start(port)


# ── Auto-start when executed directly ──────────────────────────────────────

if os.environ.get("UEFN_BRIDGE_NO_AUTOSTART") != "1":
    start()
