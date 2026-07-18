"""Blender regression for persistent timer-driven Live Mario control.

Run with:
  blender --background --factory-startup --python tests/blender_live_control_test.py
"""

from pathlib import Path
import math
import sys

import bpy


root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
import libsm64_studio as addon
from libsm64_studio import mario
from libsm64_studio.recording import recorder


class NativeCall:
    def __init__(self, result=None, failure=None, name=None, events=None):
        self.result = result
        self.failure = failure
        self.calls = []
        self.argtypes = None
        self.restype = object()
        self.name = name
        self.events = events

    def __call__(self, *args):
        self.calls.append(args)
        if self.events is not None:
            self.events.append(self.name)
        if self.failure is not None:
            raise self.failure
        return self.result


class FakeLibrary:
    def __init__(self, mario_id):
        self.events = []
        self.sm64_global_init = NativeCall()
        self.sm64_global_terminate = NativeCall()
        self.sm64_static_surfaces_load = NativeCall(name="surfaces_load", events=self.events)
        self.sm64_mario_create = NativeCall(mario_id, name="mario_create", events=self.events)
        self.sm64_mario_delete = NativeCall(name="mario_delete", events=self.events)
        self.sm64_mario_tick = NativeCall()


libraries = []


def ensure_minimal_blender_data(_texture):
    mesh = bpy.data.meshes.get("libsm64_mario_mesh")
    if mesh is None:
        mesh = bpy.data.meshes.new("libsm64_mario_mesh")
        mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])


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


scene = bpy.context.scene
scene.render.fps = 24
scene.render.fps_base = 1.001
scene.frame_current = 41

unrelated_calls = []
def unrelated_handler(_scene, _depsgraph=None):
    unrelated_calls.append(True)
bpy.app.handlers.frame_change_pre.append(unrelated_handler)

first = FakeLibrary(7)
libraries.append(first)
assert mario.insert_mario("mock.z64", 100, False) is None
session = mario._lifecycle
timer = session.timer_callback
assert len(first.sm64_static_surfaces_load.calls) == 1
assert len(first.sm64_mario_create.calls) == 1
assert first.events.index("surfaces_load") < first.events.index("mario_create")
assert mario.live_control_status() == mario.LIVE_IDLE
assert bpy.app.timers.is_registered(timer)
assert unrelated_handler in bpy.app.handlers.frame_change_pre
assert scene.render.fps == 24 and math.isclose(scene.render.fps_base, 1.001, rel_tol=1e-6)

# Idle simulation advances the native instance but never enters the recorder.
ticks_before = len(first.sm64_mario_tick.calls)
assert timer() == mario.SIMULATION_INTERVAL
assert len(first.sm64_mario_tick.calls) == ticks_before + 1
assert len(first.sm64_static_surfaces_load.calls) == 1
assert len(first.sm64_mario_create.calls) == 1
assert len(first.sm64_mario_delete.calls) == 0
assert not recorder.active and recorder.sample_count == 0

# A persistent mark is deliberate; recording captures only later ticks and does
# not replace it.
mario.mario_state.posX = 123.25
mario.mario_state.posY = 456.5
mario.mario_state.posZ = -78.75
mark = mario.set_persistent_start_mark()
assert mark["position"] == (123.25, 456.5, -78.75)
mario.mario_state.posX = 321.0
mario.begin_mario_recording(scene)
assert mario._valid_persistent_start_mark()["position"] == (123.25, 456.5, -78.75)
assert recorder.start_frame == 41.0
assert math.isclose(
    recorder.target_fps,
    scene.render.fps / scene.render.fps_base,
    rel_tol=1e-9,
)
assert mario.live_control_status() == mario.RECORDING
assert session.timer_callback is timer
timer()
assert recorder.sample_count == 1
samples = mario.freeze_mario_recording_for_bake()
assert len(samples) == 1 and mario.live_control_status() == mario.BAKING
recorder.complete("Take 001")
mario.return_to_start_mark_after_transition()
assert mario.live_control_status() == mario.LIVE_IDLE
assert session.timer_callback is timer and bpy.app.timers.is_registered(timer)
assert first.sm64_mario_create.calls[-1] == (123, 456, -79)
assert all(not value for value in addon.input_value.values())
assert scene.render.fps == 24 and math.isclose(scene.render.fps_base, 1.001, rel_tol=1e-6)

