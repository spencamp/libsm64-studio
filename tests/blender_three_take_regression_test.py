"""Blender 5.2 three-take ownership, redraw, reset, and persistence regression.

Run with:
  blender --background --factory-startup --python tests/blender_three_take_regression_test.py
"""

from array import array
from pathlib import Path
from types import SimpleNamespace
import os
import sys
import tempfile

import bpy


root = Path(__file__).resolve().parents[1]
installed_test = os.environ.get("LIBSM64_TEST_INSTALLED") == "1"
if not installed_test:
    sys.path.insert(0, str(root))
import libsm64_studio as addon
from libsm64_studio import recording
from libsm64_studio import take_manager as takes

if installed_test:
    expected_root = Path(os.environ["LIBSM64_EXPECTED_INSTALL_ROOT"]).resolve()
    assert expected_root in Path(addon.__file__).resolve().parents
    assert addon.BUILD_ID == "2.3.0+live-control"


def sample(offset_x=0.0, offset_z=0.0):
    return array('f', [
        offset_x + 0, 0, offset_z + 0,
        offset_x + 1, 0, offset_z + 0,
        offset_x + 0, 1, offset_z + 0,
    ])


def fcurve_snapshot(action):
    curves = []
    for curve in recording.iter_action_fcurves(action):
        points = tuple(
            (float(point.co[0]), float(point.co[1]), point.interpolation)
            for point in curve.keyframe_points
        )
        curves.append((curve.data_path, int(curve.array_index), points))
    return tuple(sorted(curves))


def snapshot(obj):
    mesh = obj.data
    key_data = mesh.shape_keys
    action = key_data.animation_data.action
    return {
        "object_pointer": obj.as_pointer(),
        "mesh_pointer": mesh.as_pointer(),
        "shape_key_pointer": key_data.as_pointer(),
        "action_pointer": action.as_pointer(),
        "matrix_world": tuple(float(value) for row in obj.matrix_world for value in row),
        "mesh_coordinates": tuple(tuple(float(value) for value in vertex.co) for vertex in mesh.vertices),
        "shape_keys": tuple(
            (
                key.name,
                tuple(tuple(float(value) for value in point.co) for point in key.data),
            )
            for key in key_data.key_blocks
        ),
        "action_name": action.name,
        "fcurves": fcurve_snapshot(action),
        "hidden": (obj.hide_get(), obj.hide_render),
        "users": (mesh.users, key_data.users, action.users),
    }


def immutable_snapshot(obj):
    state = snapshot(obj)
    state.pop("hidden")
    return state


def log_takes(stage, objects):
    print("TAKE SNAPSHOT {}".format(stage))
    for obj in objects:
        print(obj.name, snapshot(obj))


mesh = bpy.data.meshes.new("Regression Live Mesh")
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
material = bpy.data.materials.new("Shared Mario Material")
mesh.materials.append(material)
live = bpy.data.objects.new("LibSM64 Mario", mesh)
live[addon.mario.LIVE_ROLE] = addon.mario.LIVE_ROLE_VALUE
bpy.context.scene.collection.objects.link(live)
bpy.context.scene.frame_start = 1
bpy.context.scene.frame_end = 20
bpy.context.scene.render.fps = 30

# Reproduce the destructive legacy condition: a shallow bake shares the
# canonical live mesh. The first live write must detach instead of rewriting it.
legacy_mesh = live.data
legacy_shallow_bake = bpy.data.objects.new("Legacy Shallow Bake", legacy_mesh)
bpy.context.scene.collection.objects.link(legacy_shallow_bake)
assert legacy_mesh.users == 2
legacy_coordinates = tuple(tuple(vertex.co) for vertex in legacy_mesh.vertices)
exclusive_live_mesh = addon.mario._ensure_live_mesh_exclusive(live)
assert exclusive_live_mesh is live.data
assert exclusive_live_mesh is not legacy_shallow_bake.data
live.data.vertices[0].co = (50, 50, 50)
live.data.update()
assert tuple(tuple(vertex.co) for vertex in legacy_shallow_bake.data.vertices) == legacy_coordinates
bpy.data.objects.remove(legacy_shallow_bake, do_unlink=True)
if legacy_mesh.users == 0:
    bpy.data.meshes.remove(legacy_mesh)
for vertex, coordinates in zip(live.data.vertices, ((0, 0, 0), (1, 0, 0), (0, 1, 0))):
    vertex.co = coordinates
live.data.update()

performances = (
    (sample(0, 0), sample(-2, 0), sample(-4, 0)),       # walk left
    (sample(0, 0), sample(3, 0), sample(6, 0)),         # walk right
    (sample(0, 0), sample(0, 4), sample(0, 0)),         # jump in place
)

baked = []
frozen = []
for number, performance in enumerate(performances, 1):
    log_takes("before bake {}".format(number), baked)
    candidate = recording.bake_shape_keys(
        bpy.context, live, performance, start_frame=1, target_fps=30
    )
    assert candidate.hide_get() and candidate.hide_render
    takes.register_baked_take(bpy.context.scene, candidate)
    baked.append(candidate)
    for previous, expected in zip(baked[:-1], frozen):
        assert immutable_snapshot(previous) == expected
    frozen = [immutable_snapshot(obj) for obj in baked]
    log_takes("after bake {}".format(number), baked)

