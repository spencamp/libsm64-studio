"""Blender background coverage for evaluated object and chunk invalidation."""

from pathlib import Path
import os
import sys

import bpy


root = Path(__file__).resolve().parents[1]
installed_test = os.environ.get("LIBSM64_TEST_INSTALLED") == "1"
if installed_test:
    expected_root = Path(os.environ["LIBSM64_EXPECTED_INSTALL_ROOT"]).resolve()
    sys.path.insert(0, str(expected_root.parent))
else:
    sys.path.insert(0, str(root))

from libsm64_studio.collision_cache import CollisionCache
from libsm64_studio.collision_types import COLLISION_TYPES
from libsm64_studio.mario import SM64Surface


def add_triangle(name, vertices, location=(0, 0, 0)):
    mesh = bpy.data.meshes.new(name + " Mesh")
    mesh.from_pydata(vertices, [], [(0, 1, 2)])
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    bpy.context.scene.collection.objects.link(obj)
    return obj


for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)

near = add_triangle("Near", ((1, 1, 0), (4, 1, 0), (1, 4, 0)))
far = add_triangle("Far", ((0, 0, 0), (1, 0, 0), (0, 1, 0)), (10000, 0, 0))
scene = bpy.context.scene
cache = CollisionCache()


def prepare():
    bpy.context.view_layer.update()
    return cache.prepare(
        scene, (0, 0, 0), 50, SM64Surface, COLLISION_TYPES,
        depsgraph=bpy.context.evaluated_depsgraph_get(),
    )


first = prepare()
assert first.stats["objects_considered"] == 2
assert first.stats["objects_rejected"] == 1
assert first.stats["object_cache_misses"] == 1
assert first.stats["native_surface_count"] > 0

second = prepare()
assert second.stats["object_cache_hits"] == 1
assert second.stats["object_cache_misses"] == 0
assert second.stats["chunk_cache_hits"] == 27

# A world-transform change must rebuild the object and only chunks it touches.
near.location.x += 2
transformed = prepare()
assert transformed.stats["object_cache_misses"] == 1
assert 0 < transformed.stats["chunk_cache_hits"] < 27

# An evaluated mesh edit must invalidate object and contributing chunks.
near.data.vertices[0].co.x += 0.5
near.data.update()
geometry = prepare()
assert geometry.stats["object_cache_misses"] == 1
assert 0 < geometry.stats["chunk_cache_hits"] < 27

# Unrelated geometry remaining outside the requested neighborhood is rejected;
# the complete active neighborhood stays reusable.
far.location.x += 100
unrelated = prepare()
assert unrelated.stats["objects_rejected"] == 1
assert unrelated.stats["object_cache_hits"] == 1
assert unrelated.stats["chunk_cache_hits"] == 27

# Visibility is collision eligibility and must remove a prior contributor.
near.hide_viewport = True
hidden = prepare()
assert hidden.stats["objects_considered"] == 1
assert hidden.stats["native_surface_count"] == 0
assert hidden.stats["chunk_cache_misses"] > 0

print("COLLISION CACHE TIMINGS")
for label, result in (("cold", first), ("warm", second), ("transform", transformed),
                      ("geometry", geometry), ("unrelated", unrelated)):
    print(label, "{:.6f}s".format(result.stats["duration_seconds"]), result.stats)
print("libsm64 collision cache Blender test passed")
