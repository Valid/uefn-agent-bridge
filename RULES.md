# UEFN Hybrid Python Bridge - LLM Rules File

> Drop this file into your AI tool's context to generate working UEFN Python
> scripts and safe hybrid tool calls. Works with any LLM, including Claude,
> GPT, Gemini, and Llama.
>
> For Cursor: copy to `.cursorrules`
> For Claude Code: copy to `CLAUDE.md`
> For OpenClaw: copy to your skill's `SKILL.md`

---

## What This Is

UEFN (Unreal Editor for Fortnite) embeds Python 3.11 for **editor automation**.
Python controls the editor: placing actors, managing assets, creating materials,
batch processing, pipeline tooling.  **Gameplay logic uses Verse, not Python.**

The **UEFN Hybrid Python Bridge** runs an HTTP server inside the editor so
external tools can send commands. You can also write scripts that run directly
inside UEFN via `Tools > Execute Python Script`.

The bridge uses this route order:

1. Existing named bridge commands use direct embedded Python.
2. UEFN-specific aliases and generic `toolsets.*` calls use Epic's in-process
   Toolset Registry.
3. The official loopback UEFN MCP server is used only when the local capability
   or registry is proven unavailable before execution.

All access is programmatic. Never use browser, desktop, clipboard UI, or other
computer-control automation as a substitute for the bridge. The internal
clipboard helpers documented below are programmatic Unreal editor operations,
not UI automation.

### Hybrid routing rules

- Prefer a named direct Python command whenever it covers the task.
- Prefer a friendly UEFN alias when Python cannot cover the task.
- Use `toolsets.describe` before an unfamiliar generic tool call, then call the
  exact toolset and tool names with `toolsets.call`.
- Use `mcp.*` only to force or diagnose the official MCP transport. Do not use
  forced MCP for a capability already covered by Python or the local registry.
- Inspect the response `route`: `python`, `toolset_registry`, or `mcp`.
- If a failure contains `"outcome_unknown": true`, stop. Inspect current editor
  state before deciding whether another mutation is necessary.
- Never replay a file write, build, session control, entity mutation, device
  mutation, or event binding after a timeout, disconnect, or malformed result.
- Do not place aliases, `toolsets.*`, or `mcp.*` calls inside `batch.exec`.
- Do not overlap Toolset Registry or MCP calls. MCP serializes upstream HTTP
  requests, and registry dispatch runs on UEFN's main editor thread.

## Environment

- Python **3.11** (embedded in UEFN, no pip, no venv, stdlib only)
- The `unreal` module is always available inside the editor
- All `unreal.*` calls **must** run on the main editor thread
- Python network APIs are available. This bridge restricts its MCP transport to
  loopback, while `store.install` performs an explicit external HTTPS download.
  The bridge also reads its own release manifest over HTTPS once per start
  (`bridge.check_update`), and `bridge.update` downloads verified bridge files.
- Filesystem access is not assumed to be sandboxed. Validate every write path;
  T3D export and `store.install` use the system temporary directory.

### Required UEFN configuration

1. Enable `Python Editor Scripting` in Project Settings.
2. Enable `UEFN MCP Toolsets` for creator-toolset and MCP capabilities.
3. In Editor Preferences under `General > Model Context Protocol`, enable
   `Auto Start Server`.
4. Run `bridge/server.py` with `Tools > Execute Python Script`, or copy
   `bridge/startup.py` to `<Project>/Content/Python/init_unreal.py` alongside
   the `bridge/` package for project auto-start.

The bridge defaults to `http://127.0.0.1:9210`. Epic's MCP server defaults to
`http://127.0.0.1:8000/mcp`. The direct Python bridge remains useful if Epic's
MCP server is disabled, but UEFN-specific capability gaps cannot use the final
fallback route.

## Key Entry Points

These are the subsystems and libraries you'll use most:

### Subsystems (get via `unreal.get_editor_subsystem(ClassName)`)

| Subsystem | Methods | Use For |
|-----------|---------|---------|
| `EditorActorSubsystem` | 45 | Spawn, delete, select, query actors |
| `EditorAssetSubsystem` | 66 | Asset CRUD, metadata, validation |
| `LevelEditorSubsystem` | 49 | Level management, sub-levels |
| `StaticMeshEditorSubsystem` | 87 | LODs, collisions, UVs, Nanite |
| `EditorValidatorSubsystem` | 30 | Save-time validation rules |

