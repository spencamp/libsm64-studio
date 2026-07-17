"""Run with: blender --background --factory-startup --python tests/blender_smoke_test.py"""

from array import array
import importlib.util
from pathlib import Path
import sys

import bpy


root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("libsm64_recording_smoke", root / "recording.py")
recording = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recording)


mesh = bpy.data.meshes.new("Smoke Mario Mesh")
mesh.from_pydata(
    [(0, 0, 0), (1, 0, 0), (0, 1, 0)],
    [],
    [(0, 1, 2)],
)
source = bpy.data.objects.new("LibSM64 Mario", mesh)
bpy.context.scene.collection.objects.link(source)

sample_a = array('f', [0, 0, 0, 1, 0, 0, 0, 1, 0])
sample_b = array('f', [0, 0, 1, 1, 0, 1, 0, 1, 1])
bpy.context.scene.render.fps = 24
bpy.context.scene.frame_end = 1

bake = recording.bake_shape_keys(
    bpy.context, source, (sample_a, sample_b), start_frame=10, target_fps=24
)
assert bake is not source
assert bake.data is not source.data
assert len(bake.data.shape_keys.key_blocks) == 3
assert bake["libsm64_sample_count"] == 2
assert bake["libsm64_target_fps"] == 24.0
assert bake["libsm64_is_bake"] is True
assert bake.data.shape_keys.animation_data.action is not None
curves = list(recording.iter_action_fcurves(bake.data.shape_keys.animation_data.action))
assert len(curves) == 2
assert all(
    point.interpolation == 'CONSTANT'
    for curve in curves
    for point in curve.keyframe_points
)
assert bpy.context.scene.frame_end >= 11
assert bpy.context.view_layer.objects.active is bake
assert source.hide_render
bpy.context.scene.frame_set(10)
assert bake.data.shape_keys.key_blocks[1].value == 1.0
assert bake.data.shape_keys.key_blocks[2].value == 0.0
bpy.context.scene.frame_set(10, subframe=0.8)
assert bake.data.shape_keys.key_blocks[1].value == 0.0
assert bake.data.shape_keys.key_blocks[2].value == 1.0

first_mesh = bake.data
first_action = bake.data.shape_keys.animation_data.action
second = recording.bake_shape_keys(
    bpy.context, source, (sample_b,), start_frame=20, target_fps=24
)
assert second is not bake
assert second.data is not first_mesh
assert second.data.shape_keys.animation_data.action is not first_action
assert len(first_mesh.shape_keys.key_blocks) == 3

# Load the repository as an add-on package and verify registration plus handler
# cleanup without requiring a ROM or starting libsm64.
package_name = "libsm64_studio_smoke"
package_spec = importlib.util.spec_from_file_location(
    package_name,
    root / "__init__.py",
    submodule_search_locations=[str(root)],
)
addon = importlib.util.module_from_spec(package_spec)
sys.modules[package_name] = addon
package_spec.loader.exec_module(addon)
addon.register()
assert hasattr(bpy.types.Scene, "libsm64")


def unrelated_handler(scene, depsgraph=None):
    pass


handlers = bpy.app.handlers.frame_change_pre
handlers.append(unrelated_handler)
handlers.append(addon.mario.tick_mario)
handlers.append(addon.mario.tick_mario)
assert addon.mario.remove_tick_mario_handlers() == 2
assert unrelated_handler in handlers
addon.mario.stop_tick_mario()
addon.mario.stop_tick_mario()
assert unrelated_handler in handlers
addon.unregister()
assert unrelated_handler in handlers
handlers.remove(unrelated_handler)

print("libsm64 recording Blender smoke test passed")
