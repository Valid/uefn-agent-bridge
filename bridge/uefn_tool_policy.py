"""Pinned UEFN creator tool policy for safe in-process registry calls.

The Toolset Registry contains toolsets that UEFN intentionally does not expose
to creators. This module is a fail-closed snapshot of the toolsets and tools
described by the official UEFN MCP server for the recorded UEFN build.

42.40 (CL 59247843) re-pin, from the MCP server's tools/list with Python on:
VerseFieldsToolset gained GetSupportedVerseFieldTypes; SceneTools gained
is_actor_hidden, set_actor_hidden, replace_actors_with_asset and
replace_actors_with_class and lost the three *_level_instance tools. The new
editor_toolset.toolsets.programmatic.ProgrammaticToolset (execute_tool_script,
get_execution_environment) is deliberately left out: it runs arbitrary tool
scripts, which the bridge's own exec command already covers under its rules.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Optional


UEFN_TOOL_POLICY_REVISION = "UEFN-v42.40-CL-59247843"
CREATOR_EXPOSED_TOOLSET_COUNT = 29
CREATOR_EXPOSED_TOOL_COUNT = 386


CREATOR_EXPOSED_TOOLS: Dict[str, FrozenSet[str]] = {
    "EditorToolset.EditorAppToolset": frozenset({
        "AddAssetsToCollection",
        "CaptureAssetImage",
        "CaptureEditorImage",
        "CaptureViewport",
        "CreateCollection",
        "DestroyCollection",
        "FocusOnActors",
        "GetActiveEditorModes",
        "GetAssetThumbnails",
        "GetCameraTransform",
        "GetCollectionAssets",
        "GetContentBrowserPath",
        "GetCVarValue",
        "GetOpenAssets",
        "GetSelectedActors",
        "GetSelectedAssets",
        "GetSelectedOutlinerFolders",
        "GetShowFlag",
        "GetVisibleActors",
        "ListCollections",
        "ListEditorModes",
        "ListShowFlags",
        "OpenEditorForAsset",
        "RemoveAssetsFromCollection",
        "ScreenCoordsToWorld",
        "SearchCVars",
        "SelectActors",
        "SelectAssets",
        "SelectOutlinerFolders",
        "SetCameraTransform",
        "SetContentBrowserPath",
        "SetCVarValue",
        "SetEditorMode",
        "SetShowFlag",
        "SetViewportViewMode",
        "ShowNotification",
        "WorldPosToScreenCoords",
    }),
    "EditorToolset.LogsToolset": frozenset({
        "GetLogCategories",
        "GetLogEntries",
        "GetVerbosity",
        "SetVerbosity",
    }),
    "GameplayTagsToolset.GameplayTagsToolset": frozenset({
        "FindReferencersByTag",
        "GetTagInfo",
        "ListTags",
        "ListTagsInSource",
    }),
    "NiagaraToolsets.NiagaraToolset_Info": frozenset({
        "UEnum_Info",
    }),
    "NiagaraToolsets.NiagaraToolset_Component": frozenset({
        "GetUserVariables",
        "GetVariable",
        "SetSystem",
        "SetVariable",
    }),
    "NiagaraToolsets.NiagaraToolset_System": frozenset({
        "AddEmitter",
        "AddModule",
        "AddRenderer",
        "AddSetParameterEntry",
        "AddSetParametersModule",
        "AddUserVariables",
        "ApplyStackIssueFix",
        "CreateNiagaraSystem",
        "GetAvailableDynamicInputs",
        "GetDataInterfaceSchema",
        "GetDynamicInputChain",
        "GetDynamicInputSchema",
        "GetDynamicInputSchemaFromAsset",
        "GetEmitterData",
        "GetEmitterInputValues",
        "GetEmitterSchema",
        "GetEmitterSummary",
        "GetEmitterTopology",
        "GetModuleInputValues",
        "GetModuleSchema",
        "GetModuleSchemaFromAsset",
        "GetModuleTopology",
        "GetRendererData",
        "GetRendererSchema",
        "GetScriptStackInputValues",
        "GetScriptStackTopology",
        "GetStackInputData",
        "GetStackInputSchema",
        "GetStackInputTopology",
        "GetStackIssues",
        "GetSystemCompileState",
        "GetSystemData",
        "GetSystemDependencies",
        "GetSystemSchema",
        "GetSystemSummary",
        "GetUserVariables",
        "RemoveEmitter",
        "RemoveModule",
        "RemoveRenderer",
        "RemoveSetParameterEntry",
        "RemoveUserVariables",
        "SetEmitterData",
        "SetModuleEnabled",
        "SetRendererData",
        "SetStackInputData",
        "SetSystemData",
    }),
    "NiagaraToolsets.NiagaraToolset_Assets": frozenset({
        "FindNiagaraScripts",
        "GetAssetDiscoveryInfo",
        "GetNiagaraScriptDigest",
    }),
    "MVVMToolset.MVVMToolset": frozenset({
        "AddViewModelProperty",
        "AddViewModelToWidget",
        "CreateViewBinding",
        "CreateViewEventBinding",
        "FixupMVVMData",
        "GetWidgetViewConfig",
        "ListBindableWidgetProperties",
        "ListConversionFunctions",
        "ListViewModels",
        "ListWidgetViewBindings",
        "ListWidgetViewEvents",
        "ListWidgetViewModels",
        "RemoveWidgetViewBinding",
        "SetBindingMode",
        "SetWidgetViewConfig",
    }),
    "PhysicsToolsets.PhysicsAssetToolset": frozenset({
        "AddBody",
        "AddConstraint",
        "CreateFromMesh",
        "GetBodyMassScale",
        "GetBodyNames",
        "GetBodyPhysicsMode",
        "GetBodyShapes",
        "GetConstraints",
        "RemoveBody",
        "RemoveConstraint",
        "RemoveShape",
        "SetBodyMassScale",
        "SetBodyPhysicsMode",
        "SetBox",
        "SetCapsule",
        "SetConstraintLimits",
        "SetSphere",
    }),
    "VerseFieldsToolset.VerseFieldsToolset": frozenset({
        "AddVerseField",
        "BindWidgetPropertyToVerseField",
        "DuplicateVerseField",
        "EditVerseField",
        "GetSupportedVerseFieldTypes",
        "ListVerseFields",
        "RemoveVerseField",
    }),
    "WidgetAnimationToolset.WidgetAnimationToolset": frozenset({
        "AddWidgetToAnimation",
        "CreateWidgetAnimation",
        "FindWidgetAnimation",
        "GetWidgetAnimationBindings",
        "ListWidgetAnimations",
        "RemoveWidgetAnimation",
        "RemoveWidgetBinding",
        "RenameWidgetAnimation",
        "SetWidgetAnimationDisplayLabel",
        "SetWidgetAnimationLength",
    }),
    "UMGToolSet.UMGToolSet": frozenset({
        "AddUIComponent",
        "AddWidget",
        "CompileWidgetBlueprint",
        "CreateWidgetBlueprint",
        "GetNamedSlots",
        "GetWidgetClassInfo",
        "GetWidgetDescription",
        "GetWidgets",
        "GetWidgetTreeDepth",
        "ListWidgetBlueprints",
        "ListWidgetClasses",
        "MoveUIComponent",
        "MoveWidget",
        "RemoveUIComponent",
        "RemoveWidget",
        "RenameWidget",
        "ReplaceWidgetWithChild",
        "ReplaceWidgetWithNamedSlot",
        "ReplaceWidgetWithTemplate",
        "SetNamedSlotContent",
        "WrapWidgets",
    }),
    "ValkyrieToolset.ValkyriePythonToolset": frozenset({
        "EnablePythonInUEFN",
        "IsPythonEnabledInUEFN",
    }),
    "ValkyrieToolset.VerseToolset": frozenset({
        "BuildAll",
        "Copy",
        "CreateDirectory",
        "Delete",
        "Grep",
        "ListFiles",
        "Move",
        "ReadFile",
        "Replace",
        "WriteFile",
    }),
    "ValkyrieToolset.SessionToolset": frozenset({
        "GetClientLogEntries",
        "GetGameState",
        "GetSessionStatus",
        "PushChanges",
        "StartGame",
        "StartSession",
        "StopGame",
        "StopSession",
    }),
    "ValkyrieToolset.DeviceToolset": frozenset({
        "AddEventBinding",
        "GetBindingOptions",
        "GetDeviceProperties",
        "ListDeviceAssets",
        "ListDeviceProperties",
        "ListEventBindings",
        "PlaceDevice",
        "RemoveEventBinding",
        "SetDeviceProperty",
    }),
    "ValkyrieToolset.EntityToolset": frozenset({
        "AddComponent",
        "CreateEntity",
        "DeleteEntity",
        "FindEntities",
        "GetComponentProperty",
        "GetComponents",
        "GetEntityTransform",
        "ListComponentClasses",
        "ListComponentProperties",
        "ListEntityClasses",
        "RemoveComponent",
        "SetComponentProperty",
        "SetEntityTransform",
    }),
    "editor_toolset.toolsets.actor.ActorTools": frozenset({
        "add_component",
        "add_tag",
        "diff",
        "get_actor_bounds",
        "get_actor_transform",
        "get_component_actor",
        "get_components",
        "get_label",
        "get_parent_component",
        "get_root_component",
        "get_tags",
        "has_tag",
        "look_at",
        "remove_component",
        "remove_tag",
        "set_actor_transform",
        "set_label",
        "set_parent_component",
    }),
    "editor_toolset.toolsets.asset.AssetTools": frozenset({
        "create_folder",
        "delete",
        "duplicate",
        "exists",
        "find_assets",
        "get_asset_class",
        "get_asset_tags",
        "get_dependencies",
        "get_metadata_tags",
        "get_referencers",
        "is_dirty",
        "list_folders",
        "load_asset",
        "move",
        "reload_asset",
        "save_assets",
        "update_metadata_tags",
    }),
    "editor_toolset.toolsets.curve_table.CurveTableTools": frozenset({
        "add_key",
        "add_row",
        "create",
        "diff",
        "get_keys",
        "import_file",
        "list_rows",
        "remove_row",
        "rename_row",
        "set_keys",
    }),
    "editor_toolset.toolsets.data_table.DataTableTools": frozenset({
        "add_rows",
        "create",
        "diff",
        "get_rows",
        "get_schema",
        "import_file",
        "list_rows",
        "remove_rows",
        "rename_rows",
        "search_row_structs",
        "set_rows",
    }),
    "editor_toolset.toolsets.material.MaterialTools": frozenset({
        "add_expression",
        "connect_expressions",
        "connect_to_output",
        "create_function",
        "create_material",
        "create_parameter_collection",
        "delete_expression",
        "delete_parameter_group",
        "delete_unused_expressions",
        "diff_function",
        "diff_material",
        "disconnect_expressions",
        "disconnect_from_output",
        "get_expression_input_names",
        "get_expression_inputs",
        "get_expression_output_names",
        "get_expressions",
        "get_property_input",
        "get_referencing_materials",
        "get_statistics",
        "layout_expressions",
        "list_expression_classes",
        "list_parameter_groups",
        "recompile",
        "rename_parameter_group",
    }),
    "editor_toolset.toolsets.material_instance.MaterialInstanceTools": frozenset({
        "clear_parameters",
        "create",
        "diff",
        "get_scalar_parameter",
        "get_static_switch_parameter",
        "get_texture_parameter",
        "get_vector_parameter",
        "list_parameters",
        "set_parameter_override",
        "set_parent",
        "set_scalar_parameter",
        "set_static_switch_parameter",
        "set_texture_parameter",
        "set_vector_parameter",
    }),
    "editor_toolset.toolsets.object.ObjectTools": frozenset({
        "get_class",
        "get_properties",
        "list_properties",
        "reset_properties",
        "search_subclasses",
        "set_properties",
    }),
    "editor_toolset.toolsets.primitive.PrimitiveTools": frozenset({
        "add_cone",
        "add_cube",
        "add_cylinder",
        "add_sphere",
    }),
    "editor_toolset.toolsets.scene.SceneTools": frozenset({
        "add_actors_to_data_layer",
        "add_to_scene_from_asset",
        "add_to_scene_from_class",
        "create_data_layer_asset",
        "create_level",
        "delete_folder",
        "diff",
        "find_actors",
        "get_actor_asset_path",
        "get_actors_in_data_layer",
        "get_actors_in_folder",
        "get_collision_channels",
        "get_current_level",
        "get_data_layers",
        "get_folders",
        "is_actor_hidden",
        "load_actors",
        "load_level",
        "remove_actors_from_data_layer",
        "remove_from_scene",
        "rename_folder",
        "replace_actors_with_asset",
        "replace_actors_with_class",
        "save_actor",
        "set_actor_folder",
        "set_actor_hidden",
        "trace_world",
        "unload_actors",
    }),
    "editor_toolset.toolsets.skeletal_mesh.SkeletalMeshTools": frozenset({
        "add_socket",
        "assign_physics_asset",
        "get_bone_children",
        "get_bone_names",
        "get_bone_parent",
        "get_bounds",
        "get_lod_count",
        "get_material",
        "get_material_slots",
        "get_morph_target_names",
        "get_physics_asset",
        "get_section_count",
        "get_skeleton",
        "get_socket_bone",
        "get_socket_names",
        "get_socket_transform",
        "get_vertex_count",
        "import_file",
        "remove_socket",
        "rename_socket",
        "set_material",
        "set_socket_transform",
    }),
    "editor_toolset.toolsets.static_mesh.StaticMeshTools": frozenset({
        "generate_convex_collisions",
        "generate_lods",
        "get_bounds",
        "get_lod_count",
        "get_lod_thresholds",
        "get_material",
        "get_material_slots",
        "get_triangle_count",
        "get_vertex_count",
        "import_file",
        "is_nanite_enabled",
        "remove_collisions",
        "remove_lods",
        "set_lod_thresholds",
        "set_material",
        "set_nanite_enabled",
    }),
    "editor_toolset.toolsets.texture.TextureTools": frozenset({
        "export_png",
        "get_size",
        "import_file",
        "read_texture",
    }),
}


CREATOR_EXPOSED_TOOLSETS = frozenset(CREATOR_EXPOSED_TOOLS)


def short_tool_name(toolset_name: str, tool_name: str) -> Optional[str]:
    """Return a short tool name, rejecting a mismatched qualified name."""
    if not isinstance(toolset_name, str) or not isinstance(tool_name, str):
        return None
    if not toolset_name or not tool_name:
        return None

    prefix = toolset_name + "."
    if tool_name.startswith(prefix):
        short_name = tool_name[len(prefix):]
        return short_name if short_name and "." not in short_name else None
    if "." in tool_name:
        return None
    return tool_name


def is_tool_allowed(toolset_name: str, tool_name: str) -> bool:
    """Return whether the exact tool is creator-exposed by this policy."""
    allowed_tools = CREATOR_EXPOSED_TOOLS.get(toolset_name)
    if allowed_tools is None:
        return False
    short_name = short_tool_name(toolset_name, tool_name)
    return short_name is not None and short_name in allowed_tools


SAVE_ALL_GUARDED_TOOLS = frozenset({
    ("editor_toolset.toolsets.asset.AssetTools", "save_assets"),
})


def check_unscoped_save(
    toolset_name: str,
    tool_name: str,
    arguments: Optional[dict],
    allow_save_all: bool = False,
) -> None:
    """Refuse an AssetTools.save_assets call that would save every dirty asset.

    UEFN saves every unsaved asset in the project, other people's work
    included, when save_assets gets an empty path list. Callers must name the
    assets they changed, or pass allow_save_all=True on purpose.
    """
    short_name = short_tool_name(toolset_name, tool_name)
    if (toolset_name, short_name) not in SAVE_ALL_GUARDED_TOOLS or allow_save_all:
        return
    paths = (arguments or {}).get("asset_paths")
    if (
        not isinstance(paths, list)
        or not paths
        or not all(isinstance(path, str) and path.strip() for path in paths)
    ):
        raise ValueError(
            "save_assets needs a non-empty asset_paths list of the assets you "
            "changed: an empty list saves every unsaved asset in the project. "
            "Pass allow_save_all=true only when saving everything is intended."
        )