# No resume/reinsert is needed for a second take, and cancel restores the
# persistent mark rather than the position where that take began.
mario.mario_state.posX = 9.0
mario.mario_state.posY = 8.0
mario.mario_state.posZ = 7.0
mario.begin_mario_recording(scene)
timer()
assert recorder.sample_count == 1
recorder.cancel()
mario.return_to_start_mark_after_transition()
assert mario.live_control_status() == mario.LIVE_IDLE
assert bpy.app.timers.is_registered(timer)
assert first.sm64_mario_create.calls[-1] == (123, 456, -79)

# A failed bake can retain samples while live control safely returns to idle.
mario.begin_mario_recording(scene)
timer()
mario.freeze_mario_recording_for_bake()
recorder.fail("injected bake failure", preserve_samples=True)
mario.abandon_bake_transition()
assert recorder.has_pending_samples
assert mario.live_control_status() == mario.LIVE_IDLE
recorder.cancel()
mario.return_to_start_mark_after_transition()
assert bpy.app.timers.is_registered(timer)

# Keyboard latches are usable while idle and cleared by a reset.
addon.config["keyboard_control"] = True
addon.process_input(type("Event", (), {"type": "W", "value": "PRESS"})())
assert addon.input_value["UP"]
mario.reset_to_persistent_start_mark()
assert not addon.input_value["UP"]

# Reinstalling is idempotent; stale generation callbacks cannot tick the next session.
assert not mario._install_tick_timer(session)
stale_timer = timer
second = FakeLibrary(11)
libraries.append(second)
assert mario.insert_mario("mock.z64", 100, False) is None
new_session = mario._lifecycle
new_ticks = len(second.sm64_mario_tick.calls)
assert stale_timer() is None
assert len(second.sm64_mario_tick.calls) == new_ticks
assert bpy.app.timers.is_registered(new_session.timer_callback)
assert unrelated_handler in bpy.app.handlers.frame_change_pre

mario.stop_tick_mario()
mario.stop_tick_mario()
assert len(second.sm64_mario_delete.calls) == 1
assert len(second.sm64_global_terminate.calls) == 1
assert not bpy.app.timers.is_registered(new_session.timer_callback)
assert unrelated_handler in bpy.app.handlers.frame_change_pre
bpy.app.handlers.frame_change_pre.remove(unrelated_handler)

# A reset-delete failure is poisoned and cannot tick or terminate uncertain state.
poisoned = FakeLibrary(13)
libraries.append(poisoned)
assert mario.insert_mario("mock.z64", 100, False) is None
poisoned_session = mario._lifecycle
poisoned.sm64_mario_delete = NativeCall(failure=RuntimeError("injected reset delete failure"))
try:
    mario.set_persistent_start_mark()
    mario.reset_to_persistent_start_mark()
    raise AssertionError("Reset deletion failure should poison live control")
except mario.MarioLifecycleError as exc:
    assert "restart Blender" in str(exc)
assert mario.live_control_status() == mario.POISONED
assert not mario.is_mario_running()
assert not bpy.app.timers.is_registered(poisoned_session.timer_callback)
errors = mario.stop_tick_mario()
assert errors and "deletion did not complete" in errors[0]
assert len(poisoned.sm64_mario_delete.calls) == 1
assert len(poisoned.sm64_global_terminate.calls) == 0
mario.stop_tick_mario()
assert len(poisoned.sm64_mario_delete.calls) == 1

print("libsm64 persistent live-control regression passed")
