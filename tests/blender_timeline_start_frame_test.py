"""Blender regression for the persistent, independent timeline start frame."""

from pathlib import Path
from types import SimpleNamespace
import os
import sys
import tempfile

import bpy


root = Path(__file__).resolve().parents[1]
if os.environ.get("LIBSM64_TEST_INSTALLED") != "1":
    sys.path.insert(0, str(root))
import libsm64_studio as addon
from libsm64_studio import mario
from libsm64_studio.recording import PerformanceSample


class Reporter:
    def __init__(self):
        self.messages = []

    def report(self, levels, message):
        self.messages.append((set(levels), str(message)))


addon.register()
scene = bpy.context.scene
settings = scene.libsm64
reporter = Reporter()

assert not settings.timeline_start_frame_set
assert not settings.start_recording_from_saved_frame

# Set and recall the current frame through the public operators.
scene.frame_set(120)
assert addon.SetTimelineStartFrame_OT_Operator.execute(reporter, bpy.context) == {'FINISHED'}
assert settings.timeline_start_frame_set
assert settings.timeline_start_frame == 120
scene.frame_set(245)
assert addon.GoToTimelineStartFrame_OT_Operator.execute(reporter, bpy.context) == {'FINISHED'}
assert scene.frame_current == 120

# The runtime-owned spatial mark has no relationship to the scene-owned frame.
mario.clear_persistent_start_mark()
assert settings.timeline_start_frame_set
assert settings.timeline_start_frame == 120

# Automatic recall happens before recording begins, and the recalled frame is
# therefore the recorder's start frame.
settings.start_recording_from_saved_frame = True
scene.frame_set(333)
begin_calls = []
real_is_mario_running = addon.is_mario_running
real_begin_mario_recording = addon.begin_mario_recording
addon.is_mario_running = lambda: True
addon.begin_mario_recording = lambda target_scene, reset_to_mark=False: begin_calls.append(
    (target_scene.frame_current, reset_to_mark)
)
try:
    assert addon.StartRecording_OT_Operator.execute(reporter, bpy.context) == {'FINISHED'}
finally:
    addon.is_mario_running = real_is_mario_running
    addon.begin_mario_recording = real_begin_mario_recording
assert begin_calls == [(120, False)]

# Successful bake and cancel transitions recall the frame automatically, while
# leaving the setting independent from take registration and selection.
real_freeze = addon.freeze_mario_recording_for_bake
real_bake = addon.bake_shape_keys
real_register = addon.register_baked_take
real_select = addon.select_take
real_return = addon.return_to_start_mark_after_transition
transitions = []
sample = PerformanceSample((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 0.0)
try:
    addon.freeze_mario_recording_for_bake = lambda: (sample,)
    addon.bake_shape_keys = lambda *_args: SimpleNamespace(name="Baked")
    addon.register_baked_take = lambda _scene, _obj: 1
    addon.select_take = lambda *_args: None
    addon.return_to_start_mark_after_transition = lambda: transitions.append("resume")
    addon.recorder.samples = [sample]
    addon.recorder.start_frame = 120.0
    addon.recorder.target_fps = 30.0
    scene.frame_set(444)
    assert addon.StopAndBake_OT_Operator.execute(reporter, bpy.context) == {'FINISHED'}
    assert scene.frame_current == 120
    assert transitions == ["resume"]

    scene.frame_set(555)
    assert addon.CancelRecording_OT_Operator.execute(reporter, bpy.context) == {'FINISHED'}
    assert scene.frame_current == 120
    assert transitions == ["resume", "resume"]

    settings.start_recording_from_saved_frame = False
    scene.frame_set(666)
    assert addon.CancelRecording_OT_Operator.execute(reporter, bpy.context) == {'FINISHED'}
    assert scene.frame_current == 666
finally:
    addon.freeze_mario_recording_for_bake = real_freeze
    addon.bake_shape_keys = real_bake
    addon.register_baked_take = real_register
    addon.select_take = real_select
    addon.return_to_start_mark_after_transition = real_return
    settings.start_recording_from_saved_frame = True

# Both the value and its explicit set state survive a .blend save/reopen.
with tempfile.TemporaryDirectory(prefix="libsm64-timeline-") as temporary:
    blend_path = str(Path(temporary) / "timeline-start-frame.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend_path)
    settings.timeline_start_frame = 9
    settings.timeline_start_frame_set = False
    settings.start_recording_from_saved_frame = False
    bpy.ops.wm.open_mainfile(filepath=blend_path)
    reopened = bpy.context.scene.libsm64
    assert reopened.timeline_start_frame_set
    assert reopened.timeline_start_frame == 120
    assert reopened.start_recording_from_saved_frame

addon.unregister()
print("libsm64 persistent Timeline Start Frame regression passed")