### Static Libraries (call directly: `unreal.ClassName.method()`)

| Library | Methods | Use For |
|---------|---------|---------|
| `EditorAssetLibrary` | 62 | load/save/delete/rename/list assets |
| `EditorLevelLibrary` | 60 | Spawn actors, viewport camera, world |
| `MaterialEditingLibrary` | 89 | Material nodes, instances, parameters |
| `EditorUtilityLibrary` | N/A | Selected assets in Content Browser |
| `EditorFilterLibrary` | N/A | Filter actors by tag/class/layer |
| `AssetToolsHelpers` | N/A | `.get_asset_tools()` for import/create |
| `AssetRegistryHelpers` | N/A | `.get_asset_registry()` for search |
| `KismetMathLibrary` | N/A | Math utilities |

## Common Patterns

### Spawning Actors

```python
import unreal

# From an asset (mesh, blueprint, etc.)
loc = unreal.Vector(500, 0, 100)
rot = unreal.Rotator(roll=0, pitch=0, yaw=45)  # always by keyword: positional order is (roll, pitch, yaw)
asset = unreal.EditorAssetLibrary.load_asset("/Engine/BasicShapes/Cube")
actor = unreal.EditorLevelLibrary.spawn_actor_from_object(asset, loc, rot)
actor.set_actor_label("MyCube")

# From a class
light = unreal.EditorLevelLibrary.spawn_actor_from_class(
    unreal.PointLight, unreal.Vector(0, 0, 300), unreal.Rotator()
)
```

### Actor Transforms

```python
actor.set_actor_location(unreal.Vector(100, 200, 0), False, False)
actor.set_actor_rotation(unreal.Rotator(roll=0, pitch=0, yaw=90), False)
actor.set_actor_scale3d(unreal.Vector(2, 2, 2))

# Read back
loc = actor.get_actor_location()  # returns unreal.Vector
rot = actor.get_actor_rotation()  # returns unreal.Rotator
```

### Querying Actors

```python
sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

# All actors
all_actors = sub.get_all_level_actors()

# Filter by class
lights = [a for a in all_actors if a.get_class().get_name() == "PointLight"]

# Selected actors
selected = sub.get_selected_level_actors()

# Find by label
target = next((a for a in all_actors if a.get_actor_label() == "MyCube"), None)
```

### Asset Operations

```python
# List
paths = unreal.EditorAssetLibrary.list_assets("/Game/Materials/", recursive=True)

# Load
mat = unreal.EditorAssetLibrary.load_asset("/Game/Materials/M_Base")

# Check existence
exists = unreal.EditorAssetLibrary.does_asset_exist("/Game/Materials/M_Base")

# Rename / move
unreal.EditorAssetLibrary.rename_asset("/Game/Old/Asset", "/Game/New/Asset")

# Duplicate
unreal.EditorAssetLibrary.duplicate_asset("/Game/Source", "/Game/Copy")

# Delete
unreal.EditorAssetLibrary.delete_asset("/Game/Unused/Asset")

# Save
unreal.EditorAssetLibrary.save_asset("/Game/Materials/M_Modified")

# Import external files (FBX, glTF, etc.)
task = unreal.AssetImportTask()
task.filename = "C:/Art/model.fbx"
task.destination_path = "/Game/Meshes"
task.replace_existing = True
task.automated = True
task.save = True
unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
```

### Asset Registry Search

```python
reg = unreal.AssetRegistryHelpers.get_asset_registry()
filt = unreal.ARFilter()
filt.package_paths = ["/Game/"]
filt.recursive_paths = True
# filt.class_names = ["Material"]  # optional class filter
results = reg.get_assets(filt)
for asset_data in results:
    print(asset_data.asset_name)
```

### Materials

