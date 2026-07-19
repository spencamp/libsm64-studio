from contextlib import redirect_stdout
from pathlib import Path
import io
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

    def __call__(self, *arguments):
        self.calls.append(arguments)
        self.library.events.append(self.name)
        if self.failure is not None:
            raise self.failure
        return self.result


class DebugRegisterCall(NativeCall):
    def __call__(self, callback):
        result = super().__call__(callback)
        self.library.debug_callback = callback if bool(callback) else None
        return result


class TickCall(NativeCall):
    def __call__(self, mario_id, inputs, state_pointer, geometry_pointer):
        result = super().__call__(mario_id, inputs, state_pointer, geometry_pointer)
        state = mario.ct.cast(
            state_pointer, mario.ct.POINTER(mario.SM64MarioState)
        ).contents
        state.position[:] = (100.0, 300.0, 200.0)
        state.velocity[:] = (1.0, -2.0, 3.0)
        state.faceAngle = 0.25
        state.forwardVelocity = 4.0
        state.health = 0x880
        state.action = 0x04000440
        state.animID = 7
        state.animFrame = 8
        state.flags = 0x10
        state.particleFlags = 0x20
        state.invincTimer = 12
        geometry = mario.ct.cast(
            geometry_pointer, mario.ct.POINTER(mario.SM64MarioGeometryBuffers)
        ).contents
        geometry.numTrianglesUsed = 1
        for index, value in enumerate((
            0.0, 0.0, 0.0,
            1.0, 0.0, 0.0,
            0.0, 1.0, 0.0,
        )):
            geometry.position[index] = value
        return result


class FakeLibrary:
    def __init__(self, mario_id=81):
        self.events = []
        self.debug_callback = None
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
        self.sm64_set_mario_health = NativeCall(self, "set_health")
        self.sm64_mario_heal = NativeCall(self, "heal")
        self.sm64_mario_take_damage = NativeCall(self, "damage")
        self.sm64_mario_kill = NativeCall(self, "kill")
        self.sm64_set_mario_invincibility = NativeCall(self, "invincibility")
        self.sm64_surface_find_floor_height = NativeCall(
            self, "find_floor", 250.0
        )
        self.sm64_surface_find_water_level = NativeCall(
            self, "find_water", -10000.0
        )
        self.sm64_surface_find_poison_gas_level = NativeCall(
            self, "find_gas", 500.0
        )
        self.sm64_register_debug_print_function = DebugRegisterCall(
            self, "register_debug"
        )


def ensure_minimal_blender_data(_texture):
    mesh = bpy.data.meshes.get("libsm64_mario_mesh")
    if mesh is None:
        mesh = bpy.data.meshes.new("libsm64_mario_mesh")
        mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])


libraries = []


def insert_with(library):
    libraries.append(library)
    assert mario.insert_mario("mock.z64", 100.0, False) is None
    session = mario._lifecycle
    mario.remove_tick_mario_timer(session)
    return session


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

