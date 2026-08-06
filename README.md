# UEFN Python Bridge

A tiny HTTP server that runs inside UEFN and lets anything on your machine talk to the editor — AI agents, Python scripts, Node apps, curl one-liners, whatever speaks HTTP.

You send a JSON command, the bridge executes it on the main editor thread, and sends back the result. That's the whole idea.

## Why

UEFN doesn't expose its Python API to anything outside the editor process. This bridge fixes that. Once it's running, any tool that can make an HTTP request can spawn actors, move things around, search assets, change materials, read properties, and run arbitrary Python inside the editor.

No plugins, no C++, no MCP, no SDK. Just a Python script and HTTP.

## The Fastest Way to Start

Paste this into any AI agent (Claude Code, Cursor, ChatGPT, whatever):

> Clone https://github.com/Valid/uefn-python-bridge and help me set it up with my UEFN project.

It'll read the included [`RULES.md`](RULES.md), walk you through the two setup steps, and start controlling your editor.

## Setup (Manual)

**Step 1: Turn on Python scripting**

In UEFN: **Project dropdown → Project Settings → Enable Python Editor Scripting**

**Step 2: Run the bridge**

In UEFN: **Tools → Execute Python Script** → pick `bridge/server.py`

That's it. You'll see this in the Output Log:

```
[Bridge] Bridge v0.2.0 listening on http://127.0.0.1:9210
[Bridge] 39 commands registered
```

The bridge is now accepting commands.

## Sending Commands

Every command is a POST to `http://127.0.0.1:9210` with a JSON body:

```json
{"command": "actors.list"}
```

```json
{"command": "actors.spawn", "params": {"actor_class": "PointLight", "location": [0, 0, 200]}}
```

```json
{"command": "exec", "params": {"code": "result = len(unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors())"}}
```

The response is always `{"ok": true, "result": ...}` or `{"ok": false, "error": "..."}`.

### Python Client

```python
from bridge.client import UEFNBridge

ue = UEFNBridge()
print(ue.status())

# List every actor in the level
actors = ue.actors()

# Spawn a light
ue.spawn(actor_class="PointLight", location=[500, 0, 100], label="MyLight")

# Run arbitrary Python inside the editor
count = ue.exec("result = 42 * 10")
```

### curl

```bash
# List actors
curl -s -X POST http://127.0.0.1:9210 \
  -H "Content-Type: application/json" \
  -d '{"command": "actors.list"}' | python -m json.tool

# Spawn a cube
curl -s -X POST http://127.0.0.1:9210 \
  -H "Content-Type: application/json" \
  -d '{"command": "actors.spawn", "params": {"asset_path": "/Engine/BasicShapes/Cube", "location": [0, 0, 200]}}'
```

### JavaScript / TypeScript

```javascript
const res = await fetch("http://127.0.0.1:9210", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    command: "actors.spawn",
    params: { actor_class: "PointLight", location: [0, 0, 200], label: "MyLight" }
  })
});
const { result } = await res.json();
```

## What Commands Exist

**Actors** — find, spawn, duplicate, delete, move, read/write properties, change color

| Command | What it does | Key params |
|---------|-------------|------------|
| `actors.list` | All actors in the level | — |
| `actors.selected` | Currently selected actors | — |
| `actors.spawn` | Create a new actor | `actor_class` or `asset_path`, `location`, `label` |
| `actors.duplicate` | Clone an existing actor (keeps mesh, materials, everything) | `source`, `location`, `label` |
| `actors.delete` | Remove actors | `targets` (list of labels) |
| `actors.transform` | Move / rotate / scale | `target`, `location`, `rotation`, `scale` |
| `actors.properties` | List all editable properties | `target` |
| `actors.set_property` | Set a property value | `target`, `property_name`, `value` |
| `actors.set_color` | Apply a solid color (creates a persistent material) | `target`, `color` ([r,g,b,a] 0–1) |

**Assets** — search the content browser, check paths, manage files

| Command | What it does | Key params |
|---------|-------------|------------|
| `assets.search` | Find assets by keyword (returns spawnable props by default) | `query` ("chair", "lamp", etc.) |
| `assets.list` | List assets in a directory | `path`, `recursive` |
| `assets.info` | Details about one asset | `path` |
| `assets.exists` | Check if an asset path is valid | `path` |

**Verse devices**

| Command | What it does | Key params |
|---------|-------------|------------|
| `verse.find_device` | Find placed Verse device actors | `query`, `class_filter` |
| `verse.export_t3d` | Export one device's complete serialized state | `target`, `filename` |
| `verse.get_properties` | Read supported generated subobject properties | `path`, `properties` |
| `verse.set_property` | Write supported generated subobject properties | `path`, `property_name`, `value`, `is_object_ref` |

