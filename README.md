# UEFN Python Bridge: control Unreal Editor for Fortnite from AI agents, scripts and CI

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) ![UEFN v42](https://img.shields.io/badge/UEFN-v42-informational) ![Python 3.11 in UEFN](https://img.shields.io/badge/python-3.11%20(embedded)-informational)

[UEFN Python Bridge](https://github.com/Valid/uefn-python-bridge) lets your AI agent build in Unreal Editor for Fortnite for you. Claude Code, ChatGPT Codex or any script can place and wire devices, write and build Verse, and push to a live session, fast and safely enough to leave running unattended. Free and open source; join us on [Discord](https://discord.gg/fchq).

It is a plain HTTP + JSON server with no dependencies beyond UEFN's embedded Python, so it works from any language. A stdlib-only Python client is included.

## Why use the bridge

- **Fast by default.** Commands run as embedded Python on the editor thread. Epic's creator toolsets are called in-process through the Toolset Registry, and the official UEFN MCP server is only the last resort, so the common path skips MCP's HTTP and discovery overhead and the editor hangs Epic lists as a known MCP issue.
- **Safe for unattended automation.** One editor command runs at a time. A call that may have started and then timed out or disconnected is reported as `outcome_unknown`, and the bridge never replays it on another route. Check the editor's state before you retry a change yourself, so you do not duplicate a device, entity, file write or session. Add `"if_busy": "reject"` to fail fast instead of queueing.
- **Tells you what the editor is doing.** `status` and `GET /` answer instantly with the running command, the queue and the time since the editor last ticked, so a slow build looks different from a modal dialog or a hung editor.
- **Realistic deadlines.** Session start, push and Verse build get up to 10 minutes by default instead of timing out while they still work; any request can set its own `timeout_s`.
- **Honest results.** Python that raises fails the command with its traceback and output; deletes report what they could not delete; rotations follow the documented `[pitch, yaw, roll]`.
- **Covers what `unreal.*` cannot.** Friendly names for all 42 UEFN-specific tools (Verse files and builds, play sessions, Creative devices, Scene Graph entities) plus generic access to every approved creator toolset.
- **Stays current.** The bridge checks for a newer release at startup, and `bridge.update` installs it after verifying every file, keeping a backup. Automatic installs are opt-in.
- **Local only.** Loopback addresses only, cross-origin web pages are rejected, MCP is restricted to loopback, and nothing drives the desktop or UI.

## What you can automate

| Task | Commands |
|---|---|
| Spawn, move, duplicate, delete and inspect actors | `actors.spawn`, `actors.transform`, `actors.duplicate`, `actors.delete`, `actors.list`, `actors.properties` |
| Find, import, rename and save assets | `assets.search`, `assets.import_task`, `assets.rename`, `assets.save` |
| Place Creative devices and wire events | `devices.place`, `devices.set_property`, `devices.add_event_binding` |
| Edit Verse device `@editable` properties on placed devices | `verse.find_device`, `verse.get_properties`, `verse.set_property` |
| Read, write, search and build Verse | `verse.read_file`, `verse.write_file`, `verse.grep`, `verse.build` |
| Launch a play session, start a match, push changes | `session.start`, `session.start_game`, `session.push_changes`, `session.status` |
| Scene Graph entities and components | `entities.create`, `entities.add_component`, `entities.set_component_property` |
| Material instances, viewport camera, level save | `materials.create_instance`, `viewport.set_camera`, `level.save` |
| Anything else in the editor | `exec` (run Python), `batch.exec` (several commands in one undo step), `toolsets.call` |

## Quick Start

Paste this into an AI coding agent:

> Clone https://github.com/Valid/uefn-python-bridge and help me set it up with my UEFN project.

The included [`RULES.md`](RULES.md) explains the routing contract and working UEFN Python patterns.

## Manual Setup

### 1. Enable embedded Python

In UEFN Project Settings, enable `Python Editor Scripting`. This is the only requirement for the existing direct Python commands.

### 2. Enable Epic's creator toolsets

For the Toolset Registry and MCP capabilities, enable `UEFN MCP Toolsets` in Project Settings. Apply any restart requested by UEFN.

### 3. Enable the official MCP server

In Editor Preferences, open `General > Model Context Protocol` and enable `Auto Start Server`.

The default endpoint is:

```text
http://127.0.0.1:8000/mcp
```

The bridge accepts only a loopback MCP endpoint. To use a different loopback port or path, either set `UEFN_MCP_URL` in UEFN's process environment before running the bridge or configure it at runtime:

```json
{
  "command": "mcp.configure",
  "params": {"url": "http://127.0.0.1:8123/mcp"}
}
```

You can also manage Epic's server from the UEFN console with `ModelContextProtocol.StartServer [port]`, `ModelContextProtocol.StopServer`, and `ModelContextProtocol.RefreshTools`.

### 4. Run the bridge

In UEFN, choose `Tools > Execute Python Script` and select `bridge/server.py`.

The Output Log should include:

```text
[Bridge 12:34:56] Bridge v0.5.0 listening on http://127.0.0.1:9210
[Bridge 12:34:56] 92 commands registered
```

The bridge checks ports 9210 through 9220 and uses the first available port.
If it selects a port other than 9210, pass the logged port to
`UEFNBridge(port=...)` or use that port in raw HTTP requests.

## How Routing Works: Python First, Toolsets Next, MCP Last

Every request follows a deterministic route:

| Priority | Response `route` | Used for |
|---|---|---|
| 1 | `python` | Existing bridge commands for actors, assets, levels, materials, viewport control, and placed Verse-device inspection |
| 2 | `toolset_registry` | Epic creator-exposed toolsets called directly inside UEFN, without MCP transport overhead |
| 3 | `mcp` | Official loopback UEFN MCP when the requested capability or local registry is unavailable before dispatch |

Named direct commands never detour through MCP. The 42 UEFN-specific friendly aliases and `toolsets.*` commands try the local Toolset Registry first. The `mcp.*` commands explicitly bypass local routing for diagnostics or compatibility testing.

Fallback is mutation-safe. Once a Toolset Registry or MCP call may have started, a timeout, disconnect, malformed response, or unknown result is returned with `"outcome_unknown": true` and is not replayed through another route. This prevents duplicate entities, devices, file writes, sessions, or event bindings.

### Why hybrid

UEFN's Python API is fast and broad for normal editor automation, but some Fortnite-specific operations are not available through `unreal.*`. Epic's creator toolsets add Verse-file, play-session, device, and Scene Graph entity capabilities. Calling those toolsets through the in-process registry avoids the fixed HTTP and discovery cost of MCP whenever possible.

Epic currently lists editor slowdowns and hangs during MCP calls as a known issue. Keeping MCP as the final route makes the common path responsive while retaining the capabilities that only Epic's toolsets expose.

Official references:

- [Unreal MCP is now available in UEFN](https://www.fortnite.com/news/unreal-mcp-is-now-available-in-uefn)
- [UEFN MCP setup and known issues](https://dev.epicgames.com/documentation/fortnite/uefn-mcp)
- [Unreal MCP architecture](https://dev.epicgames.com/documentation/unreal-engine/unreal-mcp-in-unreal-editor)

## Sending Commands

Every command is a POST to `http://127.0.0.1:9210` with a JSON body:

Requests must use `Content-Type: application/json` and are limited to 16 MiB.
Browser requests with a non-loopback `Origin` are rejected, and both bundled
clients bypass environment HTTP proxies for their loopback connections.

```json
{"command": "actors.list"}
```

```json
{
  "command": "actors.spawn",
  "params": {
    "actor_class": "PointLight",
    "location": [0, 0, 200]
  }
}
```

```json
{
  "command": "session.status",
  "timeout_s": 120
}
```

The complete response envelope includes the route that actually handled the request:

```json
{
  "ok": true,
  "route": "toolset_registry",
  "result": {}
}
```

An MCP fallback also includes `fallback_reason`. Failures whose side effect cannot be determined include `outcome_unknown`.

### Busy, Stalled, and Deadlines

The bridge serves one editor command at a time; other requests wait their turn. `status` and `GET /` never wait, and both report what the bridge is doing:

```json
"activity": {
  "editor_command": {"command": "verse.build", "state": "waiting_on_tool", "seconds": 41.2},
  "dispatch_turn": {"command": "verse.build", "seconds": 41.3},
  "waiting": 1,
  "editor_tick_age_s": 0.03
}
```

- `editor_command` is the command running on the editor thread (`state` is `running`, `waiting_on_tool` while a native tool works, or `outlived_caller` while a native tool keeps working after its caller timed out), and `dispatch_turn` is the request being served. `waiting` counts the requests queued behind it.
- `editor_tick_age_s` is the time since the editor last ran the bridge's tick. It grows while the editor's game thread is blocked. A large value with `editor_command` set means a bridge command is slow; with `editor_command` empty, something outside the bridge holds the editor, such as a modal dialog.
- Add `"if_busy": "reject"` to a request to fail fast instead of queueing. A busy bridge answers HTTP 409 with `busy` (the same `activity` object) and starts nothing. Busy includes editor work whose own caller already timed out. The Python client raises `BridgeBusy`.
- Without `timeout_s`, a request's deadline is 30 seconds, except for tools that routinely take longer: `session.start`, `session.push_changes` and `verse.build` get 600 seconds, `session.stop` and `session.start_game` 180, and `session.stop_game` 120. The same applies to `toolsets.call` and `mcp.call` naming those tools. The Python client waits at least as long.

### Python Client

```python
from bridge.client import UEFNBridge

ue = UEFNBridge()
print(ue.status())

# Existing capability: direct embedded Python.
actors = ue.actors()
print(ue.last_route)  # python

# UEFN-specific capability: local registry first, MCP only on a safe miss.
session = ue.run("session.status", timeout_s=120)
print(ue.last_route)  # toolset_registry or mcp

# Generic approved tool lookup and call.
toolsets = ue.toolsets()
schema = ue.describe_toolset("ValkyrieToolset.SessionToolset")
status = ue.call_tool(
    "ValkyrieToolset.SessionToolset",
    "GetSessionStatus",
)

# Force MCP only when diagnosing the official transport.
mcp_status = ue.run("mcp.status", timeout_s=120)
```

`UEFNBridge.run()` returns only the result. Use `UEFNBridge.request()` when you need the complete envelope, or inspect `ue.last_response` and `ue.last_route` after a call. `UEFNBridge.planned_route(command)` explains the initial route without contacting UEFN. A failed command raises `BridgeError`; its `envelope` holds the complete failure response (for example an `exec` traceback with `stdout`). Pass `if_busy="reject"` to `run()` or `request()` to get `BridgeBusy` instead of waiting behind another command.

### curl

```bash
curl -s -X POST http://127.0.0.1:9210 \
  -H "Content-Type: application/json" \
  -d '{"command":"actors.list"}' | python -m json.tool
```

```bash
curl -s -X POST http://127.0.0.1:9210 \
  -H "Content-Type: application/json" \
  -d '{"command":"session.status","timeout_s":120}' | python -m json.tool
```

### JavaScript or TypeScript

```javascript
const response = await fetch("http://127.0.0.1:9210", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ command: "actors.list" })
});

const envelope = await response.json();
console.log(envelope.route, envelope.result);
```

## Direct Python Commands

These commands always use the embedded-Python route.

### Actors

| Command | What it does | Key params |
|---|---|---|
| `actors.list` | List level actors | `class_filter` |
| `actors.selected` | List selected actors | None |
| `actors.spawn` | Spawn from a class or asset | `actor_class` or `asset_path`, `location`, `rotation`, `label` |
| `actors.duplicate` | Clone an actor | `source`, `location`, `rotation`, `label` |
| `actors.delete` | Delete actors; reports each target it could not delete under `failed` | `targets` |
| `actors.transform` | Set location, rotation, or scale | `target`, `location`, `rotation`, `scale` |
| `actors.properties` | Read named editable properties | `target`, `properties` |
| `actors.set_property` | Set an editor property | `target`, `property_name`, `value` |
| `actors.set_color` | Create and apply a persistent solid-color material | `target`, `color` |

Locations and scales are `[x, y, z]`. Rotations are `[pitch, yaw, roll]` in degrees, or an object such as `{"pitch": 0, "yaw": 90, "roll": 0}` (the shape responses return), for `actors.spawn`, `actors.duplicate`, `actors.transform` and `viewport.set_camera`.

### Assets

| Command | What it does | Key params |
|---|---|---|
| `assets.search` | Search spawnable assets by default | `query` |
| `assets.list` | List a content directory | `directory`, `recursive` |
| `assets.info` | Inspect one asset | `path` |
| `assets.exists` | Check an asset path | `path` |
| `assets.rename` | Rename or move an asset | `old_path`, `new_path` |
| `assets.duplicate` | Copy an asset | `source`, `destination` |
| `assets.delete` | Delete an asset | `path` |
| `assets.save` | Save an asset | `path` |
| `assets.selected` | List Content Browser selection | None |
| `assets.import_task` | Import an external file | `source_file`, `destination_path` |

### Placed Verse Devices

| Command | What it does | Key params |
|---|---|---|
| `verse.find_device` | Find placed Verse device actors | `query`, `class_filter` |
| `verse.export_t3d` | Export complete serialized device state | `target`, `filename` |
| `verse.get_properties` | Read generated subobject properties | `path`, `properties` |
| `verse.set_property` | Write supported generated subobject properties | `path`, `property_name`, `value`, `is_object_ref` |

These direct `verse.*` commands inspect placed generated device objects. They are separate from the creator-toolset `verse.*` file aliases below.

### Level, Viewport, Materials, and System

| Command | What it does |
|---|---|
| `level.info` | Read current level information |
| `level.save` | Save the current level |
| `viewport.camera` | Read viewport location and rotation |
| `viewport.set_camera` | Move the viewport camera |
| `materials.create_instance` | Create a material instance with parameters |
| `status` | Return version, routing configuration, commands, and current activity |
| `exec` | Run embedded Python and return the variable named `result`. If the code raises, the command fails (`ok: false`) and still returns `result`, `stdout`, `stderr` and `traceback`; changes made before the exception stay |
| `batch.exec` | Run direct Python commands in one undo transaction and main-thread tick |
| `undo`, `redo` | Use the editor transaction history |
| `log`, `history` | Inspect bridge diagnostics |
| `reload`, `shutdown` | Manage the bridge process |
| `bridge.check_update` | Check now whether a newer bridge is published |
| `bridge.update` | Install the published bridge files, verified, then reload (Updates below) |
| `fix_throttle` | Disable background CPU throttling |
| `store.install` | Install the persistent helper store |

`batch.exec` accepts direct Python commands only. Toolset Registry and MCP calls are asynchronous and serialized, so friendly aliases and all `toolsets.*` or `mcp.*` commands are rejected inside a batch.

## UEFN-Specific Friendly Aliases

Version 0.3.0 maps all 42 UEFN-specific tools shipped with UEFN v42 to stable bridge command names. Pass the official tool arguments directly as `params`.

| Domain | Friendly aliases |
|---|---|
| Python setup | `uefn.python_enabled`, `uefn.enable_python` |
| Verse files and build | `verse.write_file`, `verse.replace`, `verse.read_file`, `verse.move`, `verse.list_files`, `verse.grep`, `verse.delete`, `verse.create_directory`, `verse.copy`, `verse.build` |
| Play session | `session.stop`, `session.stop_game`, `session.start`, `session.start_game`, `session.push_changes`, `session.status`, `session.game_state`, `session.client_log` |
| Creative devices | `devices.set_property`, `devices.remove_event_binding`, `devices.place`, `devices.list_event_bindings`, `devices.list_properties`, `devices.list_assets`, `devices.get_properties`, `devices.get_binding_options`, `devices.add_event_binding` |
| Scene Graph entities | `entities.set_transform`, `entities.set_component_property`, `entities.remove_component`, `entities.list_classes`, `entities.list_component_properties`, `entities.list_component_classes`, `entities.get_transform`, `entities.get_components`, `entities.get_component_property`, `entities.find`, `entities.delete`, `entities.create`, `entities.add_component` |

Use `toolsets.describe` before the first unfamiliar call. Epic's JSON schema is the source of truth for parameter names, required fields, object reference shapes, and result values. When a schema requests an Unreal object reference, it commonly uses a JSON object such as `{"refPath":"/Path/To/Object"}`. Do not substitute that shape where a tool expects a plain string.

## Generic Creator Toolsets

The bridge pins local routing to Epic's creator-exposed UEFN v42 policy: `UEFN-v42-CL-56878558`. The manifest contains exactly 29 toolsets and 384 approved tools. This prevents registered editor-internal toolsets or hidden tools from becoming available through the fast local path.

| Command | Purpose |
|---|---|
| `toolsets.list` | List locally approved toolsets, with MCP fallback if the registry is unavailable |
| `toolsets.describe` | Return the full schema for one toolset |
| `toolsets.call` | Call an exact `toolset_name` and `tool_name` locally first |

Example:

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

Set `allow_mcp_fallback` to `false` when you explicitly require local execution. If UEFN adds a tool after the pinned v42 policy, that tool is not executed through the local registry until the policy is reviewed. An exact generic call may still reach the official MCP server as a pre-dispatch capability fallback.

Tool names can collide across toolsets. Always provide the full toolset name and the exact tool name returned by its schema.

## Forced MCP Commands

Use these only when you intentionally need the official HTTP path:

| Command | Purpose |
|---|---|
| `mcp.status` | Initialize the MCP session and report negotiation state |
| `mcp.list_toolsets` | List toolsets through MCP |
| `mcp.describe_toolset` | Fetch one toolset schema through MCP |
| `mcp.call` | Invoke one tool through MCP |
| `mcp.configure` | Change the loopback endpoint and reset the session |
| `mcp.reset` | Close and discard the cached MCP session |

The MCP client uses Streamable HTTP, negotiates one session lazily, serializes requests through a single mutex, and caches discovery results per session. It accepts JSON and server-sent event responses. It does not use stdio, WebSocket, browser control, or UI automation.

## Mutation and Session Safety

- Treat `outcome_unknown: true` as a hard stop. Inspect current editor state before deciding whether another operation is needed.
- Do not automatically retry file writes, builds, entity or device mutations, event bindings, or session controls.
- Read-only discovery can be refreshed deliberately with `refresh: true`.
- Toolset schemas are cached locally. Use `toolsets.list` or `toolsets.describe` with `refresh: true` after a UEFN tool refresh.
- MCP discovery is cached for its session. Use `mcp.reset` after restarting Epic's server or changing its tool configuration.
- MCP requests are serialized, and Toolset Registry dispatch runs on UEFN's main editor thread. Do not issue overlapping session or mutation calls.
- If a session result reports `StillRunning`, poll `session.status` before issuing another session mutation.

Epic's UEFN session tools operate Play in Client (PIC) sessions. Do not assume Play in Editor (PIE) behavior or state.

## Coordinates and Scene Graph

UEFN currently has two coordinate naming conventions in related APIs:

- Unreal Python and the creator toolset JSON schemas use X, Y, and Z.
- UEFN Scene Graph and `/Verse.org/SpatialMath` use Forward, Left, and Up (LUF).

For `entities.*` calls, send the X, Y, and Z field names required by the tool schema, even though the affected Scene Graph transform is represented as LUF in Verse. Do not rename fields or swap axes by intuition. The older `/UnrealEngine.com/Temporary/SpatialMath` types are also distinct from `/Verse.org/SpatialMath`; use the documented Verse conversion functions when crossing between them.

## Editing Placed Verse Devices

Find a placed `VerseDevice_C` actor by internal name, then export its serialized state for programmatic inspection:

```json
{"command":"verse.find_device","params":{"query":"game manager"}}
```

```json
{
  "command": "verse.export_t3d",
  "params": {
    "target": "VerseDevice_C_UAID_ABC123"
  },
  "timeout_s": 120
}
```

Plain property access works for numbers, object references, and reference arrays. Verse string, enum, and option values require the editor text-import roundtrip documented in [`RULES.md`](RULES.md). Writes mutate the open level, so get user approval before editing or saving.

## Updates

A few seconds after it starts, the bridge reads the published `bridge/release.json` from this repository's `main` branch. If that version is newer, the Output Log says so once, and `status` and `GET /` carry the result:

```json
"update": {"current": "0.5.0", "latest": "0.5.1", "available": true, "checked_at": "2026-10-13T04:00:00Z", "error": null, "install": "copy"}
```

- **Install it:** send `{"command": "bridge.update"}`. The bridge downloads every file the manifest lists, checks each file's SHA-256 against it, and changes nothing unless all of them match. Each install copies the files it replaces to its own `bridge/.bridge-backup/<installed version>-<UTC time>/` folder, swaps the new ones in (rolling back if a swap fails), and reloads itself once no command is running or waiting on a native tool. Pass `"reload": false` to reload later; a later `bridge.update` or `reload` picks up the installed files. It never installs an older version unless you pass `"allow_downgrade": true`.
- **Git checkouts are left to git.** When the bridge runs from a clone of this repository, `install` is `"git"` and `bridge.update` refuses; run `git pull` there instead.
- **Automatic updates are off by default.** The bridge usually lives inside a project's `Content/Python/`, so an update changes project files that revision control will pick up. To install updates automatically at startup, add `bridge/settings.json` with `{"auto_update": true}`. To stop the startup check, use `{"check_for_updates": false}`. The environment variables `UEFN_BRIDGE_AUTO_UPDATE` and `UEFN_BRIDGE_UPDATE_CHECK` (`1` or `0`) override the file.
- The check is one HTTPS request to `raw.githubusercontent.com` per bridge start, with no identifying data beyond the request itself. A failed check is reported in `update.error` and never stops the bridge.
- Bridges older than 0.5.0 do not check. Update those by copying the `bridge/` folder once.

## How It Works

1. A local HTTP server listens on `127.0.0.1:9210` or the next free bridge port.
2. Direct commands that touch Unreal are queued to UEFN's main editor thread and executed on a Slate tick.
3. Creator-toolset calls are dispatched through `unreal.ToolsetRegistry` on the editor thread and polled on later ticks, without blocking UEFN in a busy loop.
4. A proven pre-dispatch capability miss is handed to the official loopback MCP client on the HTTP worker thread.
5. Results return in one bridge envelope with the actual route and safety metadata.

The local toolset schema cache avoids repeated discovery. MCP maintains its own session-scoped discovery caches and serializes its HTTP requests. Registry dispatch stays on UEFN's main editor thread. Callers should not overlap Epic tool operations.

On startup, the bridge disables UEFN's background CPU throttle so commands continue while the editor is not focused.

## Repository Layout

```text
bridge/
  server.py            HTTP bridge and hybrid router, runs inside UEFN
  client.py            Local Python client
  routes.py            Direct-command and 42-alias routing metadata
  uefn_tool_policy.py  Exact UEFN v42 local tool allowlist
  mcp_client.py        Stdlib-only official UEFN MCP client
  startup.py           Optional UEFN init_unreal.py auto-start helper
  updates.py           Update check and verified install
  release.json         Published version and file digests (tools/write_release_manifest.py)

examples/
  hybrid_routing.py    Read-only routing and discovery example
  verse_devices.py     Placed Verse device workflow

tests/                 Unit tests that run outside UEFN with fakes
tools/                 API introspection, stub generation, release manifest
RULES.md               LLM context and operating rules
```

## IDE Autocomplete

1. In UEFN, choose `Tools > Execute Python Script` and run `tools/generate_stubs.py`.
2. Find `unreal.pyi` under your project's `Saved` directory.
3. Add that directory to your Python analysis paths.

```json
{
  "python.analysis.extraPaths": ["path/to/Saved"]
}
```

## Good to Know

- UEFN is not standard Unreal Engine. Many standard UE5 Python APIs are stripped or changed. See [`RULES.md`](RULES.md) for tested patterns.
- Epic documents editor hitching or hanging during MCP calls. Prefer direct Python and the in-process registry.
- Any request can include a top-level `timeout_s` up to 600 seconds; without one, slow session and build tools get longer defaults (Busy, Stalled, and Deadlines above). A timeout after dispatch is not proof that the operation failed.
- If background throttling returns, run `fix_throttle` or focus UEFN.
- Start manually with `bridge/server.py`, or copy `bridge/startup.py` to `<Project>/Content/Python/init_unreal.py` alongside the `bridge/` package for project auto-start.
- `reload` re-reads `server.py` and its sibling modules (`routes.py`, `uefn_tool_policy.py`, `mcp_client.py`, `updates.py`) from disk, so a changed sibling module no longer needs a UEFN restart.
- The bridge only connects to loopback MCP endpoints and has no remote-access mode.

## Contributing

To add a direct Python command:

1. Add a function with `@command("category.name")` in `bridge/server.py`.
2. Add the command to `PYTHON_COMMANDS` in `bridge/routes.py`.
3. Add tests and a working example.
4. Update `RULES.md` and this README.
5. After changing any file in `bridge/` (or `VERSION` in `server.py`), run `python tools/write_release_manifest.py`. `tests/test_updates.py` fails until the manifest matches, because installed bridges verify their downloads against it.

When Epic changes its creator toolsets, review the official exposed schemas before updating `bridge/uefn_tool_policy.py`. Do not widen the local policy from registered toolsets alone.

## Support

- [GitHub Issues](https://github.com/Valid/uefn-python-bridge/issues)
- [Discord](https://discord.gg/fchq)

## License

MIT
