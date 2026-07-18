"""Blender regression for one whole-scene static collision extraction."""

from pathlib import Path
import sys

import bpy


root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from libsm64_studio import mario


bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
mesh = bpy.data.meshes.new("Static Collision Plane")
mesh.from_pydata(
    [(-2.0, -2.0, 0.0), (2.0, -2.0, 0.0), (2.0, 2.0, 0.0), (-2.0, 2.0, 0.0)],
    [],
    [(0, 1, 2, 3)],
)
obj = bpy.data.objects.new("Static Collision Plane", mesh)
bpy.context.scene.collection.objects.link(obj)
bpy.context.scene.cursor.location = (0.0, 0.0, 1.0)
mario.origin_offset[:] = bpy.context.scene.cursor.location
mario.SM64_SCALE_FACTOR = 100

surfaces, count = mario.get_surface_array_from_scene()
assert count == 2
assert len(surfaces) == 2
assert all(surface.v0y == -100 for surface in surfaces)
assert all(surface.terrain == mario.COLLISION_TYPES['TERRAIN_GRASS'] for surface in surfaces)

print("libsm64 whole-scene static collision regression passed")
