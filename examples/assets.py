"""Example: Asset management via the UEFN Agent Bridge.

    python examples/assets.py
"""

from bridge.client import UEFNBridge

ue = UEFNBridge()


# ── Browse assets ─────────────────────────────────────────────────────────

all_assets = ue.assets("/Game/")
print(f"Total assets: {len(all_assets)}")

# Filter by class
materials = ue.run("assets.list", directory="/Game/", class_filter="Material")
print(f"Materials: {materials['count']}")

meshes = ue.run("assets.list", directory="/Game/", class_filter="StaticMesh")
print(f"Static meshes: {meshes['count']}")


# ── Asset info ────────────────────────────────────────────────────────────

if all_assets:
    info = ue.run("assets.info", path=all_assets[0])
    print(f"First asset: {info['asset']}")


# ── Check existence ───────────────────────────────────────────────────────

check = ue.run("assets.exists", path="/Game/Materials/M_Base")
print(f"M_Base exists: {check['exists']}")


# ── Search with Asset Registry ────────────────────────────────────────────

textures = ue.run("assets.search", class_name="Texture2D", directory="/Game/")
print(f"Textures found: {textures['count']}")


# ── Batch rename (add prefix) ────────────────────────────────────────────

# Using exec for complex operations
rename_code = '''
selected = unreal.EditorUtilityLibrary.get_selected_assets()
renamed = []
for asset in selected:
    name = asset.get_name()
    if not name.startswith("SM_"):
        old_path = asset.get_path_name()
        folder = unreal.Paths.get_path(old_path)
        new_path = folder + "/SM_" + name
        if unreal.EditorAssetLibrary.rename_asset(old_path, new_path):
            renamed.append(name)
result = {"renamed": renamed, "count": len(renamed)}
'''
# Uncomment to run (requires selected assets in Content Browser):
# print(ue.exec(rename_code))
