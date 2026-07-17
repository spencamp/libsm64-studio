"""Blender 5.2 regression for persistent, lifecycle-owned Start Marks."""

from array import array
from pathlib import Path
from types import SimpleNamespace
import os
import sys

import bpy


root = Path(__file__).resolve().parents[1]
installed_test = os.environ.get("LIBSM64_TEST_INSTALLED") == "1"
if not installed_test:
    sys.path.insert(0, str(root))
import libsm64_studio as addon
from libsm64_studio import mario
from libsm64_studio.recording import recorder


class NativeCall:
    def __init__(self, result=None, failure=None):
        self.result = result
        self.failure = failure
        self.calls = []
        self.argtypes = None
        self.restype = object()

    def __call__(self, *args):
        self.calls.append(args)
        if self.failure is not None:
            raise self.failure
        return self.result


class FakeLibrary:
    def __init__(self, mario_id=7):
        self.sm64_global_init = NativeCall()
        self.sm64_global_terminate = NativeCall()
        self.sm64_static_surfaces_load = NativeCall()
        self.sm64_mario_create = NativeCall(mario_id)
        self.sm64_mario_delete = NativeCall()
        self.sm64_mario_tick = NativeCall()


class Reporter:
    def __init__(self):
        self.messages = []

    def report(self, levels, message):
        self.messages.append((set(levels), str(message)))


libraries = []


def ensure_minimal_blender_data(_texture):
    mesh = bpy.data.meshes.get("libsm64_mario_mesh")
    if mesh is None:
        mesh = bpy.data.meshes.new("libsm64_mario_mesh")
        mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])


def insert_with(library):
    libraries.append(library)
    assert mario.insert_mario("mock.z64", 100, False) is None


mario._read_validated_rom = lambda _path: bytearray(b"mock-rom")
mario._load_native_library = lambda: libraries.pop(0)
mario.initialize_all_data = ensure_minimal_blender_data
mario.get_surface_array_from_scene = lambda: ((mario.SM64Surface * 0)(), 0)
mario.start_input_reader = lambda: None
mario.stop_input_reader = lambda: None
mario.sample_input_reader = lambda _inputs: None
mario.update_mesh_data = lambda _mesh: None
mario.update_mesh_data_fast = lambda _mesh: None
mario._prepare_blender_for_insert = lambda: None

addon.register()
scene = bpy.context.scene
scene.frame_current = 12
scene.render.fps = 30
scene.render.fps_base = 1.0
context = SimpleNamespace(scene=scene)

unrelated_handler = lambda _scene, _depsgraph=None: None
bpy.app.handlers.frame_change_pre.append(unrelated_handler)

library = FakeLibrary()
insert_with(library)
assert not mario.has_valid_start_mark()

# Setting captures public state without moving or recreating Mario; setting again replaces it.
mario.mario_state.posX, mario.mario_state.posY, mario.mario_state.posZ = (10.25, 20.5, -30.75)
native_counts = (len(library.sm64_mario_delete.calls), len(library.sm64_mario_create.calls))
first_mark = mario.set_persistent_start_mark()
assert first_mark["position"] == (10.25, 20.5, -30.75)
assert native_counts == (len(library.sm64_mario_delete.calls), len(library.sm64_mario_create.calls))
assert mario.has_valid_start_mark()

mario.mario_state.posX, mario.mario_state.posY, mario.mario_state.posZ = (90.0, 80.0, 70.0)
second_mark = mario.set_persistent_start_mark()
assert second_mark["position"] == (90.0, 80.0, 70.0)
assert mario._valid_persistent_start_mark()["position"] == (90.0, 80.0, 70.0)

# Manual reset recreates native Mario, updates the mesh, and returns to controllable idle.
mario.reset_to_persistent_start_mark()
assert library.sm64_mario_create.calls[-1] == (90, 80, 70)
assert mario.live_control_status() == mario.LIVE_IDLE

