"""Fake-native regression for transactional surface-object streaming."""

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

from libsm64_studio import mario
from libsm64_studio.collision_cache import PreparedChunk, SurfaceRecord


class Call:
    def __init__(self, library, name, result=None):
        self.library = library
        self.name = name
        self.result = result
        self.calls = []
        self.argtypes = None
        self.restype = object()

    def __call__(self, *args):
        self.calls.append(args)
        self.library.events.append(self.name)
        return self.result


class SurfaceCreateCall(Call):
    def __call__(self, descriptor_pointer):
        self.calls.append((descriptor_pointer,))
        self.library.events.append("surface_create")
        self.library.create_attempts += 1
        if self.library.fail_create_at == self.library.create_attempts:
            raise RuntimeError("injected surface create failure")
        descriptor = mario.ct.cast(
            descriptor_pointer, mario.ct.POINTER(mario.SM64SurfaceObject)
        ).contents
        copied = []
        for index in range(descriptor.surfaceCount):
            surface = descriptor.surfaces[index]
            copied.append({
                "metadata": (surface.type, surface.force, surface.terrain),
                "vertices": tuple(tuple(surface.vertices[v]) for v in range(3)),
            })
        transform = descriptor.transform
        self.library.created_payloads.append({
            "position": tuple(transform.position),
            "rotation": tuple(transform.eulerRotation),
            "surfaces": tuple(copied),
        })
        if self.library.reusable_ids:
            return self.library.reusable_ids.pop(0)
        result = self.library.next_id
        self.library.next_id += 1
        return result


class SurfaceDeleteCall(Call):
    def __call__(self, object_id):
        self.calls.append((object_id,))
        self.library.events.append("surface_delete")
        self.library.delete_attempts += 1
        if self.library.fail_delete_at == self.library.delete_attempts:
            raise RuntimeError("injected surface delete failure")
        self.library.deleted_ids.append(int(object_id))
        self.library.reusable_ids.append(int(object_id))


class FakeLibrary:
    def __init__(self, mario_result=7, fail_create_at=None, fail_delete_at=None):
        self.events = []
        self.next_id = 100
        self.reusable_ids = []
        self.created_payloads = []
        self.deleted_ids = []
        self.create_attempts = 0
        self.delete_attempts = 0
        self.fail_create_at = fail_create_at
        self.fail_delete_at = fail_delete_at
        self.sm64_global_init = Call(self, "global_init")
        self.sm64_global_terminate = Call(self, "global_terminate")
        self.sm64_static_surfaces_load = Call(self, "static_load")
        self.sm64_mario_create = Call(self, "mario_create", mario_result)
        self.sm64_mario_tick = Call(self, "mario_tick")
        self.sm64_mario_delete = Call(self, "mario_delete")
        self.sm64_set_mario_faceangle = Call(self, "faceangle")
        self.sm64_surface_object_create = SurfaceCreateCall(self, "surface_create")
        self.sm64_surface_object_move = Call(self, "surface_move")
        self.sm64_surface_object_delete = SurfaceDeleteCall(self, "surface_delete")


def prepared(keys):
    result = {}
    size = mario.CHUNK_SIZE_BLENDER
    for key in keys:
        x = key[0] * size
        y = key[1] * size
        triangle = ((x + 1.0, y + 1.0, 0.0),
                    (x + 40.0, y + 40.0, 0.0),
                    (x + 40.0, y + 1.0, 0.0))
        result[key] = PreparedChunk(
            key, "chunk-{}-{}".format(*key),
            (SurfaceRecord(5, -2, 3, triangle),),
        )
    return result


def ensure_data(_texture):
    mesh = bpy.data.meshes.get("libsm64_mario_mesh")
    if mesh is None:
        mesh = bpy.data.meshes.new("libsm64_mario_mesh")
        mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])


libraries = []
real = {
    "read_rom": mario._read_validated_rom,
    "load_library": mario._load_native_library,
    "initialize": mario.initialize_all_data,
    "prepare": mario._prepare_collision_chunks,
    "start_input": mario.start_input_reader,
    "stop_input": mario.stop_input_reader,
    "prepare_blender": mario._prepare_blender_for_insert,
}
mario._read_validated_rom = lambda _path: bytearray(b"fake-rom")
mario._load_native_library = lambda: libraries.pop(0)
mario.initialize_all_data = ensure_data
mario._prepare_collision_chunks = lambda _session, keys: prepared(keys)
mario.start_input_reader = lambda: None
mario.stop_input_reader = lambda: None
mario._prepare_blender_for_insert = lambda: None


