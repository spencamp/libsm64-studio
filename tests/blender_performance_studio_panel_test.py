"""Focused draw regression for the Performance Studio tab."""

from pathlib import Path
from types import SimpleNamespace
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
from libsm64_studio import mario


class RecordingLayout:
    def __init__(self, events=None):
        self.events = events if events is not None else []
        self.enabled = True
        self.scale_y = 1.0

    def row(self, *args, **kwargs):
        return RecordingLayout(self.events)

    def column(self, *args, **kwargs):
        return RecordingLayout(self.events)

    def box(self, *args, **kwargs):
        self.events.append(("box", None, {}))
        return RecordingLayout(self.events)

    def split(self, *args, **kwargs):
        return RecordingLayout(self.events)

    def separator(self, *args, **kwargs):
        self.events.append(("separator", None, {}))

    def label(self, *args, **kwargs):
        self.events.append(("label", kwargs.get("text", ""), kwargs))

    def prop(self, data, property_name, *args, **kwargs):
        self.events.append(("prop", property_name, kwargs))

    def operator(self, operator_id, *args, **kwargs):
        self.events.append(("operator", operator_id, kwargs))
        return SimpleNamespace()


addon.register()
original_mario_flags = int(mario.mario_state.flags)
originals = {
    "iter_takes": addon.iter_takes,
    "current_take": addon.current_take,
    "has_owned_native_session": addon.has_owned_native_session,
    "live_control_status": addon.live_control_status,
    "recorder": addon.recorder,
}
try:
    take = {
        addon.TAKE_ID: "panel-test-take",
        "libsm64_take_number": 1,
        addon.TAKE_DISPOSITION: addon.REGULAR,
        "libsm64_sample_count": 31,
        "libsm64_sample_fps": 30.0,
        "libsm64_target_fps": 24.0,
        "libsm64_recording_start_frame": 12.0,
    }
    addon.iter_takes = lambda: [take]
    addon.current_take = lambda _scene: take
    addon.has_owned_native_session = lambda: False
    addon.live_control_status = lambda: addon.STOPPED
    mario.mario_state.flags = mario.MARIO_WING_CAP

    layout = RecordingLayout()
    panel = SimpleNamespace(layout=layout)
    addon.Main_PT_Panel.draw(panel, bpy.context)

    labels = [value for kind, value, _kwargs in layout.events if kind == "label"]
    properties = [value for kind, value, _kwargs in layout.events if kind == "prop"]
    operators = [value for kind, value, _kwargs in layout.events if kind == "operator"]

    required_headings = (
        "Record a Mario Performance",
        "Active Take",
        "Performance Controls",
        "Environment",
    )
    heading_positions = [labels.index(heading) for heading in required_headings]
    assert heading_positions == sorted(heading_positions)
    assert labels[-1] != "Environment"  # Environment retains its own contents.
    assert not any(
        label in ("Audio", "Diagnostics", "Health / Damage") for label in labels
    )
    assert not any(
        label.startswith(prefix)
        for label in labels
        for prefix in (
            "Action:", "Animation:", "Velocity:", "Forward velocity:",
            "Health:", "Flags:", "Particles:", "Invincibility:", "Held sample",
        )
    )
    assert "Duration: 1.03 seconds" in labels
    assert "Start Frame: 12" in labels
    assert "End Frame: 36" in labels
    assert all(
        property_name not in properties
        for property_name in (
            "directing_health", "directing_heal_counter", "directing_damage",
            "directing_damage_subtype", "directing_invincibility_ticks",
            "enable_live_audio", "audio_volume", "audio_mute",
            "enable_native_debug_messages",
        )
    )
    assert addon.ClearCap_OT_Operator.bl_idname in operators
    cap_buttons = {
        kwargs.get("text"): kwargs.get("depress")
        for kind, operator_id, kwargs in layout.events
        if kind == "operator" and operator_id in (
            addon.GrantCap_OT_Operator.bl_idname,
            addon.ClearCap_OT_Operator.bl_idname,
        )
    }
    assert cap_buttons == {
        "Wing": True,
        "Metal": False,
        "Vanish": False,
        "No Cap": False,
    }
    assert all(
        operator_id not in operators
        for operator_id in (
            addon.SetMarioHealth_OT_Operator.bl_idname,
            addon.HealMario_OT_Operator.bl_idname,
            addon.DamageMario_OT_Operator.bl_idname,
            addon.SetMarioInvincibility_OT_Operator.bl_idname,
            addon.KillMario_OT_Operator.bl_idname,
            addon.ProbeCollision_OT_Operator.bl_idname,
        )
    )

    # Missing timing is explained without presenting zeroes as real metadata.
    del take["libsm64_sample_fps"]
    incomplete = RecordingLayout()
    addon._draw_take_sections(incomplete, bpy.context.scene, bpy.context.scene.libsm64)
    incomplete_labels = [
        value for kind, value, _kwargs in incomplete.events if kind == "label"
    ]
    assert "Take timing unavailable" in incomplete_labels
    assert not any(label.startswith("Duration:") for label in incomplete_labels)

    # A just-started recording has real duration/start data but no invented end.
    addon.recorder = SimpleNamespace(
        active=True,
        duration_seconds=0.0,
        start_frame=48.0,
        sample_count=0,
        target_fps=24.0,
    )
    recording = RecordingLayout()
    addon._draw_take_sections(recording, bpy.context.scene, bpy.context.scene.libsm64)
    recording_labels = [
        value for kind, value, _kwargs in recording.events if kind == "label"
    ]
    assert "Recording in progress" in recording_labels
    assert "Duration: 0.00 seconds" in recording_labels
    assert "Start Frame: 48" in recording_labels
    assert not any(label.startswith("End Frame:") for label in recording_labels)

    addon.recorder = originals["recorder"]
    addon.iter_takes = lambda: []
    addon.current_take = lambda _scene: None
    empty = RecordingLayout()
    addon._draw_take_sections(empty, bpy.context.scene, bpy.context.scene.libsm64)
    empty_labels = [value for kind, value, _kwargs in empty.events if kind == "label"]
    assert "No active take" in empty_labels
    assert not any(label.startswith("Duration:") for label in empty_labels)
finally:
    mario.mario_state.flags = original_mario_flags
    for name, value in originals.items():
        setattr(addon, name, value)
    addon.unregister()

print("Performance Studio panel layout regression passed")
