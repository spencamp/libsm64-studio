"""Blender 5.2 validation for explicit libsm64 native lifecycle ownership."""

from pathlib import Path
import importlib
import os
import sys
import tempfile

import bpy


root = Path(__file__).resolve().parents[1]
installed_test = os.environ.get("LIBSM64_TEST_INSTALLED") == "1"
if installed_test:
    expected_root = Path(os.environ["LIBSM64_EXPECTED_INSTALL_ROOT"]).resolve()
    sys.path.insert(0, str(expected_root.parent))
else:
    sys.path.insert(0, str(root))
import libsm64_studio as addon
from libsm64_studio import mario

if installed_test:
    assert expected_root in Path(addon.__file__).resolve().parents
    assert addon.BUILD_ID == "2.7.0+surface-streaming-fd118132"


class NativeCall:
    def __init__(self, name, result=None, failure=None):
        self.name = name
        self.result = result
        self.failure = failure
        self.calls = []
        self.argtypes = None
        self.restype = object()

    def __call__(self, *args):
        self.calls.append(args)
        if self.failure is not None:
            raise self.failure
        return self.result


class FakeLibrary:
    def __init__(self, create_result=7, init_failure=None, delete_failure=None):
        self.sm64_global_init = NativeCall("global_init", failure=init_failure)
        self.sm64_global_terminate = NativeCall("global_terminate")
        self.sm64_static_surfaces_load = NativeCall("surfaces_load")
        self.sm64_mario_create = NativeCall("mario_create", result=create_result)
        self.sm64_mario_delete = NativeCall("mario_delete", failure=delete_failure)
        self.sm64_mario_tick = NativeCall("mario_tick")
        self.sm64_set_mario_faceangle = NativeCall("set_faceangle")
        self.sm64_surface_object_create = NativeCall("surface_create", result=100)
        self.sm64_surface_object_move = NativeCall("surface_move")
        self.sm64_surface_object_delete = NativeCall("surface_delete")

    def counts(self):
        return {
            "global_init": len(self.sm64_global_init.calls),
            "global_terminate": len(self.sm64_global_terminate.calls),
            "mario_create": len(self.sm64_mario_create.calls),
            "mario_delete": len(self.sm64_mario_delete.calls),
        }


libraries = []
input_counts = {"start": 0, "stop": 0}


def ensure_minimal_blender_data(_texture):
    mesh = bpy.data.meshes.get("libsm64_mario_mesh")
    if mesh is None:
        mesh = bpy.data.meshes.new("libsm64_mario_mesh")
        mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])


def install_harness(module):
    module._read_validated_rom = lambda _path: bytearray(b"mock-rom")
    module._load_native_library = lambda: libraries.pop(0)
    module.initialize_all_data = ensure_minimal_blender_data
    module._initialize_streamed_collision = lambda _session, _position: None
    module._stream_collision_for_position = lambda _session, _position: False
    module.start_input_reader = lambda: input_counts.__setitem__(
        "start", input_counts["start"] + 1
    )
    module.stop_input_reader = lambda: input_counts.__setitem__(
        "stop", input_counts["stop"] + 1
    )
    module._prepare_blender_for_insert = lambda: None
    module.update_mesh_data = lambda _mesh: None
    module.update_mesh_data_fast = lambda _mesh: None
    module.sample_input_reader = lambda _inputs: None


def insert_with(library, live_factory=None):
    libraries.append(library)
    original_factory = mario._create_live_object
    if live_factory is not None:
        mario._create_live_object = live_factory
    try:
        error = mario.insert_mario("mock.z64", 100, False)
    finally:
        mario._create_live_object = original_factory
    return error


def assert_idle():
    state = mario.lifecycle_snapshot()
    callback = mario._lifecycle.timer_callback
    assert not state["library_loaded"]
    assert not state["global_initialized"]
    assert not state["mario_created"]
    assert not state["tick_handler_installed"]
    assert not state["timer_installed"]
    assert callback is None or not bpy.app.timers.is_registered(callback)
    assert state["control_state"] == mario.STOPPED
    assert not mario.is_mario_running()
    assert mario._lifecycle_registry().get("active") is None


real_rom_validator = mario._read_validated_rom
install_harness(mario)

# Shutdown before initialization performs no native calls.
mario.stop_tick_mario()
assert_idle()

