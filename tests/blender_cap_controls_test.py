"""Blender 5.2 fake-native regression for Wing, Metal, and Vanish Cap controls."""

from pathlib import Path
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
from libsm64_studio.recording import bake_shape_keys, discard_baked_take, recorder


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


class CapCall(NativeCall):
    def __call__(self, mario_id, cap_flag, duration, play_music):
        result = super().__call__(mario_id, cap_flag, duration, play_music)
        self.library.current_cap = int(cap_flag)
        return result


class StateCall(NativeCall):
    def __call__(self, mario_id, flags):
        result = super().__call__(mario_id, flags)
        self.library.current_cap = int(flags) & mario.MARIO_SPECIAL_CAP_MASK
        return result


class TickCall(NativeCall):
    def __call__(self, mario_id, _inputs, state_pointer, geometry_pointer):
        result = super().__call__(mario_id, _inputs, state_pointer, geometry_pointer)
        state = mario.ct.cast(
            state_pointer, mario.ct.POINTER(mario.SM64MarioState)
        ).contents
        state.position[:] = (0.0, 0.0, 0.0)
        state.velocity[:] = (0.0, 0.0, 0.0)
        state.faceAngle = 0.0
        state.forwardVelocity = 0.0
        state.health = 0x880
        state.action = self.library.action
        state.animID = 0
        state.animFrame = 0
        state.flags = self.library.current_cap | 0x10
        state.particleFlags = 0
        state.invincTimer = 0
        geometry = mario.ct.cast(
            geometry_pointer, mario.ct.POINTER(mario.SM64MarioGeometryBuffers)
        ).contents
        geometry.numTrianglesUsed = 1
        cap_value = float(self.library.current_cap)
        coordinates = (
            cap_value, 0.0, 0.0,
            cap_value, 1.0, 0.0,
            cap_value, 0.0, 1.0,
        )
        for index, value in enumerate(coordinates):
            geometry.position[index] = value
        return result


class FakeLibrary:
    def __init__(self, mario_id=17):
        self.events = []
        self.current_cap = 0
        self.action = 0
        self.sm64_global_init = NativeCall(self, "global_init")
        self.sm64_global_terminate = NativeCall(self, "global_terminate")
        self.sm64_static_surfaces_load = NativeCall(self, "static_load")
        self.sm64_mario_create = NativeCall(self, "mario_create", mario_id)
        self.sm64_mario_tick = TickCall(self, "mario_tick")
        self.sm64_mario_delete = NativeCall(self, "mario_delete")
        self.sm64_set_mario_faceangle = NativeCall(self, "faceangle")
        self.sm64_surface_object_create = NativeCall(self, "surface_create", 100)
        self.sm64_surface_object_move = NativeCall(self, "surface_move")
        self.sm64_surface_object_delete = NativeCall(self, "surface_delete")
        self.sm64_set_mario_action = NativeCall(self, "action_set")
        self.sm64_set_mario_animation = NativeCall(self, "animation")
        self.sm64_set_mario_anim_frame = NativeCall(self, "anim_frame")
        self.sm64_set_mario_state = StateCall(self, "flags")
        self.sm64_set_mario_position = NativeCall(self, "position")
        self.sm64_set_mario_velocity = NativeCall(self, "velocity")
        self.sm64_set_mario_forward_velocity = NativeCall(self, "forward_velocity")
        self.sm64_set_mario_health = NativeCall(self, "health")
        self.sm64_set_mario_invincibility = NativeCall(self, "invincibility")
        self.sm64_mario_interact_cap = CapCall(self, "interact_cap")
        self.sm64_mario_extend_cap = NativeCall(self, "extend_cap")


def ensure_minimal_blender_data(_texture):
    mesh = bpy.data.meshes.get("libsm64_mario_mesh")
    if mesh is None:
        mesh = bpy.data.meshes.new("libsm64_mario_mesh")
        mesh.from_pydata([(0, 0, 0), (0, 1, 0), (0, 0, 1)], [], [(0, 1, 2)])


full_mesh_updates = []
fast_mesh_updates = []


