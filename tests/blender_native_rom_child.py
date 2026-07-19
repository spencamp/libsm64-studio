"""Crash-isolated ROM-backed exercise of the packaged Windows native runtime."""

from pathlib import Path
import json
import math
import os
import platform
import sys
import traceback


RESULT_PREFIX = "LIBSM64_NATIVE_RESULT"


def _result(value):
    print("{} {}".format(RESULT_PREFIX, value), flush=True)


def _load_packaged_mario():
    install_root = Path(os.environ["LIBSM64_EXPECTED_INSTALL_ROOT"]).resolve()
    sys.path.insert(0, str(install_root.parent))
    from libsm64_studio import mario

    if Path(mario.__file__).resolve().parent != install_root:
        raise AssertionError(
            "Loaded libsm64_studio from {}, expected {}".format(
                mario.__file__, install_root
            )
        )
    return mario


def _make_floor(mario, x_offset=0):
    surfaces = (mario.SM64Surface * 2)()
    vertices = (
        ((-2000, 0, -2000), (2000, 0, 2000), (2000, 0, -2000)),
        ((-2000, 0, -2000), (-2000, 0, 2000), (2000, 0, 2000)),
    )
    for surface, triangle in zip(surfaces, vertices):
        surface.type = 0
        surface.force = 0
        surface.terrain = 0
        for vertex_index, vertex in enumerate(triangle):
            for axis_index, coordinate in enumerate(vertex):
                surface.vertices[vertex_index][axis_index] = (
                    coordinate + x_offset if axis_index == 0 else coordinate
                )
    return surfaces


def _create_surface_object(mario, library, surfaces, translation):
    transform = mario.SM64ObjectTransform()
    transform.position[:] = translation
    transform.eulerRotation[:] = (0.0, 0.0, 0.0)
    descriptor = mario.SM64SurfaceObject()
    descriptor.transform = transform
    descriptor.surfaceCount = len(surfaces)
    descriptor.surfaces = mario.ct.cast(
        surfaces, mario.ct.POINTER(mario.SM64Surface)
    )
    object_id = int(library.sm64_surface_object_create(mario.ct.byref(descriptor)))
    return object_id


def _tick(mario, library, mario_id, inputs, state, geometry):
    library.sm64_mario_tick(
        mario_id,
        mario.ct.byref(inputs),
        mario.ct.byref(state),
        mario.ct.byref(geometry),
    )


def _start_mark_fidelity(expected, state):
    actual = {
        "position": tuple(float(value) for value in state.position),
        "velocity": tuple(float(value) for value in state.velocity),
        "face_angle": float(state.faceAngle),
        "forward_velocity": float(state.forwardVelocity),
        "health": int(state.health),
        "action": int(state.action),
        "anim_id": int(state.animID),
        "anim_frame": int(state.animFrame),
        "flags": int(state.flags),
        "invincibility_timer": int(state.invincTimer),
    }
    result = {}
    for name, target in expected.items():
        observed = actual[name]
        if isinstance(target, tuple):
            exact = observed == target
            approximate = all(
                math.isclose(left, right, rel_tol=1e-6, abs_tol=1e-4)
                for left, right in zip(observed, target)
            )
        elif isinstance(target, float):
            exact = observed == target
            approximate = math.isclose(
                observed, target, rel_tol=1e-6, abs_tol=1e-4
            )
        else:
            exact = observed == target
            approximate = exact
        result[name] = "exact" if exact else (
            "approximate" if approximate else "changed_after_neutral_tick"
        )
    return result


