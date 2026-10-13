"""Example: Material creation via the UEFN Agent Bridge.

    python examples/materials.py
"""

from bridge.client import UEFNBridge

ue = UEFNBridge()


# ── Create a material instance ────────────────────────────────────────────

# Create an instance from an existing parent material
mi = ue.run(
    "materials.create_instance",
    parent_path="/Engine/EngineMaterials/DefaultMaterial",
    instance_name="MI_CustomRed",
    destination="/Game/Materials",
    vector_params={
        "BaseColor": [0.8, 0.1, 0.1, 1.0],
    },
    scalar_params={
        "Roughness": 0.3,
        "Metallic": 0.9,
    },
)
print(f"Created material instance: {mi['path']}")


# ── Create a material from scratch using exec ─────────────────────────────

create_mat = '''
import unreal

tools = unreal.AssetToolsHelpers.get_asset_tools()

# Create a new material
mat = tools.create_asset(
    "M_Glowing",
    "/Game/Materials",
    unreal.Material,
    unreal.MaterialFactoryNew(),
)

# Add a constant color node
color_node = unreal.MaterialEditingLibrary.create_material_expression(
    mat, unreal.MaterialExpressionConstant3Vector, -300, 0
)
color_node.set_editor_property("constant", unreal.LinearColor(0.0, 1.0, 0.5, 1.0))

# Connect to Base Color
unreal.MaterialEditingLibrary.connect_material_property(
    color_node, "RGB", unreal.MaterialProperty.MP_BASE_COLOR
)

# Add emissive with multiplier
emissive_mul = unreal.MaterialEditingLibrary.create_material_expression(
    mat, unreal.MaterialExpressionMultiply, -150, 200
)
unreal.MaterialEditingLibrary.connect_material_expressions(
    color_node, "RGB", emissive_mul, "A"
)

scalar_node = unreal.MaterialEditingLibrary.create_material_expression(
    mat, unreal.MaterialExpressionConstant, -300, 200
)
scalar_node.set_editor_property("r", 5.0)
unreal.MaterialEditingLibrary.connect_material_expressions(
    scalar_node, "", emissive_mul, "B"
)

unreal.MaterialEditingLibrary.connect_material_property(
    emissive_mul, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR
)

result = str(mat.get_path_name())
'''

# Uncomment to run:
# r = ue.exec(create_mat)
# print(f"Created material: {r['result']}")
