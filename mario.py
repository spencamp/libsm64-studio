import bpy
from array import array
from dataclasses import dataclass
import hashlib
import json
import os
import platform
import ctypes as ct
import math
import mathutils
import uuid
from typing import cast, List
from . collision_types import COLLISION_TYPES
from .collision_cache import (
    CHUNK_SIZE_BLENDER,
    CollisionCache,
    chunk_coordinate,
    native_chunk_payload,
    plan_transition,
)
from . recording import recorder

if platform.system() == 'Windows':
    from . input_reader_win import (
        clear_input_reader_state, sample_input_reader, start_input_reader, stop_input_reader,
    )
else:
    from . input_reader import (
        clear_input_reader_state, sample_input_reader, start_input_reader, stop_input_reader,
    )

SM64_TEXTURE_WIDTH = 64 * 11
SM64_TEXTURE_HEIGHT = 64
SM64_GEO_MAX_TRIANGLES = 1024
SM64_SCALE_FACTOR = 50
LIVE_ROLE = "libsm64_live_role"
LIVE_ROLE_VALUE = "LIVE_MARIO"
EXPECTED_ROM_SHA1 = "9bef1128717f958171a4afac3ed78ee2bb4e86ce"
LIFECYCLE_REGISTRY_KEY = "libsm64_studio_native_lifecycle"
SIMULATION_FPS = 30.0
SIMULATION_INTERVAL = 1.0 / SIMULATION_FPS
PINNED_LIBSM64_COMMIT = "fd11813208272b4271d92bd92feb8f3fdbe61be5"
NATIVE_STAGE_PREFIX = "LIBSM64_NATIVE_STAGE"

STOPPED = "STOPPED"
LIVE_IDLE = "LIVE_IDLE"
RECORDING = "RECORDING"
BAKING = "BAKING"
RESETTING = "RESETTING"
POISONED = "POISONED"

SURFACE_PREPARED = "prepared"
SURFACE_CREATE_ATTEMPTED = "create attempted"
SURFACE_CREATED = "created"
SURFACE_DELETE_ATTEMPTED = "delete attempted"
SURFACE_DELETED = "deleted"
SURFACE_FAILED = "failed/uncertain"

origin_offset = [0.0, 0.0, 0.0]
original_fps = 0
original_fps_setting = 0
original_fps_base = 1.0
original_cursor_pos = [0.0, 0.0, 0.0]
simulation_scene = None
RUNTIME_API_VERSION = 6

SM64SurfaceVertex = ct.c_int32 * 3
SM64SurfaceVertices = SM64SurfaceVertex * 3

class SM64Surface(ct.Structure):
    _fields_ = [
        ('type', ct.c_int16),
        ('force', ct.c_int16),
        ('terrain', ct.c_uint16),
        ('vertices', SM64SurfaceVertices),
    ]

class SM64MarioInputs(ct.Structure):
    _fields_ = [
        ('camLookX', ct.c_float), ('camLookZ', ct.c_float),
        ('stickX', ct.c_float), ('stickY', ct.c_float),
        ('buttonA', ct.c_uint8), ('buttonB', ct.c_uint8), ('buttonZ', ct.c_uint8),
    ]

Float3 = ct.c_float * 3

class SM64ObjectTransform(ct.Structure):
    _fields_ = [
        ('position', Float3),
        ('eulerRotation', Float3),
    ]

class SM64SurfaceObject(ct.Structure):
    _fields_ = [
        ('transform', SM64ObjectTransform),
        ('surfaceCount', ct.c_uint32),
        ('surfaces', ct.POINTER(SM64Surface)),
    ]

class SM64MarioState(ct.Structure):
    _fields_ = [
        ('position', Float3),
        ('velocity', Float3),
        ('faceAngle', ct.c_float),
        ('forwardVelocity', ct.c_float),
        ('health', ct.c_int16),
        ('action', ct.c_uint32),
        ('animID', ct.c_int32),
        ('animFrame', ct.c_int16),
        ('flags', ct.c_uint32),
        ('particleFlags', ct.c_uint32),
        ('invincTimer', ct.c_int16),
    ]

class SM64MarioGeometryBuffers(ct.Structure):
    _fields_ = [
        ('position', ct.POINTER(ct.c_float)),
        ('normal', ct.POINTER(ct.c_float)),
        ('color', ct.POINTER(ct.c_float)),
        ('uv', ct.POINTER(ct.c_float)),
        ('numTrianglesUsed', ct.c_uint16)
    ]

    def __init__(self):
        self.position_data = (ct.c_float * (SM64_GEO_MAX_TRIANGLES * 3 * 3))()
        self.position = ct.cast(self.position_data , ct.POINTER(ct.c_float))
        self.normal_data = (ct.c_float * (SM64_GEO_MAX_TRIANGLES * 3 * 3))()
        self.normal = ct.cast(self.normal_data , ct.POINTER(ct.c_float))
        self.color_data = (ct.c_float * (SM64_GEO_MAX_TRIANGLES * 3 * 3))()
        self.color = ct.cast(self.color_data , ct.POINTER(ct.c_float))
        self.uv_data = (ct.c_float * (SM64_GEO_MAX_TRIANGLES * 3 * 2))()
        self.uv = ct.cast(self.uv_data , ct.POINTER(ct.c_float))
        self.numTrianglesUsed = 0

    def __del__(self):
        pass

class MarioLifecycleError(RuntimeError):
    pass


@dataclass
class NativeSurfaceOwnership:
    chunk_key: tuple
    owner_token: str
    generation: int
    surface_count: int
    state: str = SURFACE_PREPARED
    object_id: object = None
    creation_order: int = 0
    diagnostic: str = ""


def _native_stage(stage):
    """Emit a crash-resilient native boundary marker for console/subprocess logs."""
    print("{} {}".format(NATIVE_STAGE_PREFIX, stage), flush=True)


def _abi_layout_failure(subject, expected, actual):
    raise MarioLifecycleError(
        "libsm64 ABI layout mismatch for {}: expected {}, actual {}. "
        "The packaged native ABI is pinned to upstream commit {}. "
        "Disable the add-on, remove the installed libsm64_studio directory, "
        "and reinstall a clean matching package.".format(
            subject, expected, actual, PINNED_LIBSM64_COMMIT
        )
    )


