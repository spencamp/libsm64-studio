"""Blender 5.2 persistence and ownership regression for per-take runtime metadata."""

from array import array
from pathlib import Path
import json
import math
import os
import sys

import bpy


root = Path(__file__).resolve().parents[1]
if os.environ.get("LIBSM64_TEST_INSTALLED") == "1":
    install_root = Path(os.environ["LIBSM64_EXPECTED_INSTALL_ROOT"]).resolve()
    sys.path.insert(0, str(install_root.parent))
else:
    sys.path.insert(0, str(root))

import libsm64_studio as addon
from libsm64_studio import recording
from libsm64_studio import take_manager as takes


def metadata(seed, index):
    return recording.MarioRuntimeMetadata(
        native_position=(seed + index + 0.25, -index - 0.5, seed * 2.0),
        native_velocity=(-index - 1.0, index + 2.0, -seed - 3.0),
        face_angle=-3.0 + index * 0.25,
        forward_velocity=seed * 10.0 + index + 0.5,
        health=0x880 - index,
        action=0x04000440 + seed * 16 + index,
        animation_id=-100 + seed + index,
        animation_frame=-5 + index,
        flags=0x80000000 | (seed << 8) | index,
        particle_flags=0x40000000 | (seed << 4) | index,
        invincibility_timer=30 - index,
    )


def make_samples(seed, count=3):
    result = []
    for index in range(count):
        world_location = (float(index * 2), float(-index), float(seed))
        coordinates = array('f', (
            world_location[0] + 0.0, world_location[1] + 0.0, world_location[2] + 0.0,
            world_location[0] + 1.0, world_location[1] + 0.0, world_location[2] + 0.0,
            world_location[0] + 0.0, world_location[1] + 1.0, world_location[2] + 0.0,
        ))
        result.append(recording.PerformanceSample(
            coordinates,
            world_location,
            -3.0 + index * 0.25,
            metadata(seed, index),
        ))
    return tuple(result)


def make_candidate(source, samples, start_frame, target_fps):
    return recording.bake_shape_keys(
        bpy.context, source, samples, start_frame, target_fps
    )


bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
addon.register()
try:
    mesh = bpy.data.meshes.new("Runtime Metadata Source Mesh")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    mesh.update()
    source = bpy.data.objects.new("Runtime Metadata Source", mesh)
    bpy.context.scene.collection.objects.link(source)

    first_samples = make_samples(1)
    first = make_candidate(source, first_samples, 10.0, 24.0)
    first_number = takes.register_baked_take(
        bpy.context.scene, first, runtime_samples=first_samples
    )
    assert first_number == 1
    first_id = first[takes.TAKE_ID]
    first_validated = takes.runtime_metadata_for_take(first)
    assert first_validated is not None
    first_text = first_validated["text"]
    assert first_text.name == "LibSM64 Studio Take 001 Runtime Metadata"
    assert first_text.get(takes.TAKE_OWNER) == first_id
    assert first_validated["document"]["schema_version"] == 1
    assert first_validated["document"]["sample_rate"] == 30.0
    assert first_validated["document"]["target_fps"] == 24.0
    assert first_validated["document"]["sample_count"] == 3
    assert first_validated["document"]["source_take_owner_id"] == first_id
    assert first_validated["document"]["sample_to_frame_mapping"]["sample_frames"] == [
        10.0, 10.8, 11.6
    ]
    assert first_validated["samples"] == tuple(
        sample.runtime_metadata for sample in first_samples
    )
    first_body = first_text.as_string()
    first_pose_action = first.data.shape_keys.animation_data.action
    first_transform_action = first.animation_data.action
    first_curve_snapshot = tuple(
        (curve.data_path, curve.array_index, tuple(
            (point.co.x, point.co.y, point.interpolation)
            for point in curve.keyframe_points
        ))
        for action in (first_pose_action, first_transform_action)
        for curve in recording.iter_action_fcurves(action)
    )

    # The inspector uses the same constant-held timing at integer/fractional frames.
    index, current, _validated = recording.runtime_metadata_at_frame(first, 10.79)
    assert index == 0 and current == first_samples[0].runtime_metadata
    index, current, _validated = recording.runtime_metadata_at_frame(first, 10.8)
    assert index == 1 and current == first_samples[1].runtime_metadata
    index, current, _validated = recording.runtime_metadata_at_frame(first, 999.0)
    assert index == 2 and current == first_samples[2].runtime_metadata

    # Route/action edits do not mutate the independent Text or visual playback data.
    first.location.x = 123.0
    first["unrelated_route_note"] = "edited"
    assert first_text.as_string() == first_body
    assert first_curve_snapshot == tuple(
        (curve.data_path, curve.array_index, tuple(
            (point.co.x, point.co.y, point.interpolation)
            for point in curve.keyframe_points
        ))
        for action in (first_pose_action, first_transform_action)
        for curve in recording.iter_action_fcurves(action)
    )

    # A later take owns a different Text and cannot rewrite the earlier payload.
    second_samples = make_samples(2)
    second = make_candidate(source, second_samples, -5.5, 60.0)
    takes.register_baked_take(bpy.context.scene, second, runtime_samples=second_samples)
    second_id = second[takes.TAKE_ID]
    second_validated = takes.runtime_metadata_for_take(second)
    second_text = second_validated["text"]
    assert second_text is not first_text
    assert second_text.name == "LibSM64 Studio Take 002 Runtime Metadata"
    assert second_text.get(takes.TAKE_OWNER) == second_id
    assert first_text.as_string() == first_body
    assert second_validated["samples"][0].action != first_validated["samples"][0].action

    # Exclusive ownership rejects another take pointing at the same Text.
    second_original_text_name = second[recording.RUNTIME_METADATA_TEXT_PROPERTY]
    second[recording.RUNTIME_METADATA_TEXT_PROPERTY] = first_text.name
    try:
        recording.validate_take_runtime_metadata(first)
        raise AssertionError("Shared runtime metadata Text was accepted")
    except recording.RecordingError:
        pass
    second[recording.RUNTIME_METADATA_TEXT_PROPERTY] = second_original_text_name
    recording.validate_take_runtime_metadata(first)

    # Legacy takes remain compatible and deliberately expose no metadata panel data.
    legacy_samples = (
        recording.PerformanceSample(
            array('f', sample.coordinates), sample.world_location, sample.face_angle
        )
        for sample in make_samples(3)
    )
    legacy_samples = tuple(legacy_samples)
    legacy = make_candidate(source, legacy_samples, 1.0, 30.0)
    takes.register_baked_take(bpy.context.scene, legacy)
    assert takes.runtime_metadata_for_take(legacy) is None
    assert recording.runtime_metadata_at_frame(legacy, 1.0) is None

    # A failure after Text creation rolls back only the candidate metadata.
    failed_samples = make_samples(4)
    failed = make_candidate(source, failed_samples, 3.0, 30.0)
    texts_before_failure = set(text.name for text in bpy.data.texts)
    real_apply_visibility = takes.apply_visibility
    takes.apply_visibility = lambda _scene: (_ for _ in ()).throw(
        RuntimeError("injected metadata registration failure")
    )
    try:
        try:
            takes.register_baked_take(
                bpy.context.scene, failed, runtime_samples=failed_samples
            )
            raise AssertionError("Injected metadata registration failure was accepted")
        except RuntimeError as exc:
            assert "injected metadata" in str(exc)
    finally:
        takes.apply_visibility = real_apply_visibility
    assert set(text.name for text in bpy.data.texts) == texts_before_failure
    assert not failed.get(recording.RUNTIME_METADATA_TEXT_PROPERTY, "")
    recording.discard_baked_take(failed)

    # Reject/restore retains metadata; deleting a rejected take removes only its
    # exclusively owned Text. Save/reopen preserves both metadata and legacy takes.
    takes.reject_take(bpy.context.scene, first)
    assert bpy.data.texts.get(first_text.name) is first_text
    takes.restore_take(bpy.context, first)
    assert takes.current_take(bpy.context.scene) is first

    blend_path = os.environ["LIBSM64_TEST_BLEND"]
    bpy.ops.wm.save_as_mainfile(filepath=blend_path, check_existing=False)
    bpy.ops.wm.open_mainfile(filepath=blend_path)
    first = takes.find_take(first_id)
    second = takes.find_take(second_id)
    assert first is not None and second is not None
    first_validated = takes.runtime_metadata_for_take(first)
    second_validated = takes.runtime_metadata_for_take(second)
    assert first_validated["text"].as_string() == first_body
    assert first_validated["samples"][1].action == first_samples[1].runtime_metadata.action
    assert second_validated["samples"][2].flags == second_samples[2].runtime_metadata.flags
    assert any(takes.runtime_metadata_for_take(obj) is None for obj in takes.iter_takes())

    second_text_name = second_validated["text"].name
    takes.reject_take(bpy.context.scene, second)
    assert takes.cleanup_rejected(bpy.context.scene) == 1
    assert bpy.data.texts.get(second_text_name) is None
    assert first_validated["text"].as_string() == first_body

    # Compact JSON stays directly inspectable with no ROM, DLL, or live add-on state.
    direct_document = json.loads(first_validated["text"].as_string())
    assert direct_document["coordinate_conventions"]["metadata_effect"].startswith(
        "inspection_only"
    )
    assert math.isclose(direct_document["samples"][0]["face_angle"], -3.0)
finally:
    addon.unregister()

print("libsm64 runtime-metadata persistence regression passed")
