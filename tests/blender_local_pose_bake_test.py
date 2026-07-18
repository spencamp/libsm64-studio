"""Focused Blender reconstruction regression for schema-2 performance bakes."""

from pathlib import Path
import math
import os
import sys
import tempfile

import bpy


root = Path(__file__).resolve().parents[1]
if os.environ.get("LIBSM64_TEST_INSTALLED") != "1":
    sys.path.insert(0, str(root))

from libsm64_studio import recording
from libsm64_studio import take_manager as takes


TOLERANCE = 2e-5
LOCAL_A = (
    -0.5, -0.25, 0.0,
    0.75, -0.25, 0.0,
    0.0, 0.9, 0.25,
    0.1, 0.1, 1.4,
)
LOCAL_B = (
    -0.6, -0.2, 0.1,
    0.8, -0.15, 0.2,
    -0.1, 1.0, 0.45,
    0.2, 0.0, 1.65,
)
LOCAL_JUMP = (
    -0.4, -0.35, 0.3,
    0.65, -0.4, 0.35,
    0.0, 0.75, 0.8,
    0.0, 0.1, 1.9,
)


def make_sample(local_pose, location, angle):
    world = recording.mario_local_to_world(local_pose, location, angle)
    return recording.PerformanceSample(world, location, angle), tuple(local_pose)


sample_specs = (
    (LOCAL_A, (0.0, 0.0, 0.0), 0.0),               # initial pose
    (LOCAL_A, (2.0, 0.0, 0.0), 0.0),               # straight translation
    (LOCAL_A, (2.0, 0.0, 0.0), math.pi / 2.0),     # rotation in place
    (LOCAL_B, (4.0, 1.5, 0.0), 1.2),               # translation + rotation
    (LOCAL_JUMP, (4.0, 1.5, 3.25), 1.2),           # vertical jump movement
    (LOCAL_A, (5.0, 2.0, 0.0), 3.10),              # just below +pi
    (LOCAL_A, (6.0, 3.0, 0.0), -3.10),             # just above -pi
)
sample_and_local = tuple(make_sample(*spec) for spec in sample_specs)
samples = tuple(item[0] for item in sample_and_local)
local_poses = tuple(item[1] for item in sample_and_local)
unwrapped_angles = recording.unwrap_face_angles(sample.face_angle for sample in samples)


def set_scene_frame(frame):
    integer = math.floor(frame)
    bpy.context.scene.frame_set(integer, subframe=frame - integer)


def assert_vector_close(actual, expected, tolerance=TOLERANCE):
    assert len(actual) == len(expected)
    assert all(abs(float(left) - float(right)) <= tolerance for left, right in zip(actual, expected)), (
        tuple(actual), tuple(expected)
    )


def flat_world_vertices(obj):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated_object = obj.evaluated_get(depsgraph)
    evaluated_mesh = evaluated_object.to_mesh()
    try:
        values = []
        for vertex in evaluated_mesh.vertices:
            world = evaluated_object.matrix_world @ vertex.co
            values.extend(float(value) for value in world)
        return tuple(values), evaluated_object
    finally:
        evaluated_object.to_mesh_clear()


def action_curves(action):
    return tuple(recording.iter_action_fcurves(action))


def keyframe_frames(curve):
    return tuple(float(point.co[0]) for point in curve.keyframe_points)