```python
# Create a material
tools = unreal.AssetToolsHelpers.get_asset_tools()
mat = tools.create_asset("M_Custom", "/Game/Materials",
                          unreal.Material, unreal.MaterialFactoryNew())

# Add nodes
color = unreal.MaterialEditingLibrary.create_material_expression(
    mat, unreal.MaterialExpressionConstant3Vector, -300, 0
)
color.set_editor_property("constant", unreal.LinearColor(1.0, 0.0, 0.0, 1.0))

# Connect to material output
unreal.MaterialEditingLibrary.connect_material_property(
    color, "RGB", unreal.MaterialProperty.MP_BASE_COLOR
)

# Material instances
factory = unreal.MaterialInstanceConstantFactoryNew()
mi = tools.create_asset("MI_Red", "/Game/Materials",
                         unreal.MaterialInstanceConstant, factory)
mi.set_editor_property("parent", mat)
unreal.MaterialEditingLibrary.set_material_instance_scalar_parameter_value(
    mi, "Roughness", 0.3
)
unreal.MaterialEditingLibrary.set_material_instance_vector_parameter_value(
    mi, "BaseColor", unreal.LinearColor(0.8, 0.1, 0.1, 1.0)
)
```

### Static Mesh Processing

```python
mesh_sub = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)

# Auto-generate LODs
mesh = unreal.EditorAssetLibrary.load_asset("/Game/Meshes/SM_Rock")
# mesh_sub.set_lods(mesh, ...)

# Add collision
mesh_sub.add_simple_collisions(mesh, unreal.ScriptingCollisionShapeType.BOX)

# Enable Nanite
# mesh_sub.set_nanite_settings(mesh, enabled=True)

# Get stats
verts = mesh_sub.get_number_verts(mesh, 0)  # LOD 0
print(f"Vertices: {verts}")
```

### Viewport Camera

```python
# Get current camera
loc, rot = unreal.EditorLevelLibrary.get_level_viewport_camera_info()

# Set camera
unreal.EditorLevelLibrary.set_level_viewport_camera_info(
    unreal.Vector(1000, 0, 500),
    unreal.Rotator(roll=0, pitch=-30, yaw=0)
)
```

### Progress Dialogs (for long operations)

```python
total = 100
with unreal.ScopedSlowTask(total, "Processing...") as task:
    task.make_dialog(True)  # show cancel button
    for i in range(total):
        if task.should_cancel():
            break
        task.enter_progress_frame(1, f"Step {i+1}/{total}")
        # do work here
```

### Async Work (spread across frames)

```python
class FrameWorker:
    def __init__(self):
        self.frame = 0
        self.max_frames = 100
        self.handle = None

    def start(self):
        self.handle = unreal.register_slate_post_tick_callback(self.tick)

    def tick(self, dt):
        # do per-frame work here
        self.frame += 1
        if self.frame >= self.max_frames:
            unreal.unregister_slate_post_tick_callback(self.handle)

worker = FrameWorker()
worker.start()
```

## Bridge HTTP Protocol

When using the bridge, external tools send HTTP POST requests to
`http://127.0.0.1:9210`:

Use `Content-Type: application/json`. The server rejects request bodies above
16 MiB and browser origins that are not loopback. The bundled clients bypass
environment proxies for loopback traffic.

```json
{
  "command": "actors.spawn",
  "params": {
    "asset_path": "/Engine/BasicShapes/Cube",
    "location": [500, 0, 100]
  }
}
```

Every command-result envelope includes the route that handled the request.
HTTP protocol rejections such as invalid JSON, content type, origin, size, or a
stopping server can omit `route` because no command was dispatched:

```json
{
  "ok": true,
  "route": "python",
  "result": {
    "actor": {
      "name": "StaticMeshActor_0",
      "label": "Cube",
      "class": "StaticMeshActor",
      "path": "...",
      "location": {"x": 500, "y": 0, "z": 100},
      "rotation": {"pitch": 0, "yaw": 0, "roll": 0},
      "scale": {"x": 1, "y": 1, "z": 1}
    }
  }
}
```

Possible routes are `python`, `toolset_registry`, and `mcp`. An MCP fallback
also reports `fallback_reason`. If a dispatched mutation times out or the
client cannot determine its result, the failure includes:

```json
{
  "ok": false,
  "route": "toolset_registry",
  "outcome_unknown": true,
  "error": "The operation may still complete."
}
```