mario._read_validated_rom = lambda _path: bytearray(b"ephemeral-mock-rom")
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
    scene.cursor.location = (10.0, 20.0, 30.0)
    scene.libsm64.enable_native_debug_messages = True
    library = FakeLibrary()
    session = insert_with(library)

    # Every optional signature is exact, while callback ownership is retained
    # by the active lifecycle generation rather than a temporary local value.
    mario._configure_directing_api(library)
    mario._configure_collision_query_api(library)
    assert library.sm64_set_mario_health.argtypes == [
        mario.ct.c_int32, mario.ct.c_uint16,
    ]
    assert library.sm64_mario_heal.argtypes == [
        mario.ct.c_int32, mario.ct.c_uint8,
    ]
    assert library.sm64_mario_take_damage.argtypes == [
        mario.ct.c_int32, mario.ct.c_uint32, mario.ct.c_uint32,
        mario.ct.c_float, mario.ct.c_float, mario.ct.c_float,
    ]
    assert library.sm64_mario_kill.argtypes == [mario.ct.c_int32]
    assert library.sm64_set_mario_invincibility.argtypes == [
        mario.ct.c_int32, mario.ct.c_int16,
    ]
    assert library.sm64_surface_find_floor_height.argtypes == [
        mario.ct.c_float, mario.ct.c_float, mario.ct.c_float,
    ]
    assert library.sm64_surface_find_floor_height.restype is mario.ct.c_float
    assert library.sm64_surface_find_water_level.argtypes == [
        mario.ct.c_float, mario.ct.c_float,
    ]
    assert library.sm64_surface_find_poison_gas_level.argtypes == [
        mario.ct.c_float, mario.ct.c_float,
    ]
    assert library.sm64_register_debug_print_function.argtypes == [
        mario.SM64DebugPrintFunctionPtr,
    ]
    assert session.debug_print_callback is not None
    assert library.debug_callback is session.debug_print_callback

    mario.tick_count = 20
    mario.set_mario_health(0x880)
    mario.heal_mario(3)
    mario.damage_mario(2, 7, (11.0, 18.0, 33.0))
    mario.set_mario_invincibility(45)
    mario.kill_mario()
    assert library.sm64_set_mario_health.calls[-1] == (81, 0x880)
    assert library.sm64_mario_heal.calls[-1] == (81, 3)
    damage_call = library.sm64_mario_take_damage.calls[-1]
    assert damage_call[:3] == (81, 2, 7)
    assert damage_call[3:] == (100.0, 300.0, 200.0)
    assert library.sm64_set_mario_invincibility.calls[-1] == (81, 45)
    assert library.sm64_mario_kill.calls[-1] == (81,)
    assert len(library.sm64_mario_create.calls) == 1

    key = mario.chunk_coordinate((11.0, 18.0, 33.0), mario.CHUNK_SIZE_BLENDER)
    session.active_chunk_keys.add(key)
    result = mario.probe_collision((11.0, 18.0, 33.0))
    assert result.native_position == (100.0, 300.0, 200.0)
    assert result.floor_blender_height == 32.5
    assert result.water_blender_height is None
    assert result.gas_blender_height == 35.0
    assert result.chunk_key == key and result.chunk_active
    assert library.sm64_surface_find_floor_height.calls[-1] == (
        100.0, 300.0, 200.0
    )
    assert library.sm64_surface_find_water_level.calls[-1] == (100.0, 200.0)
    assert library.sm64_surface_find_poison_gas_level.calls[-1] == (
        100.0, 200.0
    )
    library.sm64_surface_find_floor_height.result = -110000.0
    no_floor = mario.probe_collision((11.0, 18.0, 33.0))
    assert no_floor.no_floor and no_floor.floor_blender_height is None

    # Callback work is plain Python only, bounded, generation-labelled, and
    # robust to invalid bytes and overlong native messages.
    with redirect_stdout(io.StringIO()):
        for index in range(mario.NATIVE_DEBUG_LOG_LIMIT + 20):
            library.debug_callback("debug {}".format(index).encode("ascii"))
        library.debug_callback(b"x" * 2048)
    assert len(session.native_debug_log) == mario.NATIVE_DEBUG_LOG_LIMIT
    assert session.native_debug_log[-1].text.endswith("...")
    assert len(session.native_debug_log[-1].text) == 1024
    assert all(
        record.owner_token == session.owner_token
        and record.lifecycle_generation == session.generation
        for record in session.native_debug_log
    )
    owned_mario_id = session.mario_id
    scene.libsm64.enable_native_debug_messages = False
    assert library.debug_callback is None
    assert not session.debug_print_callback_registered
    scene.libsm64.enable_native_debug_messages = True
    assert library.debug_callback is session.debug_print_callback
    assert session.debug_print_callback_registered
    assert session.mario_id == owned_mario_id

    # Directing remains available while recording and does not disturb sample
    # continuity, Mario ID, or any bake/playback data path.
    recorder.start(1.0, 30.0)
    session.control_state = mario.RECORDING
    mario.heal_mario(1)
    mario.tick_mario(scene, _session=session)
    assert recorder.active and recorder.sample_count == 1
    assert session.mario_id == 81
    samples = recorder.freeze_for_bake()
    baked = bake_shape_keys(bpy.context, session.live_object, samples, 1.0, 30.0)
    assert int(baked.get("libsm64_sample_count", 0)) == 1
    discard_baked_take(baked)
    recorder.complete("Directing continuity checked")
    session.control_state = mario.LIVE_IDLE

    # Range and float validation happens before native calls and is recoverable.
    counts = tuple(len(call.calls) for call in (
        library.sm64_set_mario_health,
        library.sm64_mario_heal,
        library.sm64_mario_take_damage,
        library.sm64_set_mario_invincibility,
    ))
    invalid_calls = (
        lambda: mario.set_mario_health(-1),
        lambda: mario.set_mario_health(0x10000),
        lambda: mario.heal_mario(0),
        lambda: mario.heal_mario(0x100),
        lambda: mario.damage_mario(0, 0, (0.0, 0.0, 0.0)),
        lambda: mario.damage_mario(1, -1, (0.0, 0.0, 0.0)),
        lambda: mario.damage_mario(1, 0, (math.nan, 0.0, 0.0)),
        lambda: mario.set_mario_invincibility(-1),
        lambda: mario.set_mario_invincibility(0x8000),
    )
    for invoke in invalid_calls:
        try:
            invoke()
            raise AssertionError("Invalid directing value was accepted")
        except ValueError:
            pass
    assert counts == tuple(len(call.calls) for call in (
        library.sm64_set_mario_health,
        library.sm64_mario_heal,
        library.sm64_mario_take_damage,
        library.sm64_set_mario_invincibility,
    ))
    assert session.control_state == mario.LIVE_IDLE

    diagnostics = mario.studio_diagnostics()
    assert diagnostics["last_collision_query"] is no_floor
    assert diagnostics["last_directing_operation"]["operation"] == "heal"
    assert diagnostics["runtime_metadata_schema"] == 1
    assert diagnostics["debug_callback_registered"]

    # Shutdown keeps diagnostics alive through Mario deletion, unregisters the
    # callback before global termination, and is idempotent.
    assert not mario.stop_tick_mario(_session=session, _cleanup_rejected=False)
    assert library.debug_callback is None
    unregister_index = max(
        index for index, event in enumerate(library.events)
        if event == "register_debug"
    )
    assert library.events.index("mario_delete") < unregister_index
    assert unregister_index < library.events.index("global_terminate")
    health_calls = len(library.sm64_set_mario_health.calls)
    try:
        mario.set_mario_health(0x880)
        raise AssertionError("Retired Mario accepted a directing operation")
    except mario.MarioLifecycleError:
        pass
    assert len(library.sm64_set_mario_health.calls) == health_calls
    assert not mario.stop_tick_mario(_session=session, _cleanup_rejected=False)

    # Missing optional exports do not block a new core session. Native setter
    # failure poisons that generation, but teardown still owns each ID once.
    missing_library = FakeLibrary(mario_id=82)
    del missing_library.sm64_mario_heal
    del missing_library.sm64_surface_find_floor_height
    missing_session = insert_with(missing_library)
    try:
        mario.heal_mario(1)
        raise AssertionError("Missing directing export was accepted")
    except mario.MarioFeatureUnavailableError:
        pass
    try:
        mario.probe_collision((0.0, 0.0, 0.0))
        raise AssertionError("Missing collision-query export was accepted")
    except mario.MarioFeatureUnavailableError:
        pass
    assert missing_session.control_state == mario.LIVE_IDLE
    assert not mario.stop_tick_mario(
        _session=missing_session, _cleanup_rejected=False
    )

    failing_library = FakeLibrary(mario_id=83)
    failing_session = insert_with(failing_library)
    failing_library.sm64_mario_take_damage.failure = RuntimeError(
        "injected directing failure"
    )
    try:
        mario.damage_mario(1, 0, (10.0, 20.0, 30.0))
        raise AssertionError("Failed native directing call was accepted")
    except mario.MarioLifecycleError:
        pass
    assert failing_session.control_state == mario.POISONED
    failing_library.sm64_mario_take_damage.failure = None
    assert not mario.stop_tick_mario(
        _session=failing_session, _cleanup_rejected=False
    )
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

print("libsm64 directing/debugging regression passed")
