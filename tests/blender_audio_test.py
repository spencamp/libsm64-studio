"""Blender 5.2 fake-native regression for optional live audio lifecycle."""

from pathlib import Path
import os
import sys
import time

import bpy


root = Path(__file__).resolve().parents[1]
if os.environ.get("LIBSM64_TEST_INSTALLED") == "1":
    install_root = Path(os.environ["LIBSM64_EXPECTED_INSTALL_ROOT"]).resolve()
    sys.path.insert(0, str(install_root.parent))
else:
    sys.path.insert(0, str(root))

import libsm64_studio as addon
from libsm64_studio import mario
from libsm64_studio.audio_runtime import LiveAudioRuntime


class NativeCall:
    def __init__(self, library, name, result=None):
        self.library = library
        self.name = name
        self.result = result
        self.calls = []
        self.argtypes = None
        self.restype = object()

    def __call__(self, *arguments):
        self.calls.append(arguments)
        self.library.events.append(self.name)
        return self.result


class AudioTickCall(NativeCall):
    def __call__(self, queued, desired, buffer):
        super().__call__(queued, desired, buffer)
        for index in range(544 * 2 * 2):
            buffer[index] = 1000 if index % 2 else -1000
        return 544


class RegisterSoundCall(NativeCall):
    def __call__(self, callback):
        super().__call__(callback)
        self.library.sound_callback = callback if bool(callback) else None


class FakeLibrary:
    def __init__(self, mario_id=71):
        self.events = []
        self.sound_callback = None
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
        self.sm64_audio_init = NativeCall(self, "audio_init")
        self.sm64_audio_tick = AudioTickCall(self, "audio_tick")
        self.sm64_set_sound_volume = NativeCall(self, "sound_volume")
        self.sm64_register_play_sound_function = RegisterSoundCall(
            self, "register_sound"
        )


backend_events = []


class FakeBackend:
    instances = []

    def __init__(self, volume, muted):
        self.volume = volume
        self.muted = muted
        self.queued = 0
        self.appended = []
        self.stopped = False
        type(self).instances.append(self)
        backend_events.append("backend_open")

    def queued_samples(self):
        return self.queued

    def append_int16_stereo(self, values, frame_count):
        self.appended.append((tuple(values), frame_count))
        self.queued = 6000

    def retire_consumed(self):
        return None

    def set_output(self, volume, muted):
        self.volume = volume
        self.muted = muted

    def stop(self):
        self.stopped = True
        backend_events.append("backend_stop")


def ensure_minimal_blender_data(_texture):
    mesh = bpy.data.meshes.get("libsm64_mario_mesh")
    if mesh is None:
        mesh = bpy.data.meshes.new("libsm64_mario_mesh")
        mesh.from_pydata([(0, 0, 0), (0, 1, 0), (0, 0, 1)], [], [(0, 1, 2)])


libraries = []
real = {
    "read_rom": mario._read_validated_rom,
    "load_library": mario._load_native_library,
    "initialize": mario.initialize_all_data,
    "prepare_chunks": mario._initialize_streamed_collision,
    "start_input": mario.start_input_reader,
    "stop_input": mario.stop_input_reader,
    "prepare_blender": mario._prepare_blender_for_insert,
    "runtime_factory": mario.LiveAudioRuntime,
}

mario._read_validated_rom = lambda _path: bytearray(b"ephemeral-rom-bytes")
mario._load_native_library = lambda: libraries.pop(0)
mario.initialize_all_data = ensure_minimal_blender_data
mario._initialize_streamed_collision = lambda _session, _position: None
mario.start_input_reader = lambda: None
mario.stop_input_reader = lambda: None
mario._prepare_blender_for_insert = lambda: None
mario.LiveAudioRuntime = lambda: LiveAudioRuntime(backend_factory=FakeBackend)


