"""Deterministic routing metadata for the hybrid UEFN bridge.

Named bridge commands stay on the direct embedded-Python path. Commands in
``TOOLSET_ROUTES`` use Epic's Toolset Registry through embedded Python first,
then fall back to the official UEFN MCP Streamable HTTP endpoint only when the
local registry capability is unavailable.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

try:
    from .uefn_tool_policy import (
        CREATOR_EXPOSED_TOOLSETS as UEFN_EXPOSED_TOOLSETS,
        is_tool_allowed,
        short_tool_name,
    )
except ImportError:  # Executed as loose scripts inside UEFN.
    from uefn_tool_policy import (  # type: ignore
        CREATOR_EXPOSED_TOOLSETS as UEFN_EXPOSED_TOOLSETS,
        is_tool_allowed,
        short_tool_name,
    )


ToolRoute = Tuple[str, str]


# The existing direct Python surface. Keeping this list in a module that can be
# imported outside UEFN lets clients explain routing without importing unreal.
PYTHON_COMMANDS = frozenset({
    "actors.delete",
    "actors.duplicate",
    "actors.list",
    "actors.properties",
    "actors.selected",
    "actors.set_color",
    "actors.set_property",
    "actors.spawn",
    "actors.transform",
    "assets.delete",
    "assets.duplicate",
    "assets.exists",
    "assets.import_task",
    "assets.info",
    "assets.list",
    "assets.rename",
    "assets.save",
    "assets.search",
    "assets.selected",
    "batch.exec",
    "exec",
    "fix_throttle",
    "history",
    "level.info",
    "level.save",
    "log",
    "materials.create_instance",
    "redo",
    "reload",
    "shutdown",
    "status",
    "store.install",
    "undo",
    "verse.export_t3d",
    "verse.find_device",
    "verse.get_properties",
    "verse.set_property",
    "viewport.camera",
    "viewport.set_camera",
})


_PYTHON_TOOLSET = "ValkyrieToolset.ValkyriePythonToolset"
_VERSE_TOOLSET = "ValkyrieToolset.VerseToolset"
_SESSION_TOOLSET = "ValkyrieToolset.SessionToolset"
_DEVICE_TOOLSET = "ValkyrieToolset.DeviceToolset"
_ENTITY_TOOLSET = "ValkyrieToolset.EntityToolset"


# Friendly bridge aliases for the 42 UEFN-specific tools shipped in v42. The
# generic toolsets remain available through toolsets.call without inflating the
# command list or forcing schema discovery into every agent context.
TOOLSET_ROUTES: Dict[str, ToolRoute] = {
    "uefn.python_enabled": (_PYTHON_TOOLSET, "IsPythonEnabledInUEFN"),
    "uefn.enable_python": (_PYTHON_TOOLSET, "EnablePythonInUEFN"),

    "verse.write_file": (_VERSE_TOOLSET, "WriteFile"),
    "verse.replace": (_VERSE_TOOLSET, "Replace"),
    "verse.read_file": (_VERSE_TOOLSET, "ReadFile"),
    "verse.move": (_VERSE_TOOLSET, "Move"),
    "verse.list_files": (_VERSE_TOOLSET, "ListFiles"),
    "verse.grep": (_VERSE_TOOLSET, "Grep"),
    "verse.delete": (_VERSE_TOOLSET, "Delete"),
    "verse.create_directory": (_VERSE_TOOLSET, "CreateDirectory"),
    "verse.copy": (_VERSE_TOOLSET, "Copy"),
    "verse.build": (_VERSE_TOOLSET, "BuildAll"),

    "session.stop": (_SESSION_TOOLSET, "StopSession"),
    "session.stop_game": (_SESSION_TOOLSET, "StopGame"),
    "session.start": (_SESSION_TOOLSET, "StartSession"),
    "session.start_game": (_SESSION_TOOLSET, "StartGame"),
    "session.push_changes": (_SESSION_TOOLSET, "PushChanges"),
    "session.status": (_SESSION_TOOLSET, "GetSessionStatus"),
    "session.game_state": (_SESSION_TOOLSET, "GetGameState"),
    "session.client_log": (_SESSION_TOOLSET, "GetClientLogEntries"),

    "devices.set_property": (_DEVICE_TOOLSET, "SetDeviceProperty"),
    "devices.remove_event_binding": (_DEVICE_TOOLSET, "RemoveEventBinding"),
    "devices.place": (_DEVICE_TOOLSET, "PlaceDevice"),
    "devices.list_event_bindings": (_DEVICE_TOOLSET, "ListEventBindings"),
    "devices.list_properties": (_DEVICE_TOOLSET, "ListDeviceProperties"),
    "devices.list_assets": (_DEVICE_TOOLSET, "ListDeviceAssets"),
    "devices.get_properties": (_DEVICE_TOOLSET, "GetDeviceProperties"),
    "devices.get_binding_options": (_DEVICE_TOOLSET, "GetBindingOptions"),
    "devices.add_event_binding": (_DEVICE_TOOLSET, "AddEventBinding"),

    "entities.set_transform": (_ENTITY_TOOLSET, "SetEntityTransform"),
    "entities.set_component_property": (_ENTITY_TOOLSET, "SetComponentProperty"),
    "entities.remove_component": (_ENTITY_TOOLSET, "RemoveComponent"),
    "entities.list_classes": (_ENTITY_TOOLSET, "ListEntityClasses"),
    "entities.list_component_properties": (_ENTITY_TOOLSET, "ListComponentProperties"),
    "entities.list_component_classes": (_ENTITY_TOOLSET, "ListComponentClasses"),
    "entities.get_transform": (_ENTITY_TOOLSET, "GetEntityTransform"),
    "entities.get_components": (_ENTITY_TOOLSET, "GetComponents"),
    "entities.get_component_property": (_ENTITY_TOOLSET, "GetComponentProperty"),
    "entities.find": (_ENTITY_TOOLSET, "FindEntities"),
    "entities.delete": (_ENTITY_TOOLSET, "DeleteEntity"),
    "entities.create": (_ENTITY_TOOLSET, "CreateEntity"),
    "entities.add_component": (_ENTITY_TOOLSET, "AddComponent"),
}


# This classification is deliberately conservative. It is useful for clients
# deciding whether a failed request is safe to retry. Anything not listed here
# is mutating, control-plane, build-related, or otherwise outcome-sensitive.
READ_ONLY_TOOL_ROUTES = frozenset({
    "uefn.python_enabled",
    "verse.read_file",
    "verse.list_files",
    "verse.grep",
    "session.status",
    "session.game_state",
    "session.client_log",
    "devices.list_event_bindings",
    "devices.list_properties",
    "devices.list_assets",
    "devices.get_properties",
    "devices.get_binding_options",
    "entities.list_classes",
    "entities.list_component_properties",
    "entities.list_component_classes",
    "entities.get_transform",
    "entities.get_components",
    "entities.get_component_property",
    "entities.find",
})


# Native tools that routinely run longer than the bridge's 30 s default
# deadline. The bridge applies these when a request sets no ``timeout_s``, and
# the bundled client waits at least as long. Starting a session cooks and
# uploads content; building and pushing compile Verse.
SLOW_TOOL_DEADLINES_S: Dict[ToolRoute, float] = {
    (_SESSION_TOOLSET, "StartSession"): 600.0,
    (_SESSION_TOOLSET, "StopSession"): 180.0,
    (_SESSION_TOOLSET, "StartGame"): 180.0,
    (_SESSION_TOOLSET, "StopGame"): 120.0,
    (_SESSION_TOOLSET, "PushChanges"): 600.0,
    (_VERSE_TOOLSET, "BuildAll"): 600.0,
}


def default_deadline_s(command: str, params: Optional[dict] = None, fallback: float = 30.0) -> float:
    """Deadline for a request that sets no ``timeout_s``.

    Friendly aliases and ``toolsets.call``/``mcp.call`` naming a slow native
    tool get that tool's deadline; everything else gets ``fallback``.
    """
    route = TOOLSET_ROUTES.get(command)
    if route is None and command in ("toolsets.call", "mcp.call") and isinstance(params, dict):
        toolset = params.get("toolset_name")
        # Qualified names ("Toolset.Tool") are accepted like short ones.
        tool = short_tool_name(toolset, params.get("tool_name"))
        if tool:
            route = (toolset, tool)
    return SLOW_TOOL_DEADLINES_S.get(route, fallback) if route else fallback


def resolve_tool_route(command: str) -> Optional[ToolRoute]:
    """Return the native toolset route for a friendly bridge command."""
    return TOOLSET_ROUTES.get(command)