def validate_bake(obj, target_fps, verify_every_sample=True):
    pose_action = obj.data.shape_keys.animation_data.action
    transform_action = obj.animation_data.action
    assert pose_action is not None and transform_action is not None
    assert pose_action is not transform_action
    assert pose_action.users == 1 and transform_action.users == 1
    assert obj[recording.BAKE_SCHEMA_VERSION] == 2
    assert obj[recording.BAKE_LAYOUT] == recording.OBJECT_MOTION_LOCAL_POSE
    assert_vector_close(obj.scale, (1.0, 1.0, 1.0))

    pose_curves = action_curves(pose_action)
    transform_curves = action_curves(transform_action)
    assert len(transform_curves) == 4
    assert all(
        point.interpolation == 'CONSTANT'
        for curve in pose_curves + transform_curves
        for point in curve.keyframe_points
    )

    expected_frames = tuple(
        recording.sample_target_frame(10.0, index, target_fps)
        for index in range(len(samples))
    )
    for curve in transform_curves:
        assert_vector_close(keyframe_frames(curve), expected_frames, tolerance=1e-6)
    pose_sample_frames = sorted(
        float(point.co[0])
        for curve in pose_curves
        for point in curve.keyframe_points
        if abs(float(point.co[1]) - 1.0) <= 1e-6
    )
    assert_vector_close(pose_sample_frames, expected_frames, tolerance=1e-6)

    rotation_curve = next(
        curve for curve in transform_curves
        if curve.data_path == "rotation_euler" and curve.array_index == 2
    )
    rotation_values = tuple(float(point.co[1]) for point in rotation_curve.keyframe_points)
    assert_vector_close(rotation_values, unwrapped_angles)
    assert abs(rotation_values[-1] - rotation_values[-2]) < 0.1
    assert rotation_values[-1] > math.pi

    key_blocks = obj.data.shape_keys.key_blocks
    assert_vector_close(
        tuple(value for point in key_blocks[0].data for value in point.co),
        LOCAL_A,
    )
    for index, expected_local in enumerate(local_poses):
        localized = tuple(
            float(value)
            for point in key_blocks[index + 1].data
            for value in point.co
        )
        assert_vector_close(localized, expected_local)

    constant_pose_indices = (0, 1, 2, 5, 6)
    constant_localized = [
        tuple(
            float(value)
            for point in key_blocks[index + 1].data
            for value in point.co
        )
        for index in constant_pose_indices
    ]
    for values in constant_localized[1:]:
        assert_vector_close(values, constant_localized[0])

    if not verify_every_sample:
        return
    for index, (sample, angle, target_frame) in enumerate(
            zip(samples, unwrapped_angles, expected_frames)):
        set_scene_frame(target_frame)
        world_vertices, evaluated_object = flat_world_vertices(obj)
        assert_vector_close(world_vertices, sample.coordinates)
        assert_vector_close(evaluated_object.location, sample.world_location)
        assert abs(float(evaluated_object.rotation_euler.z) - angle) <= TOLERANCE
        assert_vector_close(evaluated_object.scale, (1.0, 1.0, 1.0))
        assert_vector_close(evaluated_object.matrix_world.translation, sample.world_location)
        active_key = key_blocks[index + 1]
        assert abs(float(active_key.value) - 1.0) <= 1e-6


source_mesh = bpy.data.meshes.new("Local Pose Bake Source Mesh")
source_mesh.from_pydata(
    [tuple(LOCAL_A[index:index + 3]) for index in range(0, len(LOCAL_A), 3)],
    [],
    [(0, 1, 2), (0, 2, 3)],
)
source = bpy.data.objects.new("Local Pose Bake Live Mario", source_mesh)
bpy.context.scene.collection.objects.link(source)
bpy.context.scene.frame_start = 1
bpy.context.scene.frame_end = 40

bakes_by_fps = {}
for target_fps in (24, 30, 60):
    baked = recording.bake_shape_keys(
        bpy.context,
        source,
        samples,
        start_frame=10.0,
        target_fps=target_fps,
    )
    takes.register_baked_take(bpy.context.scene, baked)
    bakes_by_fps[target_fps] = baked
    validate_bake(baked, target_fps)

# Save/reopen must retain both direct actions and reproduce world geometry
# without a controller, ROM, native tick, or frame-change handler.
identities = {
    target_fps: baked[takes.TAKE_ID]
    for target_fps, baked in bakes_by_fps.items()
}
blend_path = Path(tempfile.gettempdir()) / "libsm64_local_pose_bake_regression.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
bpy.ops.wm.open_mainfile(filepath=str(blend_path))
takes.reconcile_scene(bpy.context.scene)
for target_fps, take_id in identities.items():
    baked = takes.find_take(take_id)
    assert baked is not None
    assert baked.animation_data.action.name == (
        "LibSM64 Studio Take {:03d} Transform Action".format(
            int(baked[takes.TAKE_NUMBER])
        )
    )
    assert baked.data.shape_keys.animation_data.action.name == (
        "LibSM64 Studio Take {:03d} Pose Action".format(
            int(baked[takes.TAKE_NUMBER])
        )
    )
    validate_bake(baked, target_fps)

print("libsm64 local-pose schema-2 bake reconstruction regression passed")