def update_fake_cap_mesh(mesh):
    full_mesh_updates.append(mario._lifecycle.library.current_cap)
    cap_value = float(mario._lifecycle.library.current_cap)
    coordinates = (
        cap_value, 0.0, 0.0,
        cap_value, 1.0, 0.0,
        cap_value, 0.0, 1.0,
    )
    mesh.vertices.foreach_set("co", coordinates)
    mesh.update()


def update_fake_cap_mesh_fast(mesh):
    fast_mesh_updates.append(mario._lifecycle.library.current_cap)
    update_fake_cap_mesh(mesh)


libraries = []


def insert_with(library):
    libraries.append(library)
    assert mario.insert_mario("mock.z64", 1.0, False) is None
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
mario.update_mesh_data = update_fake_cap_mesh
mario.update_mesh_data_fast = update_fake_cap_mesh_fast

addon.register()
try:
    assert mario.MARIO_VANISH_CAP == 0x00000002
    assert mario.MARIO_METAL_CAP == 0x00000004
    assert mario.MARIO_WING_CAP == 0x00000008
    assert mario.SUPPORTED_CAP_FLAGS == (0x8, 0x4, 0x2)

    scene = bpy.context.scene
    library = FakeLibrary()
    session = insert_with(library)
    mario.remove_tick_mario_timer(session)
    mario.tick_count = 20
    mario.origin_offset[:] = (0.0, 0.0, 0.0)

    # Native default duration behavior is selected by passing zero unchanged;
    # audio is not active in 3D, so every grant explicitly disables cap music.
    assert mario.grant_mario_cap(mario.MARIO_WING_CAP) == mario.MARIO_WING_CAP
    assert library.sm64_mario_interact_cap.calls[-1] == (17, 0x8, 0, 0)
    mario.tick_mario(scene, _session=session)
    assert tuple(session.live_object.data.vertices[0].co) == (8.0, 0.0, 0.0)
    assert int(mario.mario_state.flags) & mario.MARIO_WING_CAP
    assert full_mesh_updates[-1] == mario.MARIO_WING_CAP
    assert not fast_mesh_updates

    # Explicit durations, running/airborne actions, and each cap flag all pass
    # without action-specific Python guardrails or Mario recreation.
    library.action = 0x04000440  # running-like synthetic action
    mario.grant_mario_cap(mario.MARIO_METAL_CAP, 90)
    assert library.sm64_mario_interact_cap.calls[-1] == (17, 0x4, 90, 0)
    library.action = 0x03000880  # airborne-like synthetic action
    mario.grant_mario_cap(mario.MARIO_VANISH_CAP, 120)
    assert library.sm64_mario_interact_cap.calls[-1] == (17, 0x2, 120, 0)
    assert len(library.sm64_mario_create.calls) == 1

    # No Cap clears only the special-cap bits through the existing live-state
    # setter. The cached UI diagnostic and the next native tick agree.
    mario.tick_mario(scene, _session=session)
    assert int(mario.mario_state.flags) == mario.MARIO_VANISH_CAP | 0x10
    assert mario.clear_mario_cap() == 0
    assert library.sm64_set_mario_state.calls[-1] == (17, 0x10)
    assert mario.cap_diagnostics()["active_flags"] == 0
    mario.tick_mario(scene, _session=session)
    assert not int(mario.mario_state.flags) & mario.MARIO_SPECIAL_CAP_MASK
    assert mario.cap_diagnostics()["history"][-1].operation == "clear"

    # Extension requires a meaningful positive tick count.
    assert mario.extend_mario_cap(45) == 45
    assert library.sm64_mario_extend_cap.calls[-1] == (17, 45)

    # Grant during recording; same-tick cap geometry is captured and survives in
    # independently baked local pose shape keys without any ROM playback path.
    recorder.start(1.0, 30.0)
    session.control_state = mario.RECORDING
    mario.grant_mario_cap(mario.MARIO_WING_CAP, 60)
    mario.tick_mario(scene, _session=session)
    mario.grant_mario_cap(mario.MARIO_METAL_CAP, 60)
    mario.tick_mario(scene, _session=session)
    mario.grant_mario_cap(mario.MARIO_VANISH_CAP, 60)
    mario.tick_mario(scene, _session=session)
    assert recorder.sample_count == 3
    assert [float(sample.coordinates[0]) for sample in recorder.samples] == [8.0, 4.0, 2.0]
    assert [
        sample.runtime_metadata.flags & 0xE for sample in recorder.samples
    ] == [mario.MARIO_WING_CAP, mario.MARIO_METAL_CAP, mario.MARIO_VANISH_CAP]
    samples = recorder.freeze_for_bake()
    baked = bake_shape_keys(bpy.context, session.live_object, samples, 1.0, 30.0)
    assert int(baked.get("libsm64_sample_count", 0)) == 3
    pose_values = [
        float(baked.data.shape_keys.key_blocks[index].data[0].co.x)
        for index in range(1, 4)
    ]
    assert pose_values == [8.0, 4.0, 2.0]
    discard_baked_take(baked)
    recorder.complete("Cap geometry")
    session.control_state = mario.LIVE_IDLE

    # Start Mark metadata captures the cap flag. Performance restoration writes
    # that flag safely; cap timer/internal state remains intentionally unsupported.
    mario.grant_mario_cap(mario.MARIO_WING_CAP, 300)
    mario.tick_mario(scene, _session=session)
    mark = mario.set_persistent_start_mark()
    assert mark.flags & mario.MARIO_WING_CAP
    create_count = len(library.sm64_mario_create.calls)
    mario.restore_mario_starting_mark(mark)
    assert len(library.sm64_mario_create.calls) == create_count + 1
    assert any(int(call[1]) & mario.MARIO_WING_CAP for call in library.sm64_set_mario_state.calls)

    # Validation errors occur before native calls and do not poison the session.
    interact_count = len(library.sm64_mario_interact_cap.calls)
    for invalid_flag in (0, 1, 3, 0x10, -1, 0x100000000):
        try:
            mario.grant_mario_cap(invalid_flag)
            raise AssertionError("Invalid cap flag was accepted")
        except ValueError:
            pass
    for invalid_duration in (-1, 0x10000):
        try:
            mario.grant_mario_cap(mario.MARIO_WING_CAP, invalid_duration)
            raise AssertionError("Invalid cap duration was accepted")
        except ValueError:
            pass
    try:
        mario.extend_mario_cap(0)
        raise AssertionError("Zero-tick cap extension was accepted")
    except ValueError:
        pass
    assert len(library.sm64_mario_interact_cap.calls) == interact_count
    assert session.control_state == mario.LIVE_IDLE

    history = mario.cap_diagnostics()["history"]
    assert history
    assert all(isinstance(entry, mario.MarioCapRequest) for entry in history)
    assert all(not entry.play_music for entry in history)
    assert not mario.stop_tick_mario(_cleanup_rejected=False)

    # Session restart starts with isolated cap history. Missing exports are
    # recoverable when invoked; native exceptions poison exact ownership.
    missing_library = FakeLibrary(mario_id=23)
    del missing_library.sm64_mario_extend_cap
    missing_session = insert_with(missing_library)
    mario.remove_tick_mario_timer(missing_session)
    assert not mario.cap_diagnostics()["history"]
    try:
        mario.grant_mario_cap(mario.MARIO_WING_CAP)
        raise AssertionError("Missing cap export was accepted")
    except mario.MarioFeatureUnavailableError:
        pass
    assert missing_session.control_state == mario.LIVE_IDLE
    assert not mario.stop_tick_mario(_cleanup_rejected=False)

    failing_library = FakeLibrary(mario_id=31)
    failing_session = insert_with(failing_library)
    mario.remove_tick_mario_timer(failing_session)
    failing_library.sm64_mario_interact_cap.failure = RuntimeError("injected cap failure")
    try:
        mario.grant_mario_cap(mario.MARIO_METAL_CAP)
        raise AssertionError("Failed native cap grant was accepted")
    except mario.MarioLifecycleError:
        pass
    assert failing_session.control_state == mario.POISONED
    failing_library.sm64_mario_interact_cap.failure = None
    assert not mario.stop_tick_mario(_cleanup_rejected=False)
finally:
    try:
        mario.stop_tick_mario(_cleanup_rejected=False)
    except Exception:
        pass
    recorder.cancel()
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

print("libsm64 cap-controls regression passed")
