"""Pure-stdlib client for Unreal MCP's Streamable HTTP endpoint.

The client runs outside the editor-facing command dispatcher. It keeps one MCP
session alive, serializes requests through one mutex, and understands both JSON
and Server-Sent Events (SSE) responses.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
import http.client
import ipaddress
import io
import json
import math
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional


PROTOCOL_VERSION = "2025-06-18"
DEFAULT_ENDPOINT = "http://127.0.0.1:8000/mcp"


class MCPError(Exception):
    """Base class for Unreal MCP client errors."""


class MCPConfigurationError(MCPError, ValueError):
    """The client configuration is invalid or unsafe."""


class MCPTransportError(MCPError):
    """An HTTP or connection-level failure occurred."""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        body: str = "",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class MCPTimeoutError(MCPTransportError, TimeoutError):
    """An MCP HTTP request exceeded its timeout."""


class MCPProtocolError(MCPError):
    """The server returned a malformed or incompatible MCP response."""


class MCPJSONRPCError(MCPError):
    """The server returned a JSON-RPC error object."""

    def __init__(
        self,
        code: int,
        message: str,
        *,
        data: Any = None,
        request_id: Any = None,
    ) -> None:
        super().__init__(f"MCP JSON-RPC error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data
        self.request_id = request_id


class MCPToolError(MCPError):
    """An MCP tools/call response reported isError=true."""

    def __init__(self, tool_name: str, result: Dict[str, Any]) -> None:
        self.tool_name = tool_name
        self.result = result
        self.content = result.get("content")
        detail = _tool_error_detail(self.content)
        super().__init__(f"MCP tool '{tool_name}' failed: {detail}")


class MCPInvalidSessionError(MCPTransportError):
    """The server rejected an expired or otherwise invalid MCP session."""


class MCPOutcomeUnknownError(MCPError):
    """A mutating tool call failed after dispatch with an unknown outcome."""

    outcome_unknown = True

    def __init__(self, operation: str, cause: MCPError) -> None:
        super().__init__(
            f"MCP tool '{operation}' may have run, but its outcome is unknown: {cause}"
        )
        self.operation = operation
        self.cause = cause


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not allow an endpoint redirect to bypass loopback validation."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validate_endpoint(endpoint: str, allow_remote: bool) -> str:
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise MCPConfigurationError("MCP endpoint must be a non-empty URL")

    endpoint = endpoint.strip()
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme not in ("http", "https"):
        raise MCPConfigurationError("MCP endpoint must use http or https")
    if not parsed.hostname:
        raise MCPConfigurationError("MCP endpoint must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise MCPConfigurationError("MCP endpoint must not include credentials")
    if parsed.fragment:
        raise MCPConfigurationError("MCP endpoint must not include a URL fragment")
    try:
        parsed.port
    except ValueError as exc:
        raise MCPConfigurationError(f"Invalid MCP endpoint port: {exc}") from exc

    if not allow_remote:
        host = parsed.hostname.rstrip(".").lower()
        is_loopback = host == "localhost"
        if not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                is_loopback = False
        if not is_loopback:
            raise MCPConfigurationError(
                "Remote MCP endpoints are disabled; use localhost, a loopback "
                "address, or pass allow_remote=True"
            )
    return endpoint


def _tool_error_detail(content: Any) -> str:
    if isinstance(content, list):
        text_parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        detail = "\n".join(part for part in text_parts if part)
        if detail:
            return detail
    return str(content) if content is not None else "Unknown tool error"


def _parse_json_text_tool_result(result: Any) -> Any:
    """Parse a sole JSON text content block while preserving other results."""
    if not isinstance(result, dict):
        return result
    content = result.get("content")
    if not isinstance(content, list) or len(content) != 1:
        return result
    item = content[0]
    if not isinstance(item, dict) or item.get("type") != "text":
        return result
    text = item.get("text")
    if not isinstance(text, str):
        return result
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return result


def _parse_toolset_list_result(result: Any) -> Any:
    """Normalize Unreal MCP's Markdown toolset list into a stable object."""
    if not isinstance(result, dict):
        return result
    content = result.get("content")
    if not isinstance(content, list) or len(content) != 1:
        return result
    block = content[0]
    if not isinstance(block, dict) or block.get("type") != "text":
        return result
    value = block.get("text")
    if not isinstance(value, str):
        return result

    entries: List[Dict[str, str]] = []
    current: Optional[Dict[str, str]] = None
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        candidate = line[2:] if line.startswith("- ") else ""
        name, separator, description = candidate.partition(":")
        is_toolset_entry = bool(
            separator
            and re.fullmatch(
                r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+",
                name.strip(),
            )
        )
        if is_toolset_entry:
            if current is not None:
                entries.append(current)
            current = {
                "name": name.strip(),
                "description": description.strip(),
            }
        elif current is None:
            return result
        else:
            current["description"] = (
                f"{current['description']} {line}".strip()
            )
    if current is not None:
        entries.append(current)
    if not entries:
        return result
    return {"toolsets": entries, "count": len(entries)}