**Level & Viewport**

| Command | What it does |
|---------|-------------|
| `level.info` | Current level name, path, actor count |
| `level.save` | Save the level |
| `viewport.camera` | Get camera position/rotation |
| `viewport.set_camera` | Move the viewport camera |

**System**

| Command | What it does |
|---------|-------------|
| `status` | Bridge version, uptime, command count |
| `exec` | Run arbitrary Python (set `result = ...` to return data) |
| `batch.exec` | Run multiple commands in a single undo transaction |
| `undo` / `redo` | Undo or redo the last operation |
| `reload` | Hot-reload the bridge from disk (no UEFN restart needed) |
| `fix_throttle` | Disable "Use Less CPU in Background" so commands work when UEFN isn't focused |

## Editing Placed Verse Devices

Find the placed `VerseDevice_C` actor by internal name, then export its complete serialized state for programmatic inspection.

```json
{"command": "verse.find_device", "params": {"query": "game manager"}}
{"command": "verse.export_t3d", "params": {"target": "VerseDevice_C_UAID_ABC123"}, "timeout_s": 120}
```

Plain property access works for numbers, object references, and reference arrays. Verse string, enum, and option values can only be written through the editor's own text import, which the bridge supports with clipboard helpers inside `exec`; the full procedure is in [`RULES.md`](RULES.md). Writes mutate the open level, so an agent must get user approval before editing or saving.

## Using with AI Agents

The repo includes [`RULES.md`](RULES.md) — a context file that teaches LLMs how to write working UEFN Python. Drop it into your tool:

| Tool | Where to put it |
|------|----------------|
| Cursor | `.cursorrules` in project root |
| Claude Code | `CLAUDE.md` in project root |
| OpenClaw | Skill's `SKILL.md` |
| ChatGPT / Gemini / others | Paste into system prompt |

## How It Works Under the Hood

UEFN's Python API (`unreal.*`) can only be called from the main editor thread. The bridge works around this:

1. An HTTP server runs on a background thread, listening on `127.0.0.1:9210`
2. When a command arrives, it's queued for the main thread
3. A Slate tick callback picks up the command on the next editor frame and executes it
4. The result goes back to the HTTP response

Some commands (`status`, `log`, `history`, `fix_throttle`) are thread-safe and skip the queue entirely for instant responses.

On startup, the bridge automatically disables UEFN's background CPU throttle so commands work even when the editor isn't focused.

## Repo Layout

```
bridge/
  server.py         ← the HTTP server (runs inside UEFN)
  client.py         ← Python client library (runs on your machine)
  startup.py        ← auto-start helper (standard UE5 only, not UEFN)

tools/
  introspect_api.py ← dump every available unreal.* type to JSON
  generate_stubs.py ← create .pyi stubs for IDE autocomplete

examples/           ← working examples for actors, assets, materials, batching
  verse_devices.py  ← placed Verse device inspection and editing workflow

RULES.md            ← LLM context file — the cheat sheet for AI agents
```

## IDE Autocomplete

Want autocomplete for `unreal.*` in VS Code or Cursor?

1. In UEFN: **Tools → Execute Python Script** → `tools/generate_stubs.py`
2. Find the generated `unreal.pyi` in `<YourProject>/Saved/`
3. Add to your editor settings:
```json
{ "python.analysis.extraPaths": ["path/to/Saved"] }
```

## Good to Know

- **UEFN ≠ Unreal Engine.** Lots of standard UE5 Python APIs are stripped or changed. `RULES.md` documents what actually works.
- **Background throttle.** UEFN slows down when it's not focused. The bridge disables this automatically, but if commands start timing out, run `fix_throttle` or just click the UEFN window.
- **Long operations.** Any command payload may carry a top-level `"timeout_s"` in seconds, up to 600. The Python client exposes it as `run(..., timeout_s=...)`.
- **Port conflicts.** If UEFN crashes while the bridge is running, the port might stay bound. Restarting UEFN clears it. The bridge auto-detects this and picks a free port.
- **Auto-start doesn't work in UEFN.** `init_unreal.py` only works in standard Unreal Engine. In UEFN you run the script manually via **Tools → Execute Python Script** each session.
- **Hot reload.** Changed `server.py`? Send `{"command": "reload"}` — no need to restart UEFN.

## Contributing

PRs welcome. To add a command:

1. Add a function with the `@command("category.name")` decorator in `server.py`
2. Add type hints and a docstring
3. Add a working example in `examples/`
4. Update `RULES.md`

## Support

- [GitHub Issues](https://github.com/Valid/uefn-python-bridge/issues)
- [Discord](https://discord.gg/fchq)

## License

MIT
