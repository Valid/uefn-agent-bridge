"""Example: Working with actors via the UEFN Agent Bridge.

Run from your local machine (outside UEFN) with the bridge server running.

    python examples/actors.py
"""

from bridge.client import UEFNBridge

ue = UEFNBridge()


# ── List all actors ────────────────────────────────────────────────────────

actors = ue.actors()
print(f"Level has {len(actors)} actors")
for a in actors[:5]:
    print(f"  {a['label']} ({a['class']}) at {a['location']}")


# ── Spawn shapes ──────────────────────────────────────────────────────────

cube = ue.spawn(asset_path="/Engine/BasicShapes/Cube", location=[500, 0, 50])
print(f"Spawned: {cube['actor']['label']}")

sphere = ue.spawn(asset_path="/Engine/BasicShapes/Sphere", location=[700, 0, 50])
print(f"Spawned: {sphere['actor']['label']}")


# ── Move an actor ─────────────────────────────────────────────────────────

ue.run("actors.transform", target=cube["actor"]["label"], location=[500, 200, 100])
print("Moved cube")


# ── Spawn a light ─────────────────────────────────────────────────────────

light = ue.spawn(actor_class="PointLight", location=[600, 100, 300])
print(f"Spawned light: {light['actor']['label']}")


# ── Read properties ───────────────────────────────────────────────────────

props = ue.run("actors.properties", target=light["actor"]["label"], properties=["light_color", "intensity"])
print(f"Light props: {props['properties']}")


# ── Clean up ──────────────────────────────────────────────────────────────

labels = [cube["actor"]["label"], sphere["actor"]["label"], light["actor"]["label"]]
deleted = ue.run("actors.delete", targets=labels)
print(f"Cleaned up {deleted['count']} actors")
