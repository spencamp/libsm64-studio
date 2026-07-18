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
    assert addon.BUILD_ID == "2.5.0+chunked-collision"


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
    module.get_surface_array_from_scene = lambda: ((module.SM64Surface * 0)(), 0)
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

# Insert twice: the first session is deleted/terminated exactly once before
# the second generation initializes.
first = FakeLibrary()
second = FakeLibrary(create_result=11)
libraries.extend((first, second))
assert mario.insert_mario("mock.z64", 100, False) is None
assert first.sm64_global_init.restype is None
assert len(first.sm64_global_init.argtypes) == 3
assert first.sm64_global_terminate.argtypes == []
assert first.sm64_global_terminate.restype is None
assert first.sm64_mario_create.restype is mario.ct.c_int32
assert first.sm64_mario_delete.restype is None
assert first.sm64_mario_tick.restype is None
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