Never retry an `outcome_unknown` mutation automatically.

### Direct Python commands

These commands always stay on embedded Python:

| Command | Description |
|---|---|
| `status` | Bridge version, uptime, available commands |
| `exec` | Run arbitrary Python (assign to `result` to return values) |
| `log` | Recent bridge log entries |
| `history` | Recent command execution history with timing |
| `actors.list` | List actors (optional `class_filter`) |
| `actors.selected` | Currently selected actors |
| `actors.spawn` | Spawn from `asset_path` or `actor_class` |
| `actors.duplicate` | Duplicate an actor |
| `actors.delete` | Delete by path or label (`targets` array) |
| `actors.transform` | Set location/rotation/scale |
| `actors.properties` | Read actor properties |
| `actors.set_property` | Write an actor property |
| `actors.set_color` | Create and apply a persistent solid-color material |
| `assets.list` | List assets in directory |
| `assets.info` | Asset metadata |
| `assets.exists` | Check if asset exists |
| `assets.rename` | Rename/move asset |
| `assets.duplicate` | Copy asset |
| `assets.delete` | Delete asset |
| `assets.save` | Save modified asset |
| `assets.search` | Asset Registry search |
| `assets.selected` | Selected in Content Browser |
| `assets.import_task` | Import external file (FBX, glTF) |
| `level.info` | World name, actor count |
| `level.save` | Save current level |
| `viewport.camera` | Get camera position/rotation |
| `viewport.set_camera` | Move viewport camera |
| `materials.create_instance` | Create material instance with params |
| `verse.find_device` | Find placed Verse device actors by internal name or label query |
| `verse.export_t3d` | Export one device's complete serialized state |
| `verse.get_properties` | Read supported properties from a generated subobject path |
| `verse.set_property` | Write numeric and object-reference properties |
| `batch.exec` | Run multiple commands in one tick |
| `undo` | Undo last action |
| `redo` | Redo last undone action |
| `fix_throttle` | Disable editor background CPU throttling |
| `reload` | Reload the bridge and its sibling modules from disk |
| `bridge.check_update` | Check whether a newer bridge is published |
| `bridge.update` | Install the published bridge files after verifying them, then reload |
| `shutdown` | Stop the bridge after flushing the response |
| `store.install` | Download and install a persistent helper from the bridge store |

`batch.exec` is for direct Python commands only. It rejects friendly aliases,
`toolsets.*`, and `mcp.*` because Epic tool calls are asynchronous and must be
serialized.

### UEFN-specific friendly aliases

The following 42 aliases call Epic's in-process Toolset Registry first and use
the official MCP server only on a safe capability miss:

| Domain | Commands |
|---|---|
| Python setup | `uefn.python_enabled`, `uefn.enable_python` |
| Verse files and build | `verse.write_file`, `verse.replace`, `verse.read_file`, `verse.move`, `verse.list_files`, `verse.grep`, `verse.delete`, `verse.create_directory`, `verse.copy`, `verse.build` |
| PIC session | `session.stop`, `session.stop_game`, `session.start`, `session.start_game`, `session.push_changes`, `session.status`, `session.game_state`, `session.client_log` |
| Devices | `devices.set_property`, `devices.remove_event_binding`, `devices.place`, `devices.list_event_bindings`, `devices.list_properties`, `devices.list_assets`, `devices.get_properties`, `devices.get_binding_options`, `devices.add_event_binding` |
| Scene Graph entities | `entities.set_transform`, `entities.set_component_property`, `entities.remove_component`, `entities.list_classes`, `entities.list_component_properties`, `entities.list_component_classes`, `entities.get_transform`, `entities.get_components`, `entities.get_component_property`, `entities.find`, `entities.delete`, `entities.create`, `entities.add_component` |

Pass each official tool's arguments directly inside bridge `params`. Inspect the
schema first when parameter names or reference shapes are not already known.
When a schema requests an Unreal object reference, it commonly uses an object
such as `{"refPath":"/Path/To/Object"}`. Do not use that shape when a tool
expects a plain string.

```json
{
  "command": "session.status",
  "params": {},
  "timeout_s": 120
}
```

### Generic creator-toolset commands