def insert(library):
    libraries.append(library)
    error = mario.insert_mario("private-test-rom", 50.0, False)
    return error, mario._lifecycle


def emulate_restart_after_uncertain_fake_state():
    """Forget fake-only state as a real process restart would."""
    mario._lifecycle_registry()["active"] = None
    replacement = mario._new_lifecycle()
    replacement.shutdown_complete = True
    mario._lifecycle = replacement
    mario.sm64 = None
    mario.sm64_mario_id = -1


try:
    # Initial collision is dynamic, precedes the one Mario create, and the
    # legacy static list is explicitly empty.
    library = FakeLibrary()
    error, session = insert(library)
    assert error is None, error
    assert len(library.sm64_surface_object_create.calls) == 9
    assert library.events.index("surface_create") < library.events.index("mario_create")
    assert len(library.sm64_mario_create.calls) == 1
    assert len(library.sm64_static_surfaces_load.calls) == 1
    assert int(library.sm64_static_surfaces_load.calls[0][1]) == 0
    assert not library.sm64_surface_object_move.calls
    first_payload = library.created_payloads[0]
    assert first_payload["rotation"] == (0.0, 0.0, 0.0)
    assert first_payload["surfaces"][0]["metadata"] == (5, -2, 3)

    # Cross a boundary while keeping all Mario/recording/Start Mark identity.
    mario_id = session.mario_id
    session.control_state = mario.RECORDING
    session.recording_tick_origin = 123
    mario.recorder.start(1.0, 30.0)
    assert mario.recorder.capture_mesh(
        session.live_object.data, 123, (0.0, 0.0, 0.0), 0.0
    )
    session.persistent_start_mark = {"owner_token": session.owner_token,
                                     "generation": session.generation,
                                     "native_position": (1.0, 2.0, 3.0)}
    mark = session.persistent_start_mark
    event_start = len(library.events)
    assert mario._stream_collision_for_position(session, (3.1 * mario.CHUNK_SIZE_BLENDER, 0.0, 0.0))
    transition_events = library.events[event_start:]
    assert "surface_create" in transition_events and "surface_delete" in transition_events
    assert transition_events.index("surface_create") < transition_events.index("surface_delete")
    assert session.mario_id == mario_id
    assert session.control_state == mario.RECORDING
    assert session.recording_tick_origin == 123
    assert session.persistent_start_mark is mark
    assert mario.recorder.active
    assert mario.recorder.capture_mesh(
        session.live_object.data, 124,
        (3.1 * mario.CHUNK_SIZE_BLENDER, 0.0, 0.0), 0.25,
    )
    assert mario.recorder.sample_count == 2
    assert len(library.sm64_mario_create.calls) == 1
    assert not library.sm64_surface_object_move.calls
    assert len(session.active_chunk_keys) <= 25

    # Same center is stable; return recreates missing chunks and safely reuses
    # numeric IDs that were removed from ownership after successful deletion.
    creates_before = library.create_attempts
    deletes_before = library.delete_attempts
    assert not mario._stream_collision_for_position(session, (3.2 * mario.CHUNK_SIZE_BLENDER, 0.0, 0.0))
    assert (library.create_attempts, library.delete_attempts) == (creates_before, deletes_before)
    assert mario._stream_collision_for_position(session, (0.1, 0.1, 0.0))
    assert len({record.object_id for record in session.native_surface_objects.values()}) == len(
        session.native_surface_objects
    )
    assert session.mario_id == mario_id

    # Repeated long travel stays bounded and never recreates Mario.
    for chunk_x in range(1, 18):
        mario._stream_collision_for_position(
            session, ((chunk_x + 0.1) * mario.CHUNK_SIZE_BLENDER, 0.0, 0.0)
        )
        assert len(session.active_chunk_keys) <= 25
        assert session.mario_id == mario_id
    assert len(library.sm64_mario_create.calls) == 1

    owned_before_shutdown = sorted(
        record.object_id for record in session.native_surface_objects.values()
    )
    errors = mario.stop_tick_mario(_session=session, _cleanup_rejected=False)
    assert not errors, errors
    assert sorted(library.deleted_ids[-len(owned_before_shutdown):]) == owned_before_shutdown
    delete_count = library.delete_attempts
    mario.stop_tick_mario(_session=session, _cleanup_rejected=False)
    assert library.delete_attempts == delete_count
    first_surface_delete = library.events.index("surface_delete")
    assert first_surface_delete < library.events.index("mario_delete")
    assert library.events.index("mario_delete") < library.events.index("global_terminate")

    # A partial incoming failure rolls back only new IDs and leaves the old
    # active keys authoritative. A rollback delete failure makes ownership uncertain.
    partial = FakeLibrary(fail_create_at=11)
    error, partial_session = insert(partial)
    assert error is None, error
    old_keys = set(partial_session.active_chunk_keys)
    try:
        mario._stream_collision_for_position(
            partial_session, (3.1 * mario.CHUNK_SIZE_BLENDER, 0.0, 0.0)
        )
        raise AssertionError("partial create failure was not raised")
    except mario.MarioLifecycleError:
        pass
    assert partial_session.active_chunk_keys == old_keys
    assert partial_session.control_state == mario.POISONED
    assert partial.deleted_ids
    mario.stop_tick_mario(_session=partial_session, _cleanup_rejected=False)

    rollback = FakeLibrary(fail_create_at=11, fail_delete_at=1)
    error, rollback_session = insert(rollback)
    assert error is None, error
    try:
        mario._stream_collision_for_position(
            rollback_session, (3.1 * mario.CHUNK_SIZE_BLENDER, 0.0, 0.0)
        )
        raise AssertionError("rollback failure was not raised")
    except mario.MarioLifecycleError:
        pass
    assert rollback_session.native_ownership_uncertain
    assert rollback_session.control_state == mario.POISONED
    native_events = len(rollback.events)
    mario.stop_tick_mario(_session=rollback_session, _cleanup_rejected=False)
    assert len(rollback.events) == native_events
    emulate_restart_after_uncertain_fake_state()

    # Outgoing deletion failure retains the ownership record and blocks Mario
    # deletion/global termination because the native generation is uncertain.
    outgoing = FakeLibrary(fail_delete_at=1)
    error, outgoing_session = insert(outgoing)
    assert error is None, error
    try:
        mario._stream_collision_for_position(
            outgoing_session, (3.1 * mario.CHUNK_SIZE_BLENDER, 0.0, 0.0)
        )
        raise AssertionError("outgoing delete failure was not raised")
    except mario.MarioLifecycleError:
        pass
    assert outgoing_session.native_ownership_uncertain
    mario.stop_tick_mario(_session=outgoing_session, _cleanup_rejected=False)
    assert not outgoing.sm64_mario_delete.calls
    assert not outgoing.sm64_global_terminate.calls
    emulate_restart_after_uncertain_fake_state()

    # Failed initial chunk creation never creates Mario. Failed Mario creation
    # deletes every initial object before safe global termination.
    initial = FakeLibrary(fail_create_at=2)
    error, _initial_session = insert(initial)
    assert error and "create" in error.lower()
    assert not initial.sm64_mario_create.calls
    assert initial.deleted_ids == [100]

    no_mario = FakeLibrary(mario_result=-1)
    error, _no_mario_session = insert(no_mario)
    assert error and "ground" in error.lower()
    assert len(no_mario.deleted_ids) == 9
    assert len(no_mario.sm64_global_terminate.calls) == 1

    print("libsm64 surface-object streaming regression passed")
finally:
    try:
        mario.stop_tick_mario(_cleanup_rejected=False)
    except Exception:
        pass
    mario._read_validated_rom = real["read_rom"]
    mario._load_native_library = real["load_library"]
    mario.initialize_all_data = real["initialize"]
    mario._prepare_collision_chunks = real["prepare"]
    mario.start_input_reader = real["start_input"]
    mario.stop_input_reader = real["stop_input"]
    mario._prepare_blender_for_insert = real["prepare_blender"]
