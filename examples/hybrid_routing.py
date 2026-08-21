"""Read-only examples for the hybrid UEFN bridge.

Run this with normal desktop Python while bridge/server.py is running in UEFN.
No browser or desktop automation is involved.
"""

from bridge.client import BridgeError, UEFNBridge


ue = UEFNBridge(timeout=30.0)


def show_route(label: str) -> None:
    print(f"{label}: route={ue.last_route}")


# Existing bridge commands always use embedded Python.
bridge_status = ue.status()
show_route("bridge status")
print("version:", bridge_status["bridge_version"])


# UEFN-specific aliases use the in-process Toolset Registry first.
# MCP is attempted only when the local capability is unavailable before dispatch.
try:
    session_status = ue.run("session.status", timeout_s=120.0)
    show_route("session status")
    print(session_status)
except BridgeError as exc:
    print(
        "session status failed:",
        f"route={exc.route}",
        f"outcome_unknown={exc.outcome_unknown}",
        str(exc),
    )


# Discovery returns only the exact UEFN v42 creator-exposed local policy.
toolsets = ue.toolsets(timeout_s=120.0)
show_route("toolset list")
print("approved toolsets:", toolsets.get("count", "see payload"))


# Generic calls use an exact toolset and tool name from the schema.
session_schema = ue.describe_toolset(
    "ValkyrieToolset.SessionToolset",
    timeout_s=120.0,
)
show_route("session schema")
print("session tools:", len(session_schema.get("tools", [])))


generic_status = ue.call_tool(
    "ValkyrieToolset.SessionToolset",
    "GetSessionStatus",
    timeout_s=120.0,
)
show_route("generic session status")
print(generic_status)


# Force the official HTTP transport only for diagnostics or compatibility work:
# mcp_status = ue.run("mcp.status", timeout_s=120.0)
# show_route("forced MCP status")