Local routing is pinned to the exact creator-exposed policy from UEFN v42,
revision `UEFN-v42-CL-56878558`: 29 approved toolsets and 384 approved tools.
The bridge does not expose registered editor-internal tools through the local
path.

| Command | Description |
|---|---|
| `toolsets.list` | List approved toolsets and tool counts |
| `toolsets.describe` | Return one full toolset schema |
| `toolsets.call` | Call an exact toolset and tool name |

Use exact names because tool names can collide between toolsets:

```json
{
  "command": "toolsets.call",
  "params": {
    "toolset_name": "ValkyrieToolset.SessionToolset",
    "tool_name": "GetSessionStatus",
    "arguments": {},
    "allow_mcp_fallback": true
  },
  "timeout_s": 120
}
```

Set `allow_mcp_fallback` to `false` to require the in-process registry. Local
schemas are cached. Pass `refresh: true` to `toolsets.list` or
`toolsets.describe` after Epic refreshes its tools.

### Forced MCP commands

The official MCP endpoint defaults to `http://127.0.0.1:8000/mcp`. Set
`UEFN_MCP_URL` before launching the bridge or call `mcp.configure` with another
loopback URL. Non-loopback MCP endpoints are rejected.

| Command | Description |
|---|---|
| `mcp.status` | Initialize MCP and report the negotiated server state |
| `mcp.list_toolsets` | List toolsets through MCP only |
| `mcp.describe_toolset` | Fetch one schema through MCP only |
| `mcp.call` | Invoke one tool through MCP only |
| `mcp.configure` | Change the loopback endpoint and reset the session |
| `mcp.reset` | Discard the session and discovery caches |

MCP uses Streamable HTTP and a lazy session. Requests are serialized through
one mutex, and tool discovery is cached per session. Epic documents UEFN editor
hitching or hanging during MCP calls, so do not force MCP for work already
covered by direct Python or the local Toolset Registry.

Official documentation:

- <https://www.fortnite.com/news/unreal-mcp-is-now-available-in-uefn>
- <https://dev.epicgames.com/documentation/fortnite/uefn-mcp>
- <https://dev.epicgames.com/documentation/unreal-engine/unreal-mcp-in-unreal-editor>

## API Surface (37K+ types)

UEFN exposes 37,276 Python types, 4.3x more than standard UE5. The major
domains with full read/write support:

| Domain | Key Classes | Capability |
|--------|------------|------------|
| **Actors & Levels** | EditorActorSubsystem, EditorLevelLibrary | Full CRUD |
| **Assets** | EditorAssetLibrary, AssetTools, AssetRegistry | Full CRUD + import |
| **Materials** | MaterialEditingLibrary (89 methods) | Create from scratch |
| **Static Meshes** | StaticMeshEditorSubsystem (87 methods) | LODs, collision, UVs |
| **Geometry** | GeometryScriptingCore (46 classes) | Booleans, remesh, bake |
| **PCG** | 597 types | Procedural content generation |
| **Sequencer** | LevelSequence, MovieScene | Keyframes, cameras |
| **Niagara VFX** | NiagaraFunctionLibrary | Spawn, parameters |
| **Animation** | AnimSequence, AnimMontage | Create/modify |
| **Audio** | AudioMixer, MetaSound | Sound management |
| **Rendering** | MovieRenderPipeline (210 types) | Batch rendering |

### Fortnite-specific direct Python limits

Many Fortnite types exposed to Python are useful mainly for inspection. Direct
Python can write normal editor actor transforms, but it cannot safely cover
every UEFN-specific device, entity, session, or Verse workflow.

Use the hybrid aliases for those gaps:

- Use `devices.*` to place and configure supported Creative devices and event
  bindings.
- Use `entities.*` for supported Scene Graph entity and component operations.
- Use the file-oriented `verse.*` aliases to read, edit, move, search, and build
  Verse source.
- Use `session.*` for Play in Client (PIC) session control and inspection.

Python and the creator toolsets do not turn editor automation into gameplay
runtime code. Gameplay logic, player interaction, and game rules still belong
in Verse. File and build tools do not provide arbitrary calls into running Verse
functions.

### Session and coordinate caveats

