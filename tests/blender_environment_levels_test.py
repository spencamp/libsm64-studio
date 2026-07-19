"""Blender 5.2 fake-native regression for global water and poison gas levels."""

from pathlib import Path
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
from libsm64_studio import mario


class NativeCall:
    def __init__(self, library, name, result=None, failure=None):
        self.library = library
        self.name = name
        self.result = result
        self.failure = failure
        self.calls = []
        self.argtypes = None
        self.restype = object()

    def __call__(self, *args):
        self.calls.append(args)
        self.library.events.append(self.name)
        if self.failure is not None:
            raise self.failure
        return self.result


class FakeLibrary:
    def __init__(self, mario_id=17):
        self.events = []
        self.sm64_global_init = NativeCall(self, "global_init")
        self.sm64_global_terminate = NativeCall(self, "global_terminate")
        self.sm64_static_surfaces_load = NativeCall(self, "static_load")
        self.sm64_mario_create = NativeCall(self, "mario_create", mario_id)
        self.sm64_mario_tick = NativeCall(self, "mario_tick")
        self.sm64_mario_delete = NativeCall(self, "mario_delete")
        self.sm64_set_mario_faceangle = NativeCall(self, "faceangle")
        self.sm64_surface_object_create = NativeCall(self, "surface_create", 100)
        self.sm64_surface_object_move = NativeCall(self, "surface_move")
        self.sm64_surface_object_delete = NativeCall(self, "surface_delete")
        self.sm64_set_mario_action = NativeCall(self, "action")
        self.sm64_set_mario_animation = NativeCall(self, "animation")
        self.sm64_set_mario_anim_frame = NativeCall(self, "anim_frame")
        self.sm64_set_mario_state = NativeCall(self, "flags")
        self.sm64_set_mario_position = NativeCall(self, "position")
        self.sm64_set_mario_velocity = NativeCall(self, "velocity")
        self.sm64_set_mario_forward_velocity = NativeCall(self, "forward_velocity")
        self.sm64_set_mario_health = NativeCall(self, "health")
        self.sm64_set_mario_invincibility = NativeCall(self, "invincibility")
        self.sm64_set_mario_water_level = NativeCall(self, "water")
        self.sm64_set_mario_gas_level = NativeCall(self, "gas")


def ensure_minimal_blender_data(_texture):
    mesh = bpy.data.meshes.get("libsm64_mario_mesh")
    if mesh is None:
        mesh = bpy.data.meshes.new("libsm64_mario_mesh")
        mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])


libraries = []


def insert_with(library, scale=100.0):
    libraries.append(library)
    assert mario.insert_mario("mock.z64", scale, False) is None
    return mario._lifecycle


real = {
    "read_rom": mario._read_validated_rom,
    "load_library": mario._load_native_library,
    "initialize": mario.initialize_all_data,
    "prepare_chunks": mario._initialize_streamed_collision,
    "stream": mario._stream_collision_for_position,
    "start_input": mario.start_input_reader,
    "stop_input": mario.stop_input_reader,
    "sample_input": mario.sample_input_reader,
    "prepare_blender": mario._prepare_blender_for_insert,
    "update": mario.update_mesh_data,
    "update_fast": mario.update_mesh_data_fast,
}

mario._read_validated_rom = lambda _path: bytearray(b"mock-rom")
mario._load_native_library = lambda: libraries.pop(0)
mario.initialize_all_data = ensure_minimal_blender_data
mario._initialize_streamed_collision = lambda _session, _position: None
mario._stream_collision_for_position = lambda _session, _position: False
mario.start_input_reader = lambda: None
mario.stop_input_reader = lambda: None
mario.sample_input_reader = lambda _inputs: None
mario._prepare_blender_for_insert = lambda: None
mario.update_mesh_data = lambda _mesh: None
mario.update_mesh_data_fast = lambda _mesh: None

