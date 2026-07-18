"""Native call-order coverage for automatic collision neighborhood switches."""

from pathlib import Path
import os
import sys

import bpy


root = Path(__file__).resolve().parents[1]
installed_test = os.environ.get("LIBSM64_TEST_INSTALLED") == "1"
if installed_test:
    expected_root = Path(os.environ["LIBSM64_EXPECTED_INSTALL_ROOT"]).resolve()
    sys.path.insert(0, str(expected_root.parent))
else:
    sys.path.insert(0, str(root))

from libsm64_studio import mario
from libsm64_studio.collision_cache import (
    CollisionPreparation, chunk_center, chunk_coordinate, chunk_size_for_scale,
    collision_status_message,
)
from libsm64_studio.recording import recorder


class NativeCall:
    def __init__(self, name, events, result=None):
        self.name = name
        self.events = events
        self.result = result
        self.calls = []
        self.argtypes = None
        self.restype = object()

    def __call__(self, *args):
        self.calls.append(args)
        self.events.append(self.name)
        return self.result


class FakeLibrary:
    def __init__(self):
        self.events = []
        self.sm64_global_init = NativeCall("global_init", self.events)
        self.sm64_global_terminate = NativeCall("global_terminate", self.events)
        self.sm64_static_surfaces_load = NativeCall("surfaces_load", self.events)
        self.sm64_mario_delete = NativeCall("mario_delete", self.events)
        self.sm64_mario_tick = NativeCall("mario_tick", self.events)
        self.sm64_set_mario_faceangle = NativeCall("faceangle", self.events)
        self.sm64_mario_create = NativeCall("mario_create", self.events, result=10)


library = FakeLibrary()


def ensure_data(_texture):
    mesh = bpy.data.meshes.get("libsm64_mario_mesh")
    if mesh is None:
        mesh = bpy.data.meshes.new("libsm64_mario_mesh")
        mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])


def fake_prepare(world_position, scene=None):
    size = chunk_size_for_scale(mario.SM64_SCALE_FACTOR)
    center = chunk_coordinate(world_position, size)
    surfaces = (mario.SM64Surface * 1)()
    surfaces[0].v0x = -10
    surfaces[0].v1x = 10
    surfaces[0].v2z = 10
    stats = {
        "native_surface_count": 1, "duration_seconds": 0.001,
        "object_cache_hits": 0, "object_cache_misses": 0,
        "chunk_cache_hits": 26, "chunk_cache_misses": 1,
    }
    preparation = CollisionPreparation(center, chunk_center(center, size), surfaces, 1, stats)
    mario._last_collision_preparation = preparation
    return preparation


real_prepare = mario._prepare_collision
real_validator = mario._read_validated_rom
real_loader = mario._load_native_library
real_initialize = mario.initialize_all_data
real_start_input = mario.start_input_reader
real_stop_input = mario.stop_input_reader
real_update = mario.update_mesh_data
real_update_fast = mario.update_mesh_data_fast
real_sample = mario.sample_input_reader

mario._prepare_collision = fake_prepare
mario._read_validated_rom = lambda _path: bytearray(b"mock-rom")
mario._load_native_library = lambda: library
mario.initialize_all_data = ensure_data
mario.start_input_reader = lambda: None
mario.stop_input_reader = lambda: None
mario.update_mesh_data = lambda _mesh: None
mario.update_mesh_data_fast = lambda _mesh: None
mario.sample_input_reader = lambda _inputs: None
bpy.context.scene.cursor.location = (1, 1, 1)

try:
    assert mario.insert_mario("mock.z64", 50, False) is None
    generation = mario._lifecycle.generation
    assert len(library.sm64_static_surfaces_load.calls) == 1
    assert len(library.sm64_mario_create.calls) == 1
    assert mario._lifecycle.collision_center == (0, 0, 0)

    # Move the public state across the active center chunk. The switch happens
    # before this callback's native tick and uses delete/load/create ordering.
    mario.mario_state.posX = 11000
    mario.mario_state.posY = 0
    mario.mario_state.posZ = 0
    before = len(library.events)
    mario.tick_mario(bpy.context.scene, _session=mario._lifecycle)
    transition_events = library.events[before:]
    assert transition_events[:4] == ["mario_delete", "surfaces_load", "mario_create", "faceangle"]
    assert transition_events[-1] == "mario_tick"
    assert mario._lifecycle.generation == generation
    assert mario._lifecycle.collision_center == (1, 0, 0)
    assert mario.is_mario_running()

    # Recording blocks an otherwise-required switch and performs no native
    # delete, load, create, or tick, preserving the take without discontinuity.
    mario.begin_mario_recording(bpy.context.scene)
    mario.tick_mario(bpy.context.scene, _session=mario._lifecycle)
    assert recorder.sample_count == 1
    mario.mario_state.posX = 11000
    blocked_counts = (
        len(library.sm64_mario_delete.calls),
        len(library.sm64_static_surfaces_load.calls),
        len(library.sm64_mario_create.calls),
        len(library.sm64_mario_tick.calls),
    )
    mario.tick_mario(bpy.context.scene, _session=mario._lifecycle)
    assert mario._lifecycle.collision_boundary_blocked
    assert "Stop recording" in collision_status_message()
    assert blocked_counts == (
        len(library.sm64_mario_delete.calls),
        len(library.sm64_static_surfaces_load.calls),
        len(library.sm64_mario_create.calls),
        len(library.sm64_mario_tick.calls),
    )
    samples = mario.freeze_mario_recording_for_bake()
    assert len(samples) == 1
    recorder.complete("transition test take")
    mario.abandon_bake_transition()
    assert mario._lifecycle.collision_center == (2, 0, 0)

    mario.stop_tick_mario()
    mario.stop_tick_mario()
    assert len(library.sm64_mario_delete.calls) == 3  # two switches + final Mario
    assert len(library.sm64_global_terminate.calls) == 1
    callback = mario._lifecycle.timer_callback
    assert callback is None or not bpy.app.timers.is_registered(callback)
finally:
    recorder.cancel("transition test cleanup")
    mario.stop_tick_mario()
    mario._prepare_collision = real_prepare
    mario._read_validated_rom = real_validator
    mario._load_native_library = real_loader
    mario.initialize_all_data = real_initialize
    mario.start_input_reader = real_start_input
    mario.stop_input_reader = real_stop_input
    mario.update_mesh_data = real_update
    mario.update_mesh_data_fast = real_update_fast
    mario.sample_input_reader = real_sample

print("libsm64 collision transition lifecycle test passed")