# Reproduce the crash condition: a DLL handle exists, but global init never
# succeeded. Termination must be skipped even though the handle is non-null.
partial = mario._new_lifecycle()
partial_library = FakeLibrary()
partial.library = partial_library
partial.library_loaded = True
mario._lifecycle = partial
mario.sm64 = partial_library
mario._publish_session(partial)
mario.stop_tick_mario()
assert partial_library.counts() == {
    "global_init": 0, "global_terminate": 0, "mario_create": 0, "mario_delete": 0,
}
assert_idle()

# Invalid path and wrong checksum never load or clean up native code.
original_validator = mario._read_validated_rom
del libraries[:]
mario._read_validated_rom = real_rom_validator
missing_error = mario.insert_mario("definitely-missing.z64", 100, False)
assert "valid" in missing_error.lower()
with tempfile.TemporaryDirectory() as directory:
    wrong_rom = Path(directory) / "wrong.z64"
    wrong_rom.write_bytes(b"not a supported rom")
    wrong_error = mario.insert_mario(str(wrong_rom), 100, False)
assert "supported" in wrong_error.lower()
assert not libraries
mario._read_validated_rom = original_validator

# The exact pinned header layout is validated before even resolving a library.
assert mario.ct.sizeof(mario.SM64Surface) == 44
assert mario.SM64Surface.vertices.offset == 8
assert mario.ct.sizeof(mario.SM64MarioInputs) == 20
assert mario.ct.sizeof(mario.SM64MarioState) == 60
for field_name, expected_offset in {
    "position": 0,
    "velocity": 12,
    "faceAngle": 24,
    "forwardVelocity": 28,
    "health": 32,
    "action": 36,
    "animID": 40,
    "animFrame": 44,
    "flags": 48,
    "particleFlags": 52,
    "invincTimer": 56,
}.items():
    assert getattr(mario.SM64MarioState, field_name).offset == expected_offset
mario._validate_ctypes_abi_layout()

class WrongMarioState(mario.ct.Structure):
    _fields_ = [("position", mario.ct.c_float * 3)]

original_state_type = mario.SM64MarioState
mario.SM64MarioState = WrongMarioState
abi_unused = FakeLibrary()
libraries.append(abi_unused)
abi_error = mario.insert_mario("mock.z64", 100, False)
assert "SM64MarioState" in abi_error
assert mario.PINNED_LIBSM64_COMMIT in abi_error
assert "reinstall" in abi_error.lower()
assert libraries.pop() is abi_unused
assert abi_unused.counts() == {
    "global_init": 0, "global_terminate": 0, "mario_create": 0, "mario_delete": 0,
}
mario.SM64MarioState = original_state_type
assert_idle()

# Every Phase-2 export, including surface objects, is required before init.
missing_export = FakeLibrary()
del missing_export.sm64_surface_object_move
missing_error = insert_with(missing_export)
assert "sm64_surface_object_move" in missing_error
assert missing_export.counts() == {
    "global_init": 0, "global_terminate": 0, "mario_create": 0, "mario_delete": 0,
}
assert_idle()

# Provenance/hash rejection occurs before LoadLibrary and before any fake call.
real_native_loader = mario._load_native_library
native_manifest = mario._read_native_build_manifest()
with tempfile.TemporaryDirectory() as directory:
    Path(directory, "sm64.dll").write_bytes(b"stale native artifact")
    mario._load_native_library = lambda: mario._verify_native_artifact(
        native_manifest, directory, "Windows"
    )
    hash_unused = FakeLibrary()
    libraries.append(hash_unused)
    hash_error = mario.insert_mario("mock.z64", 100, False)
    assert "SHA-256" in hash_error
    assert mario.PINNED_LIBSM64_COMMIT in hash_error
    assert libraries.pop() is hash_unused
    assert hash_unused.counts() == {
        "global_init": 0,
        "global_terminate": 0,
        "mario_create": 0,
        "mario_delete": 0,
    }
mario._load_native_library = real_native_loader
assert_idle()

