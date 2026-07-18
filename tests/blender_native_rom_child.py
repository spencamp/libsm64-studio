"""Crash-isolated ROM-backed exercise of the packaged Windows native runtime."""

from pathlib import Path
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


def _make_floor(mario):
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
                surface.vertices[vertex_index][axis_index] = coordinate
    return surfaces


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

    initialized = False
    mario_id = -1
    try:
        mario._native_stage("before_global_init")
        library.sm64_global_init(rom_array, texture)
        mario._native_stage("after_global_init")
        initialized = True

        surfaces = _make_floor(mario)
        mario._native_stage("before_static_surface_load")
        library.sm64_static_surfaces_load(surfaces, len(surfaces))
        mario._native_stage("after_static_surface_load")

        mario._native_stage("before_mario_create")
        mario_id = int(library.sm64_mario_create(0.0, 1000.0, 0.0))
        mario._native_stage("after_mario_create")
        if mario_id < 0:
            raise RuntimeError("sm64_mario_create returned {}".format(mario_id))

        inputs = mario.SM64MarioInputs()
        state = mario.SM64MarioState()
        geometry = mario.SM64MarioGeometryBuffers()
        mario._native_stage("before_mario_tick")
        library.sm64_mario_tick(
            mario_id,
            mario.ct.byref(inputs),
            mario.ct.byref(state),
            mario.ct.byref(geometry),
        )
        mario._native_stage("after_mario_tick")
        if geometry.numTrianglesUsed > mario.SM64_GEO_MAX_TRIANGLES:
            raise RuntimeError(
                "Native tick returned {} triangles, maximum is {}".format(
                    geometry.numTrianglesUsed, mario.SM64_GEO_MAX_TRIANGLES
                )
            )

        mario._native_stage("before_mario_delete")
        library.sm64_mario_delete(mario_id)
        mario._native_stage("after_mario_delete")
        mario_id = -1

        mario._native_stage("before_global_terminate")
        library.sm64_global_terminate()
        mario._native_stage("after_global_terminate")
        initialized = False
    finally:
        # Never terminate a global context whose initializer did not return.
        if mario_id >= 0:
            mario._native_stage("before_mario_delete_cleanup")
            library.sm64_mario_delete(mario_id)
            mario._native_stage("after_mario_delete_cleanup")
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