# Recording from the current position does not reset or replace the persistent mark.
create_count = len(library.sm64_mario_create.calls)
mario.mario_state.posX = 999.0
mario.begin_mario_recording(scene, reset_to_mark=False)
assert len(library.sm64_mario_create.calls) == create_count
assert mario._valid_persistent_start_mark()["position"] == (90.0, 80.0, 70.0)
assert not addon.ResetToStartMark_OT_Operator.poll(context)
recorder.cancel()
mario.return_to_start_mark_after_transition()

# Automatic reset ordering is strict: validate, restore, resume, then recorder.start.
events = []
real_validate = mario._valid_persistent_start_mark
real_restore = mario.restore_mario_starting_mark
real_resume = mario.resume_mario_for_recording
real_start = recorder.start

def traced_validate(*args, **kwargs):
    events.append("validate")
    return real_validate(*args, **kwargs)

def traced_restore(*args, **kwargs):
    events.append("restore")
    return real_restore(*args, **kwargs)

def traced_resume(*args, **kwargs):
    events.append("resume")
    return real_resume(*args, **kwargs)

def traced_start(*args, **kwargs):
    events.append("start")
    return real_start(*args, **kwargs)

mario._valid_persistent_start_mark = traced_validate
mario.restore_mario_starting_mark = traced_restore
mario.resume_mario_for_recording = traced_resume
recorder.start = traced_start
try:
    mario.begin_mario_recording(scene, reset_to_mark=True)
finally:
    mario._valid_persistent_start_mark = real_validate
    mario.restore_mario_starting_mark = real_restore
    mario.resume_mario_for_recording = real_resume
    recorder.start = real_start
assert events == ["validate", "restore", "resume", "start"]
assert recorder.active
recorder.cancel()
mario.return_to_start_mark_after_transition()

# A failed automatic reset cannot start the recorder and leaves truthful idle state.
real_restore = mario.restore_mario_starting_mark
mario.restore_mario_starting_mark = lambda _mark: (_ for _ in ()).throw(RuntimeError("injected reset failure"))
try:
    try:
        mario.begin_mario_recording(scene, reset_to_mark=True)
        raise AssertionError("Automatic reset failure should abort recording")
    except RuntimeError as exc:
        assert "injected reset failure" in str(exc)
finally:
    mario.restore_mario_starting_mark = real_restore
assert not recorder.active and not recorder.has_pending_samples
assert mario.live_control_status() == mario.LIVE_IDLE

# Stop & Bake and Cancel both reset when a mark exists.
sample = array('f', [0.0] * (len(mario.get_live_mario_object().data.vertices) * 3))
real_freeze = addon.freeze_mario_recording_for_bake
real_bake = addon.bake_shape_keys
real_register = addon.register_baked_take
real_select = addon.select_take
try:
    addon.freeze_mario_recording_for_bake = lambda: (sample,)
    addon.bake_shape_keys = lambda *_args: SimpleNamespace(name="Baked")
    addon.register_baked_take = lambda _scene, _obj: 1
    addon.select_take = lambda *_args: None
    recorder.samples = [sample]
    recorder.start_frame = 1.0
    recorder.target_fps = 30.0
    mario._lifecycle.control_state = mario.BAKING
    before_stop_reset = len(library.sm64_mario_create.calls)
    assert addon.StopAndBake_OT_Operator.execute(Reporter(), context) == {'FINISHED'}
    assert len(library.sm64_mario_create.calls) == before_stop_reset + 1
finally:
    addon.freeze_mario_recording_for_bake = real_freeze
    addon.bake_shape_keys = real_bake
    addon.register_baked_take = real_register
    addon.select_take = real_select

mario.begin_mario_recording(scene)
before_cancel_reset = len(library.sm64_mario_create.calls)
assert addon.CancelRecording_OT_Operator.execute(Reporter(), context) == {'FINISHED'}
assert len(library.sm64_mario_create.calls) == before_cancel_reset + 1