# Insert twice: the first session is deleted/terminated exactly once before
# the second generation initializes.
first = FakeLibrary()
second = FakeLibrary(create_result=11)
libraries.extend((first, second))
assert mario.insert_mario("mock.z64", 100, False) is None
assert first.sm64_global_init.restype is None
assert first.sm64_global_init.argtypes == [
    mario.ct.POINTER(mario.ct.c_uint8), mario.ct.POINTER(mario.ct.c_uint8),
]
assert first.sm64_global_terminate.argtypes == []
assert first.sm64_global_terminate.restype is None
assert first.sm64_static_surfaces_load.argtypes == [
    mario.ct.POINTER(mario.SM64Surface), mario.ct.c_uint32,
]
assert first.sm64_mario_create.argtypes == [
    mario.ct.c_float, mario.ct.c_float, mario.ct.c_float,
]
assert first.sm64_mario_create.restype is mario.ct.c_int32
assert first.sm64_mario_delete.argtypes == [mario.ct.c_int32]
assert first.sm64_mario_delete.restype is None
assert first.sm64_mario_tick.argtypes[0] is mario.ct.c_int32
assert first.sm64_mario_tick.restype is None
assert first.sm64_set_mario_faceangle.argtypes == [mario.ct.c_int32, mario.ct.c_float]
assert first.sm64_set_mario_faceangle.restype is None
assert first.sm64_surface_object_create.argtypes == [mario.ct.POINTER(mario.SM64SurfaceObject)]
assert first.sm64_surface_object_create.restype is mario.ct.c_uint32
assert first.sm64_surface_object_move.argtypes == [
    mario.ct.c_uint32, mario.ct.POINTER(mario.SM64ObjectTransform),
]
assert first.sm64_surface_object_move.restype is None
assert first.sm64_surface_object_delete.argtypes == [mario.ct.c_uint32]
assert first.sm64_surface_object_delete.restype is None
assert len(first.sm64_global_init.calls[0]) == 2
assert first.sm64_mario_create.calls[0] == (0.0, 0.0, 0.0)
assert all(isinstance(value, float) for value in first.sm64_mario_create.calls[0])
assert mario.insert_mario("mock.z64", 100, False) is None
assert first.counts() == {
    "global_init": 1, "global_terminate": 1, "mario_create": 1, "mario_delete": 1,
}
assert second.counts() == {
    "global_init": 1, "global_terminate": 0, "mario_create": 1, "mario_delete": 0,
}
assert mario.lifecycle_snapshot()["control_state"] == mario.LIVE_IDLE
assert bpy.app.timers.is_registered(mario._lifecycle.timer_callback)
assert not [
    handler for handler in bpy.app.handlers.frame_change_pre
    if getattr(handler, "_libsm64_owner_token", None) == mario._lifecycle.owner_token
]
mario.resume_mario_for_recording()
assert bpy.app.timers.is_registered(mario._lifecycle.timer_callback)
mario.stop_tick_mario()
mario.stop_tick_mario()
assert second.counts() == {
    "global_init": 1, "global_terminate": 1, "mario_create": 1, "mario_delete": 1,
}
assert_idle()

# A new insert remains possible after stopping.
after_stop = FakeLibrary()
assert insert_with(after_stop) is None
mario.stop_tick_mario()
assert after_stop.counts() == {
    "global_init": 1, "global_terminate": 1, "mario_create": 1, "mario_delete": 1,
}

# A Blender-side preparation failure occurs before loading the DLL and still
# releases the published generation cleanly.
original_prepare = mario._prepare_blender_for_insert
mario._prepare_blender_for_insert = lambda: (_ for _ in ()).throw(
    RuntimeError("injected Blender preparation failure")
)
prepare_unused = FakeLibrary()
libraries.append(prepare_unused)
assert "injected Blender preparation failure" in mario.insert_mario("mock.z64", 100, False)
assert libraries.pop() is prepare_unused
assert prepare_unused.counts() == {
    "global_init": 0, "global_terminate": 0, "mario_create": 0, "mario_delete": 0,
}
mario._prepare_blender_for_insert = original_prepare
assert_idle()

# Failed global init: no Mario creation and no global termination.
init_failure = FakeLibrary(init_failure=RuntimeError("injected init failure"))
assert "injected init failure" in insert_with(init_failure)
assert init_failure.counts() == {
    "global_init": 1, "global_terminate": 0, "mario_create": 0, "mario_delete": 0,
}
assert_idle()