assert [obj[takes.TAKE_NUMBER] for obj in baked] == [1, 2, 3]
assert len({obj.as_pointer() for obj in baked}) == 3
assert len({obj.data.as_pointer() for obj in baked}) == 3
assert len({obj.data.shape_keys.as_pointer() for obj in baked}) == 3
assert len({obj.data.shape_keys.animation_data.action.as_pointer() for obj in baked}) == 3
assert all(obj.data.users == 1 for obj in baked)
assert all(obj.data.shape_keys.users == 1 for obj in baked)
assert all(obj.data.shape_keys.animation_data.action.users == 1 for obj in baked)
assert all(obj.data.materials[0] is material for obj in baked)
assert all(live.data is not obj.data for obj in baked)

# Scrubbing the common timeline produces three distinct performances.
bpy.context.scene.frame_set(2)
assert baked[0].data.shape_keys.key_blocks[2].value == 1.0
assert baked[1].data.shape_keys.key_blocks[2].value == 1.0
assert baked[2].data.shape_keys.key_blocks[2].value == 1.0
midpoints = [tuple(obj.data.shape_keys.key_blocks[2].data[0].co) for obj in baked]
assert midpoints == [(-2.0, 0.0, 0.0), (3.0, 0.0, 0.0), (0.0, 0.0, 4.0)]

# Visibility changes are allowed; transforms and animation content are not.
takes.favorite_take(bpy.context.scene, baked[0])
assert not baked[0].hide_render
assert baked[1].hide_render
assert not baked[2].hide_render
before_live_reset = [immutable_snapshot(obj) for obj in baked]
live.data.vertices[0].co = (99, 99, 99)
live.location = (12, 34, 56)
live.data.update()
assert [immutable_snapshot(obj) for obj in baked] == before_live_reset

# A failed commit restores previous current/counter/visibility and is removable.
failed = recording.bake_shape_keys(
    bpy.context, live, performances[0], start_frame=10, target_fps=30
)
scene_state = (
    bpy.context.scene.get(takes.SCENE_CURRENT_TAKE),
    bpy.context.scene.get(takes.SCENE_NEXT_TAKE),
    tuple((obj.hide_get(), obj.hide_render) for obj in baked),
)
original_apply_visibility = takes.apply_visibility
def fail_visibility(_scene):
    raise RuntimeError("injected visibility failure")
takes.apply_visibility = fail_visibility
try:
    try:
        takes.register_baked_take(bpy.context.scene, failed)
        raise AssertionError("Injected registration failure did not fail")
    except RuntimeError as exc:
        assert "injected visibility failure" in str(exc)
finally:
    takes.apply_visibility = original_apply_visibility
assert scene_state == (
    bpy.context.scene.get(takes.SCENE_CURRENT_TAKE),
    bpy.context.scene.get(takes.SCENE_NEXT_TAKE),
    tuple((obj.hide_get(), obj.hide_render) for obj in baked),
)
assert [immutable_snapshot(obj) for obj in baked] == before_live_reset
recording.discard_baked_take(failed)

# Panel draw is read-only even when Blender invokes it repeatedly.
class ReadOnlyLayout:
    def row(self, *args, **kwargs): return self
    def column(self, *args, **kwargs): return self
    def box(self, *args, **kwargs): return self
    def split(self, *args, **kwargs): return self
    def separator(self, *args, **kwargs): return None
    def label(self, *args, **kwargs): return None
    def prop(self, *args, **kwargs): return None
    def operator(self, *args, **kwargs): return SimpleNamespace()
    enabled = True
    scale_y = 1.0

addon.register()
bpy.context.scene[takes.SCENE_SCHEMA_VERSION] = takes.TAKE_SCHEMA_VERSION
draw_before = (
    tuple(sorted((key, repr(value)) for key, value in bpy.context.scene.items())),
    [immutable_snapshot(obj) for obj in baked],
)
original_reconcile = addon.reconcile_scene
addon.reconcile_scene = lambda *_args, **_kwargs: (_ for _ in ()).throw(
    AssertionError("Panel draw called reconciliation")
)
panel = SimpleNamespace(layout=ReadOnlyLayout())
for _ in range(20):
    addon.Main_PT_Panel.draw(panel, bpy.context)
addon.reconcile_scene = original_reconcile
draw_after = (
    tuple(sorted((key, repr(value)) for key, value in bpy.context.scene.items())),
    [immutable_snapshot(obj) for obj in baked],
)
assert draw_after == draw_before

# Saving and reopening preserves values and independent datablock identities.
saved_values = {obj.name: immutable_snapshot(obj) for obj in baked}
blend_path = Path(tempfile.gettempdir()) / "libsm64_three_take_regression.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
bpy.ops.wm.open_mainfile(filepath=str(blend_path))
reloaded = sorted(takes.iter_takes(), key=lambda obj: int(obj[takes.TAKE_NUMBER]))
assert len(reloaded) == 3
assert len({obj.data.as_pointer() for obj in reloaded}) == 3
assert len({obj.data.shape_keys.as_pointer() for obj in reloaded}) == 3
assert len({obj.data.shape_keys.animation_data.action.as_pointer() for obj in reloaded}) == 3
for obj in reloaded:
    expected = saved_values[obj.name]
    actual = immutable_snapshot(obj)
    for pointer_key in ("object_pointer", "mesh_pointer", "shape_key_pointer", "action_pointer"):
        actual.pop(pointer_key)
        expected.pop(pointer_key)
    assert actual == expected

addon.unregister()
print("libsm64 Blender 5.2 three-take regression passed")
