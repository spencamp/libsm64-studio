"""Load and configure the real bundled library without a ROM or global init."""

from pathlib import Path
import hashlib
import os
import platform
import sys


root = Path(__file__).resolve().parents[1]
installed_test = os.environ.get("LIBSM64_TEST_INSTALLED") == "1"
if installed_test:
    expected_root = Path(os.environ["LIBSM64_EXPECTED_INSTALL_ROOT"]).resolve()
    sys.path.insert(0, str(expected_root.parent))
else:
    sys.path.insert(0, str(root))

from libsm64_studio import mario


package_root = Path(mario.__file__).resolve().parent
if installed_test:
    assert package_root == expected_root
lib_directory = package_root / "lib"
manifest_path = lib_directory / "libsm64-build.json"
assert manifest_path.is_file()
manifest = mario._read_native_build_manifest(str(lib_directory))
assert manifest["repository"] == "libsm64/libsm64"
assert manifest["commit"] == "fd11813208272b4271d92bd92feb8f3fdbe61be5"
assert manifest["header"] == "src/libsm64.h"

for system_name, artifact_name in (
    ("Windows", "sm64.dll"),
    ("Linux", "libsm64.so"),
):
    artifact_path = Path(
        mario._verify_native_artifact(manifest, str(lib_directory), system_name)
    )
    assert artifact_path.name == artifact_name
    assert hashlib.sha256(artifact_path.read_bytes()).hexdigest() == (
        manifest["artifacts"][artifact_name]["sha256"]
    )

mario._validate_ctypes_abi_layout()
mario._validate_manifest_abi_probe(manifest)
assert mario.ct.sizeof(mario.SM64Surface) == 44
assert mario.SM64Surface.vertices.offset == 8
assert mario.ct.sizeof(mario.SM64MarioInputs) == 20
assert mario.ct.sizeof(mario.SM64MarioState) == 60
assert mario.ct.sizeof(mario.SM64MarioGeometryBuffers) == 40
assert mario.ct.sizeof(mario.SM64ObjectTransform) == 24
assert mario.ct.sizeof(mario.SM64SurfaceObject) == 40
assert mario.SM64SurfaceObject.transform.offset == 0
assert mario.SM64SurfaceObject.surfaceCount.offset == 24
assert mario.SM64SurfaceObject.surfaces.offset == 32

before = mario.lifecycle_snapshot()
assert not before["global_init_attempted"]
assert not before["global_initialized"]
library = mario._load_native_library()
for export_name in (
    "sm64_global_init",
    "sm64_global_terminate",
    "sm64_static_surfaces_load",
    "sm64_mario_create",
    "sm64_mario_tick",
    "sm64_mario_delete",
    "sm64_set_mario_action",
    "sm64_set_mario_animation",
    "sm64_set_mario_anim_frame",
    "sm64_set_mario_state",
    "sm64_set_mario_position",
    "sm64_set_mario_faceangle",
    "sm64_set_mario_velocity",
    "sm64_set_mario_forward_velocity",
    "sm64_set_mario_health",
    "sm64_set_mario_invincibility",
    "sm64_set_mario_water_level",
    "sm64_set_mario_gas_level",
    "sm64_mario_interact_cap",
    "sm64_mario_extend_cap",
    "sm64_mario_take_damage",
    "sm64_mario_heal",
    "sm64_mario_kill",
    "sm64_surface_find_floor_height",
    "sm64_surface_find_water_level",
    "sm64_surface_find_poison_gas_level",
    "sm64_register_debug_print_function",
    "sm64_audio_init",
    "sm64_audio_tick",
    "sm64_set_sound_volume",
    "sm64_register_play_sound_function",
    "sm64_surface_object_create",
    "sm64_surface_object_move",
    "sm64_surface_object_delete",
):
    assert getattr(library, export_name) is not None