Epic's session tools target PIC, not Play in Editor (PIE). Treat a
`StillRunning` result as transient: poll `session.status` before issuing another
session mutation.

Python and the creator toolset JSON schemas express vectors as X, Y, and Z.
UEFN Scene Graph and `/Verse.org/SpatialMath` use Forward, Left, and Up (LUF).
For `entities.*`, send the X, Y, and Z fields required by the tool schema even
though Verse represents the affected transform as LUF. Do not swap or rename
axes by intuition. The older `/UnrealEngine.com/Temporary/SpatialMath` types are
distinct from `/Verse.org/SpatialMath` and require documented conversions.

## Editing Placed Verse Devices (@editable values)

### Mental model

A placed Verse device is a wrapper actor whose class is usually `VerseDevice_C`.
The editable data lives in a generated subobject chain below that actor:

```text
VerseDevice_C_UAID_...
  generated_verse_class_0
    nested_config_1
      array_element_2
```

Generated property names look like `__verse_0xHASH_FieldName`.
Always copy the exact serialized property name from T3D.
Never guess the hash or strip it from the name.

Object references inside a block can be relative names such as `'config_2'`.
Subobject names are unique only within their outer object.
Use full object paths when loading objects from Python.

Verse enums serialize by value name.
Delta serialization omits an enum property when it has the default value.
An absent enum line means default, not missing data.

Verse options serialize with parentheses around their contained value.
Arrays serialize as indexed property lines such as `MyArray(0)=...`.
The indexed lines define array order, not numeric suffixes in object names.

### Universal read path: T3D export

Use `verse.find_device` to resolve the wrapper actor by internal name.
Labels are not unique, so keep the returned internal `UAID` name and full path.

Use `verse.export_t3d` to export only that actor.
Large devices can produce multi-megabyte files, so use `timeout_s` when needed.
Parse the file programmatically and never dump raw T3D into the conversation.

Use a depth-aware stack parser for `Begin Object` and `End Object` lines.
The `t3d_parse` helper is available inside the `exec` namespace.
Every subobject appears twice in an export:

1. A declaration block has `Class=`, `Name=`, and `ExportPath=`.
2. An instance block has no `Class=` and contains serialized property values.

Match blocks by full `ExportPath`, not by `Name` alone.
Treat the exported T3D as ground truth for current values.
Read the authored Verse source first for type meaning and runtime overrides.

### What plain Python property access can write

Load generated objects with `unreal.load_object(None, full_path)`.
Use exact serialized names with `get_editor_property` and `set_editor_property`.
The bridge commands `verse.get_properties` and `verse.set_property` cover this path.

Proven writable through Python:

- Numeric and float properties.
- Object references, including `SavedActor`.
- Arrays of object references by reading, appending, and setting the list back.
- Moving a subobject with `obj.rename(name=..., outer=...)`.
- Creating a subobject with `unreal.new_object(cls, outer=..., name=...)`.

`VerseStringProperty` reads and writes always fail through Python conversion.
Verse enum and option conversion is not reliable enough to use directly.
Use the editor text-import clipboard route for strings, enums, and options.
Never use the class-wide `SET` console command because it can change every instance.

### Clipboard T3D roundtrip

The `exec` namespace provides `clip_get`, `clip_set`, `export_t3d`,
`t3d_parse`, and `find_actor` so the entire roundtrip stays in one call.
Run all steps in one `exec` call and one `ScopedEditorTransaction`.
Use `try/finally` to restore selection and clipboard state and destroy temp actors.

1. Load the real owner and array by full path.
   Assert the current length and expected donor object.
   Assert the destination name does not already exist.
   Verify every referenced asset with `EditorAssetLibrary.does_asset_exist`.

2. Save the current selection and clipboard text.
   Select only the real device actor.
   Run `EDIT COPY`, wait briefly, then call `clip_get()`.
   Assert the copied text contains a known marker.

3. Parse the copied text with a stack parser.
   Locate the donor in both declaration and instance segments.
   Match `Name` plus the owning path in `ExportPath` because names repeat.
   Locate the final indexed array property line on the real owner.

