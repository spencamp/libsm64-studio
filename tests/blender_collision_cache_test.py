"""Blender 5.2 evaluated-mesh/cache/performance regression for Phase 2."""

from pathlib import Path
import os
import sys
import time

import bpy


root = Path(__file__).resolve().parents[1]
if os.environ.get("LIBSM64_TEST_INSTALLED") == "1":
    install_root = Path(os.environ["LIBSM64_EXPECTED_INSTALL_ROOT"]).resolve()
    sys.path.insert(0, str(install_root.parent))
else:
    sys.path.insert(0, str(root))

from libsm64_studio import mario
from libsm64_studio.collision_cache import CollisionCache, preload_coordinates
from libsm64_studio.collision_types import COLLISION_TYPES


def mesh_object(name, vertices, faces, location=(0.0, 0.0, 0.0)):
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    bpy.context.scene.collection.objects.link(obj)
    return obj


bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)

slope = mesh_object(
    "NearbySlope",
    [(-20, -20, 0), (20, -20, 0), (20, 20, 10), (-20, 20, 5)],
    [(0, 1, 2, 3)],
)
wall = mesh_object(
    "BoundaryWall",
    [(256, -12, 0), (256, 12, 0), (256, 12, 30), (256, -12, 30)],
    [(0, 1, 2, 3)],
)
large = mesh_object(
    "HugeJoinedMesh",
    [(-100, 50, 0), (700, 50, 0), (300, 220, 100)],
    [(0, 1, 2)],
)
negative = mesh_object(
    "NegativeFloor",
    [(-350, -30, 0), (-260, -30, 0), (-260, 30, 0), (-350, 30, 0)],
    [(0, 1, 2, 3)],
)
bpy.context.view_layer.update()

keys = preload_coordinates((0, 0))
depsgraph = bpy.context.evaluated_depsgraph_get()
small_cache = CollisionCache()
small_started = time.perf_counter()
small_chunks, small_stats = small_cache.prepare_chunks(
    bpy.context.scene, keys, 50.0, COLLISION_TYPES, depsgraph=depsgraph
)
small_duration = time.perf_counter() - small_started
assert small_stats.objects_evaluated == 3, small_stats.as_dict()
assert any(chunk.surface_count for chunk in small_chunks.values())
assert small_chunks[(0, 0)].surface_count
assert small_chunks[(1, 0)].surface_count  # exact-boundary wall in both chunks
for key in ((-1, 0), (0, 0), (1, 0), (2, 0)):
    requested, _ = small_cache.prepare_chunks(
        bpy.context.scene, (key,), 50.0, COLLISION_TYPES, depsgraph=depsgraph
    )
    assert requested[key].surface_count, "large joined mesh missing from {}".format(key)
negative_chunk, _ = small_cache.prepare_chunks(
    bpy.context.scene, ((-2, 0),), 50.0, COLLISION_TYPES, depsgraph=depsgraph
)
assert negative_chunk[(-2, 0)].surface_count

huge_only_cache = CollisionCache()
huge_started = time.perf_counter()
huge_only, huge_stats = huge_only_cache.prepare_chunks(
    bpy.context.scene, ((2, 0),), 50.0, COLLISION_TYPES, depsgraph=depsgraph
)
huge_duration = time.perf_counter() - huge_started
assert huge_stats.objects_evaluated == 1
assert huge_only[(2, 0)].surface_count

# Thousands of far objects share a mesh, as commonly happens with instanced
# set dressing. Bounds are scanned cheaply, but only nearby contributors may
# call evaluated to_mesh/triangulation during startup.
distant_mesh = bpy.data.meshes.new("DistantSharedMesh")
distant_mesh.from_pydata(
    [(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)], [], [(0, 1, 2, 3)]
)
distant_mesh.update()
DISTANT_COUNT = 1200
for index in range(DISTANT_COUNT):
    obj = bpy.data.objects.new("Distant_{:04d}".format(index), distant_mesh)
    obj.location = (10000.0 + index * 1000.0, 10000.0, 0.0)
    bpy.context.scene.collection.objects.link(obj)
bpy.context.view_layer.update()
depsgraph = bpy.context.evaluated_depsgraph_get()

stream_cache = CollisionCache()
near_started = time.perf_counter()
near_chunks, near_stats = stream_cache.prepare_chunks(
    bpy.context.scene, keys, 50.0, COLLISION_TYPES, depsgraph=depsgraph
)
near_duration = time.perf_counter() - near_started
assert near_stats.objects_scanned_by_bounds == DISTANT_COUNT + 4
assert near_stats.objects_evaluated == 3
assert near_stats.object_cache_misses == 3
assert len(near_chunks) == 9

return_started = time.perf_counter()
returned, return_stats = stream_cache.prepare_chunks(
    bpy.context.scene, keys, 50.0, COLLISION_TYPES, depsgraph=depsgraph
)
return_duration = time.perf_counter() - return_started
assert return_stats.objects_evaluated == 0
assert return_stats.object_cache_hits == 3
assert return_stats.chunk_cache_hits == 9
assert tuple(returned) == tuple(near_chunks)

# Dirty geometry is never silently reused. Active sessions show a restart
# notice; the cache rebuilds the object before any subsequent preparation.
slope.data.vertices[0].co.z += 4.0
slope.data.update()
bpy.context.view_layer.update()
depsgraph = bpy.context.evaluated_depsgraph_get()
stream_cache.mark_object_dirty(slope)
dirty_chunks, dirty_stats = stream_cache.prepare_chunks(
    bpy.context.scene, keys, 50.0, COLLISION_TYPES, depsgraph=depsgraph
)
assert dirty_stats.object_cache_misses >= 1
assert dirty_chunks[(0, 0)].fingerprint != near_chunks[(0, 0)].fingerprint

# Compare the old full-scene conversion cost with nearby streamed preparation.
mario.SM64_SCALE_FACTOR = 50.0
mario.origin_offset[:] = (0.0, 0.0, 0.0)
phase1_started = time.perf_counter()
phase1_surfaces, phase1_count = mario.get_surface_array_from_scene()
phase1_duration = time.perf_counter() - phase1_started
assert phase1_count > sum(chunk.surface_count for chunk in near_chunks.values())
del phase1_surfaces

print(
    "COLLISION PERFORMANCE small={:.6f}s many_distant_stream={:.6f}s "
    "phase1_full_scene={:.6f}s huge_joined_first={:.6f}s return_cached={:.6f}s "
    "scanned={} evaluated={} active_chunks={} active_surfaces={}".format(
        small_duration,
        near_duration,
        phase1_duration,
        huge_duration,
        return_duration,
        near_stats.objects_scanned_by_bounds,
        near_stats.objects_evaluated,
        len(near_chunks),
        sum(chunk.surface_count for chunk in near_chunks.values()),
    )
)
print("libsm64 evaluated collision cache regression passed")