mario._configure_native_api(library)
mario._configure_start_mark_api(library)
mario._configure_environment_level_api(library, mario.ENVIRONMENT_WATER)
mario._configure_environment_level_api(library, mario.ENVIRONMENT_GAS)
mario._configure_cap_api(library)
mario._configure_audio_api(library)
mario._configure_directing_api(library)
mario._configure_collision_query_api(library)
mario._configure_debug_print_api(library)
assert library.sm64_global_init.argtypes == [
    mario.ct.POINTER(mario.ct.c_uint8),
    mario.ct.POINTER(mario.ct.c_uint8),
]
assert library.sm64_global_terminate.argtypes == []
assert library.sm64_static_surfaces_load.argtypes == [
    mario.ct.POINTER(mario.SM64Surface), mario.ct.c_uint32,
]
assert library.sm64_mario_create.argtypes == [
    mario.ct.c_float, mario.ct.c_float, mario.ct.c_float,
]
assert library.sm64_mario_create.restype is mario.ct.c_int32
assert library.sm64_mario_tick.argtypes[0] is mario.ct.c_int32
assert library.sm64_mario_delete.argtypes == [mario.ct.c_int32]
assert library.sm64_set_mario_faceangle.argtypes == [
    mario.ct.c_int32, mario.ct.c_float,
]
assert library.sm64_set_mario_action.argtypes == [
    mario.ct.c_int32, mario.ct.c_uint32,
]
assert library.sm64_set_mario_animation.argtypes == [
    mario.ct.c_int32, mario.ct.c_int32,
]
assert library.sm64_set_mario_anim_frame.argtypes == [
    mario.ct.c_int32, mario.ct.c_int16,
]
assert library.sm64_set_mario_state.argtypes == [
    mario.ct.c_int32, mario.ct.c_uint32,
]
assert library.sm64_set_mario_position.argtypes == [
    mario.ct.c_int32, mario.ct.c_float, mario.ct.c_float, mario.ct.c_float,
]
assert library.sm64_set_mario_velocity.argtypes == [
    mario.ct.c_int32, mario.ct.c_float, mario.ct.c_float, mario.ct.c_float,
]
assert library.sm64_set_mario_forward_velocity.argtypes == [
    mario.ct.c_int32, mario.ct.c_float,
]
assert library.sm64_set_mario_health.argtypes == [
    mario.ct.c_int32, mario.ct.c_uint16,
]
assert library.sm64_set_mario_invincibility.argtypes == [
    mario.ct.c_int32, mario.ct.c_int16,
]
assert library.sm64_set_mario_water_level.argtypes == [
    mario.ct.c_int32, mario.ct.c_int,
]
assert library.sm64_set_mario_gas_level.argtypes == [
    mario.ct.c_int32, mario.ct.c_int,
]
assert library.sm64_mario_interact_cap.argtypes == [
    mario.ct.c_int32, mario.ct.c_uint32, mario.ct.c_uint16, mario.ct.c_uint8,
]
assert library.sm64_mario_extend_cap.argtypes == [
    mario.ct.c_int32, mario.ct.c_uint16,
]
assert library.sm64_mario_take_damage.argtypes == [
    mario.ct.c_int32, mario.ct.c_uint32, mario.ct.c_uint32,
    mario.ct.c_float, mario.ct.c_float, mario.ct.c_float,
]
assert library.sm64_mario_heal.argtypes == [
    mario.ct.c_int32, mario.ct.c_uint8,
]
assert library.sm64_mario_kill.argtypes == [mario.ct.c_int32]
assert library.sm64_surface_find_floor_height.argtypes == [
    mario.ct.c_float, mario.ct.c_float, mario.ct.c_float,
]
assert library.sm64_surface_find_floor_height.restype is mario.ct.c_float
assert library.sm64_surface_find_water_level.argtypes == [
    mario.ct.c_float, mario.ct.c_float,
]
assert library.sm64_surface_find_water_level.restype is mario.ct.c_float
assert library.sm64_surface_find_poison_gas_level.argtypes == [
    mario.ct.c_float, mario.ct.c_float,
]
assert library.sm64_surface_find_poison_gas_level.restype is mario.ct.c_float
assert library.sm64_register_debug_print_function.argtypes == [
    mario.SM64DebugPrintFunctionPtr,
]
assert library.sm64_audio_init.argtypes == [
    mario.ct.POINTER(mario.ct.c_uint8),
]
assert library.sm64_audio_tick.argtypes == [
    mario.ct.c_uint32, mario.ct.c_uint32, mario.ct.POINTER(mario.ct.c_int16),
]
assert library.sm64_audio_tick.restype is mario.ct.c_uint32
assert library.sm64_set_sound_volume.argtypes == [mario.ct.c_float]
assert library.sm64_register_play_sound_function.argtypes == [
    mario.SM64PlaySoundFunctionPtr,
]
assert library.sm64_surface_object_create.argtypes == [
    mario.ct.POINTER(mario.SM64SurfaceObject),
]
assert library.sm64_surface_object_create.restype is mario.ct.c_uint32
assert library.sm64_surface_object_move.argtypes == [
    mario.ct.c_uint32, mario.ct.POINTER(mario.SM64ObjectTransform),
]
assert library.sm64_surface_object_delete.argtypes == [mario.ct.c_uint32]
for function_name in (
    "sm64_global_init",
    "sm64_global_terminate",
    "sm64_static_surfaces_load",
    "sm64_mario_tick",
    "sm64_mario_delete",
    "sm64_set_mario_faceangle",
    "sm64_set_mario_action",
    "sm64_set_mario_animation",
    "sm64_set_mario_anim_frame",
    "sm64_set_mario_state",
    "sm64_set_mario_position",
    "sm64_set_mario_velocity",
    "sm64_set_mario_forward_velocity",
    "sm64_set_mario_health",
    "sm64_set_mario_invincibility",
    "sm64_set_mario_water_level",
    "sm64_set_mario_gas_level",
    "sm64_mario_interact_cap",
    "sm64_mario_extend_cap",
    "sm64_mario_take_damage",
    "sm64_mario_heal",
    "sm64_mario_kill",
    "sm64_register_debug_print_function",
    "sm64_audio_init",
    "sm64_set_sound_volume",
    "sm64_register_play_sound_function",
    "sm64_surface_object_move",
    "sm64_surface_object_delete",
):
    assert getattr(library, function_name).restype is None
after = mario.lifecycle_snapshot()
assert not after["global_init_attempted"]
assert not after["global_initialized"]
assert not mario._lifecycle.global_terminate_attempted

print("libsm64 real native ABI smoke passed on {}".format(platform.system()))
