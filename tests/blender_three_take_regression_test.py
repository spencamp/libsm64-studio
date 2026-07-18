"""Blender 5.2 three-take ownership, rollback, and persistence regression.

Run with:
  blender --background --factory-startup --python tests/blender_three_take_regression_test.py
"""

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
    assert addon.BUILD_ID == "2.6.0+libsm64-fd118132"


LOCAL_POSE = (0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0)


def sample(offset_x=0.0, offset_z=0.0, face_angle=0.0):
    location = (float(offset_x), 0.0, float(offset_z))
    world = recording.mario_local_to_world(LOCAL_POSE, location, face_angle)
    return recording.PerformanceSample(world, location, face_angle)


def fcurve_snapshot(action):
    curves = []
    for curve in recording.iter_action_fcurves(action):
        points = tuple(
            (float(point.co[0]), float(point.co[1]), point.interpolation)
            for point in curve.keyframe_points
        )
        curves.append((curve.data_path, int(curve.array_index), points))
    return tuple(sorted(curves))


def evaluated_world_coordinates(obj):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated_object = obj.evaluated_get(depsgraph)
    evaluated_mesh = evaluated_object.to_mesh()
    try:
        return tuple(
            tuple(float(value) for value in (evaluated_object.matrix_world @ vertex.co))
            for vertex in evaluated_mesh.vertices
        )
    finally:
        evaluated_object.to_mesh_clear()


def flat_vertices(coordinates):
    return tuple(
        tuple(float(value) for value in coordinates[index:index + 3])
        for index in range(0, len(coordinates), 3)
    )


def assert_vertices_close(actual, expected, tolerance=1e-5):
    assert len(actual) == len(expected)
    for actual_vertex, expected_vertex in zip(actual, expected):
        assert all(
            abs(left - right) <= tolerance
            for left, right in zip(actual_vertex, expected_vertex)
        ), (actual_vertex, expected_vertex)