def _messages_from_json(value: Any) -> List[Any]:
    if isinstance(value, list):
        return list(value)
    return [value]


def _remaining_seconds(deadline: Optional[float]) -> Optional[float]:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MCPTimeoutError("MCP operation exceeded its wall-clock deadline")
    return remaining


def _set_stream_timeout(stream, timeout: Optional[float]) -> None:
    if timeout is None:
        return
    candidates = [stream, getattr(stream, "fp", None)]
    fp = getattr(stream, "fp", None)
    candidates.extend([
        getattr(stream, "raw", None),
        getattr(fp, "raw", None) if fp is not None else None,
    ])
    for candidate in candidates:
        sock = getattr(candidate, "_sock", None)
        setter = getattr(sock, "settimeout", None)
        if callable(setter):
            try:
                setter(max(0.001, timeout))
            except OSError:
                pass
            return


def _read_all_with_deadline(
    stream,
    deadline: Optional[float],
) -> bytes:
    chunks: List[bytes] = []
    while True:
        remaining = _remaining_seconds(deadline)
        _set_stream_timeout(stream, remaining)
        reader = getattr(stream, "read1", stream.read)
        chunk = reader(64 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _read_http_error_body(
    stream,
    deadline: Optional[float],
) -> bytes:
    """Read an HTTP error body without leaking raw transport exceptions."""
    try:
        return _read_all_with_deadline(stream, deadline)
    except MCPTimeoutError:
        raise
    except (socket.timeout, TimeoutError) as exc:
        raise MCPTimeoutError(
            "MCP HTTP error body exceeded its wall-clock deadline"
        ) from exc
    except http.client.HTTPException as exc:
        raise MCPTransportError(
            f"MCP HTTP error body was incomplete: {exc}"
        ) from exc
    except OSError as exc:
        raise MCPTransportError(
            f"MCP HTTP error body transport failed: {exc}"
        ) from exc


def _parse_sse_stream(
    stream,
    expected_id: Any = None,
    deadline: Optional[float] = None,
) -> List[Any]:
    messages: List[Any] = []
    data_lines: List[str] = []

    def flush_event() -> bool:
        if not data_lines:
            return False
        data = "\n".join(data_lines)
        data_lines.clear()
        try:
            value = json.loads(data)
        except json.JSONDecodeError as exc:
            raise MCPProtocolError(f"Invalid JSON in SSE data: {exc}") from exc
        messages.extend(_messages_from_json(value))
        if expected_id is None:
            return False
        return any(
            isinstance(message, dict) and message.get("id") == expected_id
            for message in messages
        )

    while True:
        remaining = _remaining_seconds(deadline)
        _set_stream_timeout(stream, remaining)
        raw_line = stream.readline()
        if not raw_line:
            flush_event()
            break
        try:
            line = raw_line.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError as exc:
            raise MCPProtocolError(f"SSE response was not UTF-8: {exc}") from exc
        if not line:
            if flush_event():
                break
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if field != "data":
            continue
        if separator and value.startswith(" "):
            value = value[1:]
        data_lines.append(value)
    return messages


def _parse_response_stream(
    stream,
    content_type: str,
    expected_id: Any = None,
    deadline: Optional[float] = None,
) -> List[Any]:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type == "text/event-stream":
        return _parse_sse_stream(stream, expected_id, deadline)

    body = _read_all_with_deadline(stream, deadline)
    if not body or not body.strip():
        return []
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MCPProtocolError(f"Invalid JSON response: {exc}") from exc
    return _messages_from_json(value)


def _parse_error_body(body: bytes, content_type: str) -> List[Any]:
    if not body or not body.strip():
        return []
    stream = io.BytesIO(body)
    return _parse_response_stream(stream, content_type)


def _jsonrpc_error_from(messages: List[Any], request_id: Any = None):
    candidates = [message for message in messages if isinstance(message, dict)]
    if request_id is not None:
        matching = [message for message in candidates if message.get("id") == request_id]
        if matching:
            candidates = matching
    for message in candidates:
        error = message.get("error")
        if not isinstance(error, dict):
            continue
        code = error.get("code", -32603)
        if not isinstance(code, int):
            code = -32603
        return MCPJSONRPCError(
            code,
            str(error.get("message", "Unknown JSON-RPC error")),
            data=error.get("data"),
            request_id=message.get("id"),
        )
    return None


def _looks_like_invalid_session(status: int, body: bytes) -> bool:
    if status == 404:
        return True
    if status != 400:
        return False
    text = body.decode("utf-8", errors="replace").lower()
    identifies_mcp_session = any(
        marker in text
        for marker in ("mcp-session-id", "mcp session")
    )
    return identifies_mcp_session and any(
        marker in text
        for marker in ("invalid", "expired", "unknown", "not found", "missing")
    )


def _jsonrpc_error_is_invalid_session(error: Dict[str, Any]) -> bool:
    message = str(error.get("message", "")).lower()
    data = str(error.get("data", "")).lower()
    text = f"{message} {data}"
    identifies_mcp_session = any(
        marker in text for marker in ("mcp-session-id", "mcp session")
    )
    return identifies_mcp_session and any(
        marker in text
        for marker in ("invalid", "expired", "unknown", "not found", "missing")
    )


class UnrealMCPClient:
    """Synchronous Streamable HTTP client for Unreal MCP.

    Network access is lazy. The first operation initializes a session, sends
    notifications/initialized, and then performs the requested operation.
    """

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        *,
        timeout: float = 30.0,
        allow_remote: bool = False,
        client_name: str = "uefn-agent-bridge",
        client_version: str = "0.3.0",
    ) -> None:
        if not isinstance(timeout, (int, float)):
            raise MCPConfigurationError("timeout must be a positive finite number")
        timeout_value = float(timeout)
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise MCPConfigurationError("timeout must be a positive finite number")
        if not client_name:
            raise MCPConfigurationError("client_name must not be empty")
        if not client_version:
            raise MCPConfigurationError("client_version must not be empty")

        self.endpoint = _validate_endpoint(endpoint, allow_remote)
        self.url = self.endpoint
        self.timeout = timeout_value
        self.allow_remote = allow_remote
        self.client_name = client_name
        self.client_version = client_version

        self._lock = threading.Lock()
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )
        self._next_request_id = 1
        self._initialized = False
        self._session_id: Optional[str] = None
        self._protocol_version = PROTOCOL_VERSION
        self._initialize_result: Optional[Dict[str, Any]] = None
        self._tools_cache: Optional[List[Dict[str, Any]]] = None
        self._toolsets_cached = False
        self._toolsets_cache: Any = None
        self._toolset_description_cache: Dict[str, Any] = {}

    @property
    def session_id(self) -> Optional[str]:
        with self._lock:
            return self._session_id

    @property
    def initialized(self) -> bool:
        with self._lock:
            return self._initialized

    @property
    def protocol_version(self) -> str:
        with self._lock:
            return self._protocol_version

    @property
    def server_info(self) -> Dict[str, Any]:
        with self._lock:
            if not self._initialize_result:
                return {}
            value = self._initialize_result.get("serverInfo", {})
            return copy.deepcopy(value) if isinstance(value, dict) else {}

    def _effective_timeout(self, timeout: Optional[float]) -> float:
        if timeout is None:
            return self.timeout
        if not isinstance(timeout, (int, float)):
            raise MCPConfigurationError("timeout must be a positive finite number")
        timeout_value = float(timeout)
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise MCPConfigurationError("timeout must be a positive finite number")
        return timeout_value

    def _operation_deadline(self, timeout: Optional[float]) -> float:
        return time.monotonic() + self._effective_timeout(timeout)

    @contextmanager
    def _lock_until(self, deadline: float):
        remaining = _remaining_seconds(deadline)
        if not self._lock.acquire(timeout=remaining):
            raise MCPTimeoutError(
                "MCP operation timed out waiting for the serialized client"
            )
        try:
            _remaining_seconds(deadline)
            yield
        finally:
            self._lock.release()

    def _new_request_id_locked(self) -> int:
        request_id = self._next_request_id
        self._next_request_id += 1
        return request_id

    def _clear_discovery_cache_locked(self) -> None:
        self._tools_cache = None
        self._toolsets_cached = False
        self._toolsets_cache = None
        self._toolset_description_cache.clear()

    def _reset_locked(self) -> None:
        self._initialized = False
        self._session_id = None
        self._protocol_version = PROTOCOL_VERSION
        self._initialize_result = None
        self._clear_discovery_cache_locked()

    def reset(self) -> None:
        """Forget the current session and all per-session discovery caches."""
        with self._lock:
            self._reset_locked()

    def _headers_locked(
        self,
        *,
        include_session: bool,
        has_json_body: bool = True,
    ) -> Dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self._protocol_version,
            "User-Agent": f"{self.client_name}/{self.client_version}",
        }
        if has_json_body:
            headers["Content-Type"] = "application/json"
        if include_session and self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _raise_http_error_locked(
        self,
        exc: urllib.error.HTTPError,
        *,
        request_id: Any,
        session_was_sent: bool,
        deadline: float,
    ) -> None:
        body = _read_http_error_body(exc, deadline)
        content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
        if session_was_sent and _looks_like_invalid_session(exc.code, body):
            raise MCPInvalidSessionError(
                f"MCP session was rejected with HTTP {exc.code}",
                status=exc.code,
                body=body.decode("utf-8", errors="replace"),
            ) from exc

        try:
            messages = _parse_error_body(body, content_type)
        except MCPProtocolError:
            messages = []
        rpc_error = _jsonrpc_error_from(messages, request_id)
        if rpc_error is not None:
            raise rpc_error from exc

        detail = body.decode("utf-8", errors="replace").strip()
        message = f"MCP HTTP request failed with status {exc.code}"
        if detail:
            message = f"{message}: {detail}"
        raise MCPTransportError(message, status=exc.code, body=detail) from exc

    def _post_locked(
        self,
        message: Dict[str, Any],
        *,
        deadline: float,
        expected_id: Any,
        include_session: bool,
    ) -> tuple[List[Any], Any, int]:
        try:
            body = json.dumps(
                message,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise MCPProtocolError(f"MCP request is not JSON serializable: {exc}") from exc

        headers = self._headers_locked(include_session=include_session)
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers=headers,
            method="POST",
        )
        session_was_sent = bool(include_session and self._session_id)
        try:
            request_timeout = _remaining_seconds(deadline)
            with self._opener.open(request, timeout=request_timeout) as response:
                response_headers = response.headers
                status = response.status
                messages = _parse_response_stream(
                    response,
                    response_headers.get("Content-Type", ""),
                    expected_id,
                    deadline,
                )
                return messages, response_headers, status
        except urllib.error.HTTPError as exc:
            self._raise_http_error_locked(
                exc,
                request_id=expected_id,
                session_was_sent=session_was_sent,
                deadline=deadline,
            )
            raise AssertionError("unreachable")
        except MCPTimeoutError:
            raise
        except (socket.timeout, TimeoutError) as exc:
            raise MCPTimeoutError("MCP request exceeded its wall-clock deadline") from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise MCPTimeoutError(
                    "MCP request exceeded its wall-clock deadline"
                ) from exc
            raise MCPTransportError(f"Could not connect to MCP endpoint: {exc}") from exc
        except http.client.HTTPException as exc:
            raise MCPTransportError(f"MCP HTTP response was incomplete: {exc}") from exc
        except OSError as exc:
            raise MCPTransportError(f"MCP transport failed: {exc}") from exc

    def _response_result(self, messages: List[Any], request_id: int) -> Any:
        if any(
            isinstance(message, dict)
            and message.get("method") == "notifications/tools/list_changed"
            for message in messages
        ):
            self._clear_discovery_cache_locked()
        response = next(
            (
                message
                for message in messages
                if isinstance(message, dict) and message.get("id") == request_id
            ),
            None,
        )
        if response is None:
            raise MCPProtocolError(
                f"MCP response did not contain JSON-RPC id {request_id}"
            )
        if response.get("jsonrpc") != "2.0":
            raise MCPProtocolError("MCP response did not declare jsonrpc='2.0'")
        error = response.get("error")
        if isinstance(error, dict):
            if self._session_id and _jsonrpc_error_is_invalid_session(error):
                raise MCPInvalidSessionError(
                    str(error.get("message", "MCP session was rejected"))
                )
            code = error.get("code", -32603)
            if not isinstance(code, int):
                code = -32603
            raise MCPJSONRPCError(
                code,
                str(error.get("message", "Unknown JSON-RPC error")),
                data=error.get("data"),
                request_id=request_id,
            )
        if "result" not in response:
            raise MCPProtocolError("MCP JSON-RPC response omitted both result and error")
        return response["result"]

    def _initialize_locked(self, deadline: float) -> Dict[str, Any]:
        _remaining_seconds(deadline)
        if self._initialized and self._initialize_result is not None:
            return copy.deepcopy(self._initialize_result)

        self._reset_locked()
        request_id = self._new_request_id_locked()
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": self.client_name,
                    "version": self.client_version,
                },
            },
        }
        messages, headers, _ = self._post_locked(
            request,
            deadline=deadline,
            expected_id=request_id,
            include_session=False,
        )
        result = self._response_result(messages, request_id)
        if not isinstance(result, dict):
            raise MCPProtocolError("MCP initialize result must be an object")
        negotiated = result.get("protocolVersion")
        if negotiated != PROTOCOL_VERSION:
            raise MCPProtocolError(
                f"Server negotiated unsupported MCP protocol version {negotiated!r}"
            )

        session_id = headers.get("Mcp-Session-Id")
        if session_id is not None:
            if not session_id or any(not 0x21 <= ord(char) <= 0x7E for char in session_id):
                raise MCPProtocolError("Server returned an invalid Mcp-Session-Id header")
            self._session_id = session_id
        self._protocol_version = negotiated
        self._initialize_result = copy.deepcopy(result)

        notification = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
        try:
            self._post_locked(
                notification,
                deadline=deadline,
                expected_id=None,
                include_session=True,
            )
        except Exception:
            self._reset_locked()
            raise
        self._initialized = True
        return copy.deepcopy(result)

    def initialize(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Initialize lazily and return the server's InitializeResult."""
        deadline = self._operation_deadline(timeout)
        with self._lock_until(deadline):
            return self._initialize_locked(deadline)

    def _request_once_locked(
        self,
        method: str,
        params: Optional[Dict[str, Any]],
        deadline: float,
    ) -> Any:
        request_id = self._new_request_id_locked()
        request: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            request["params"] = params
        messages, _, _ = self._post_locked(
            request,
            deadline=deadline,
            expected_id=request_id,
            include_session=True,
        )
        return self._response_result(messages, request_id)

    def _request_locked(
        self,
        method: str,
        params: Optional[Dict[str, Any]],
        deadline: float,
        *,
        retry_on_ambiguous: bool = False,
    ) -> Any:
        if not isinstance(method, str) or not method:
            raise MCPConfigurationError("JSON-RPC method must not be empty")
        self._initialize_locked(deadline)
        invalid_session_retried = False
        ambiguous_retried = False
        while True:
            try:
                return self._request_once_locked(method, params, deadline)
            except MCPInvalidSessionError:
                if invalid_session_retried:
                    raise
                invalid_session_retried = True
                self._reset_locked()
                self._initialize_locked(deadline)
            except (MCPTimeoutError, MCPTransportError, MCPProtocolError):
                if not retry_on_ambiguous or ambiguous_retried:
                    raise
                ambiguous_retried = True
                self._reset_locked()
                self._initialize_locked(deadline)

    def request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        """Send an arbitrary JSON-RPC request within the persistent session."""
        deadline = self._operation_deadline(timeout)
        with self._lock_until(deadline):
            return self._request_locked(method, params, deadline)

    def ping(self, timeout: Optional[float] = None) -> Any:
        """Initialize if needed and issue the MCP ping request."""
        deadline = self._operation_deadline(timeout)
        with self._lock_until(deadline):
            return self._request_locked(
                "ping",
                None,
                deadline,
                retry_on_ambiguous=True,
            )

    def list_tools(
        self,
        refresh: bool = False,
        timeout: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Return the top-level tools/list tools, cached for this session."""
        deadline = self._operation_deadline(timeout)
        with self._lock_until(deadline):
            if not refresh and self._tools_cache is not None:
                return copy.deepcopy(self._tools_cache)
            result = self._request_locked(
                "tools/list",
                {},
                deadline,
                retry_on_ambiguous=True,
            )
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                raise MCPProtocolError("tools/list result must contain a tools array")
            self._tools_cache = copy.deepcopy(result["tools"])
            return copy.deepcopy(self._tools_cache)

    def _call_meta_tool_locked(
        self,
        name: str,
        arguments: Dict[str, Any],
        deadline: float,
        *,
        retry_on_ambiguous: bool = False,
    ) -> Any:
        result = self._request_locked(
            "tools/call",
            {"name": name, "arguments": arguments},
            deadline,
            retry_on_ambiguous=retry_on_ambiguous,
        )
        if isinstance(result, dict) and result.get("isError"):
            raise MCPToolError(name, result)
        return _parse_json_text_tool_result(result)

    def list_toolsets(
        self,
        refresh: bool = False,
        timeout: Optional[float] = None,
    ) -> Any:
        """Call the list_toolsets meta-tool and cache its parsed result."""
        deadline = self._operation_deadline(timeout)
        with self._lock_until(deadline):
            if not refresh and self._toolsets_cached:
                return copy.deepcopy(self._toolsets_cache)
            result = self._call_meta_tool_locked(
                "list_toolsets",
                {},
                deadline,
                retry_on_ambiguous=True,
            )
            result = _parse_toolset_list_result(result)
            self._toolsets_cache = copy.deepcopy(result)
            self._toolsets_cached = True
            return copy.deepcopy(result)

    def describe_toolset(
        self,
        toolset_name: str,
        refresh: bool = False,
        timeout: Optional[float] = None,
    ) -> Any:
        """Return schemas for one exact, fully qualified toolset name."""
        if not isinstance(toolset_name, str) or not toolset_name:
            raise MCPConfigurationError("toolset_name must not be empty")
        deadline = self._operation_deadline(timeout)
        with self._lock_until(deadline):
            if not refresh and toolset_name in self._toolset_description_cache:
                return copy.deepcopy(self._toolset_description_cache[toolset_name])
            result = self._call_meta_tool_locked(
                "describe_toolset",
                {"toolset_name": toolset_name},
                deadline,
                retry_on_ambiguous=True,
            )
            self._toolset_description_cache[toolset_name] = copy.deepcopy(result)
            return copy.deepcopy(result)

    def call_tool(
        self,
        toolset_name: str,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
        *,
        retry_safe: bool = False,
    ) -> Any:
        """Dispatch a tool through Epic's call_tool discovery meta-tool."""
        if not isinstance(toolset_name, str) or not toolset_name:
            raise MCPConfigurationError("toolset_name must not be empty")
        if not isinstance(tool_name, str) or not tool_name:
            raise MCPConfigurationError("tool_name must not be empty")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise MCPConfigurationError("arguments must be a dictionary")
        try:
            json.dumps(arguments, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise MCPConfigurationError(
                f"arguments must be JSON serializable: {exc}"
            ) from exc

        short_tool_name = tool_name
        if "." in tool_name:
            expected_prefix = f"{toolset_name}."
            if not tool_name.startswith(expected_prefix):
                raise MCPConfigurationError(
                    "Qualified tool_name must start with the exact toolset_name"
                )
            short_tool_name = tool_name[len(expected_prefix):]
            if "." in short_tool_name:
                raise MCPConfigurationError(
                    "Qualified tool_name must contain one short name after toolset_name"
                )
        if not short_tool_name:
            raise MCPConfigurationError("tool_name must contain a short tool name")
        deadline = self._operation_deadline(timeout)
        with self._lock_until(deadline):
            self._initialize_locked(deadline)
            try:
                return self._call_meta_tool_locked(
                    "call_tool",
                    {
                        "toolset_name": toolset_name,
                        "tool_name": short_tool_name,
                        "arguments": arguments,
                    },
                    deadline,
                    retry_on_ambiguous=retry_safe,
                )
            except MCPInvalidSessionError:
                raise
            except (MCPTimeoutError, MCPTransportError, MCPProtocolError) as exc:
                raise MCPOutcomeUnknownError(short_tool_name, exc) from exc

    def close(self, timeout: Optional[float] = None) -> None:
        """DELETE the active MCP session, then clear all local session state."""
        deadline = self._operation_deadline(timeout)
        with self._lock_until(deadline):
            if not self._session_id:
                self._reset_locked()
                return
            headers = self._headers_locked(
                include_session=True,
                has_json_body=False,
            )
            request = urllib.request.Request(
                self.endpoint,
                headers=headers,
                method="DELETE",
            )
            error: Optional[Exception] = None
            try:
                request_timeout = _remaining_seconds(deadline)
                with self._opener.open(request, timeout=request_timeout) as response:
                    _read_all_with_deadline(response, deadline)
            except urllib.error.HTTPError as exc:
                if exc.code not in (404, 405):
                    try:
                        detail = _read_http_error_body(
                            exc,
                            deadline,
                        ).decode("utf-8", errors="replace").strip()
                    except (MCPTimeoutError, MCPTransportError) as read_error:
                        error = read_error
                    else:
                        error = MCPTransportError(
                            f"MCP session DELETE failed with status {exc.code}",
                            status=exc.code,
                            body=detail,
                        )
            except (socket.timeout, TimeoutError) as exc:
                error = MCPTimeoutError(
                    "MCP session DELETE exceeded its wall-clock deadline"
                )
                error.__cause__ = exc
            except urllib.error.URLError as exc:
                error = MCPTransportError(f"MCP session DELETE failed: {exc}")
                error.__cause__ = exc
            except (http.client.HTTPException, OSError) as exc:
                error = MCPTransportError(f"MCP session DELETE failed: {exc}")
                error.__cause__ = exc
            finally:
                self._reset_locked()
            if error is not None:
                raise error

    def __enter__(self) -> "UnrealMCPClient":
        return self

    def __exit__(self, exc_type, exc_value, traceback_value) -> None:
        self.close()


__all__ = [
    "DEFAULT_ENDPOINT",
    "PROTOCOL_VERSION",
    "MCPConfigurationError",
    "MCPError",
    "MCPInvalidSessionError",
    "MCPJSONRPCError",
    "MCPOutcomeUnknownError",
    "MCPProtocolError",
    "MCPTimeoutError",
    "MCPToolError",
    "MCPTransportError",
    "UnrealMCPClient",
]
