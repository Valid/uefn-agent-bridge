"""Example: Inspecting placed Verse devices via the UEFN Python Bridge.

Run from your local machine (outside UEFN) with the bridge server running.

    python examples/verse_devices.py
"""

from bridge.client import UEFNBridge

ue = UEFNBridge()

# Set these after inspecting the exported T3D.
DEVICE_QUERY = ""
SUBOBJECT_PATH = ""
PROPERTY_NAMES = [
    "__verse_0xHASH_FirstProperty",
    "__verse_0xHASH_SecondProperty",
]


# ── Find a placed Verse device ─────────────────────────────────────────────

devices = ue.run("verse.find_device", query=DEVICE_QUERY)["devices"]
if not devices:
    raise RuntimeError(f"No Verse device matched {DEVICE_QUERY!r}")

device = devices[0]
print(f"Using: {device['label']} ({device['name']})")
print(f"Path: {device['path']}")


# ── Export complete serialized state ──────────────────────────────────────

exported = ue.run("verse.export_t3d", target=device["name"], timeout_s=120)
print(f"Exported {exported['size_bytes']} bytes to {exported['file']}")


# ── Read Python-supported subobject properties ────────────────────────────

if SUBOBJECT_PATH:
    props = ue.run(
        "verse.get_properties",
        path=SUBOBJECT_PATH,
        properties=PROPERTY_NAMES,
    )
    print(f"Properties on {props['class']}: {props['properties']}")
else:
    print("Set SUBOBJECT_PATH and PROPERTY_NAMES from the exported T3D to read properties")


# ── Clipboard roundtrip sketch ────────────────────────────────────────────

# The complete safe procedure is in RULES.md. It must run in one exec call,
# preserve selection and clipboard state, use a transaction, and clean up the
# temporary pasted actor in try/finally.
# roundtrip = ue.exec(
#     """
# import time
# world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
# original_clipboard = clip_get()
# temp_actor = None
# try:
#     actor = find_actor("VerseDevice_C_UAID_ABC123")
#     actors.set_selected_level_actors([actor])
#     unreal.SystemLibrary.execute_console_command(world, "EDIT COPY")
#     time.sleep(0.1)
#     source_text = clip_get()
#     blocks = t3d_parse(source_text)
#     new_text = source_text  # Clone donor blocks and edit values as documented.
#     before = {a.get_name() for a in actors.get_all_level_actors()}
#     clip_set(new_text)
#     unreal.SystemLibrary.execute_console_command(world, "EDIT PASTE")
#     new_names = {a.get_name() for a in actors.get_all_level_actors()} - before
#     assert len(new_names) == 1
#     temp_actor = find_actor(new_names.pop())
#     # Transplant new subobjects into the real device (see RULES.md), then:
#     result = {"blocks": len(blocks)}
# finally:
#     if temp_actor is not None:
#         actors.destroy_actor(temp_actor)
#     if original_clipboard is not None:
#         clip_set(original_clipboard)
#     """,
#     timeout_s=120,
# )
