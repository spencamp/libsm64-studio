"""Blender regression for one whole-scene static collision extraction."""

from pathlib import Path
from contextlib import redirect_stdout
import io
import sys

import bpy


root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from libsm64_studio import mario


def clear_scene():
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)


def add_mesh(name, vertices, faces, is_bake=False):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    obj = bpy.data.objects.new(name, mesh)
    obj['libsm64_is_bake'] = is_bake
    bpy.context.scene.collection.objects.link(obj)
    return obj


clear_scene()
add_mesh(
    "Static Collision Plane",
    [(-2.0, -2.0, 0.0), (2.0, -2.0, 0.0), (2.0, 2.0, 0.0), (-2.0, 2.0, 0.0)],
    [(0, 1, 2, 3)],
)
bpy.context.scene.cursor.location = (0.0, 0.0, 1.0)
mario.origin_offset[:] = bpy.context.scene.cursor.location
mario.SM64_SCALE_FACTOR = 100

surfaces, count = mario.get_surface_array_from_scene()
assert count == 2
assert len(surfaces) == 2
assert all(
    surface.vertices[vertex_index][1] == -100
    for surface in surfaces
    for vertex_index in range(3)
)
assert all(surface.terrain == mario.COLLISION_TYPES['TERRAIN_GRASS'] for surface in surfaces)

# Coordinates beyond the old int16 ceiling retain sign, axis order, and exact
# nested int32 slots after the established Blender -> native mapping.
clear_scene()
expected_native = [
    (40000, -50000, 60000),
    (-70000, 80000, -90000),
    (100000, -110000, 120000),
]
blender_vertices = [(x, -z, y) for x, y, z in expected_native]
add_mesh("Asymmetric int32 collision", blender_vertices, [(0, 1, 2)])
add_mesh(
    "Excluded baked take",
    [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
    [(0, 1, 2)],
    is_bake=True,
)
mario.origin_offset[:] = (0.0, 0.0, 0.0)
mario.SM64_SCALE_FACTOR = 1
surfaces, count = mario.get_surface_array_from_scene()
assert count == 1
assert len(surfaces) == 1
assert [tuple(surfaces[0].vertices[index]) for index in range(3)] == expected_native
assert surfaces[0].type == mario.COLLISION_TYPES['SURFACE_DEFAULT']
assert surfaces[0].terrain == mario.COLLISION_TYPES['TERRAIN_GRASS']

# One invalid axis invalidates the entire vertex/triangle; neither positive nor
# negative overflow can pass via another valid axis or wrap in ctypes.
clear_scene()
too_large = float(2 ** 32)
add_mesh(
    "Out of int32 collision",
    [
        (too_large, 0.0, 0.0), (too_large, 1.0, 0.0), (too_large, 0.0, 1.0),
        (0.0, too_large, 0.0), (1.0, too_large, 0.0), (0.0, too_large, 1.0),
    ],
    [(0, 1, 2), (3, 4, 5)],
)
diagnostic = io.StringIO()
with redirect_stdout(diagnostic):
    surfaces, count = mario.get_surface_array_from_scene()
assert count == 0
assert "2 collision surface(s)" in diagnostic.getvalue()
assert "signed 32-bit" in diagnostic.getvalue()
assert mario._checked_int32_coordinate(2 ** 31) == (2 ** 31, False)
assert mario._checked_int32_coordinate(-(2 ** 31) - 1) == (-(2 ** 31) - 1, False)
assert mario._checked_int32_coordinate(2 ** 31 - 1) == (2 ** 31 - 1, True)
assert mario._checked_int32_coordinate(-(2 ** 31)) == (-(2 ** 31), True)

print("libsm64 whole-scene static collision regression passed")