def _validate_ctypes_abi_layout(structure_overrides=None):
    """Reject ctypes declarations that do not match the pinned libsm64.h."""
    structures = {
        "SM64Surface": SM64Surface,
        "SM64MarioInputs": SM64MarioInputs,
        "SM64ObjectTransform": SM64ObjectTransform,
        "SM64SurfaceObject": SM64SurfaceObject,
        "SM64MarioState": SM64MarioState,
        "SM64MarioGeometryBuffers": SM64MarioGeometryBuffers,
    }
    if structure_overrides:
        structures.update(structure_overrides)

    expected_fields = {
        "SM64Surface": (
            ('type', ct.c_int16),
            ('force', ct.c_int16),
            ('terrain', ct.c_uint16),
            ('vertices', SM64SurfaceVertices),
        ),
        "SM64MarioInputs": (
            ('camLookX', ct.c_float),
            ('camLookZ', ct.c_float),
            ('stickX', ct.c_float),
            ('stickY', ct.c_float),
            ('buttonA', ct.c_uint8),
            ('buttonB', ct.c_uint8),
            ('buttonZ', ct.c_uint8),
        ),
        "SM64ObjectTransform": (
            ('position', Float3),
            ('eulerRotation', Float3),
        ),
        "SM64SurfaceObject": (
            ('transform', SM64ObjectTransform),
            ('surfaceCount', ct.c_uint32),
            ('surfaces', ct.POINTER(SM64Surface)),
        ),
        "SM64MarioState": (
            ('position', Float3),
            ('velocity', Float3),
            ('faceAngle', ct.c_float),
            ('forwardVelocity', ct.c_float),
            ('health', ct.c_int16),
            ('action', ct.c_uint32),
            ('animID', ct.c_int32),
            ('animFrame', ct.c_int16),
            ('flags', ct.c_uint32),
            ('particleFlags', ct.c_uint32),
            ('invincTimer', ct.c_int16),
        ),
        "SM64MarioGeometryBuffers": (
            ('position', ct.POINTER(ct.c_float)),
            ('normal', ct.POINTER(ct.c_float)),
            ('color', ct.POINTER(ct.c_float)),
            ('uv', ct.POINTER(ct.c_float)),
            ('numTrianglesUsed', ct.c_uint16),
        ),
    }
    for name, expected in expected_fields.items():
        actual = tuple(getattr(structures[name], "_fields_", ()))
        if actual != expected:
            _abi_layout_failure("{} fields".format(name), expected, actual)

    pointer_size = ct.sizeof(ct.c_void_p)
    if pointer_size not in (4, 8):
        _abi_layout_failure("native pointer size", "4 or 8 bytes", pointer_size)

    surface_pointer_offset = ((28 + pointer_size - 1) // pointer_size) * pointer_size
    surface_object_size = (
        (surface_pointer_offset + pointer_size + pointer_size - 1)
        // pointer_size * pointer_size
    )

    expected_sizes = {
        "SM64Surface": 44,
        "SM64MarioInputs": 20,
        "SM64ObjectTransform": 24,
        "SM64SurfaceObject": surface_object_size,
        "SM64MarioState": 60,
        "SM64MarioGeometryBuffers": ((4 * pointer_size + 2 + pointer_size - 1)
                                      // pointer_size * pointer_size),
    }
    expected_offsets = {
        "SM64Surface": {
            "type": 0, "force": 2, "terrain": 4, "vertices": 8,
        },
        "SM64MarioInputs": {
            "camLookX": 0, "camLookZ": 4, "stickX": 8, "stickY": 12,
            "buttonA": 16, "buttonB": 17, "buttonZ": 18,
        },
        "SM64ObjectTransform": {
            "position": 0, "eulerRotation": 12,
        },
        "SM64SurfaceObject": {
            "transform": 0, "surfaceCount": 24,
            "surfaces": surface_pointer_offset,
        },
        "SM64MarioState": {
            "position": 0, "velocity": 12, "faceAngle": 24,
            "forwardVelocity": 28, "health": 32, "action": 36,
            "animID": 40, "animFrame": 44, "flags": 48,
            "particleFlags": 52, "invincTimer": 56,
        },
        "SM64MarioGeometryBuffers": {
            "position": 0, "normal": pointer_size, "color": 2 * pointer_size,
            "uv": 3 * pointer_size, "numTrianglesUsed": 4 * pointer_size,
        },
    }
    for name, expected_size in expected_sizes.items():
        actual_size = ct.sizeof(structures[name])
        if actual_size != expected_size:
            _abi_layout_failure("sizeof({})".format(name), expected_size, actual_size)
        for field_name, expected_offset in expected_offsets[name].items():
            actual_offset = getattr(structures[name], field_name).offset
            if actual_offset != expected_offset:
                _abi_layout_failure(
                    "{}.{}.offset".format(name, field_name),
                    expected_offset,
                    actual_offset,
                )


class NativeLifecycle:
    """Explicit ownership record for exactly one native libsm64 session."""

    def __init__(self, generation):
        self.owner_token = _MODULE_OWNER_TOKEN
        self.generation = generation
        self.library = None
        self.library_loaded = False
        self.global_init_attempted = False
        self.global_initialized = False
        self.mario_create_attempted = False
        self.mario_created = False
        self.mario_id = -1
        self.tick_handler = None
        self.tick_handler_installed = False
        self.collision_update_handler = None
        self.collision_update_handler_installed = False
        self.timer_callback = None
        self.timer_installed = False
        self.control_state = STOPPED
        self.last_error = ""
        self.neutral_input_ticks = 0
        self.recording_tick_origin = None
        self.persistent_start_mark = None
        self.native_ownership_uncertain = False
        self.shutdown_in_progress = False
        self.shutdown_complete = False
        self.session_committed = False
        self.mario_delete_attempted = False
        self.global_terminate_attempted = False
        self.live_object = None
        self.live_object_name = ""
        self.scene = None
        self.input_started = False
        self.collision_cache = CollisionCache()
        self.native_surface_objects = {}
        self.active_chunk_keys = set()
        self.collision_center = None
        self.pending_transition_state = "idle"
        self.surface_create_count = 0
        self.surface_delete_count = 0
        self.active_surface_count = 0
        self.collision_stats = {
            "objects_scanned_by_bounds": 0,
            "objects_evaluated": 0,
            "object_cache_hits": 0,
            "object_cache_misses": 0,
            "chunk_cache_hits": 0,
            "chunk_cache_misses": 0,
            "chunks_prepared": 0,
        }
        self.last_collision_error = ""
        self.last_stream_seconds = 0.0
        self.collision_changed_while_live = False


_MODULE_OWNER_TOKEN = uuid.uuid4().hex


def _lifecycle_registry():
    registry = bpy.app.driver_namespace.get(LIFECYCLE_REGISTRY_KEY)
    if not isinstance(registry, dict):
        registry = {"next_generation": 0, "active": None}
        bpy.app.driver_namespace[LIFECYCLE_REGISTRY_KEY] = registry
    return registry


def _new_lifecycle():
    registry = _lifecycle_registry()
    registry["next_generation"] = int(registry.get("next_generation", 0)) + 1
    return NativeLifecycle(registry["next_generation"])


_lifecycle = _new_lifecycle()
_lifecycle.shutdown_complete = True
sm64: ct.CDLL = None
sm64_mario_id = -1

mario_inputs = SM64MarioInputs()
mario_state = SM64MarioState()
mario_geo = SM64MarioGeometryBuffers()


class _MeshCoordinateCache:
    """Reusable bulk-coordinate storage without retaining a Blender RNA object."""

    def __init__(self):
        self.mesh_identity = None
        self.vertex_count = 0
        self.coordinates = None


_mesh_coordinate_cache = _MeshCoordinateCache()


def _invalidate_mesh_coordinate_cache():
    _mesh_coordinate_cache.mesh_identity = None
    _mesh_coordinate_cache.vertex_count = 0
    _mesh_coordinate_cache.coordinates = None


def _mesh_identity(mesh):
    """Return Blender's process-lifetime ID identity without caching the ID itself."""
    session_uid = getattr(mesh, "session_uid", None)
    if session_uid is not None:
        return int(session_uid)
    return int(mesh.as_pointer())


def _coordinate_buffer_for_mesh(mesh):
    vertex_count = len(mesh.vertices)
    identity = _mesh_identity(mesh)
    cache = _mesh_coordinate_cache
    if (
        cache.coordinates is None
        or cache.mesh_identity != identity
        or cache.vertex_count != vertex_count
    ):
        coordinates = array('f', [0.0]) * (vertex_count * 3)
        mesh.vertices.foreach_get("co", coordinates)
        cache.mesh_identity = identity
        cache.vertex_count = vertex_count
        cache.coordinates = coordinates
    return cache.coordinates


def _active_mario_vertex_count(mesh):
    active_vertex_count = int(mario_geo.numTrianglesUsed) * 3
    vertex_count = len(mesh.vertices)
    if active_vertex_count > vertex_count:
        raise ValueError(
            "Mario geometry has {} active vertices, but mesh {!r} has capacity for {}".format(
                active_vertex_count, mesh.name, vertex_count
            )
        )
    return active_vertex_count


def native_position_to_blender(native_x, native_y, native_z):
    """Convert one libsm64 XYZ position to Blender world XYZ coordinates."""
    return (
        origin_offset[0] + float(native_x) / SM64_SCALE_FACTOR,
        origin_offset[1] - float(native_z) / SM64_SCALE_FACTOR,
        origin_offset[2] + float(native_y) / SM64_SCALE_FACTOR,
    )


def _write_active_mario_coordinates(coordinates, active_vertex_count):
    positions = mario_geo.position_data
    for vertex_index in range(active_vertex_count):
        source_offset = vertex_index * 3
        target_offset = vertex_index * 3
        blender_position = native_position_to_blender(
            positions[source_offset],
            positions[source_offset + 1],
            positions[source_offset + 2],
        )
        coordinates[target_offset] = blender_position[0]
        coordinates[target_offset + 1] = blender_position[1]
        coordinates[target_offset + 2] = blender_position[2]
follow_cam = False
tick_count = 0
last_cam_change_tick = -30

def _lifecycle_log(session, message):
    print("libsm64 lifecycle [{}] {}".format(session.generation, message))


def lifecycle_snapshot():
    session = _lifecycle
    return {
        "owner_token": session.owner_token,
        "generation": session.generation,
        "library_loaded": session.library_loaded,
        "global_init_attempted": session.global_init_attempted,
        "global_initialized": session.global_initialized,
        "mario_create_attempted": session.mario_create_attempted,
        "mario_created": session.mario_created,
        "mario_id": session.mario_id,
        "tick_handler_installed": session.tick_handler_installed,
        "collision_update_handler_installed": session.collision_update_handler_installed,
        "timer_installed": session.timer_installed,
        "control_state": session.control_state,
        "last_error": session.last_error,
        "native_ownership_uncertain": session.native_ownership_uncertain,
        "shutdown_in_progress": session.shutdown_in_progress,
        "shutdown_complete": session.shutdown_complete,
        "session_committed": session.session_committed,
        "active_native_surface_object_count": sum(
            1 for record in session.native_surface_objects.values()
            if record.object_id is not None and record.state in (
                SURFACE_CREATED, SURFACE_DELETE_ATTEMPTED, SURFACE_FAILED,
            )
        ),
        "owned_surface_ids": tuple(sorted(
            int(record.object_id) for record in session.native_surface_objects.values()
            if record.object_id is not None
        )),
        "active_chunk_keys": tuple(sorted(session.active_chunk_keys)),
        "pending_transition_state": session.pending_transition_state,
        "surface_create_count": session.surface_create_count,
        "surface_delete_count": session.surface_delete_count,
        "active_surface_count": session.active_surface_count,
        "last_collision_error": session.last_collision_error,
        "collision_changed_while_live": session.collision_changed_while_live,
        "collision_stats": dict(session.collision_stats),
    }


def _publish_session(session):
    _lifecycle_registry()["active"] = {
        "owner_token": session.owner_token,
        "generation": session.generation,
        "session": session,
        "shutdown": lambda: stop_tick_mario(_session=session),
    }


def _session_is_registered_owner(session):
    active = _lifecycle_registry().get("active")
    return bool(
        active
        and active.get("owner_token") == session.owner_token
        and active.get("generation") == session.generation
        and active.get("session") is session
    )


def _release_session(session):
    registry = _lifecycle_registry()
    if _session_is_registered_owner(session):
        registry["active"] = None


def _retire_registered_session():
    active = _lifecycle_registry().get("active")
    if not active:
        return
    shutdown = active.get("shutdown")
    if not callable(shutdown):
        raise MarioLifecycleError(
            "Native lifecycle ownership is inconsistent; restart Blender before inserting Mario"
        )
    errors = shutdown()
    if errors:
        raise MarioLifecycleError(
            "The previous native session failed to shut down cleanly; restart Blender: {}".format(
                "; ".join(errors)
            )
        )
    if _lifecycle_registry().get("active"):
        raise MarioLifecycleError(
            "The previous native session did not shut down cleanly; restart Blender"
        )


def _read_validated_rom(rom_path):
    expanded = os.path.expanduser(rom_path or "")
    if not expanded or not os.path.isfile(expanded):
        raise MarioLifecycleError("Select a valid unmodified SM64 US ROM")
    with open(expanded, "rb") as rom_file:
        rom_bytes = bytearray(rom_file.read())
    checksum = hashlib.sha1(rom_bytes).hexdigest()
    if checksum != EXPECTED_ROM_SHA1:
        raise MarioLifecycleError("The selected ROM is not the supported unmodified SM64 US ROM")
    return rom_bytes


def _native_package_error(detail):
    raise MarioLifecycleError(
        "{} The native package must match pinned upstream commit {}. "
        "Disable the add-on, remove the installed libsm64_studio directory, "
        "and reinstall a clean matching package.".format(
            detail, PINNED_LIBSM64_COMMIT
        )
    )


def _read_native_build_manifest(lib_directory=None):
    lib_directory = lib_directory or os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "lib"
    )
    manifest_path = os.path.join(lib_directory, "libsm64-build.json")
    try:
        with open(manifest_path, "r", encoding="utf-8") as manifest_file:
            manifest = json.load(manifest_file)
    except (OSError, ValueError) as exc:
        _native_package_error(
            "Could not read the bundled native build manifest {}: {}.".format(
                manifest_path, exc
            )
        )

    expected_values = {
        "repository": "libsm64/libsm64",
        "commit": PINNED_LIBSM64_COMMIT,
        "header": "src/libsm64.h",
        "windows_artifact": "sm64.dll",
        "linux_artifact": "libsm64.so",
    }
    for field_name, expected in expected_values.items():
        actual = manifest.get(field_name) if isinstance(manifest, dict) else None
        if actual != expected:
            _native_package_error(
                "Native build manifest field {} expected {!r}, actual {!r}.".format(
                    field_name, expected, actual
                )
            )
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        _native_package_error("Native build manifest has no artifact hash table.")
    for artifact_name in ("sm64.dll", "libsm64.so"):
        metadata = artifacts.get(artifact_name)
        artifact_hash = metadata.get("sha256") if isinstance(metadata, dict) else None
        if not isinstance(artifact_hash, str) or len(artifact_hash) != 64:
            _native_package_error(
                "Native build manifest has no valid SHA-256 for {}.".format(
                    artifact_name
                )
            )
        try:
            int(artifact_hash, 16)
        except ValueError:
            _native_package_error(
                "Native build manifest SHA-256 for {} is not hexadecimal.".format(
                    artifact_name
                )
            )
    return manifest


def _validate_manifest_abi_probe(manifest):
    probe = manifest.get("abi_probe") if isinstance(manifest, dict) else None
    if not isinstance(probe, dict):
        _native_package_error("Native build manifest has no pinned-header ABI probe data.")
    pointer_size = ct.sizeof(ct.c_void_p)
    if probe.get("pointer_size") != pointer_size:
        _abi_layout_failure(
            "manifest ABI probe pointer_size", probe.get("pointer_size"), pointer_size
        )
    structures = {
        "SM64Surface": SM64Surface,
        "SM64MarioInputs": SM64MarioInputs,
        "SM64ObjectTransform": SM64ObjectTransform,
        "SM64SurfaceObject": SM64SurfaceObject,
        "SM64MarioState": SM64MarioState,
        "SM64MarioGeometryBuffers": SM64MarioGeometryBuffers,
    }
    for structure_name, structure in structures.items():
        expected = probe.get(structure_name)
        if not isinstance(expected, dict):
            _native_package_error(
                "Native build manifest ABI probe is missing {}.".format(structure_name)
            )
        actual_size = ct.sizeof(structure)
        if expected.get("size") != actual_size:
            _abi_layout_failure(
                "manifest ABI probe sizeof({})".format(structure_name),
                expected.get("size"),
                actual_size,
            )
        for field_name, _field_type in structure._fields_:
            actual_offset = getattr(structure, field_name).offset
            if expected.get(field_name) != actual_offset:
                _abi_layout_failure(
                    "manifest ABI probe {}.{}.offset".format(
                        structure_name, field_name
                    ),
                    expected.get(field_name),
                    actual_offset,
                )


def _verify_native_artifact(manifest, lib_directory=None, system_name=None):
    lib_directory = lib_directory or os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "lib"
    )
    system_name = system_name or platform.system()
    if system_name == "Windows":
        artifact_name = manifest["windows_artifact"]
    elif system_name == "Linux":
        artifact_name = manifest["linux_artifact"]
    else:
        _native_package_error(
            "Unsupported native platform {!r}; Studio currently supports Windows "
            "and Linux.".format(system_name)
        )
    artifact_path = os.path.join(lib_directory, artifact_name)
    if not os.path.isfile(artifact_path):
        _native_package_error("Bundled native artifact is missing: {}.".format(artifact_path))
    digest = hashlib.sha256()
    try:
        with open(artifact_path, "rb") as artifact_file:
            for block in iter(lambda: artifact_file.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        _native_package_error(
            "Could not hash bundled native artifact {}: {}.".format(artifact_path, exc)
        )
    expected_hash = manifest["artifacts"][artifact_name]["sha256"].lower()
    actual_hash = digest.hexdigest()
    if actual_hash != expected_hash:
        _native_package_error(
            "Bundled native artifact {} has SHA-256 {}, expected {}.".format(
                artifact_name, actual_hash, expected_hash
            )
        )
    return artifact_path


def _load_native_library():
    _validate_ctypes_abi_layout()
    lib_directory = os.path.join(os.path.dirname(os.path.realpath(__file__)), "lib")
    manifest = _read_native_build_manifest(lib_directory)
    _validate_manifest_abi_probe(manifest)
    artifact_path = _verify_native_artifact(manifest, lib_directory)
    _native_stage("before_dll_load")
    try:
        library = ct.cdll.LoadLibrary(artifact_path)
    except OSError as exc:
        _native_package_error(
            "Could not load verified native artifact {}: {}.".format(
                artifact_path, exc
            )
        )
    _native_stage("after_dll_load")
    return library


def _configure_native_api(library):
    """Validate and configure the Phase-2 API from the pinned libsm64.h."""
    _validate_ctypes_abi_layout()
    required_exports = (
        "sm64_global_init",
        "sm64_global_terminate",
        "sm64_static_surfaces_load",
        "sm64_mario_create",
        "sm64_mario_tick",
        "sm64_mario_delete",
        "sm64_set_mario_faceangle",
        "sm64_surface_object_create",
        "sm64_surface_object_move",
        "sm64_surface_object_delete",
    )
    missing = [name for name in required_exports if not hasattr(library, name)]
    if missing:
        raise MarioLifecycleError(
            "The bundled libsm64 library is missing required export(s) {} for "
            "pinned upstream commit {}. Reinstall a clean matching package.".format(
                ", ".join(missing), PINNED_LIBSM64_COMMIT
            )
        )

    library.sm64_global_init.argtypes = [
        ct.POINTER(ct.c_uint8), ct.POINTER(ct.c_uint8),
    ]
    library.sm64_global_init.restype = None
    library.sm64_global_terminate.argtypes = []
    library.sm64_global_terminate.restype = None
    library.sm64_static_surfaces_load.argtypes = [ct.POINTER(SM64Surface), ct.c_uint32]
    library.sm64_static_surfaces_load.restype = None
    library.sm64_mario_create.argtypes = [ct.c_float, ct.c_float, ct.c_float]
    library.sm64_mario_create.restype = ct.c_int32
    library.sm64_mario_delete.argtypes = [ct.c_int32]
    library.sm64_mario_delete.restype = None
    library.sm64_mario_tick.argtypes = [
        ct.c_int32,
        ct.POINTER(SM64MarioInputs),
        ct.POINTER(SM64MarioState),
        ct.POINTER(SM64MarioGeometryBuffers),
    ]
    library.sm64_mario_tick.restype = None
    library.sm64_set_mario_faceangle.argtypes = [ct.c_int32, ct.c_float]
    library.sm64_set_mario_faceangle.restype = None
    library.sm64_surface_object_create.argtypes = [ct.POINTER(SM64SurfaceObject)]
    library.sm64_surface_object_create.restype = ct.c_uint32
    library.sm64_surface_object_move.argtypes = [
        ct.c_uint32, ct.POINTER(SM64ObjectTransform),
    ]
    library.sm64_surface_object_move.restype = None
    library.sm64_surface_object_delete.argtypes = [ct.c_uint32]
    library.sm64_surface_object_delete.restype = None
    _native_stage("after_abi_configuration")


def _make_tick_timer(session):
    def session_tick():
        if not _session_is_registered_owner(session):
            session.timer_installed = False
            return None
        if session.shutdown_in_progress or session.shutdown_complete:
            session.timer_installed = False
            return None
        if session.control_state in (STOPPED, POISONED):
            session.timer_installed = False
            return None
        if session.control_state in (BAKING, RESETTING):
            return SIMULATION_INTERVAL
        try:
            tick_mario(session.scene or bpy.context.scene, _session=session)
        except Exception as exc:
            message = "Live Mario simulation failed: {}".format(exc)
            if recorder.active:
                recorder.fail(message, preserve_samples=True)
            _poison_session(session, message)
            return None
        if not _session_is_registered_owner(session):
            session.timer_installed = False
            return None
        if session.shutdown_in_progress or session.shutdown_complete:
            session.timer_installed = False
            return None
        return SIMULATION_INTERVAL

    session_tick.__name__ = "libsm64_session_timer"
    session_tick._libsm64_owner_token = session.owner_token
    session_tick._libsm64_generation = session.generation
    return session_tick


def _install_tick_timer(session):
    if session.timer_callback is None:
        session.timer_callback = _make_tick_timer(session)
    if bpy.app.timers.is_registered(session.timer_callback):
        session.timer_installed = True
        return False
    bpy.app.timers.register(session.timer_callback, first_interval=SIMULATION_INTERVAL)
    session.timer_installed = True
    _lifecycle_log(session, "timer installed")
    return True


def remove_tick_mario_timer(session=None):
    """Unregister exactly one timer owned by one lifecycle generation."""
    session = session or _lifecycle
    callback = session.timer_callback
    removed = False
    if callback is not None and bpy.app.timers.is_registered(callback):
        try:
            bpy.app.timers.unregister(callback)
            removed = True
        except (ValueError, RuntimeError):
            # A callback currently on Blender's timer stack also stops itself
            # by returning None after observing shutdown/poisoned state.
            pass
    if removed or session.timer_installed:
        _lifecycle_log(session, "timer removed")
    session.timer_installed = False
    return removed


def _poison_session(session, message):
    session.control_state = POISONED
    session.last_error = str(message)
    remove_tick_mario_timer(session)
    _clear_transient_input_state(session)
    _lifecycle_log(session, "session poisoned: {}".format(message))


def _create_live_object():
    mario_obj = bpy.data.objects.new(
        'LibSM64 Studio Live Mario', bpy.data.meshes['libsm64_mario_mesh']
    )
    mario_obj[LIVE_ROLE] = LIVE_ROLE_VALUE
    bpy.context.scene.collection.objects.link(mario_obj)
    return mario_obj


def _prepare_blender_for_insert():
    try:
        bpy.ops.object.mode_set(mode='OBJECT')
    except Exception:
        pass
    bpy.ops.object.select_all(action='DESELECT')


def _record_collision_stats(session, stats):
    values = stats.as_dict()
    for key in session.collision_stats:
        session.collision_stats[key] += int(values.get(key, 0))
    session.last_stream_seconds = float(values.get("duration_seconds", 0.0))


def collision_diagnostics():
    snapshot = lifecycle_snapshot()
    return {
        "active_chunks": len(snapshot["active_chunk_keys"]),
        "active_native_objects": snapshot["active_native_surface_object_count"],
        "active_surfaces": snapshot["active_surface_count"],
        "cache": snapshot["collision_stats"],
        "last_stream_seconds": _lifecycle.last_stream_seconds,
        "last_error": snapshot["last_collision_error"],
    }


def collision_status_message():
    diagnostics = collision_diagnostics()
    cache = diagnostics["cache"]
    message = (
        "Collision: {} active chunks / {:,} surfaces | Cache: {} hits / {} misses | "
        "Last stream: {:.1f} ms".format(
            diagnostics["active_chunks"], diagnostics["active_surfaces"],
            cache["chunk_cache_hits"], cache["chunk_cache_misses"],
            diagnostics["last_stream_seconds"] * 1000.0,
        )
    )
    if _lifecycle.collision_changed_while_live:
        message += " | Scene collision changed: restart Live Mario to refresh active chunks"
    return message


def _collision_object_is_eligible(obj):
    return bool(
        getattr(obj, "type", None) == "MESH"
        and not obj.get("libsm64_is_bake", False)
        and not obj.get(LIVE_ROLE, "")
    )


def _make_collision_update_handler(session):
    def collision_update(_scene, depsgraph):
        if not _session_is_registered_owner(session) or session.shutdown_in_progress:
            return
        affected = []
        for update in getattr(depsgraph, "updates", ()):
            data = getattr(update, "id", None)
            if isinstance(data, bpy.types.Object):
                if _collision_object_is_eligible(data):
                    affected.append(data)
                elif session.scene is not None:
                    # A Fast64 Area Root or another collision parent can own
                    # terrain metadata inherited by descendant meshes.
                    for obj in session.scene.objects:
                        parent = getattr(obj, "parent", None)
                        while parent is not None:
                            if parent is data:
                                if _collision_object_is_eligible(obj):
                                    affected.append(obj)
                                break
                            parent = getattr(parent, "parent", None)
            elif isinstance(data, bpy.types.Mesh) and session.scene is not None:
                affected.extend(
                    obj for obj in session.scene.objects
                    if getattr(obj, "data", None) is data and _collision_object_is_eligible(obj)
                )
            elif isinstance(data, bpy.types.Material) and session.scene is not None:
                affected.extend(
                    obj for obj in session.scene.objects
                    if _collision_object_is_eligible(obj)
                    and data in getattr(getattr(obj, "data", None), "materials", ())
                )
        if not affected:
            return
        for obj in affected:
            session.collision_cache.mark_object_dirty(obj)
        session.collision_changed_while_live = True
        session.last_collision_error = (
            "Scene collision changed during generation {}; active chunks retain their safe "
            "session snapshot. Restart Live Mario to refresh them; dirty cached geometry will "
            "be rebuilt before any later chunk preparation.".format(session.generation)
        )

    collision_update.__name__ = "libsm64_collision_update"
    collision_update._libsm64_owner_token = session.owner_token
    collision_update._libsm64_generation = session.generation
    return collision_update


def _install_collision_update_handler(session):
    if session.collision_update_handler is None:
        session.collision_update_handler = _make_collision_update_handler(session)
    handlers = bpy.app.handlers.depsgraph_update_post
    if session.collision_update_handler not in handlers:
        handlers.append(session.collision_update_handler)
    session.collision_update_handler_installed = True


def _prepare_collision_chunks(session, keys):
    prepared, stats = session.collision_cache.prepare_chunks(
        session.scene,
        keys,
        SM64_SCALE_FACTOR,
        COLLISION_TYPES,
        depsgraph=bpy.context.evaluated_depsgraph_get(),
        chunk_size=CHUNK_SIZE_BLENDER,
    )
    _record_collision_stats(session, stats)
    return prepared


def _surface_record_is_owned(session, record):
    return (
        record.owner_token == session.owner_token
        and record.generation == session.generation
        and session.native_surface_objects.get(record.chunk_key) is record
    )


def _create_native_surface_object(session, chunk):
    if not _session_is_registered_owner(session):
        raise MarioLifecycleError("Collision create rejected for a stale lifecycle generation")
    if chunk.key in session.native_surface_objects:
        raise MarioLifecycleError("Collision chunk {} already has an ownership record".format(chunk.key))
    record = NativeSurfaceOwnership(
        chunk_key=chunk.key,
        owner_token=session.owner_token,
        generation=session.generation,
        surface_count=chunk.surface_count,
        creation_order=session.surface_create_count + 1,
    )
    session.native_surface_objects[chunk.key] = record
    if chunk.surface_count == 0:
        record.state = SURFACE_DELETED
        session.native_surface_objects.pop(chunk.key, None)
        return None
    surfaces, translation = native_chunk_payload(
        chunk,
        SM64_SCALE_FACTOR,
        origin_offset,
        SM64Surface,
        chunk_size=CHUNK_SIZE_BLENDER,
    )
    transform = SM64ObjectTransform()
    transform.position[:] = translation
    transform.eulerRotation[:] = (0.0, 0.0, 0.0)
    descriptor = SM64SurfaceObject()
    descriptor.transform = transform
    descriptor.surfaceCount = chunk.surface_count
    descriptor.surfaces = ct.cast(surfaces, ct.POINTER(SM64Surface))
    record.state = SURFACE_CREATE_ATTEMPTED
    try:
        _native_stage("before_surface_object_create {}".format(chunk.key))
        object_id = int(session.library.sm64_surface_object_create(ct.byref(descriptor)))
    except Exception as exc:
        record.state = SURFACE_FAILED
        record.diagnostic = str(exc)
        session.last_collision_error = (
            "chunk {} create failed in generation {}: {}".format(
                chunk.key, session.generation, exc
            )
        )
        raise
    record.object_id = object_id
    record.state = SURFACE_CREATED
    session.surface_create_count += 1
    session.active_surface_count += chunk.surface_count
    _native_stage("after_surface_object_create {}".format(chunk.key))
    # Upstream copies the entire SM64Surface array before returning.  The
    # temporary ctypes array intentionally dies with this stack frame.
    return record


def _delete_native_surface_object(session, chunk_key, operation="delete"):
    record = session.native_surface_objects.get(chunk_key)
    if record is None:
        return False
    if not _surface_record_is_owned(session, record):
        session.native_ownership_uncertain = True
        raise MarioLifecycleError(
            "Collision {} rejected for stale ownership of chunk {}".format(operation, chunk_key)
        )
    if record.object_id is None:
        session.native_surface_objects.pop(chunk_key, None)
        return False
    if record.state in (SURFACE_DELETE_ATTEMPTED, SURFACE_DELETED):
        raise MarioLifecycleError("Collision object {} would be deleted twice".format(record.object_id))
    record.state = SURFACE_DELETE_ATTEMPTED
    try:
        _native_stage("before_surface_object_delete {}".format(chunk_key))
        session.library.sm64_surface_object_delete(record.object_id)
        _native_stage("after_surface_object_delete {}".format(chunk_key))
    except Exception as exc:
        record.state = SURFACE_FAILED
        record.diagnostic = str(exc)
        session.native_ownership_uncertain = True
        session.last_collision_error = (
            "chunk {} {} failed in generation {}; ownership is uncertain: {}".format(
                chunk_key, operation, session.generation, exc
            )
        )
        raise
    record.state = SURFACE_DELETED
    session.surface_delete_count += 1
    session.active_surface_count -= record.surface_count
    # IDs may be reused by upstream.  Remove successful ownership immediately
    # so this generation can never delete the old numeric ID twice.
    session.native_surface_objects.pop(chunk_key, None)
    return True


def _rollback_surface_creates(session, created_keys):
    for chunk_key in reversed(created_keys):
        _delete_native_surface_object(session, chunk_key, operation="rollback")


def _create_incoming_chunks(session, prepared, incoming):
    created_keys = []
    try:
        for key in incoming:
            record = _create_native_surface_object(session, prepared[key])
            if record is not None:
                created_keys.append(key)
    except Exception as create_exc:
        try:
            _rollback_surface_creates(session, created_keys)
        except Exception as rollback_exc:
            session.native_ownership_uncertain = True
            session.pending_transition_state = "rollback failed"
            _poison_session(
                session,
                "Collision rollback failed; restart Blender. Create error: {}; rollback error: {}".format(
                    create_exc, rollback_exc
                ),
            )
            raise MarioLifecycleError(session.last_error) from rollback_exc
        session.pending_transition_state = "create failed; rolled back"
        _poison_session(session, session.last_collision_error or str(create_exc))
        raise MarioLifecycleError(session.last_error) from create_exc
    return created_keys


def _initialize_streamed_collision(session, world_position):
    center = chunk_coordinate(world_position, CHUNK_SIZE_BLENDER)
    plan = plan_transition((), center)
    session.pending_transition_state = "preparing initial chunks"
    prepared = _prepare_collision_chunks(session, plan.incoming)
    if not any(chunk.surface_count for chunk in prepared.values()):
        session.pending_transition_state = "initial collision empty"
        raise MarioLifecycleError("There is no ground under the 3D cursor where Mario can spawn")
    session.pending_transition_state = "creating initial chunks"
    _create_incoming_chunks(session, prepared, plan.incoming)
    session.active_chunk_keys.update(plan.incoming)
    session.collision_center = center
    session.pending_transition_state = "idle"
    _lifecycle_log(
        session,
        "initial collision: {} chunks, {} native objects, {} surfaces".format(
            len(session.active_chunk_keys), len(session.native_surface_objects),
            session.active_surface_count,
        ),
    )


def _stream_collision_for_position(session, world_position):
    center = chunk_coordinate(world_position, CHUNK_SIZE_BLENDER)
    plan = plan_transition(session.active_chunk_keys, center)
    if not plan.incoming and not plan.outgoing:
        session.collision_center = center
        return False
    session.pending_transition_state = "preparing incoming {}".format(plan.incoming)
    prepared = _prepare_collision_chunks(session, plan.incoming)
    session.pending_transition_state = "creating incoming {}".format(plan.incoming)
    _create_incoming_chunks(session, prepared, plan.incoming)
    # Commit every incoming key, including empty prepared chunks, before any
    # outgoing deletion.  This is the collision-gap prevention boundary.
    session.active_chunk_keys.update(plan.incoming)
    session.pending_transition_state = "deleting outgoing {}".format(plan.outgoing)
    try:
        for key in plan.outgoing:
            _delete_native_surface_object(session, key)
            session.active_chunk_keys.discard(key)
    except Exception as exc:
        session.pending_transition_state = "outgoing delete failed"
        _poison_session(session, session.last_collision_error or str(exc))
        raise MarioLifecycleError(session.last_error) from exc
    session.collision_center = center
    session.pending_transition_state = "idle"
    _lifecycle_log(
        session,
        "collision streamed: center={} incoming={} outgoing={} active={} surfaces={} {:.1f}ms".format(
            center, len(plan.incoming), len(plan.outgoing),
            len(session.active_chunk_keys), session.active_surface_count,
            session.last_stream_seconds * 1000.0,
        ),
    )
    return True


def insert_mario(rom_path: str, scale: float, camera_follow: bool):
    global _lifecycle, sm64, sm64_mario_id
    global SM64_SCALE_FACTOR, original_fps, original_fps_setting
    global original_fps_base, tick_count, origin_offset, original_cursor_pos, follow_cam, simulation_scene

    try:
        rom_bytes = _read_validated_rom(rom_path)
    except MarioLifecycleError as exc:
        return str(exc)

    try:
        _retire_registered_session()
    except MarioLifecycleError as exc:
        return str(exc)

    session = _new_lifecycle()
    _lifecycle = session
    _publish_session(session)
    _invalidate_mesh_coordinate_cache()

    SM64_SCALE_FACTOR = scale
    try:
        _prepare_blender_for_insert()

        original_cursor_pos = [
            bpy.context.scene.cursor.location.x,
            bpy.context.scene.cursor.location.y,
            bpy.context.scene.cursor.location.z
        ]
        follow_cam = camera_follow

        origin_offset[0] = bpy.context.scene.cursor.location.x
        origin_offset[1] = bpy.context.scene.cursor.location.y
        origin_offset[2] = bpy.context.scene.cursor.location.z
        session.scene = bpy.context.scene

        recorder.cancel("Ready for a new take")

        existing_live = get_live_mario_object()
        if existing_live is not None:
            bpy.data.objects.remove(existing_live, do_unlink=True)
    except Exception as exc:
        stop_tick_mario(_session=session, _cleanup_rejected=False)
        return "Could not prepare Blender for Mario: {}".format(exc)

    try:
        _validate_ctypes_abi_layout()
        session.library = _load_native_library()
        session.library_loaded = True
        sm64 = session.library
        _lifecycle_log(session, "library loaded")
        _configure_native_api(session.library)

        rom_chars = ct.c_ubyte * len(rom_bytes)
        texture_buff = (ct.c_ubyte * (4 * SM64_TEXTURE_WIDTH * SM64_TEXTURE_HEIGHT))()
        session.global_init_attempted = True
        _lifecycle_log(session, "global init started")
        _native_stage("before_global_init")
        session.library.sm64_global_init(rom_chars.from_buffer(rom_bytes), texture_buff)
        _native_stage("after_global_init")
        session.global_initialized = True
        _lifecycle_log(session, "global init succeeded")
        initialize_all_data(texture_buff)

        # Dynamic surface objects carry all Studio scene collision.  Keep the
        # pinned static API initialized with an explicitly empty set so no
        # geometry is represented in both systems.
        empty_surfaces = (SM64Surface * 0)()
        _native_stage("before_static_surface_load")
        session.library.sm64_static_surfaces_load(empty_surfaces, 0)
        _native_stage("after_static_surface_load")
        print("Preparing nearby streamed collision\u2026")
        _initialize_streamed_collision(session, tuple(original_cursor_pos))

        session.mario_create_attempted = True
        _lifecycle_log(session, "Mario create started")
        _native_stage("before_mario_create")
        print("Starting Live Mario\u2026")
        mario_id = int(session.library.sm64_mario_create(0.0, 0.0, 0.0))
        _native_stage("after_mario_create")
        if mario_id < 0:
            _lifecycle_log(session, "Mario create failed")
            raise MarioLifecycleError("There is no ground under the 3D cursor where Mario can spawn")
        session.mario_id = mario_id
        session.mario_created = True
        sm64_mario_id = mario_id
        _lifecycle_log(session, "Mario create succeeded")

        session.live_object = _create_live_object()
        session.live_object_name = session.live_object.name
        session.live_object["libsm64_session_owner"] = session.owner_token
        session.live_object["libsm64_session_generation"] = session.generation
        start_input_reader()
        session.input_started = True

        simulation_scene = session.scene
        session.scene = simulation_scene
        original_fps_setting = simulation_scene.render.fps
        original_fps_base = simulation_scene.render.fps_base
        original_fps = float(original_fps_setting) / float(original_fps_base)
        tick_count = 0
        session.session_committed = True
        session.control_state = LIVE_IDLE
        _install_collision_update_handler(session)
        _install_tick_timer(session)
        return None
    except Exception as exc:
        if session.global_init_attempted and not session.global_initialized:
            _lifecycle_log(session, "global init failed")
        stop_tick_mario(_session=session, _cleanup_rejected=False)
        if isinstance(exc, MarioLifecycleError):
            return str(exc)
        return "Could not initialize Mario: {}".format(exc)


def remove_tick_mario_handlers(session=None):
    """Remove only callbacks owned by one explicit lifecycle generation."""
    session = session or _lifecycle
    handlers = bpy.app.handlers.frame_change_pre
    removed = 0
    for handler in list(handlers):
        if (
            handler is session.tick_handler
            or (
                getattr(handler, '_libsm64_owner_token', None) == session.owner_token
                and getattr(handler, '_libsm64_generation', None) == session.generation
            )
        ):
            handlers.remove(handler)
            removed += 1
    if removed or session.tick_handler_installed:
        _lifecycle_log(session, "tick removed")
    session.tick_handler_installed = False
    collision_handlers = bpy.app.handlers.depsgraph_update_post
    collision_removed = 0
    for handler in list(collision_handlers):
        if (
            handler is session.collision_update_handler
            or (
                getattr(handler, '_libsm64_owner_token', None) == session.owner_token
                and getattr(handler, '_libsm64_generation', None) == session.generation
                and getattr(handler, '__name__', '') == 'libsm64_collision_update'
            )
        ):
            collision_handlers.remove(handler)
            collision_removed += 1
    session.collision_update_handler_installed = False
    return removed


def get_live_mario_object():
    """Return only the stable live-role object, with an exact-name legacy fallback."""
    for obj in bpy.data.objects:
        if obj.get(LIVE_ROLE) == LIVE_ROLE_VALUE and not obj.get('libsm64_is_bake', False):
            return obj
    legacy = bpy.data.objects.get('LibSM64 Mario')
    if legacy is not None and not legacy.get('libsm64_is_bake', False):
        return legacy
    return None


def _ensure_live_mesh_exclusive(live_object):
    """Detach Live Mario before simulation writes if any object shares its mesh."""
    mesh = live_object.data
    if mesh.users <= 1:
        return mesh
    exclusive = mesh.copy()
    exclusive.name = "libsm64_mario_live_mesh"
    live_object.data = exclusive
    if exclusive.users != 1:
        raise RuntimeError("Could not make the Live Mario mesh exclusive")
    copied_keys = getattr(exclusive, "shape_keys", None)
    if copied_keys is not None:
        if copied_keys is getattr(mesh, "shape_keys", None) or copied_keys.users != 1:
            raise RuntimeError("Could not detach Live Mario's shape keys")
        live_object.shape_key_clear()
    _invalidate_mesh_coordinate_cache()
    return exclusive


def is_mario_running():
    session = _lifecycle
    return bool(
        _session_is_registered_owner(session)
        and session.session_committed
        and session.library_loaded
        and session.global_initialized
        and session.mario_created
        and session.mario_id >= 0
        and not session.shutdown_in_progress
        and not session.shutdown_complete
        and session.control_state != POISONED
        and get_live_mario_object() is not None
    )


def has_owned_native_session():
    session = _lifecycle
    return bool(
        _session_is_registered_owner(session)
        and not session.shutdown_complete
    )


def live_control_status():
    """Return the explicit native-control state used by the panel and tests."""
    session = _lifecycle
    if session.control_state == POISONED:
        return POISONED
    if not is_mario_running():
        return STOPPED
    return session.control_state


def live_control_error():
    return _lifecycle.last_error


def get_simulation_target_fps(scene=None):
    scene = scene or bpy.context.scene
    return float(scene.render.fps) / float(scene.render.fps_base)


def _clear_transient_input_state(session=None):
    session = session or _lifecycle
    for field in (
        "camLookX", "camLookZ", "stickX", "stickY",
        "buttonA", "buttonB", "buttonZ",
    ):
        setattr(mario_inputs, field, 0)
    try:
        from . import input_value
        for key in input_value:
            input_value[key] = False
    except (ImportError, AttributeError, TypeError):
        pass
    clear_input_reader_state()
    session.neutral_input_ticks = max(session.neutral_input_ticks, 1)


def _disable_keyboard_control():
    try:
        from . import config, input_value
        config["keyboard_control"] = False
        for key in input_value:
            input_value[key] = False
    except (ImportError, AttributeError, TypeError):
        pass


def capture_mario_starting_mark():
    """Capture the public simulation state needed for a safe fresh Mario spawn."""
    if not is_mario_running():
        raise RuntimeError("Live Mario is unavailable")
    return {
        "position": (
            float(mario_state.position[0]),
            float(mario_state.position[1]),
            float(mario_state.position[2]),
        ),
        "velocity": (
            float(mario_state.velocity[0]),
            float(mario_state.velocity[1]),
            float(mario_state.velocity[2]),
        ),
        "face_angle": float(mario_state.faceAngle),
        "health": int(mario_state.health),
    }


def _valid_persistent_start_mark(session=None):
    """Return this generation's mark data, or None for stale/unowned state."""
    session = session or _lifecycle
    stored = session.persistent_start_mark
    if not isinstance(stored, dict):
        return None
    if stored.get("owner_token") != session.owner_token:
        return None
    if stored.get("generation") != session.generation:
        return None
    mark = stored.get("mark")
    if not isinstance(mark, dict) or "position" not in mark:
        return None
    if session is not _lifecycle or not is_mario_running():
        return None
    return mark


def has_valid_start_mark():
    """Whether the running Live Mario owns a usable persistent Start Mark."""
    return _valid_persistent_start_mark() is not None


def set_persistent_start_mark():
    """Capture and replace the active generation's persistent Start Mark."""
    session = _lifecycle
    if not is_mario_running() or session.control_state != LIVE_IDLE:
        raise RuntimeError("Live Mario is not ready to set a Start Mark")
    mark = capture_mario_starting_mark()
    session.persistent_start_mark = {
        "owner_token": session.owner_token,
        "generation": session.generation,
        "mark": mark,
    }
    return mark


def clear_persistent_start_mark(session=None):
    """Forget a Start Mark without touching native Mario state."""
    (session or _lifecycle).persistent_start_mark = None


def reset_to_persistent_start_mark():
    """Safely recreate Live Mario at this generation's persistent mark."""
    session = _lifecycle
    if recorder.active or session.control_state == RECORDING:
        raise RuntimeError("Reset to Mark is unavailable while recording")
    if session.control_state != LIVE_IDLE:
        raise RuntimeError("Live Mario is not ready to reset")
    mark = _valid_persistent_start_mark(session)
    if mark is None:
        raise RuntimeError("Start Mark unavailable")

    session.control_state = RESETTING
    try:
        restore_mario_starting_mark(mark)
        session.recording_tick_origin = None
        session.control_state = LIVE_IDLE
        resume_mario_for_recording()
    except Exception:
        if session.control_state != POISONED:
            session.control_state = LIVE_IDLE if is_mario_running() else STOPPED
        raise
    return mark


def restore_mario_starting_mark(mark):
    """Recreate libsm64 Mario at a mark, clearing all transient internal state.

    The bundled libsm64 exposes create/delete but no complete state setter. A
    fresh instance is therefore safer than moving only Blender geometry or
    attempting to mutate the output state structure.
    """
    global sm64_mario_id, tick_count

    session = _lifecycle
    if not is_mario_running():
        raise RuntimeError("Live Mario is unavailable")
    spawn = tuple(float(value) for value in mark["position"])
    old_id = session.mario_id
    try:
        session.library.sm64_mario_delete(old_id)
    except Exception as exc:
        session.mario_delete_attempted = True
        _poison_session(
            session,
            "Mario reset cleanup failed; use End Studio Session and restart Blender: {}".format(exc),
        )
        raise MarioLifecycleError(session.last_error)
    session.mario_created = False
    session.mario_id = -1
    sm64_mario_id = -1
    try:
        replacement_id = int(session.library.sm64_mario_create(*spawn))
    except Exception as exc:
        session.native_ownership_uncertain = True
        _poison_session(
            session,
            "Mario reset creation failed with uncertain native ownership; restart Blender: {}".format(exc),
        )
        raise MarioLifecycleError(session.last_error)
    if replacement_id < 0:
        _poison_session(
            session,
            "Could not restore Live Mario at the starting mark; use End Studio Session",
        )
        raise MarioLifecycleError(session.last_error)
    session.mario_id = replacement_id
    session.mario_created = True
    sm64_mario_id = replacement_id
    try:
        session.library.sm64_set_mario_faceangle(
            session.mario_id, float(mark["face_angle"])
        )
        _clear_transient_input_state(session)
        session.library.sm64_mario_tick(
            session.mario_id,
            ct.byref(mario_inputs),
            ct.byref(mario_state),
            ct.byref(mario_geo),
        )
    except Exception as exc:
        _poison_session(
            session,
            "Mario reset state restoration failed; use End Studio Session and "
            "restart Blender: {}".format(exc),
        )
        raise MarioLifecycleError(session.last_error)
    tick_count = 1

    live_object = get_live_mario_object()
    if live_object is not None:
        update_mesh_data(_ensure_live_mesh_exclusive(live_object))
        live_object.hide_render = False
        try:
            live_object.hide_set(False)
        except AttributeError:
            live_object.hide_viewport = False
    return sm64_mario_id


def resume_mario_for_recording():
    """Compatibility wrapper: ensure the persistent live timer is installed."""
    if not is_mario_running():
        raise RuntimeError("Live Mario is unavailable")
    _install_tick_timer(_lifecycle)


def pause_mario_for_review():
    """Pause Live Mario ticks while preserving the owned native session."""
    if is_mario_running():
        remove_tick_mario_timer(_lifecycle)


def begin_mario_recording(scene, reset_to_mark=False):
    """Atomically prepare optional Start Mark reset, then begin capture."""
    session = _lifecycle
    if not is_mario_running() or session.control_state != LIVE_IDLE:
        raise RuntimeError("Live Mario is not ready to record")
    if recorder.has_pending_samples:
        raise RuntimeError("Cancel or finish the pending take before recording again")
    try:
        if reset_to_mark:
            reset_to_persistent_start_mark()
        recorder.start(float(scene.frame_current), get_simulation_target_fps(scene))
        session.recording_tick_origin = tick_count
        session.control_state = RECORDING
        _install_tick_timer(session)
    except Exception:
        recorder.cancel("Recording did not start")
        session.recording_tick_origin = None
        if session.control_state != POISONED and is_mario_running():
            session.control_state = LIVE_IDLE
        raise


def freeze_mario_recording_for_bake():
    session = _lifecycle
    if session.control_state != RECORDING and not recorder.has_pending_samples:
        raise RecordingError("Live Mario is not recording")
    try:
        samples = recorder.freeze_for_bake()
    except Exception:
        session.control_state = LIVE_IDLE if is_mario_running() else STOPPED
        raise
    session.control_state = BAKING
    return samples


def resume_live_idle_after_transition(mark):
    """Restore one take mark and immediately resume controllable idle operation."""
    session = _lifecycle
    if not is_mario_running():
        raise RuntimeError("Live Mario is unavailable")
    session.control_state = RESETTING
    try:
        restore_mario_starting_mark(mark)
    except Exception:
        if session.control_state != POISONED:
            _poison_session(session, "Live Mario reset failed")
        raise
    session.recording_tick_origin = None
    session.control_state = LIVE_IDLE
    _install_tick_timer(session)


def return_to_start_mark_after_transition():
    """Return to a valid Start Mark and resume controllable idle operation."""
    session = _lifecycle
    valid_mark = has_valid_start_mark()
    if recorder.active:
        raise RuntimeError("Cannot finish the recording transition while capture is active")
    if session.control_state in (RECORDING, BAKING):
        session.control_state = LIVE_IDLE
    if valid_mark:
        mark = reset_to_persistent_start_mark()
    else:
        abandon_bake_transition()
        mark = None
    resume_mario_for_recording()
    return mark


def abandon_bake_transition():
    """Return a safe session to idle while retaining recorder error samples."""
    session = _lifecycle
    if is_mario_running() and session.control_state != POISONED:
        session.control_state = LIVE_IDLE
        _install_tick_timer(session)


def stop_tick_mario(_session=None, _cleanup_rejected=True):
    """Idempotently tear down only native resources this generation owns."""
    global sm64, sm64_mario_id, original_fps, original_fps_setting
    global original_fps_base, original_cursor_pos, simulation_scene

    session = _session or _lifecycle
    if session.shutdown_in_progress:
        _lifecycle_log(session, "shutdown already in progress")
        return ()
    if session.shutdown_complete:
        _lifecycle_log(session, "shutdown already complete")
        return ()
    if not _session_is_registered_owner(session):
        _lifecycle_log(session, "shutdown skipped: session is not owned by this generation")
        return ()

    session.shutdown_in_progress = True
    _invalidate_mesh_coordinate_cache()
    errors = []
    scene = session.scene or simulation_scene
    was_running = session.session_committed
    remove_tick_mario_timer(session)
    remove_tick_mario_handlers(session)
    if session.input_started:
        try:
            stop_input_reader()
        except Exception as exc:
            errors.append("Input cleanup failed: {}".format(exc))
        session.input_started = False
    _disable_keyboard_control()
    if scene is not None:
        scene.cursor.location = (
            original_cursor_pos[0],
            original_cursor_pos[1],
            original_cursor_pos[2]
        )
    try:
        surface_cleanup_safe = not session.native_ownership_uncertain
        if session.native_ownership_uncertain:
            errors.append("Surface-object cleanup skipped because native ownership is already uncertain")
            _lifecycle_log(session, "surface cleanup skipped: native ownership is uncertain")
        elif session.native_surface_objects:
            if not (session.library_loaded and session.global_initialized and session.library):
                errors.append("Surface-object state is inconsistent; native cleanup skipped")
                session.native_ownership_uncertain = True
                surface_cleanup_safe = False
            else:
                owned_records = sorted(
                    tuple(session.native_surface_objects.values()),
                    key=lambda record: record.creation_order,
                    reverse=True,
                )
                for record in owned_records:
                    try:
                        _delete_native_surface_object(session, record.chunk_key, operation="shutdown")
                    except Exception as exc:
                        errors.append("Surface object delete failed: {}".format(exc))
                        surface_cleanup_safe = False
                        # Once one ID is uncertain, make no further native calls.
                        break
        if surface_cleanup_safe:
            session.active_chunk_keys.clear()
            session.active_surface_count = 0

        if not surface_cleanup_safe:
            if session.mario_created:
                errors.append("Mario deletion skipped because surface ownership is uncertain")
                _lifecycle_log(session, "Mario delete skipped: surface ownership is uncertain")
        elif session.mario_created and not session.mario_delete_attempted:
            if not (session.library_loaded and session.global_initialized and session.library):
                errors.append("Mario state is inconsistent; native delete skipped")
                _lifecycle_log(session, "Mario delete skipped: lifecycle prerequisites are not valid")
            else:
                session.mario_delete_attempted = True
                _lifecycle_log(session, "Mario delete invoked")
                try:
                    session.library.sm64_mario_delete(session.mario_id)
                    session.mario_created = False
                    session.mario_id = -1
                except Exception as exc:
                    errors.append("Mario delete failed: {}".format(exc))
        elif session.mario_created:
            _lifecycle_log(session, "Mario delete skipped: deletion was already attempted")
        else:
            _lifecycle_log(session, "Mario delete skipped: no Mario instance was created")

        if session.global_initialized and not session.global_terminate_attempted:
            if session.native_ownership_uncertain:
                errors.append("Global termination skipped because native Mario ownership is uncertain")
                _lifecycle_log(session, "global terminate skipped: native ownership is uncertain")
            elif session.mario_created:
                errors.append("Global termination skipped because Mario deletion did not complete")
                _lifecycle_log(session, "global terminate skipped: Mario instance may still be active")
            elif not (session.library_loaded and session.library):
                errors.append("Global state is inconsistent; native terminate skipped")
                _lifecycle_log(session, "global terminate skipped: library ownership is invalid")
            else:
                session.global_terminate_attempted = True
                _lifecycle_log(session, "global terminate invoked")
                try:
                    session.library.sm64_global_terminate()
                    session.global_initialized = False
                except Exception as exc:
                    errors.append("Global termination failed: {}".format(exc))
        elif session.global_initialized:
            _lifecycle_log(session, "global terminate skipped: termination was already attempted")
        else:
            _lifecycle_log(
                session,
                "Skipping sm64_global_terminate: global initialization was not successfully completed.",
            )
    finally:
        if session.input_started:
            try:
                stop_input_reader()
            except Exception as exc:
                errors.append("Input cleanup failed: {}".format(exc))
            session.input_started = False
        _disable_keyboard_control()
        live_object = bpy.data.objects.get(session.live_object_name) if session.live_object_name else None
        if (
            live_object is not None
            and live_object.get("libsm64_session_owner") == session.owner_token
            and int(live_object.get("libsm64_session_generation", -1)) == session.generation
        ):
            bpy.data.objects.remove(live_object, do_unlink=True)
        session.live_object = None
        session.live_object_name = ""
        session.collision_cache.clear()
        session.pending_transition_state = "shutdown"
        session.library = None
        session.library_loaded = False
        session.session_committed = False
        session.shutdown_in_progress = False
        session.shutdown_complete = True
        sm64_mario_id = -1
        sm64 = None
        original_fps = 0
        original_fps_setting = 0
        original_fps_base = 1.0
        simulation_scene = None
        clear_persistent_start_mark(session)
        if errors:
            session.control_state = POISONED
            session.last_error = "; ".join(errors)
            _lifecycle_log(session, "shutdown completed with errors; lifecycle is poisoned")
        else:
            session.control_state = STOPPED
            session.last_error = ""
            _release_session(session)
            _lifecycle_log(session, "shutdown completed")
    if was_running and _cleanup_rejected:
        # Import lazily to keep take management out of the simulation module's
        # import path and make this the single live-control cleanup boundary.
        from .take_manager import cleanup_rejected
        cleanup_rejected(scene or bpy.context.scene)
    return tuple(errors)

def tick_mario(scene, depsgraph=None, _session=None):
    global sm64, sm64_mario_id, mario_state, mario_geo, tick_count, last_cam_change_tick, origin_offset, follow_cam

    session = _session or _lifecycle
    if not _session_is_registered_owner(session) or not session.mario_created:
        return 0

    live_object = get_live_mario_object()
    if live_object is None:
        if recorder.active:
            recorder.fail("Live Mario was deleted; recording cancelled", preserve_samples=False)
        stop_tick_mario(_session=session)
        return 0

    if session.neutral_input_ticks > 0:
        _clear_transient_input_state(session)
        session.neutral_input_ticks -= 1
    else:
        sample_input_reader(mario_inputs)

    view3d = None
    for window in getattr(bpy.context.window_manager, "windows", ()):
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                view3d = area
                break
        if view3d is not None:
            break
    r3d = view3d.spaces[0].region_3d if view3d is not None else None

    # Get rotation inputs (assuming values from 0-1)
    camLookX = mario_inputs.camLookX  # horizontal rotation (around Z)
    camLookZ = mario_inputs.camLookZ  # vertical rotation (pitch)
    ticks_since_cam_change = tick_count - last_cam_change_tick
    is_cam_change_ok = ticks_since_cam_change > 8

    if r3d is not None and is_cam_change_ok:
        if camLookX != 0:  # Dead zone handled in inputs
            rot_angle = math.radians(360.0 * camLookX)
            rotation = mathutils.Quaternion((0, 0, 1), rot_angle)
            r3d.view_rotation = rotation @ r3d.view_rotation
            last_cam_change_tick = tick_count
        elif camLookZ != 0:
            zoom_factor = 1.0 + camLookZ
            r3d.view_distance *= zoom_factor
            last_cam_change_tick = tick_count

    if r3d is not None:
        look_dir = r3d.view_rotation @ mathutils.Vector((0.0, 0.0, -1.0))
        mario_inputs.camLookX = look_dir.x
        mario_inputs.camLookZ = -look_dir.y

    session.library.sm64_mario_tick(
        session.mario_id, ct.byref(mario_inputs), ct.byref(mario_state), ct.byref(mario_geo)
    )
    mario_world_location = native_position_to_blender(
        mario_state.position[0], mario_state.position[1], mario_state.position[2]
    )
    # Stream only after a complete native tick has supplied Mario's position.
    # Incoming objects are committed before distant objects are deleted, and
    # the Mario ID/state/recording timeline are never touched by this path.
    _stream_collision_for_position(session, mario_world_location)

    if follow_cam:
        scene.cursor.location = (
            mario_world_location[0] + scene.libsm64.camera_shift.x,
            mario_world_location[1] + scene.libsm64.camera_shift.y,
            mario_world_location[2] + scene.libsm64.camera_shift.z,
        )

        if view3d is not None:
            for region in (r for r in view3d.regions if r.type == 'WINDOW'):
                with bpy.context.temp_override(area=view3d, region=region):
                    bpy.ops.view3d.view_center_cursor()

    live_mesh = _ensure_live_mesh_exclusive(live_object)
    if tick_count < 15: # This is enough frames to get Mario to open his eyes, then we'll stop updating uv/color
        update_mesh_data(live_mesh)
    else:
        update_mesh_data_fast(live_mesh)

    try:
        recorder.capture_mesh(
            live_mesh,
            tick_count,
            mario_world_location,
            float(mario_state.faceAngle),
        )
    except Exception as exc:
        recorder.fail("Recording stopped: {}".format(exc), preserve_samples=True)
        if session.control_state == RECORDING:
            session.control_state = LIVE_IDLE
    tick_count += 1
    if view3d is not None:
        view3d.tag_redraw()

def _checked_int32_coordinate(value):
    """Preserve truncation-to-int semantics while rejecting non-int32 values."""
    try:
        integer = int(value)
    except (OverflowError, ValueError):
        return 0, False
    return integer, -0x80000000 <= integer <= 0x7FFFFFFF


def _object_terrain(obj):
    seek = obj
    while seek is not None:
        if (getattr(seek, 'sm64_obj_type', None) == 'Area Root'
                and hasattr(seek, 'terrainEnum')):
            return COLLISION_TYPES[seek.terrainEnum]
        seek = seek.parent
    return COLLISION_TYPES['TERRAIN_GRASS']


def get_surface_array_from_scene():
    """Extract one whole-scene static collision set for the live session."""
    scene = bpy.context.window.scene
    objects = [
        obj for obj in cast(List[bpy.types.Object], scene.collection.all_objects)
        if isinstance(obj.data, bpy.types.Mesh) and not obj.get('libsm64_is_bake', False)
    ]
    triangle_count = 0
    for obj in objects:
        obj.data.calc_loop_triangles()
        triangle_count += len(obj.data.loop_triangles)

    surface_array = (SM64Surface * triangle_count)()
    surface_count = 0
    skipped_out_of_range = 0
    for obj in objects:
        mesh = obj.data
        terrain = _object_terrain(obj)
        matrix_world = obj.matrix_world
        materials = mesh.materials
        vertices = mesh.vertices
        for tri in cast(List[bpy.types.MeshLoopTriangle], mesh.loop_triangles):
            world = [matrix_world @ vertices[index].co for index in tri.vertices]
            native = []
            vertex_in_range = []
            for vertex in world:
                x, in_x = _checked_int32_coordinate(
                    SM64_SCALE_FACTOR * (vertex.x - origin_offset[0])
                )
                y, in_y = _checked_int32_coordinate(
                    SM64_SCALE_FACTOR * (vertex.z - origin_offset[2])
                )
                z, in_z = _checked_int32_coordinate(
                    SM64_SCALE_FACTOR * (-vertex.y + origin_offset[1])
                )
                native.append((x, y, z))
                vertex_in_range.append(in_x and in_y and in_z)
            if not all(vertex_in_range):
                skipped_out_of_range += 1
                continue

            surface_type = COLLISION_TYPES['SURFACE_DEFAULT']
            if 0 < tri.material_index < len(materials):
                material = materials[tri.material_index]
                collision_type = getattr(material, 'collision_type_simple', None)
                if collision_type is not None:
                    surface_type = COLLISION_TYPES[collision_type]
            surface = surface_array[surface_count]
            surface.type = surface_type
            surface.force = 0
            surface.terrain = terrain
            for vertex_index in range(3):
                for axis_index in range(3):
                    surface.vertices[vertex_index][axis_index] = native[vertex_index][axis_index]
            surface_count += 1

    if skipped_out_of_range:
        print(
            "Skipped {} collision surface(s): at least one native X, Y, or Z "
            "coordinate was outside the signed 32-bit range.".format(
                skipped_out_of_range
            )
        )
    print("Scene collision ready: {} surfaces".format(surface_count))
    return surface_array, surface_count

def _new_mario_texture_image():
    image = bpy.data.images.new(
        "libsm64_mario_texture",
        width=SM64_TEXTURE_WIDTH,
        height=SM64_TEXTURE_HEIGHT,
        alpha=True,
    )
    image.source = 'GENERATED'
    image.generated_type = 'BLANK'
    return image


def _prepare_mario_texture_image():
    """Return a writable generated image and any unusable image it replaced."""
    image = bpy.data.images.get("libsm64_mario_texture")
    if image is None:
        return _new_mario_texture_image(), None

    try:
        if getattr(image, "packed_file", None) is not None:
            # image.pack() changes GENERATED images to FILE-backed packed images.
            # Remove that old encoded payload before replacing the pixel buffer.
            image.unpack(method='REMOVE')
        image.source = 'GENERATED'
        image.generated_type = 'BLANK'
        image.generated_width = SM64_TEXTURE_WIDTH
        image.generated_height = SM64_TEXTURE_HEIGHT
        if tuple(image.size) != (SM64_TEXTURE_WIDTH, SM64_TEXTURE_HEIGHT):
            image.scale(SM64_TEXTURE_WIDTH, SM64_TEXTURE_HEIGHT)
        if (not image.has_data or
                len(image.pixels) != SM64_TEXTURE_WIDTH * SM64_TEXTURE_HEIGHT * 4):
            raise RuntimeError("Mario texture image has no usable pixel buffer")
        return image, None
    except (RuntimeError, TypeError, ValueError):
        # Preserve an image used elsewhere, but free the canonical name so the
        # shared Mario material can be repaired to reference a valid replacement.
        image.name = "libsm64_mario_texture_invalid"
        return _new_mario_texture_image(), image


def initialize_texture_image(texture_buffer):
    """Write, update, pack, and verify the shared ROM-generated Mario texture."""
    expected_length = SM64_TEXTURE_WIDTH * SM64_TEXTURE_HEIGHT * 4
    if len(texture_buffer) != expected_length:
        raise ValueError(
            "Mario texture buffer has {} bytes; expected {}".format(
                len(texture_buffer), expected_length
            )
        )

    image, replaced_image = _prepare_mario_texture_image()
    pixels = array('f', (float(channel) / 255.0 for channel in texture_buffer))
    image.alpha_mode = 'STRAIGHT'
    image.file_format = 'PNG'
    image.pixels.foreach_set(pixels)
    image.update()

    # Packing encodes the current generated buffer into the .blend. It must
    # happen after update(), otherwise Blender can preserve the previous/black
    # encoded payload even though the in-memory pixels look correct.
    image.pack()
    image.update()
    if getattr(image, "packed_file", None) is None:
        raise RuntimeError("Blender did not pack the generated Mario texture")
    if tuple(image.size) != (SM64_TEXTURE_WIDTH, SM64_TEXTURE_HEIGHT):
        raise RuntimeError("Packed Mario texture has invalid dimensions")

    verified_pixels = array('f', [0.0]) * expected_length
    image.pixels.foreach_get(verified_pixels)
    if any(abs(actual - expected) > 1e-6
           for actual, expected in zip(verified_pixels, pixels)):
        raise RuntimeError("Packed Mario texture does not match the ROM pixel buffer")
    return image, replaced_image


def initialize_all_data(texture_buffer):
    image, replaced_image = initialize_texture_image(texture_buffer)

    if 'libsm64_mario_material' in bpy.data.materials:
        mat = bpy.data.materials["libsm64_mario_material"]
    else:
        mat = bpy.data.materials.new(name="libsm64_mario_material")
        mat.use_nodes = True
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        nodes.clear()
        tex_node = nodes.new(type='ShaderNodeTexImage')
        tex_node.image = bpy.data.images.get("libsm64_mario_texture")
        color_node = nodes.new(type='ShaderNodeVertexColor')
        color_node.layer_name = 'Col'
        color_node.location = [0, 100]
        mix_node = nodes.new(type='ShaderNodeMix')
        mix_node.data_type = 'RGBA'
        mix_node.location = [250, 0]
        diffuse_node = nodes.new(type='ShaderNodeBsdfDiffuse')
        diffuse_node.location = [500, 0]
        out_node = nodes.new(type='ShaderNodeOutputMaterial')
        out_node.location = [750, 0]
        links.new(tex_node.outputs[0], mix_node.inputs[7])
        links.new(tex_node.outputs[1], mix_node.inputs[0])
        links.new(color_node.outputs[0], mix_node.inputs[6])
        links.new(mix_node.outputs[2], diffuse_node.inputs[0])
        links.new(diffuse_node.outputs[0], out_node.inputs[0])

    # Existing baked objects share this material. Repairing its image node once
    # therefore repairs every take without duplicating texture datablocks.
    if mat.use_nodes and mat.node_tree:
        for node in mat.node_tree.nodes:
            if node.type == 'TEX_IMAGE' and (
                    node.image is replaced_image or
                    node.image is None or
                    node.image.name.startswith("libsm64_mario_texture")):
                node.image = image

    if replaced_image is not None and replaced_image.users == 0:
        bpy.data.images.remove(replaced_image)

    if not ('libsm64_mario_mesh' in bpy.data.meshes):
        mesh = bpy.data.meshes.new('libsm64_mario_mesh')
        mesh.vertex_colors.new()
        verts = []
        edges = []
        faces = []
        for i in range(SM64_GEO_MAX_TRIANGLES):
            verts.append((0,0,0))
            verts.append((0,0,0))
            verts.append((0,0,0))
            edges.append((3*i+0, 3*i+1))
            edges.append((3*i+1, 3*i+2))
            edges.append((3*i+2, 3*i+0))
            faces.append((3*i+0, 3*i+1, 3*i+2))
        mesh.from_pydata(verts, edges, faces)
        mesh.uv_layers.active = mesh.uv_layers.new(name="uv0")
        mesh.materials.append(mat)

def update_mesh_data(mesh: bpy.types.Mesh):
    global mario_geo
    _invalidate_mesh_coordinate_cache()
    _active_mario_vertex_count(mesh)
    vcol = mesh.vertex_colors.active
    for i in range(mario_geo.numTrianglesUsed):
        for corner in range(3):
            position_offset = 9 * i + 3 * corner
            mesh.vertices[3 * i + corner].co = native_position_to_blender(
                mario_geo.position_data[position_offset],
                mario_geo.position_data[position_offset + 1],
                mario_geo.position_data[position_offset + 2],
            )
        mesh.uv_layers.active.data[mesh.loops[3*i+0].index].uv = (mario_geo.uv_data[6*i+0], mario_geo.uv_data[6*i+1])
        mesh.uv_layers.active.data[mesh.loops[3*i+1].index].uv = (mario_geo.uv_data[6*i+2], mario_geo.uv_data[6*i+3])
        mesh.uv_layers.active.data[mesh.loops[3*i+2].index].uv = (mario_geo.uv_data[6*i+4], mario_geo.uv_data[6*i+5])
        vcol.data[3*i+0].color = (
            mario_geo.color_data[9*i+0],
            mario_geo.color_data[9*i+1],
            mario_geo.color_data[9*i+2],
            1.0
        )
        vcol.data[3*i+1].color = (
            mario_geo.color_data[9*i+3],
            mario_geo.color_data[9*i+4],
            mario_geo.color_data[9*i+5],
            1.0
        )
        vcol.data[3*i+2].color = (
            mario_geo.color_data[9*i+6],
            mario_geo.color_data[9*i+7],
            mario_geo.color_data[9*i+8],
            1.0
        )
    mesh.update()

def update_mesh_data_fast(mesh: bpy.types.Mesh):
    active_vertex_count = _active_mario_vertex_count(mesh)
    coordinates = _coordinate_buffer_for_mesh(mesh)
    _write_active_mario_coordinates(coordinates, active_vertex_count)
    mesh.vertices.foreach_set("co", coordinates)
    mesh.update()