addon.register()
try:
    scene = bpy.context.scene
    scene.libsm64.enable_live_audio = True
    scene.libsm64.audio_volume = 0.75
    scene.libsm64.audio_mute = False
    library = FakeLibrary()
    libraries.append(library)
    assert mario.insert_mario("mock.z64", 1.0, False) is None
    session = mario._lifecycle
    mario.remove_tick_mario_timer(session)

    deadline = time.monotonic() + 1.0
    while not FakeBackend.instances[0].appended and time.monotonic() < deadline:
        time.sleep(0.005)
    state = mario.audio_diagnostics()
    assert state["audio_requested"]
    assert state["audio_init_attempted"]
    assert state["audio_initialized"]
    assert state["audio_worker_started"]
    assert state["audio_device_opened"]
    assert state["generated_frames"] == 1088
    assert len(library.sm64_audio_init.calls) == 1
    assert library.sm64_audio_init.argtypes == [mario.ct.POINTER(mario.ct.c_uint8)]
    assert library.sm64_audio_tick.argtypes == [
        mario.ct.c_uint32,
        mario.ct.c_uint32,
        mario.ct.POINTER(mario.ct.c_int16),
    ]
    assert library.sm64_audio_tick.restype is mario.ct.c_uint32
    assert library.sm64_set_sound_volume.argtypes == [mario.ct.c_float]
    assert library.sm64_register_play_sound_function.argtypes == [
        mario.SM64PlaySoundFunctionPtr
    ]

    # Callback reference remains live for the registered native lifetime and
    # copies only bounded plain-Python event data.
    assert session.play_sound_callback is not None
    position = (mario.ct.c_float * 3)(1.0, 2.0, 3.0)
    library.sound_callback(0x12345678, position)
    assert len(session.sound_events) == 1
    assert session.sound_events[-1][2:4] == (0x12345678, (1.0, 2.0, 3.0))

    scene.libsm64.audio_volume = 0.25
    scene.libsm64.audio_mute = True
    assert FakeBackend.instances[0].volume == 0.25
    assert FakeBackend.instances[0].muted is True

    # Disabling joins the worker and unregisters the callback without touching
    # Mario or collision ownership; re-enabling reuses native audio init.
    mario_id = session.mario_id
    scene.libsm64.enable_live_audio = False
    assert session.mario_id == mario_id
    assert FakeBackend.instances[0].stopped
    assert library.sound_callback is None
    scene.libsm64.audio_mute = False
    scene.libsm64.enable_live_audio = True
    deadline = time.monotonic() + 1.0
    while len(FakeBackend.instances) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(library.sm64_audio_init.calls) == 1

    errors = mario.stop_tick_mario(_session=session, _cleanup_rejected=False)
    assert not errors
    assert "backend_stop" in backend_events
    assert library.events.index("mario_delete") < library.events.index(
        "global_terminate"
    )
    # Callback nulling occurs after worker stop (recorded separately) and
    # before Mario/global native teardown.
    register_indices = [
        index for index, event in enumerate(library.events)
        if event == "register_sound"
    ]
    assert register_indices[-1] < library.events.index("mario_delete")
    assert session.rom_image is None

    # Disabled audio never configures or initializes optional exports.
    scene.libsm64.enable_live_audio = False
    disabled_library = FakeLibrary(mario_id=72)
    libraries.append(disabled_library)
    assert mario.insert_mario("mock.z64", 1.0, False) is None
    disabled_session = mario._lifecycle
    mario.remove_tick_mario_timer(disabled_session)
    assert not disabled_library.sm64_audio_init.calls
    assert mario.is_mario_running()
    assert not mario.stop_tick_mario(
        _session=disabled_session, _cleanup_rejected=False
    )
finally:
    try:
        mario.stop_tick_mario(_cleanup_rejected=False)
    except Exception:
        pass
    addon.unregister()
    mario._read_validated_rom = real["read_rom"]
    mario._load_native_library = real["load_library"]
    mario.initialize_all_data = real["initialize"]
    mario._initialize_streamed_collision = real["prepare_chunks"]
    mario.start_input_reader = real["start_input"]
    mario.stop_input_reader = real["stop_input"]
    mario._prepare_blender_for_insert = real["prepare_blender"]
    mario.LiveAudioRuntime = real["runtime_factory"]

print("libsm64 Blender 5.2 live-audio regression passed")