# The same transitions succeed without a mark and do not attempt native recreation.
mario.clear_persistent_start_mark()
assert not mario.has_valid_start_mark()
real_freeze = addon.freeze_mario_recording_for_bake
real_bake = addon.bake_shape_keys
real_register = addon.register_baked_take
real_select = addon.select_take
try:
    addon.freeze_mario_recording_for_bake = lambda: (sample,)
    addon.bake_shape_keys = lambda *_args: SimpleNamespace(name="Baked no mark")
    addon.register_baked_take = lambda _scene, _obj: 2
    addon.select_take = lambda *_args: None
    recorder.samples = [sample]
    recorder.start_frame = 1.0
    recorder.target_fps = 30.0
    mario._lifecycle.control_state = mario.BAKING
    create_count = len(library.sm64_mario_create.calls)
    assert addon.StopAndBake_OT_Operator.execute(Reporter(), context) == {'FINISHED'}
    assert len(library.sm64_mario_create.calls) == create_count
finally:
    addon.freeze_mario_recording_for_bake = real_freeze
    addon.bake_shape_keys = real_bake
    addon.register_baked_take = real_register
    addon.select_take = real_select

mario.begin_mario_recording(scene)
create_count = len(library.sm64_mario_create.calls)
assert addon.CancelRecording_OT_Operator.execute(Reporter(), context) == {'FINISHED'}
assert len(library.sm64_mario_create.calls) == create_count

# A committed take remains committed when its post-bake reset fails.
mario.set_persistent_start_mark()
real_freeze = addon.freeze_mario_recording_for_bake
real_bake = addon.bake_shape_keys
real_register = addon.register_baked_take
real_select = addon.select_take
real_return = addon.return_to_start_mark_after_transition
commits = []
try:
    addon.freeze_mario_recording_for_bake = lambda: (sample,)
    addon.bake_shape_keys = lambda *_args: SimpleNamespace(name="Committed")
    addon.register_baked_take = lambda _scene, obj: commits.append(obj) or 3
    addon.select_take = lambda *_args: None
    addon.return_to_start_mark_after_transition = lambda: (_ for _ in ()).throw(RuntimeError("reset failed"))
    recorder.samples = [sample]
    recorder.start_frame = 1.0
    recorder.target_fps = 30.0
    mario._lifecycle.control_state = mario.BAKING
    reporter = Reporter()
    assert addon.StopAndBake_OT_Operator.execute(reporter, context) == {'FINISHED'}
    assert len(commits) == 1
    assert any("captured, but reset" in message for _levels, message in reporter.messages)
finally:
    addon.freeze_mario_recording_for_bake = real_freeze
    addon.bake_shape_keys = real_bake
    addon.register_baked_take = real_register
    addon.select_take = real_select
    addon.return_to_start_mark_after_transition = real_return
    mario.abandon_bake_transition()

# Marks cannot cross lifecycle generations and teardown clears the active mark.
old_stored_mark = mario._lifecycle.persistent_start_mark
old_generation = mario._lifecycle.generation
next_library = FakeLibrary(11)
insert_with(next_library)
assert mario._lifecycle.generation != old_generation
assert not mario.has_valid_start_mark()
mario._lifecycle.persistent_start_mark = old_stored_mark
assert not mario.has_valid_start_mark()
mario._lifecycle.persistent_start_mark = None
mario.mario_state.posX, mario.mario_state.posY, mario.mario_state.posZ = (1.0, 2.0, 3.0)
mario.set_persistent_start_mark()
mario.stop_tick_mario()
assert mario._lifecycle.persistent_start_mark is None
assert unrelated_handler in bpy.app.handlers.frame_change_pre

# A failed native recreation never leaves a deleted ID marked as running.
failure_library = FakeLibrary(13)
insert_with(failure_library)
mario.set_persistent_start_mark()
failure_library.sm64_mario_create.result = -1
try:
    mario.reset_to_persistent_start_mark()
    raise AssertionError("Failed native recreation should abort reset")
except mario.MarioLifecycleError:
    pass
assert mario._lifecycle.mario_id == -1
assert not mario._lifecycle.mario_created
assert mario.sm64_mario_id == -1
assert not mario.is_mario_running()
assert unrelated_handler in bpy.app.handlers.frame_change_pre
bpy.app.handlers.frame_change_pre.remove(unrelated_handler)

addon.unregister()
print("libsm64 persistent Start Mark regression passed")