def main():
    if platform.system() != "Windows":
        raise RuntimeError("The ROM-backed DLL child currently targets Windows")

    mario = _load_packaged_mario()
    rom_path = os.environ.get("LIBSM64_TEST_ROM", "")
    if not rom_path:
        raise RuntimeError("LIBSM64_TEST_ROM must name an unmodified SM64 US ROM")

    # These checks happen before LoadLibrary and therefore before any native call.
    mario._validate_ctypes_abi_layout()
    lib_directory = Path(mario.__file__).resolve().parent / "lib"
    manifest = mario._read_native_build_manifest(str(lib_directory))
    mario._validate_manifest_abi_probe(manifest)
    artifact_path = Path(
        mario._verify_native_artifact(manifest, str(lib_directory), "Windows")
    )
    if artifact_path.name != "sm64.dll":
        raise AssertionError("Windows manifest selected {}".format(artifact_path.name))

    rom_bytes = mario._read_validated_rom(rom_path)
    rom_array = (mario.ct.c_uint8 * len(rom_bytes)).from_buffer(rom_bytes)
    texture_size = 4 * mario.SM64_TEXTURE_WIDTH * mario.SM64_TEXTURE_HEIGHT
    texture = (mario.ct.c_uint8 * texture_size)()

    library = mario._load_native_library()
    mario._configure_native_api(library)
    mario._configure_start_mark_api(library)
    mario._configure_environment_level_api(library, mario.ENVIRONMENT_WATER)
    mario._configure_environment_level_api(library, mario.ENVIRONMENT_GAS)
    mario._configure_cap_api(library)
    mario._configure_audio_api(library)
    mario._configure_directing_api(library)
    mario._configure_collision_query_api(library)
    mario._configure_debug_print_api(library)

    initialized = False
    mario_id = -1
    moving_platform_id = -1
    owned_surface_ids = []
    debug_messages = []
    debug_callback = None
    debug_registered = False
    try:
        mario._native_stage("before_global_init")
        library.sm64_global_init(rom_array, texture)
        mario._native_stage("after_global_init")
        initialized = True

        def receive_debug(message):
            debug_messages.append((message or b"").decode("utf-8", errors="replace"))

        debug_callback = mario.SM64DebugPrintFunctionPtr(receive_debug)
        mario._native_stage("before_debug_callback_register")
        library.sm64_register_debug_print_function(debug_callback)
        mario._native_stage("after_debug_callback_register")
        debug_registered = True
        # The pinned implementation rejects an invalid Mario ID without
        # touching ownership and routes the diagnostic through this callback.
        library.sm64_mario_delete(9999)
        if not debug_messages:
            raise AssertionError("Native debug callback did not receive a message")

        empty_surfaces = (mario.SM64Surface * 0)()
        mario._native_stage("before_static_surface_load")
        library.sm64_static_surfaces_load(empty_surfaces, 0)
        mario._native_stage("after_static_surface_load")

        first_floor = _make_floor(mario)
        mario._native_stage("before_surface_object_create_initial_1")
        owned_surface_ids.append(
            _create_surface_object(mario, library, first_floor, (0.0, 0.0, 0.0))
        )
        mario._native_stage("after_surface_object_create_initial_1")
        second_floor = _make_floor(mario)
        mario._native_stage("before_surface_object_create_initial_2")
        owned_surface_ids.append(
            _create_surface_object(mario, library, second_floor, (4000.0, 0.0, 0.0))
        )
        mario._native_stage("after_surface_object_create_initial_2")

        moving_platform = _make_floor(mario)
        mario._native_stage("before_moving_platform_create")
        moving_platform_id = _create_surface_object(
            mario, library, moving_platform, (0.0, 100.0, 0.0)
        )
        mario._native_stage("after_moving_platform_create")

        mario._native_stage("before_mario_create")
        mario_id = int(library.sm64_mario_create(0.0, 1000.0, 0.0))
        mario._native_stage("after_mario_create")
        if mario_id < 0:
            raise RuntimeError("sm64_mario_create returned {}".format(mario_id))

        # Native audio has no terminate function, so initialize exactly once
        # after global state and exercise one complete two-block PCM tick.  The
        # parent process contains any native fault before Blender's main test
        # process can be affected.
        mario._native_stage("before_audio_init")
        library.sm64_audio_init(rom_array)
        mario._native_stage("after_audio_init")
        audio_capacity = 544 * 2 * 2
        audio_buffer = (mario.ct.c_int16 * (audio_capacity + 16))()
        for index in range(audio_capacity, len(audio_buffer)):
            audio_buffer[index] = 12345
        mario._native_stage("before_audio_tick")
        audio_block_samples = int(library.sm64_audio_tick(0, 1100, audio_buffer))
        mario._native_stage("after_audio_tick")
        if audio_block_samples not in (528, 544):
            raise AssertionError(
                "Unexpected native audio block size {}".format(audio_block_samples)
            )
        audio_value_count = audio_block_samples * 2 * 2
        if audio_value_count > audio_capacity:
            raise AssertionError("Native audio tick exceeded the PCM capacity")
        if any(
            int(audio_buffer[index]) != 12345
            for index in range(audio_capacity, len(audio_buffer))
        ):
            raise AssertionError("Native audio tick wrote beyond the PCM buffer")

        inputs = mario.SM64MarioInputs()
        state = mario.SM64MarioState()
        geometry = mario.SM64MarioGeometryBuffers()
        mario._native_stage("before_mario_tick")
        _tick(mario, library, mario_id, inputs, state, geometry)
        mario._native_stage("after_mario_tick")
        if geometry.numTrianglesUsed > mario.SM64_GEO_MAX_TRIANGLES:
            raise RuntimeError(
                "Native tick returned {} triangles, maximum is {}".format(
                    geometry.numTrianglesUsed, mario.SM64_GEO_MAX_TRIANGLES
                )
            )

        # Better Start Mark: recreate Mario, apply every supported setter in
        # the production order, tick neutrally, and report post-tick fidelity.
        supported_mark = {
            "position": tuple(float(value) for value in state.position),
            "velocity": (0.0, 0.0, 0.0),
            "face_angle": float(state.faceAngle),
            "forward_velocity": 0.0,
            "health": int(state.health),
            "action": int(state.action),
            "anim_id": int(state.animID),
            "anim_frame": int(state.animFrame),
            "flags": int(state.flags),
            "invincibility_timer": 120,
        }
        mario._native_stage("before_start_mark_mario_delete")
        library.sm64_mario_delete(mario_id)
        mario._native_stage("after_start_mark_mario_delete")
        mario_id = -1
        mario._native_stage("before_start_mark_mario_create")
        mario_id = int(library.sm64_mario_create(*supported_mark["position"]))
        mario._native_stage("after_start_mark_mario_create")
        if mario_id < 0:
            raise RuntimeError("Start Mark replacement create failed")
        library.sm64_set_mario_action(mario_id, supported_mark["action"])
        library.sm64_set_mario_animation(mario_id, supported_mark["anim_id"])
        library.sm64_set_mario_anim_frame(mario_id, supported_mark["anim_frame"])
        library.sm64_set_mario_state(mario_id, supported_mark["flags"])
        library.sm64_set_mario_position(mario_id, *supported_mark["position"])
        library.sm64_set_mario_faceangle(mario_id, supported_mark["face_angle"])
        library.sm64_set_mario_velocity(mario_id, *supported_mark["velocity"])
        library.sm64_set_mario_forward_velocity(
            mario_id, supported_mark["forward_velocity"]
        )
        library.sm64_set_mario_health(mario_id, supported_mark["health"])
        library.sm64_set_mario_invincibility(
            mario_id, supported_mark["invincibility_timer"]
        )
        mario._native_stage("before_start_mark_neutral_tick")
        _tick(mario, library, mario_id, inputs, state, geometry)
        mario._native_stage("after_start_mark_neutral_tick")
        for value in tuple(state.position) + tuple(state.velocity):
            if not math.isfinite(float(value)):
                raise RuntimeError("Start Mark restoration returned non-finite state")
        if int(state.health) != supported_mark["health"]:
            raise AssertionError("Start Mark health was not restored")
        fidelity = _start_mark_fidelity(supported_mark, state)
        print(
            "LIBSM64_START_MARK_FIDELITY {}".format(
                json.dumps(fidelity, sort_keys=True, separators=(",", ":"))
            ),
            flush=True,
        )

        # Settle onto the dedicated platform, move it before Mario tick, and
        # verify that the same Mario remains finite and is carried upward.
        for _index in range(90):
            _tick(mario, library, mario_id, inputs, state, geometry)
        settled_y = float(state.position[1])
        elevator_tick_id = mario_id
        for step in range(1, 21):
            moved_transform = mario.SM64ObjectTransform()
            moved_transform.position[:] = (0.0, 100.0 + step * 10.0, 0.0)
            moved_transform.eulerRotation[:] = (0.0, 0.0, 0.0)
            mario._native_stage(
                "before_moving_platform_elevator_move_{}".format(step)
            )
            library.sm64_surface_object_move(
                moving_platform_id, mario.ct.byref(moved_transform)
            )
            mario._native_stage(
                "after_moving_platform_elevator_move_{}".format(step)
            )
            _tick(mario, library, elevator_tick_id, inputs, state, geometry)
        if elevator_tick_id != mario_id:
            raise AssertionError("Mario ID changed during moving-platform update")
        if not math.isfinite(float(state.position[1])):
            raise AssertionError("Elevator tick returned non-finite Mario state")
        if float(state.position[1]) <= settled_y + 100.0:
            raise AssertionError(
                "Moving platform did not carry Mario upward: {} -> {}".format(
                    settled_y, float(state.position[1])
                )
            )
        jump_start_y = float(state.position[1])
        inputs.buttonA = 1
        _tick(mario, library, mario_id, inputs, state, geometry)
        inputs.buttonA = 0
        for _index in range(3):
            _tick(mario, library, mario_id, inputs, state, geometry)
        if float(state.position[1]) <= jump_start_y:
            raise AssertionError("Mario did not jump from the moving platform")
        rotating_transform = mario.SM64ObjectTransform()
        rotating_transform.position[:] = (100.0, 300.0, 0.0)
        rotating_transform.eulerRotation[:] = (0.0, 20.0, 0.0)
        mario._native_stage("before_moving_platform_rotating_move")
        library.sm64_surface_object_move(
            moving_platform_id, mario.ct.byref(rotating_transform)
        )
        mario._native_stage("after_moving_platform_rotating_move")
        _tick(mario, library, mario_id, inputs, state, geometry)
        if not all(math.isfinite(float(value)) for value in state.position):
            raise AssertionError("Rotating platform returned non-finite Mario state")

        incoming_floor = _make_floor(mario)
        mario._native_stage("before_surface_object_create_incoming")
        owned_surface_ids.append(
            _create_surface_object(mario, library, incoming_floor, (8000.0, 0.0, 0.0))
        )
        mario._native_stage("after_surface_object_create_incoming")

        distant_id = owned_surface_ids.pop(0)
        mario._native_stage("before_surface_object_delete_distant")
        library.sm64_surface_object_delete(distant_id)
        mario._native_stage("after_surface_object_delete_distant")

        second_tick_id = mario_id
        mario._native_stage("before_mario_tick_after_stream")
        _tick(mario, library, second_tick_id, inputs, state, geometry)
        mario._native_stage("after_mario_tick_after_stream")
        if second_tick_id != mario_id:
            raise AssertionError("Mario ID changed across surface-object streaming")

        # Exercise both exact signed-int environment setters against the same
        # Mario, then restore the pinned decomp's canonical disabled value.
        current_y = int(state.position[1])
        mario._native_stage("before_environment_water_above_gas_below")
        library.sm64_set_mario_water_level(mario_id, current_y + 500)
        library.sm64_set_mario_gas_level(mario_id, current_y - 500)
        mario._native_stage("after_environment_water_above_gas_below")
        for _index in range(3):
            _tick(mario, library, mario_id, inputs, state, geometry)
        if not all(math.isfinite(float(value)) for value in state.position):
            raise AssertionError("Water level tick returned non-finite Mario state")

        mario._native_stage("before_environment_water_below_gas_above")
        library.sm64_set_mario_water_level(mario_id, -10000)
        library.sm64_set_mario_gas_level(mario_id, int(state.position[1]) + 500)
        mario._native_stage("after_environment_water_below_gas_above")
        _tick(mario, library, mario_id, inputs, state, geometry)
        if not all(math.isfinite(float(value)) for value in state.position):
            raise AssertionError("Gas level tick returned non-finite Mario state")

        mario._native_stage("before_environment_disable")
        library.sm64_set_mario_water_level(mario_id, -10000)
        library.sm64_set_mario_gas_level(mario_id, -10000)
        mario._native_stage("after_environment_disable")

        # Grant each exact pinned special-cap flag with audio disabled. The
        # same Mario remains finite, exposes the expected state bit, and emits
        # usable geometry for live/baked mesh capture.
        for name, cap_flag, duration in (
            ("wing", mario.MARIO_WING_CAP, 0),
            ("metal", mario.MARIO_METAL_CAP, 90),
            ("vanish", mario.MARIO_VANISH_CAP, 120),
        ):
            library.sm64_set_mario_state(mario_id, 0)
            mario._native_stage("before_cap_{}_grant".format(name))
            library.sm64_mario_interact_cap(mario_id, cap_flag, duration, 0)
            mario._native_stage("after_cap_{}_grant".format(name))
            _tick(mario, library, mario_id, inputs, state, geometry)
            if not int(state.flags) & cap_flag:
                raise AssertionError("{} Cap flag was not present after tick".format(name))
            active_values = int(geometry.numTrianglesUsed) * 9
            if active_values <= 0 or not all(
                math.isfinite(float(geometry.position[index]))
                for index in range(active_values)
            ):
                raise AssertionError("{} Cap returned invalid geometry".format(name))
        mario._native_stage("before_cap_extend")
        library.sm64_mario_extend_cap(mario_id, 45)
        mario._native_stage("after_cap_extend")

        # Directing and value-returning collision diagnostics operate on the
        # same live Mario without recreation or pointer exposure.
        query_position = tuple(float(value) for value in state.position)
        floor_height = float(
            library.sm64_surface_find_floor_height(*query_position)
        )
        water_level = float(
            library.sm64_surface_find_water_level(
                query_position[0], query_position[2]
            )
        )
        gas_level = float(
            library.sm64_surface_find_poison_gas_level(
                query_position[0], query_position[2]
            )
        )
        if not all(math.isfinite(value) for value in (
            floor_height, water_level, gas_level
        )):
            raise AssertionError("Collision query returned a non-finite height")
        if math.isclose(floor_height, -110000.0, rel_tol=0.0, abs_tol=1.0e-4):
            raise AssertionError("Collision query unexpectedly found no floor")
        if water_level != -10000.0 or gas_level != -10000.0:
            raise AssertionError(
                "Disabled environment query mismatch: water {} gas {}".format(
                    water_level, gas_level
                )
            )

        directing_mario_id = mario_id
        library.sm64_set_mario_health(mario_id, 0x880)
        library.sm64_mario_heal(mario_id, 1)
        library.sm64_mario_take_damage(
            mario_id, 1, 0,
            query_position[0], query_position[1], query_position[2],
        )
        library.sm64_set_mario_invincibility(mario_id, 30)
        _tick(mario, library, mario_id, inputs, state, geometry)
        if directing_mario_id != mario_id:
            raise AssertionError("Directing operation changed Mario ID")
        if not all(math.isfinite(float(value)) for value in state.position):
            raise AssertionError("Directing tick returned non-finite Mario state")
        if int(state.invincTimer) <= 0:
            raise AssertionError("Invincibility timer did not survive a native tick")
        library.sm64_mario_kill(mario_id)
        _tick(mario, library, mario_id, inputs, state, geometry)
        if not all(math.isfinite(float(value)) for value in state.position):
            raise AssertionError("Kill tick returned non-finite Mario state")

        for index, object_id in enumerate(reversed(owned_surface_ids), start=1):
            mario._native_stage("before_surface_object_delete_remaining_{}".format(index))
            library.sm64_surface_object_delete(object_id)
            mario._native_stage("after_surface_object_delete_remaining_{}".format(index))
        owned_surface_ids.clear()

        mario._native_stage("before_moving_platform_delete")
        library.sm64_surface_object_delete(moving_platform_id)
        mario._native_stage("after_moving_platform_delete")
        moving_platform_id = -1

        mario._native_stage("before_mario_delete")
        library.sm64_mario_delete(mario_id)
        mario._native_stage("after_mario_delete")
        mario_id = -1

        mario._native_stage("before_debug_callback_unregister")
        library.sm64_register_debug_print_function(
            mario.SM64DebugPrintFunctionPtr()
        )
        mario._native_stage("after_debug_callback_unregister")
        debug_registered = False
        debug_callback = None

        mario._native_stage("before_global_terminate")
        library.sm64_global_terminate()
        mario._native_stage("after_global_terminate")
        initialized = False
    finally:
        # Never terminate a global context whose initializer did not return.
        for index, object_id in enumerate(reversed(owned_surface_ids), start=1):
            mario._native_stage("before_surface_object_delete_cleanup_{}".format(index))
            library.sm64_surface_object_delete(object_id)
            mario._native_stage("after_surface_object_delete_cleanup_{}".format(index))
        if moving_platform_id >= 0:
            mario._native_stage("before_moving_platform_delete_cleanup")
            library.sm64_surface_object_delete(moving_platform_id)
            mario._native_stage("after_moving_platform_delete_cleanup")
        if mario_id >= 0:
            mario._native_stage("before_mario_delete_cleanup")
            library.sm64_mario_delete(mario_id)
            mario._native_stage("after_mario_delete_cleanup")
        if debug_registered:
            mario._native_stage("before_debug_callback_unregister_cleanup")
            library.sm64_register_debug_print_function(
                mario.SM64DebugPrintFunctionPtr()
            )
            mario._native_stage("after_debug_callback_unregister_cleanup")
            debug_registered = False
            debug_callback = None
        if initialized:
            mario._native_stage("before_global_terminate_cleanup")
            library.sm64_global_terminate()
            mario._native_stage("after_global_terminate_cleanup")

    _result("PASS")


try:
    main()
except BaseException:
    traceback.print_exc()
    _result("PYTHON_FAILURE")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1)