def snapshot(obj):
    mesh = obj.data
    key_data = mesh.shape_keys
    pose_action = key_data.animation_data.action
    transform_action = obj.animation_data.action
    return {
        "object_pointer": obj.as_pointer(),
        "mesh_pointer": mesh.as_pointer(),
        "shape_key_pointer": key_data.as_pointer(),
        "transform_action_pointer": transform_action.as_pointer(),
        "pose_action_pointer": pose_action.as_pointer(),
        "object_location": tuple(float(value) for value in obj.location),
        "object_rotation": tuple(float(value) for value in obj.rotation_euler),
        "matrix_world": tuple(float(value) for row in obj.matrix_world for value in row),
        "mesh_coordinates": tuple(
            tuple(float(value) for value in vertex.co) for vertex in mesh.vertices
        ),
        "shape_keys": tuple(
            (
                key.name,
                tuple(tuple(float(value) for value in point.co) for point in key.data),
            )
            for key in key_data.key_blocks
        ),
        "transform_action_name": transform_action.name,
        "transform_fcurves": fcurve_snapshot(transform_action),
        "pose_action_name": pose_action.name,
        "pose_fcurves": fcurve_snapshot(pose_action),
        "hidden": (obj.hide_get(), obj.hide_render),
        "users": (
            mesh.users,
            key_data.users,
            transform_action.users,
            pose_action.users,
        ),
        "owners": (
            mesh.get(takes.TAKE_OWNER),
            key_data.get(takes.TAKE_OWNER),
            transform_action.get(takes.TAKE_OWNER),
            pose_action.get(takes.TAKE_OWNER),
        ),
        "metadata": tuple(sorted((key, repr(value)) for key, value in obj.items())),
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
assert len({obj.animation_data.action.as_pointer() for obj in baked}) == 3
assert len({obj.data.shape_keys.animation_data.action.as_pointer() for obj in baked}) == 3
assert all(obj.animation_data.action is not obj.data.shape_keys.animation_data.action for obj in baked)
assert all(obj.data.users == 1 for obj in baked)
assert all(obj.data.shape_keys.users == 1 for obj in baked)
assert all(obj.animation_data.action.users == 1 for obj in baked)
assert all(obj.data.shape_keys.animation_data.action.users == 1 for obj in baked)
assert all(obj.data.materials[0] is material for obj in baked)
assert all(live.data is not obj.data for obj in baked)
for obj in baked:
    take_id = obj[takes.TAKE_ID]
    assert all(owner == take_id for owner in snapshot(obj)["owners"])

# Scrubbing the common timeline produces three distinct performances in world
# space even though their local pose coordinates are identical.
bpy.context.scene.frame_set(2)
for obj in baked:
    assert obj.data.shape_keys.key_blocks[2].value == 1.0
localized_midpoints = [
    tuple(obj.data.shape_keys.key_blocks[2].data[0].co) for obj in baked
]
assert localized_midpoints == [(0.0, 0.0, 0.0)] * 3
for obj, performance in zip(baked, performances):
    assert_vertices_close(
        evaluated_world_coordinates(obj), flat_vertices(performance[1].coordinates)
    )

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

# World-space Live Mario coordinates are ambiguous under a second object
# transform, so schema-2 baking rejects that state instead of double-applying it.
try:
    recording.bake_shape_keys(
        bpy.context, live, performances[0], start_frame=10, target_fps=30
    )
    raise AssertionError("Non-identity Live Mario transform was accepted")
except recording.RecordingError as exc:
    assert "identity world transform" in str(exc)
live.location = (0, 0, 0)
bpy.context.view_layer.update()

# A failed commit restores previous scene state plus candidate metadata, names,
# ownership, visibility, and both action datablocks.
failed = recording.bake_shape_keys(
    bpy.context, live, performances[0], start_frame=10, target_fps=30
)
failed_before = snapshot(failed)
scene_state = (
    bpy.context.scene.get(takes.SCENE_CURRENT_TAKE),
    bpy.context.scene.get(takes.SCENE_NEXT_TAKE),
    bpy.context.scene.get(takes.SCENE_SCHEMA_VERSION),
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
    bpy.context.scene.get(takes.SCENE_SCHEMA_VERSION),
    tuple((obj.hide_get(), obj.hide_render) for obj in baked),
)
assert snapshot(failed) == failed_before
assert [immutable_snapshot(obj) for obj in baked] == before_live_reset
failed_names = (
    failed.name,
    failed.data.name,
    failed.data.shape_keys.name,
    failed.animation_data.action.name,
    failed.data.shape_keys.animation_data.action.name,
)
recording.discard_baked_take(failed)
assert all(
    collection.get(name) is None
    for collection, name in zip(
        (bpy.data.objects, bpy.data.meshes, bpy.data.shape_keys, bpy.data.actions, bpy.data.actions),
        failed_names,
    )
)

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

# Saving and reopening preserves both actions and independent datablock identity.
saved_values = {obj.name: immutable_snapshot(obj) for obj in baked}
blend_path = Path(tempfile.gettempdir()) / "libsm64_three_take_regression.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
bpy.ops.wm.open_mainfile(filepath=str(blend_path))
reloaded = sorted(takes.iter_takes(), key=lambda obj: int(obj[takes.TAKE_NUMBER]))
assert len(reloaded) == 3
assert len({obj.data.as_pointer() for obj in reloaded}) == 3
assert len({obj.data.shape_keys.as_pointer() for obj in reloaded}) == 3
assert len({obj.animation_data.action.as_pointer() for obj in reloaded}) == 3
assert len({obj.data.shape_keys.animation_data.action.as_pointer() for obj in reloaded}) == 3
pointer_keys = (
    "object_pointer",
    "mesh_pointer",
    "shape_key_pointer",
    "transform_action_pointer",
    "pose_action_pointer",
)
for obj in reloaded:
    expected = saved_values[obj.name]
    actual = immutable_snapshot(obj)
    for pointer_key in pointer_keys:
        actual.pop(pointer_key)
        expected.pop(pointer_key)
    assert actual == expected

# Rejected cleanup removes only the rejected take's mesh, Key, and two actions.
rejected = reloaded[1]
keepers = (reloaded[0], reloaded[2])
keeper_snapshots = [immutable_snapshot(obj) for obj in keepers]
rejected_names = (
    rejected.name,
    rejected.data.name,
    rejected.data.shape_keys.name,
    rejected.animation_data.action.name,
    rejected.data.shape_keys.animation_data.action.name,
)
takes.reject_take(bpy.context.scene, rejected)
assert takes.cleanup_rejected(bpy.context.scene) == 1
assert bpy.data.objects.get(rejected_names[0]) is None
assert bpy.data.meshes.get(rejected_names[1]) is None
assert bpy.data.shape_keys.get(rejected_names[2]) is None
assert bpy.data.actions.get(rejected_names[3]) is None
assert bpy.data.actions.get(rejected_names[4]) is None
assert [immutable_snapshot(obj) for obj in keepers] == keeper_snapshots

# A legacy world-space, shape-key-only take remains reconcilable, scrubbable,
# persistent, selectable, restorable, and safely cleanable without a transform
# action or inferred motion metadata.
legacy_mesh = bpy.data.meshes.new("Legacy World-Space Take Mesh")
legacy_mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
legacy = bpy.data.objects.new("Legacy World-Space Take", legacy_mesh)
legacy["libsm64_is_bake"] = True
bpy.context.scene.collection.objects.link(legacy)
legacy.shape_key_add(name="Basis", from_mix=False)
legacy_pose = legacy.shape_key_add(name="Legacy Pose", from_mix=False)
for point in legacy_pose.data:
    point.co.z += 2.0
legacy_keys = legacy.data.shape_keys
legacy_keys.animation_data_create()
legacy_action = bpy.data.actions.new("Legacy Pose Action")
legacy_keys.animation_data.action = legacy_action
legacy_pose.value = 0.0
legacy_pose.keyframe_insert(data_path="value", frame=1.0)
legacy_pose.value = 1.0
legacy_pose.keyframe_insert(data_path="value", frame=2.0)
for curve in recording.iter_action_fcurves(legacy_action):
    for point in curve.keyframe_points:
        point.interpolation = 'CONSTANT'
takes.reconcile_scene(bpy.context.scene)
legacy_id = legacy[takes.TAKE_ID]
assert legacy.animation_data is None or legacy.animation_data.action is None
assert legacy.data.get(takes.TAKE_OWNER) == legacy_id
assert legacy_keys.get(takes.TAKE_OWNER) == legacy_id
assert legacy_action.get(takes.TAKE_OWNER) == legacy_id
bpy.context.scene.frame_set(2)
assert_vertices_close(
    evaluated_world_coordinates(legacy),
    ((0.0, 0.0, 2.0), (1.0, 0.0, 2.0), (0.0, 1.0, 2.0)),
)
takes.favorite_take(bpy.context.scene, legacy)
legacy_blend_path = Path(tempfile.gettempdir()) / "libsm64_legacy_take_regression.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(legacy_blend_path))
bpy.ops.wm.open_mainfile(filepath=str(legacy_blend_path))
takes.reconcile_scene(bpy.context.scene)
legacy = takes.find_take(legacy_id)
assert legacy is not None and legacy[takes.TAKE_DISPOSITION] == takes.FAVORITE
assert not legacy.hide_render
assert legacy.animation_data is None or legacy.animation_data.action is None
bpy.context.scene.frame_set(2)
assert_vertices_close(
    evaluated_world_coordinates(legacy),
    ((0.0, 0.0, 2.0), (1.0, 0.0, 2.0), (0.0, 1.0, 2.0)),
)
legacy_action_name = legacy.data.shape_keys.animation_data.action.name
takes.unfavorite_take(bpy.context.scene, legacy)
takes.reject_take(bpy.context.scene, legacy)
takes.restore_take(bpy.context, legacy)
assert takes.current_take(bpy.context.scene) is legacy
takes.reject_take(bpy.context.scene, legacy)
assert takes.cleanup_rejected(bpy.context.scene) == 1
assert bpy.data.objects.get("Legacy World-Space Take") is None
assert bpy.data.actions.get(legacy_action_name) is None

addon.unregister()
print("libsm64 Blender 5.2 three-take regression passed")