# Failed initial streamed preparation never creates Mario, but it does release
# the successfully initialized global generation after the empty static load.
original_collision_initializer = mario._initialize_streamed_collision
mario._initialize_streamed_collision = lambda _session, _position: (_ for _ in ()).throw(
    RuntimeError("injected collision extraction failure")
)
collision_failure = FakeLibrary()
assert "injected collision extraction failure" in insert_with(collision_failure)
assert len(collision_failure.sm64_static_surfaces_load.calls) == 1
assert collision_failure.sm64_static_surfaces_load.calls[0][1] == 0
assert collision_failure.counts() == {
    "global_init": 1, "global_terminate": 1, "mario_create": 0, "mario_delete": 0,
}
mario._initialize_streamed_collision = original_collision_initializer
assert_idle()

# Failed Mario creation unwinds the successfully initialized global exactly once.
create_failure = FakeLibrary(create_result=-1)
assert "ground" in insert_with(create_failure).lower()
assert create_failure.counts() == {
    "global_init": 1, "global_terminate": 1, "mario_create": 1, "mario_delete": 0,
}
assert_idle()

# Blender object creation failure deletes Mario, then terminates global state.
object_failure = FakeLibrary()
def fail_live_object():
    raise RuntimeError("injected Blender object failure")
assert "injected Blender object failure" in insert_with(object_failure, fail_live_object)
assert object_failure.counts() == {
    "global_init": 1, "global_terminate": 1, "mario_create": 1, "mario_delete": 1,
}
assert_idle()

# Manual Live Mario deletion routes through the owned session callback once.
manual_delete = FakeLibrary()
assert insert_with(manual_delete) is None
bpy.data.objects.remove(mario.get_live_mario_object(), do_unlink=True)
mario._lifecycle.timer_callback()
assert manual_delete.counts() == {
    "global_init": 1, "global_terminate": 1, "mario_create": 1, "mario_delete": 1,
}
assert_idle()

# File-load and exit hooks tear down only the registered owner.
load_library = FakeLibrary()
assert insert_with(load_library) is None
addon._shutdown_native_session_on_load_pre(None)
assert load_library.counts()["global_terminate"] == 1
assert_idle()
exit_library = FakeLibrary()
assert insert_with(exit_library) is None
addon._shutdown_owned_session_at_exit()
assert exit_library.counts()["global_terminate"] == 1
assert_idle()

# Script reload creates a new owner token. The registry retains the exact old
# shutdown callback, allowing the new generation to retire it safely.
reload_old = FakeLibrary()
assert insert_with(reload_old) is None
mario = importlib.reload(mario)
install_harness(mario)
reload_new = FakeLibrary()
assert insert_with(reload_new) is None
assert reload_old.counts() == {
    "global_init": 1, "global_terminate": 1, "mario_create": 1, "mario_delete": 1,
}
mario.stop_tick_mario()
assert reload_new.counts()["global_terminate"] == 1
assert_idle()

# Disable/re-enable is idempotent and leaves no owned callbacks.
addon.register()
disable_library = FakeLibrary()
assert insert_with(disable_library) is None
addon.unregister()
assert disable_library.counts() == {
    "global_init": 1, "global_terminate": 1, "mario_create": 1, "mario_delete": 1,
}
addon.register()
addon.unregister()
assert_idle()

# A failed native delete poisons the generation: global termination and every
# later insert are refused until Blender restarts.
delete_failure = FakeLibrary(delete_failure=RuntimeError("injected delete failure"))
assert insert_with(delete_failure) is None
shutdown_errors = mario.stop_tick_mario()
assert "Mario delete failed" in shutdown_errors[0]
assert delete_failure.counts() == {
    "global_init": 1, "global_terminate": 0, "mario_create": 1, "mario_delete": 1,
}
refused_library = FakeLibrary()
libraries.append(refused_library)
assert "restart Blender" in mario.insert_mario("mock.z64", 100, False)
assert libraries.pop() is refused_library
assert refused_library.counts() == {
    "global_init": 0, "global_terminate": 0, "mario_create": 0, "mario_delete": 0,
}

print("NATIVE LIFECYCLE COUNTS")
for label, library in (
    ("partial", partial_library),
    ("insert-first", first),
    ("insert-second", second),
    ("after-stop", after_stop),
    ("init-failure", init_failure),
    ("create-failure", create_failure),
    ("object-failure", object_failure),
    ("manual-delete", manual_delete),
    ("file-load", load_library),
    ("exit", exit_library),
    ("reload-old", reload_old),
    ("reload-new", reload_new),
    ("disable", disable_library),
    ("delete-failure", delete_failure),
    ("refused-after-poison", refused_library),
):
    print(label, library.counts())
print("libsm64 Blender 5.2 native lifecycle regression passed")
