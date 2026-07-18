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
    "sm64_set_mario_faceangle",
):
    assert getattr(library, export_name) is not None
mario._configure_native_api(library)
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
for function_name in (
    "sm64_global_init",
    "sm64_global_terminate",
    "sm64_static_surfaces_load",
    "sm64_mario_tick",
    "sm64_mario_delete",
    "sm64_set_mario_faceangle",
):
    assert getattr(library, function_name).restype is None
after = mario.lifecycle_snapshot()
assert not after["global_init_attempted"]
assert not after["global_initialized"]
assert not mario._lifecycle.global_terminate_attempted

print("libsm64 real native ABI smoke passed on {}".format(platform.system()))