addon.register()
try:
    scene = bpy.context.scene
    settings = scene.libsm64
    scene.cursor.location = (10.0, -20.0, 30.0)

    # Pure conversion follows the centralized Blender Z -> native Y mapping,
    # including negative heights, origin offsets, scale, and int32 validation.
    mario.SM64_SCALE_FACTOR = 100.0
    mario.origin_offset[:] = tuple(scene.cursor.location)
    assert mario.blender_height_to_native_level(35.25) == 525
    assert mario.blender_height_to_native_level(-1.0) == -3100
    assert mario.blender_height_to_native_level(31.5, scale=20.0) == 30
    for invalid in (float("nan"), float("inf"), -float("inf")):
        try:
            mario.blender_height_to_native_level(invalid)
            raise AssertionError("Non-finite environment height was accepted")
        except ValueError:
            pass

    # Disabled defaults require no optional export during creation and retain
    # the pinned decomp's exact -10000 no-water/no-gas value.
    disabled_library = FakeLibrary()
    session = insert_with(disabled_library)
    assert not disabled_library.sm64_set_mario_water_level.calls
    assert not disabled_library.sm64_set_mario_gas_level.calls
    diagnostics = mario.environment_diagnostics()
    assert diagnostics["disabled_native_level"] == -10000
    assert diagnostics["levels"][mario.ENVIRONMENT_WATER]["native_level"] == -10000
    assert diagnostics["levels"][mario.ENVIRONMENT_GAS]["native_level"] == -10000
    original_id = session.mario_id

    # Ordinary property edits update the owned Mario in place. Water above and
    # below, poison gas above and below, and disabling all use exact signed ints.
    settings.enable_water = True
    settings.water_height = 35.25
    assert disabled_library.sm64_set_mario_water_level.calls[-1] == (original_id, 525)
    settings.water_height = 25.0
    assert disabled_library.sm64_set_mario_water_level.calls[-1] == (original_id, -500)
    settings.enable_poison_gas = True
    settings.gas_height = 40.0
    assert disabled_library.sm64_set_mario_gas_level.calls[-1] == (original_id, 1000)
    settings.gas_height = 20.0
    assert disabled_library.sm64_set_mario_gas_level.calls[-1] == (original_id, -1000)
    settings.enable_water = False
    assert disabled_library.sm64_set_mario_water_level.calls[-1] == (
        original_id, -10000
    )
    assert session.mario_id == original_id
    assert len(disabled_library.sm64_mario_create.calls) == 1
    assert not disabled_library.sm64_surface_object_create.calls
    assert not disabled_library.sm64_surface_object_delete.calls

    # Edits remain valid while recording and do not touch recording samples or
    # collision streaming ownership.
    session.control_state = mario.RECORDING
    settings.gas_height = 41.0
    assert disabled_library.sm64_set_mario_gas_level.calls[-1] == (original_id, 1100)
    assert session.control_state == mario.RECORDING
    session.control_state = mario.LIVE_IDLE

    # A Start Mark recreation reapplies every enabled level before its neutral tick.
    settings.enable_water = True
    settings.water_height = 36.0
    settings.enable_poison_gas = True
    settings.gas_height = 42.0
    mario.mario_state.position[:] = (0.0, 0.0, 0.0)
    mario.mario_state.velocity[:] = (0.0, 0.0, 0.0)
    mario.mario_state.faceAngle = 0.0
    mario.mario_state.forwardVelocity = 0.0
    mario.mario_state.health = 0x880
    mario.mario_state.action = 0
    mario.mario_state.animID = 0
    mario.mario_state.animFrame = 0
    mario.mario_state.flags = 0
    mario.mario_state.particleFlags = 0
    mario.mario_state.invincTimer = 0
    mark = mario.set_persistent_start_mark()
    events_before_reset = len(disabled_library.events)
    replacement_id = mario.restore_mario_starting_mark(mark)
    reset_events = disabled_library.events[events_before_reset:]
    assert replacement_id == original_id
    assert reset_events.index("water") < reset_events.index("mario_tick")
    assert reset_events.index("gas") < reset_events.index("mario_tick")
    assert disabled_library.sm64_set_mario_water_level.calls[-1] == (original_id, 600)
    assert disabled_library.sm64_set_mario_gas_level.calls[-1] == (original_id, 1200)

    # Scene settings persist through session shutdown/restart and are applied to
    # the new Mario without requiring any baked take playback support.
    assert not mario.stop_tick_mario(_cleanup_rejected=False)
    create_count = len(disabled_library.sm64_mario_create.calls)
    settings.water_height = 37.0  # no native session: saved setting only
    assert len(disabled_library.sm64_mario_create.calls) == create_count
    restart_library = FakeLibrary(mario_id=23)
    restarted = insert_with(restart_library, scale=50.0)
    assert restart_library.sm64_set_mario_water_level.calls[-1] == (23, 350)
    assert restart_library.sm64_set_mario_gas_level.calls[-1] == (23, 600)
    assert restarted.mario_id == 23
    assert not mario.stop_tick_mario(_cleanup_rejected=False)

    # Save/reopen keeps the Blender-native settings when no ROM or DLL is in use.
    blend_path = os.environ["LIBSM64_TEST_BLEND"]
    settings.enable_water = True
    settings.water_height = -12.5
    settings.enable_poison_gas = False
    settings.gas_height = 99.25
    bpy.ops.wm.save_as_mainfile(filepath=blend_path, check_existing=False)
    settings.water_height = 1.0
    settings.enable_poison_gas = True
    bpy.ops.wm.open_mainfile(filepath=blend_path)
    settings = bpy.context.scene.libsm64
    assert settings.enable_water
    assert math.isclose(settings.water_height, -12.5)
    assert not settings.enable_poison_gas
    assert math.isclose(settings.gas_height, 99.25)

    # Missing optional exports are recoverable. A native exception after a call
    # begins is session-poisoning and blocks stale follow-up operations.
    settings.enable_water = True
    settings.enable_poison_gas = False
    missing_library = FakeLibrary(mario_id=31)
    del missing_library.sm64_set_mario_water_level
    missing_session = insert_with(missing_library)
    assert missing_session.control_state == mario.LIVE_IDLE
    assert "missing sm64_set_mario_water_level" in missing_session.last_environment_error
    assert not mario.stop_tick_mario(_cleanup_rejected=False)

    settings.enable_water = False
    failing_library = FakeLibrary(mario_id=41)
    failing_session = insert_with(failing_library)
    failing_library.sm64_set_mario_water_level.failure = RuntimeError("injected water failure")
    settings.enable_water = True
    assert failing_session.control_state == mario.POISONED
    assert "Native water level update failed" in failing_session.last_error
    assert not mario.apply_scene_environment_level(scene, mario.ENVIRONMENT_WATER)
    failing_library.sm64_set_mario_water_level.failure = None
    assert not mario.stop_tick_mario(_cleanup_rejected=False)
finally:
    try:
        mario.stop_tick_mario(_cleanup_rejected=False)
    except Exception:
        pass
    mario._read_validated_rom = real["read_rom"]
    mario._load_native_library = real["load_library"]
    mario.initialize_all_data = real["initialize"]
    mario._initialize_streamed_collision = real["prepare_chunks"]
    mario._stream_collision_for_position = real["stream"]
    mario.start_input_reader = real["start_input"]
    mario.stop_input_reader = real["stop_input"]
    mario.sample_input_reader = real["sample_input"]
    mario._prepare_blender_for_insert = real["prepare_blender"]
    mario.update_mesh_data = real["update"]
    mario.update_mesh_data_fast = real["update_fast"]
    addon.unregister()

print("libsm64 water/gas environment regression passed")