4. Clone the donor declaration block and instance block.
   Choose the next free numeric suffix under that outer.
   Replace the donor name in `Name=` and `ExportPath=` values.
   Keep nested declaration names scoped inside the cloned block.
   Change only the required string, enum, option, asset, or actor values.
   Insert a missing enum line when the donor uses its omitted default.
   Append the new indexed array line with matching indentation.
   Apply text insertions in descending line-index order.

5. Call `clip_set(new_text)` and run `EDIT PASTE`.
   Diff actor internal names before and after the paste.
   Assert exactly one temporary duplicate actor appeared.

6. Load each new element below the temporary actor.
   Load its real destination outer by full path.
   Call `real_owner.modify()` before mutation.
   Rename the element into the real outer with `rename`.
   Append the element to the real object-reference array.
   Set the array property back and assert the new length.

7. Destroy the temporary actor after every transplant succeeds.
   In `finally`, destroy it if it still exists.
   Restore the previous selection and original clipboard text.

### Verification and saving

Read back array lengths, object names, outers, references, and numeric values.
Re-export T3D with `export_t3d` or `verse.export_t3d`.
Search for every exact expected string, enum, option, and array line.
Confirm pre-existing entries remain unchanged.
Only save the level after both Python readback and T3D verification pass.
Writes mutate the open level, so get user approval before editing or saving.

### Hard rules for device edits

- Use exact serialized names and hashes from T3D only.
- Make scripts idempotent by checking names and values before adding anything.
- Never edit `.uasset`, `.umap`, `__ExternalActors__`, or `__ExternalObjects__` on disk.
- Resolve placed actors by internal name, not label.
- Never run shell subprocesses from editor Python.
- Avoid `unreal.ObjectIterator` sweeps because they can crash the editor.
- Verify every referenced asset or actor exists before writing it.
- Never replace the real device actor or import a whole map.

## Critical Rules

1. **Thread safety**: All `unreal.*` calls must run on the main thread. If you are
   writing a script that runs directly in UEFN, you are already on the main
   thread. The bridge handles main-thread dispatch and async-result polling.

2. **No pip**: UEFN's embedded Python has no package manager. Only stdlib and
   `unreal` are available inside the editor.

3. **Editor only**: Python runs in the editor, not at game runtime. Use Verse
   for gameplay logic, player interaction, and game rules.

4. **Save explicitly**: Changes to assets are not saved automatically. Call
   `unreal.EditorAssetLibrary.save_asset(path)` or `save_current_level()`.

5. **Path format**: Asset paths use forward slashes and start with `/Game/`:
   `/Game/Materials/M_Base`, `/Engine/BasicShapes/Cube`.

6. **Vectors and Rotators**: For Python, construct `unreal.Vector(x, y, z)` and
   `unreal.Rotator(roll=..., pitch=..., yaw=...)`. Always pass rotator values
   by keyword: the positional order is (roll, pitch, yaw), so the positional
   values `0, 90, 0` pitch the actor 90 degrees instead of turning it. The
   bridge's own `rotation` parameters are `[pitch, yaw, roll]` lists or
   `{"pitch", "yaw", "roll"}` objects. Do not confuse Python XYZ with Scene
   Graph LUF values.

7. **Error handling**: Wrap operations in try/except. `unreal.log_error(msg)`
   writes to the Output Log. Use `unreal.log_warning(msg)` for non-fatal issues.

8. **Progress for batch ops**: Use `unreal.ScopedSlowTask` for any operation
   touching 50+ assets/actors to show progress and allow cancellation.

9. **Fastest valid route**: Use named direct Python commands first, friendly
   aliases second, generic approved toolsets third, and forced MCP only for
   diagnostics or a capability that cannot use the earlier routes.

10. **No blind replay**: Never retry a mutation when `outcome_unknown` is true.
    Read back editor state first. A timeout is not proof that a mutation failed.

11. **Exact tool identity**: For generic calls, use the complete toolset name and
    exact tool name from `toolsets.describe`. Do not infer tools from registered
    editor internals or bypass the pinned creator-exposed policy.

12. **Serialized calls**: Do not overlap Toolset Registry or MCP operations, and
    do not put them in `batch.exec`. Poll PIC session state explicitly when an
    operation reports `StillRunning`.
