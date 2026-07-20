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
import struct
import threading
import time
import uuid
from collections import deque
from typing import cast, List
from . collision_types import COLLISION_TYPES
from .collision_cache import (
    CHUNK_SIZE_BLENDER,
    COLLISION_ROLE_EXCLUDED as _COLLISION_ROLE_EXCLUDED,
    COLLISION_ROLE_MOVING_PLATFORM as _COLLISION_ROLE_MOVING_PLATFORM,
    COLLISION_ROLE_STATIC as _COLLISION_ROLE_STATIC,
    CollisionCache,
    collision_role,
    chunk_coordinate,
    native_chunk_payload,
    plan_transition,
)
from . recording import (
    MarioRuntimeMetadata, RUNTIME_METADATA_SCHEMA_VERSION, recorder,
    timeline_playback,
)
from .audio_runtime import AudioBackendError, LiveAudioRuntime

COLLISION_ROLE_STATIC = _COLLISION_ROLE_STATIC
COLLISION_ROLE_MOVING_PLATFORM = _COLLISION_ROLE_MOVING_PLATFORM
COLLISION_ROLE_EXCLUDED = _COLLISION_ROLE_EXCLUDED

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
START_MARK_SCHEMA_VERSION = 1
START_MARK_SAFE = "SAFE"
START_MARK_PERFORMANCE = "PERFORMANCE"
START_MARK_RESTORATION_MODES = (START_MARK_SAFE, START_MARK_PERFORMANCE)
START_MARK_OBSERVED_ONLY_FIELDS = ("particle_flags",)
ENVIRONMENT_WATER = "water"
ENVIRONMENT_GAS = "gas"
ENVIRONMENT_LEVEL_KINDS = (ENVIRONMENT_WATER, ENVIRONMENT_GAS)
# The pinned decomp's find_water_level() and find_poison_gas_level() both
# return -10000.0f when no environment region is present.  The public setters
# accept signed int levels, so this is the exact canonical disabled value.
ENVIRONMENT_DISABLED_NATIVE_LEVEL = -10000
# Exact src/decomp/include/sm64.h values at PINNED_LIBSM64_COMMIT.
MARIO_VANISH_CAP = 0x00000002
MARIO_METAL_CAP = 0x00000004
MARIO_WING_CAP = 0x00000008
SUPPORTED_CAP_FLAGS = (MARIO_WING_CAP, MARIO_METAL_CAP, MARIO_VANISH_CAP)
MARIO_SPECIAL_CAP_MASK = MARIO_WING_CAP | MARIO_METAL_CAP | MARIO_VANISH_CAP
CAP_REQUEST_HISTORY_LIMIT = 32
AUDIO_SOUND_EVENT_HISTORY_LIMIT = 256
NATIVE_DEBUG_LOG_LIMIT = 256
# Exact FLOOR_LOWER_LIMIT from the pinned decomp's surface_collision.h.
NO_FLOOR_NATIVE_HEIGHT = -110000.0

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
SURFACE_KIND_STATIC_CHUNK = "STATIC_CHUNK"
SURFACE_KIND_MOVING_PLATFORM = "MOVING_PLATFORM"
SURFACE_KIND_OPTIONAL_DEBUG = "OPTIONAL_DEBUG_SURFACE"
PLATFORM_TRANSFORM_TOLERANCE = 1.0e-6
PLATFORM_ROTATION_TOLERANCE = 1.0e-7

origin_offset = [0.0, 0.0, 0.0]
original_fps = 0
original_fps_setting = 0
original_fps_base = 1.0
original_cursor_pos = [0.0, 0.0, 0.0]
simulation_scene = None
RUNTIME_API_VERSION = 9

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


SM64PlaySoundFunctionPtr = ct.CFUNCTYPE(
    None, ct.c_uint32, ct.POINTER(ct.c_float)
)
SM64DebugPrintFunctionPtr = ct.CFUNCTYPE(None, ct.c_char_p)

class MarioLifecycleError(RuntimeError):
    pass


class MarioFeatureUnavailableError(RuntimeError):
    """A recoverable optional-feature failure that leaves native ownership intact."""


def _require_integer_range(name, value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("{} must be an integer".format(name))
    if value < minimum or value > maximum:
        raise ValueError(
            "{} must be between {} and {}".format(name, minimum, maximum)
        )
    return value


def _require_finite_float(name, value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("{} must be a finite number".format(name)) from exc
    if not math.isfinite(result):
        raise ValueError("{} must be a finite number".format(name))
    return result


def _require_float3(name, value):
    if not isinstance(value, tuple) or len(value) != 3:
        raise ValueError("{} must be a three-value tuple".format(name))
    for index, component in enumerate(value):
        _require_finite_float("{}[{}]".format(name, index), component)
    return value


@dataclass(frozen=True)
class MarioStartMark:
    """Immutable, generation-owned snapshot of public modern Mario state.

    ``particle_flags`` is retained for observation and diagnostics only because
    the pinned ABI does not expose a safe setter for it.
    """

    schema_version: int
    owner_token: str
    lifecycle_generation: int
    position: tuple
    velocity: tuple
    face_angle: float
    forward_velocity: float
    health: int
    action: int
    anim_id: int
    anim_frame: int
    flags: int
    particle_flags: int
    invincibility_timer: int

    def __post_init__(self):
        if self.schema_version != START_MARK_SCHEMA_VERSION:
            raise ValueError(
                "Unsupported Start Mark schema {}; expected {}".format(
                    self.schema_version, START_MARK_SCHEMA_VERSION
                )
            )
        if not isinstance(self.owner_token, str) or not self.owner_token:
            raise ValueError("Start Mark owner token is invalid")
        _require_integer_range(
            "lifecycle_generation", self.lifecycle_generation, 1, 0x7FFFFFFF
        )
        _require_float3("position", self.position)
        _require_float3("velocity", self.velocity)
        _require_finite_float("face_angle", self.face_angle)
        _require_finite_float("forward_velocity", self.forward_velocity)
        _require_integer_range("health", self.health, 0, 0x7FFF)
        _require_integer_range("action", self.action, 0, 0xFFFFFFFF)
        _require_integer_range("anim_id", self.anim_id, -0x80000000, 0x7FFFFFFF)
        _require_integer_range("anim_frame", self.anim_frame, -0x8000, 0x7FFF)
        _require_integer_range("flags", self.flags, 0, 0xFFFFFFFF)
        _require_integer_range("particle_flags", self.particle_flags, 0, 0xFFFFFFFF)
        _require_integer_range(
            "invincibility_timer", self.invincibility_timer, -0x8000, 0x7FFF
        )


@dataclass(frozen=True)
class MarioCapRequest:
    operation: str
    cap_flag: int
    duration_ticks: int
    play_music: bool
    simulation_tick: int

    def __post_init__(self):
        if self.operation not in ("grant", "extend", "clear"):
            raise ValueError("Unknown cap request operation: {}".format(self.operation))
        if self.operation == "grant" and self.cap_flag not in SUPPORTED_CAP_FLAGS:
            raise ValueError("Unsupported cap flag: 0x{:08X}".format(self.cap_flag))
        if self.operation in ("extend", "clear") and self.cap_flag != 0:
            raise ValueError("Cap {} history must use cap flag 0".format(self.operation))
        if self.operation == "clear" and self.duration_ticks != 0:
            raise ValueError("Cap clear history must use duration 0")
        minimum = 1 if self.operation == "extend" else 0
        _require_integer_range("duration_ticks", self.duration_ticks, minimum, 0xFFFF)
        if not isinstance(self.play_music, bool):
            raise ValueError("play_music must be a bool")
        _require_integer_range("simulation_tick", self.simulation_tick, 0, 0x7FFFFFFF)


@dataclass(frozen=True)
class NativeDebugMessage:
    owner_token: str
    lifecycle_generation: int
    text: str
    monotonic_time: float

    def __post_init__(self):
        if not self.owner_token or not isinstance(self.owner_token, str):
            raise ValueError("Debug message owner token is invalid")
        _require_integer_range(
            "debug lifecycle generation", self.lifecycle_generation, 1, 0x7FFFFFFF
        )
        if not isinstance(self.text, str):
            raise ValueError("Debug message text must be a string")
        _require_finite_float("debug message time", self.monotonic_time)


@dataclass(frozen=True)
class CollisionProbeResult:
    blender_position: tuple
    native_position: tuple
    floor_native_height: float
    floor_blender_height: object
    no_floor: bool
    water_native_height: float
    water_blender_height: object
    gas_native_height: float
    gas_blender_height: object
    chunk_key: tuple
    chunk_active: bool
    nearby_static_surface_count: int
    active_static_surface_count: int
    active_moving_platform_surface_count: int

    def __post_init__(self):
        _require_float3("probe Blender position", self.blender_position)
        _require_float3("probe native position", self.native_position)
        for name, value in (
            ("floor native height", self.floor_native_height),
            ("water native height", self.water_native_height),
            ("gas native height", self.gas_native_height),
        ):
            _require_finite_float(name, value)
        for name, value in (
            ("floor Blender height", self.floor_blender_height),
            ("water Blender height", self.water_blender_height),
            ("gas Blender height", self.gas_blender_height),
        ):
            if value is not None:
                _require_finite_float(name, value)
        if not isinstance(self.chunk_key, tuple) or len(self.chunk_key) != 2:
            raise ValueError("Probe chunk key must contain two integers")
        for value in self.chunk_key:
            _require_integer_range("probe chunk coordinate", value, -0x80000000, 0x7FFFFFFF)
        for name, value in (
            ("nearby surface count", self.nearby_static_surface_count),
            ("active static surface count", self.active_static_surface_count),
            ("moving platform surface count", self.active_moving_platform_surface_count),
        ):
            _require_integer_range(name, value, 0, 0x7FFFFFFF)


@dataclass
class NativeSurfaceOwnership:
    chunk_key: tuple
    owner_token: str
    generation: int
    surface_count: int
    ownership_kind: str = SURFACE_KIND_STATIC_CHUNK
    state: str = SURFACE_PREPARED
    object_id: object = None
    creation_order: int = 0
    diagnostic: str = ""


@dataclass(frozen=True)
class NativePlatformTransform:
    position: tuple
    euler_rotation_degrees: tuple
    rotation_matrix: tuple
    blender_scale: tuple

    def __post_init__(self):
        _require_float3("platform position", self.position)
        _require_float3("platform Euler rotation", self.euler_rotation_degrees)
        _require_float3("platform scale", self.blender_scale)
        if len(self.rotation_matrix) != 9:
            raise ValueError("Platform rotation matrix must contain nine values")
        for index, value in enumerate(self.rotation_matrix):
            _require_finite_float("platform rotation[{}]".format(index), value)
        if any(abs(float(value)) <= 1.0e-12 for value in self.blender_scale):
            raise ValueError("Moving Platform scale cannot contain zero")


@dataclass
class MovingPlatformOwnership:
    object_key: tuple
    object_name: str
    owner_token: str
    generation: int
    surface_count: int
    geometry_fingerprint: str
    initial_scale: tuple
    previous_transform: object
    current_transform: object
    state: str = SURFACE_PREPARED
    object_id: object = None
    creation_order: int = 0
    last_updated_tick: int = -1
    diagnostic: str = ""
    transform_valid: bool = True
    ownership_kind: str = SURFACE_KIND_MOVING_PLATFORM


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
        self.start_mark_restoration_mode = START_MARK_PERFORMANCE
        self.last_start_mark_restoration = None
        self.environment_levels = {
            ENVIRONMENT_WATER: {
                "enabled": False, "blender_height": None,
                "native_level": ENVIRONMENT_DISABLED_NATIVE_LEVEL,
            },
            ENVIRONMENT_GAS: {
                "enabled": False, "blender_height": None,
                "native_level": ENVIRONMENT_DISABLED_NATIVE_LEVEL,
            },
        }
        self.last_environment_error = ""
        self.cap_request_history = []
        self.last_cap_error = ""
        self.force_full_mesh_updates = 0
        self.optional_api_features = set()
        self.native_call_lock = threading.RLock()
        self.audio_runtime = LiveAudioRuntime()
        self.rom_image = None
        self.play_sound_callback = None
        self.play_sound_callback_registered = False
        self.sound_events = deque(maxlen=AUDIO_SOUND_EVENT_HISTORY_LIMIT)
        self.debug_print_callback = None
        self.debug_print_callback_registered = False
        self.native_debug_log = deque(maxlen=NATIVE_DEBUG_LOG_LIMIT)
        self.last_debug_error = ""
        self.last_collision_query = None
        self.last_directing_operation = None
        self.last_directing_error = ""
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
        self.moving_platform_objects = {}
        self.native_surface_id_registry = {}
        self.disabled_moving_platforms = {}
        self.dirty_moving_platforms = set()
        self.initial_collision_roles = {}
        self.active_chunk_keys = set()
        self.collision_center = None
        self.pending_transition_state = "idle"
        self.surface_create_count = 0
        self.surface_delete_count = 0
        self.active_surface_count = 0
        self.moving_platform_surface_count = 0
        self.platform_move_count = 0
        self.last_platform_error = ""
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


def blender_position_to_native(position, scale=None, session_origin=None):
    """Convert Blender world XYZ to libsm64 XYZ through one central mapping."""
    scale = float(SM64_SCALE_FACTOR if scale is None else scale)
    session_origin = origin_offset if session_origin is None else session_origin
    if not math.isfinite(scale) or scale <= 0.0:
        raise ValueError("Blender-to-SM64 scale must be finite and positive")
    _require_float3("Blender position", tuple(position))
    _require_float3("session origin", tuple(session_origin))
    return (
        scale * (float(position[0]) - float(session_origin[0])),
        scale * (float(position[2]) - float(session_origin[2])),
        scale * (-float(position[1]) + float(session_origin[1])),
    )


def blender_height_to_native_level(height, scale=None, session_origin=None):
    """Convert a Blender world-Z level to libsm64's signed native Y level."""
    height = _require_finite_float("environment height", height)
    scale = float(SM64_SCALE_FACTOR if scale is None else scale)
    session_origin = origin_offset if session_origin is None else session_origin
    native_y = blender_position_to_native(
        (float(session_origin[0]), float(session_origin[1]), height),
        scale,
        session_origin,
    )[1]
    native_level, valid = _checked_int32_coordinate(native_y)
    if not valid:
        raise ValueError("Environment height is outside libsm64's signed 32-bit range")
    return native_level


def native_height_to_blender(native_height, scale=None, session_origin=None):
    """Convert a native vertical Y height to Blender world Z."""
    native_height = _require_finite_float("native height", native_height)
    scale = float(SM64_SCALE_FACTOR if scale is None else scale)
    session_origin = origin_offset if session_origin is None else session_origin
    if not math.isfinite(scale) or scale <= 0.0:
        raise ValueError("Blender-to-SM64 scale must be finite and positive")
    _require_float3("session origin", tuple(session_origin))
    return float(session_origin[2]) + native_height / scale


def blender_local_vector_to_native(vector, scale=None):
    """Map a Blender-local vector without applying the session origin."""
    scale = float(SM64_SCALE_FACTOR if scale is None else scale)
    vector = tuple(vector)
    _require_float3("Blender local vector", vector)
    if not math.isfinite(scale) or scale <= 0.0:
        raise ValueError("Blender-to-SM64 scale must be finite and positive")
    return (
        scale * float(vector[0]),
        scale * float(vector[2]),
        -scale * float(vector[1]),
    )


def blender_rotation_matrix_to_native(rotation_matrix):
    """Conjugate a Blender XYZ rotation into native X/Y/-Z coordinates."""
    basis = mathutils.Matrix(((1.0, 0.0, 0.0),
                              (0.0, 0.0, 1.0),
                              (0.0, -1.0, 0.0)))
    matrix = mathutils.Matrix(rotation_matrix).to_3x3()
    values = tuple(float(value) for row in matrix for value in row)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Moving Platform rotation contains non-finite values")
    return basis @ matrix @ basis.transposed()


def native_transform_matrix_to_zxy_euler_degrees(native_rotation_matrix):
    """Convert a native rotation matrix to libsm64's ZXY Euler degrees."""
    matrix = mathutils.Matrix(native_rotation_matrix).to_3x3()
    euler = matrix.to_euler('ZXY')
    degrees = tuple(math.degrees(value) for value in (euler.x, euler.y, euler.z))
    _require_float3("native ZXY Euler degrees", degrees)
    return degrees


def native_zxy_euler_degrees_to_matrix(euler_degrees):
    """Reconstruct a native matrix for conversion tests and diagnostics."""
    euler_degrees = tuple(euler_degrees)
    _require_float3("native ZXY Euler degrees", euler_degrees)
    radians = tuple(math.radians(value) for value in euler_degrees)
    return mathutils.Euler(radians, 'ZXY').to_matrix()


def blender_matrix_to_native_transform(matrix_world, scale=None, session_origin=None):
    """Decompose one evaluated Blender matrix into a rigid native transform.

    Scale is returned separately for baking into local vertices. Shear or a
    non-finite/non-invertible decomposition is rejected rather than silently
    generating incorrect collision.
    """
    matrix = mathutils.Matrix(matrix_world).to_4x4()
    values = tuple(float(value) for row in matrix for value in row)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Moving Platform matrix contains non-finite values")
    location, rotation, blender_scale = matrix.decompose()
    scale_tuple = tuple(float(value) for value in blender_scale)
    _require_float3("Moving Platform scale", scale_tuple)
    if any(abs(value) <= 1.0e-12 for value in scale_tuple):
        raise ValueError("Moving Platform transform has zero scale")
    reconstructed = mathutils.Matrix.LocRotScale(location, rotation, blender_scale)
    largest = max(1.0, *(abs(value) for value in values))
    difference = max(
        abs(float(matrix[row][column]) - float(reconstructed[row][column]))
        for row in range(4) for column in range(4)
    )
    if difference > PLATFORM_TRANSFORM_TOLERANCE * largest:
        raise ValueError("Moving Platform transform contains unsupported shear")
    native_rotation = blender_rotation_matrix_to_native(rotation.to_matrix())
    rotation_values = tuple(float(value) for row in native_rotation for value in row)
    return NativePlatformTransform(
        position=blender_position_to_native(
            tuple(float(value) for value in location), scale, session_origin
        ),
        euler_rotation_degrees=native_transform_matrix_to_zxy_euler_degrees(
            native_rotation
        ),
        rotation_matrix=rotation_values,
        blender_scale=scale_tuple,
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


def _release_recording_timeline_playback(session=None):
    """Release playback started for recording without disrupting cleanup."""
    try:
        return timeline_playback.release()
    except Exception as exc:
        _lifecycle_log(
            session or _lifecycle,
            "timeline playback cleanup failed: {}".format(exc),
        )
        return False


def _native_call(session, export_name, *arguments):
    """Serialize one internal libsm64 call for this lifecycle generation.

    Public operations still pass through :func:`require_owned_mario_operation`.
    This lower-level boundary also covers startup and shutdown calls, which
    deliberately occur before commit or after shutdown begins.
    """
    library = session.library
    if library is None:
        raise MarioLifecycleError(
            "Native call {} rejected without an owned library".format(export_name)
        )
    with session.native_call_lock:
        return getattr(library, export_name)(*arguments)


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
        "start_mark_schema_version": START_MARK_SCHEMA_VERSION,
        "start_mark_restoration_mode": session.start_mark_restoration_mode,
        "last_start_mark_restoration": session.last_start_mark_restoration,
        "environment_levels": {
            kind: dict(values) for kind, values in session.environment_levels.items()
        },
        "last_environment_error": session.last_environment_error,
        "cap_request_history": tuple(session.cap_request_history),
        "last_cap_error": session.last_cap_error,
        "force_full_mesh_updates": session.force_full_mesh_updates,
        "audio": session.audio_runtime.snapshot(),
        "play_sound_callback_registered": session.play_sound_callback_registered,
        "sound_event_count": len(session.sound_events),
        "debug_print_callback_registered": session.debug_print_callback_registered,
        "native_debug_log": tuple(session.native_debug_log),
        "last_debug_error": session.last_debug_error,
        "last_collision_query": session.last_collision_query,
        "last_directing_operation": session.last_directing_operation,
        "last_directing_error": session.last_directing_error,
        "optional_api_features": tuple(sorted(session.optional_api_features)),
        "native_ownership_uncertain": session.native_ownership_uncertain,
        "shutdown_in_progress": session.shutdown_in_progress,
        "shutdown_complete": session.shutdown_complete,
        "session_committed": session.session_committed,
        "active_native_surface_object_count": sum(
            1 for record in session.native_surface_objects.values()
            if record.object_id is not None and record.state in (
                SURFACE_CREATED, SURFACE_DELETE_ATTEMPTED, SURFACE_FAILED,
            )
        ) + sum(
            1 for record in session.moving_platform_objects.values()
            if record.object_id is not None and record.state in (
                SURFACE_CREATED, SURFACE_DELETE_ATTEMPTED, SURFACE_FAILED,
            )
        ),
        "owned_surface_ids": tuple(sorted(
            int(object_id) for object_id in session.native_surface_id_registry
        )),
        "moving_platform_count": len(session.moving_platform_objects),
        "moving_platform_ids": tuple(sorted(
            int(record.object_id) for record in session.moving_platform_objects.values()
            if record.object_id is not None
        )),
        "moving_platform_surface_count": session.moving_platform_surface_count,
        "platform_move_count": session.platform_move_count,
        "last_platform_error": session.last_platform_error,
        "disabled_moving_platforms": tuple(sorted(
            (repr(key), message)
            for key, message in session.disabled_moving_platforms.items()
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


def _configure_start_mark_api(library):
    """Lazily bind only the setters used by Better Start Marks.

    These exports are intentionally not prerequisites for basic Live Mario
    startup. A mismatched library disables rich restoration when invoked while
    leaving the already-owned core session usable.
    """
    if getattr(library, "_libsm64_start_mark_api_configured", False):
        return
    exports = (
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
    )
    missing = [name for name in exports if not hasattr(library, name)]
    if missing:
        raise MarioFeatureUnavailableError(
            "Better Start Mark restoration is unavailable because the native "
            "library is missing: {}. Live Mario remains usable.".format(
                ", ".join(missing)
            )
        )

    library.sm64_set_mario_action.argtypes = [ct.c_int32, ct.c_uint32]
    library.sm64_set_mario_action.restype = None
    library.sm64_set_mario_animation.argtypes = [ct.c_int32, ct.c_int32]
    library.sm64_set_mario_animation.restype = None
    library.sm64_set_mario_anim_frame.argtypes = [ct.c_int32, ct.c_int16]
    library.sm64_set_mario_anim_frame.restype = None
    library.sm64_set_mario_state.argtypes = [ct.c_int32, ct.c_uint32]
    library.sm64_set_mario_state.restype = None
    library.sm64_set_mario_position.argtypes = [
        ct.c_int32, ct.c_float, ct.c_float, ct.c_float,
    ]
    library.sm64_set_mario_position.restype = None
    library.sm64_set_mario_faceangle.argtypes = [ct.c_int32, ct.c_float]
    library.sm64_set_mario_faceangle.restype = None
    library.sm64_set_mario_velocity.argtypes = [
        ct.c_int32, ct.c_float, ct.c_float, ct.c_float,
    ]
    library.sm64_set_mario_velocity.restype = None
    library.sm64_set_mario_forward_velocity.argtypes = [ct.c_int32, ct.c_float]
    library.sm64_set_mario_forward_velocity.restype = None
    library.sm64_set_mario_health.argtypes = [ct.c_int32, ct.c_uint16]
    library.sm64_set_mario_health.restype = None
    library.sm64_set_mario_invincibility.argtypes = [ct.c_int32, ct.c_int16]
    library.sm64_set_mario_invincibility.restype = None
    library._libsm64_start_mark_api_configured = True


def _environment_export_name(kind):
    if kind == ENVIRONMENT_WATER:
        return "sm64_set_mario_water_level"
    if kind == ENVIRONMENT_GAS:
        return "sm64_set_mario_gas_level"
    raise ValueError("Unknown environment level kind: {}".format(kind))


def _configure_environment_level_api(library, kind):
    """Lazily bind one optional global-level setter.

    Water and gas are configured independently so a disabled or unavailable
    sibling feature cannot prevent the other feature, or basic Live Mario, from
    running.
    """
    export_name = _environment_export_name(kind)
    configured_flag = "_libsm64_{}_api_configured".format(kind)
    if getattr(library, configured_flag, False):
        return
    if not hasattr(library, export_name):
        raise MarioFeatureUnavailableError(
            "{} controls are unavailable because the native library is missing "
            "{}. Live Mario remains usable.".format(kind.title(), export_name)
        )
    setter = getattr(library, export_name)
    setter.argtypes = [ct.c_int32, ct.c_int]
    setter.restype = None
    setattr(library, configured_flag, True)


def _scene_environment_request(scene, kind):
    if kind not in ENVIRONMENT_LEVEL_KINDS:
        raise ValueError("Unknown environment level kind: {}".format(kind))
    settings = getattr(scene, "libsm64", None)
    if settings is None:
        return False, None, ENVIRONMENT_DISABLED_NATIVE_LEVEL
    enabled_name = "enable_water" if kind == ENVIRONMENT_WATER else "enable_poison_gas"
    enabled = bool(getattr(settings, enabled_name))
    blender_height = _require_finite_float(
        "{} height".format(kind), getattr(settings, "{}_height".format(kind))
    )
    native_level = (
        blender_height_to_native_level(blender_height)
        if enabled else ENVIRONMENT_DISABLED_NATIVE_LEVEL
    )
    return enabled, blender_height, native_level


def _set_environment_level_for_session(session, scene, kind, include_disabled=True):
    enabled, blender_height, native_level = _scene_environment_request(scene, kind)
    if not enabled and not include_disabled:
        # A freshly created Mario already has the pinned decomp's canonical
        # -10000 value, so disabled optional exports need not be required.
        session.environment_levels[kind] = {
            "enabled": False,
            "blender_height": blender_height,
            "native_level": ENVIRONMENT_DISABLED_NATIVE_LEVEL,
        }
        return native_level
    _configure_environment_level_api(session.library, kind)
    _native_call(
        session,
        _environment_export_name(kind),
        int(session.mario_id),
        int(native_level),
    )
    session.optional_api_features.add("{}_level".format(kind))
    session.environment_levels[kind] = {
        "enabled": enabled,
        "blender_height": blender_height,
        "native_level": native_level,
    }
    session.last_environment_error = ""
    return native_level


def _apply_environment_after_mario_create(session):
    """Apply only enabled scene levels before the replacement Mario's first tick."""
    scene = session.scene
    if scene is None:
        return
    for kind in ENVIRONMENT_LEVEL_KINDS:
        try:
            _set_environment_level_for_session(
                session, scene, kind, include_disabled=False
            )
        except MarioFeatureUnavailableError as exc:
            # Optional export absence is recoverable and does not invalidate the
            # newly created Mario or any native ownership.
            session.last_environment_error = str(exc)
            print("libsm64 environment feature unavailable: {}".format(exc))
        except Exception as exc:
            session.native_ownership_uncertain = True
            raise MarioLifecycleError(
                "Could not apply the enabled {} level after Mario creation: {}".format(
                    kind, exc
                )
            ) from exc


def apply_scene_environment_level(scene, kind):
    """Apply one UI level to an exactly owned Mario without replacing him.

    Returns False when no Live Mario exists, which keeps saved environment
    settings independent from already-baked playback.
    """
    session = _lifecycle
    if not is_mario_running():
        return False
    try:
        session = require_owned_mario_operation(session)
        _set_environment_level_for_session(session, scene, kind, include_disabled=True)
    except (ValueError, MarioFeatureUnavailableError) as exc:
        session.last_environment_error = str(exc)
        print("libsm64 environment setting rejected: {}".format(exc))
        return False
    except Exception as exc:
        _poison_session(
            session,
            "Native {} level update failed; use End Studio Session and restart "
            "Blender: {}".format(kind, exc),
        )
        session.last_environment_error = session.last_error
        return False
    return True


def environment_diagnostics():
    session = _lifecycle
    return {
        "disabled_native_level": ENVIRONMENT_DISABLED_NATIVE_LEVEL,
        "levels": {
            kind: dict(values) for kind, values in session.environment_levels.items()
        },
        "last_error": session.last_environment_error,
    }


def _configure_cap_api(library):
    """Lazily bind cap grant/extension without affecting core startup."""
    if getattr(library, "_libsm64_cap_api_configured", False):
        return
    exports = ("sm64_mario_interact_cap", "sm64_mario_extend_cap")
    missing = [name for name in exports if not hasattr(library, name)]
    if missing:
        raise MarioFeatureUnavailableError(
            "Cap controls are unavailable because the native library is missing: "
            "{}. Live Mario remains usable.".format(", ".join(missing))
        )
    library.sm64_mario_interact_cap.argtypes = [
        ct.c_int32, ct.c_uint32, ct.c_uint16, ct.c_uint8,
    ]
    library.sm64_mario_interact_cap.restype = None
    library.sm64_mario_extend_cap.argtypes = [ct.c_int32, ct.c_uint16]
    library.sm64_mario_extend_cap.restype = None
    library._libsm64_cap_api_configured = True


def _record_cap_request(session, request):
    session.cap_request_history.append(request)
    if len(session.cap_request_history) > CAP_REQUEST_HISTORY_LIMIT:
        del session.cap_request_history[:-CAP_REQUEST_HISTORY_LIMIT]
    session.last_cap_error = ""


def grant_mario_cap(cap_flag, duration_ticks=0, play_music=False):
    """Grant one pinned special cap to the exactly owned Live Mario.

    A duration of zero deliberately reaches native code unchanged: the pinned
    implementation selects 600 ticks for Metal/Vanish and 1800 for Wing.
    """
    cap_flag = _require_integer_range("cap_flag", cap_flag, 0, 0xFFFFFFFF)
    if cap_flag not in SUPPORTED_CAP_FLAGS:
        raise ValueError("Unsupported cap flag: 0x{:08X}".format(cap_flag))
    duration_ticks = _require_integer_range(
        "duration_ticks", duration_ticks, 0, 0xFFFF
    )
    if not isinstance(play_music, bool):
        raise ValueError("play_music must be a bool")
    session = require_owned_mario_operation(
        allowed_states=(LIVE_IDLE, RECORDING)
    )
    try:
        _configure_cap_api(session.library)
        _native_call(
            session,
            "sm64_mario_interact_cap",
            int(session.mario_id), cap_flag, duration_ticks, int(play_music),
        )
    except MarioFeatureUnavailableError as exc:
        session.last_cap_error = str(exc)
        raise
    except Exception as exc:
        _poison_session(
            session,
            "Native cap grant failed; use End Studio Session and restart Blender: {}".format(
                exc
            ),
        )
        session.last_cap_error = session.last_error
        raise MarioLifecycleError(session.last_error) from exc
    session.optional_api_features.add("cap_controls")
    # Cap geometry can change UV/color as well as positions. Force the full
    # live mesh path after a grant even when the usual startup-only UV/color
    # refresh window has elapsed, so a subsequently baked single-cap take owns
    # the visible cap state present on its source mesh.
    session.force_full_mesh_updates = max(session.force_full_mesh_updates, 2)
    _record_cap_request(
        session,
        MarioCapRequest(
            "grant", cap_flag, duration_ticks, play_music, max(0, int(tick_count))
        ),
    )
    return cap_flag


def extend_mario_cap(duration_ticks):
    duration_ticks = _require_integer_range(
        "duration_ticks", duration_ticks, 1, 0xFFFF
    )
    session = require_owned_mario_operation(
        allowed_states=(LIVE_IDLE, RECORDING)
    )
    try:
        _configure_cap_api(session.library)
        _native_call(
            session,
            "sm64_mario_extend_cap",
            int(session.mario_id), duration_ticks,
        )
    except MarioFeatureUnavailableError as exc:
        session.last_cap_error = str(exc)
        raise
    except Exception as exc:
        _poison_session(
            session,
            "Native cap extension failed; use End Studio Session and restart "
            "Blender: {}".format(exc),
        )
        session.last_cap_error = session.last_error
        raise MarioLifecycleError(session.last_error) from exc
    session.optional_api_features.add("cap_controls")
    _record_cap_request(
        session,
        MarioCapRequest(
            "extend", 0, duration_ticks, False, max(0, int(tick_count))
        ),
    )
    return duration_ticks


def clear_mario_cap():
    """Clear every supported special-cap flag on the owned Live Mario."""
    session = require_owned_mario_operation(
        allowed_states=(LIVE_IDLE, RECORDING)
    )
    cleared_flags = int(mario_state.flags) & ~MARIO_SPECIAL_CAP_MASK
    try:
        _native_call(
            session,
            "sm64_set_mario_state",
            int(session.mario_id), cleared_flags,
        )
    except Exception as exc:
        _poison_session(
            session,
            "Native cap clear failed; use End Studio Session and restart "
            "Blender: {}".format(exc),
        )
        session.last_cap_error = session.last_error
        raise MarioLifecycleError(session.last_error) from exc
    mario_state.flags = cleared_flags
    session.force_full_mesh_updates = max(session.force_full_mesh_updates, 2)
    session.optional_api_features.add("cap_controls")
    _record_cap_request(
        session,
        MarioCapRequest(
            "clear", 0, 0, False, max(0, int(tick_count))
        ),
    )
    return 0


def cap_diagnostics():
    session = _lifecycle
    audio_state = session.audio_runtime.snapshot()
    return {
        "supported_flags": SUPPORTED_CAP_FLAGS,
        "history": tuple(session.cap_request_history),
        "last_error": session.last_cap_error,
        "active_flags": int(mario_state.flags) & MARIO_SPECIAL_CAP_MASK,
        "music_enabled": bool(
            audio_state["audio_worker_started"]
            and audio_state["audio_device_opened"]
            and not audio_state["muted"]
        ),
    }


def _configure_audio_api(library):
    """Lazily bind audio exports without making core startup depend on them."""
    if getattr(library, "_libsm64_audio_api_configured", False):
        return
    exports = ("sm64_audio_init", "sm64_audio_tick")
    missing = [name for name in exports if not hasattr(library, name)]
    if missing:
        raise MarioFeatureUnavailableError(
            "Live audio is unavailable because the native library is missing: "
            "{}. Live Mario remains usable.".format(", ".join(missing))
        )
    library.sm64_audio_init.argtypes = [ct.POINTER(ct.c_uint8)]
    library.sm64_audio_init.restype = None
    library.sm64_audio_tick.argtypes = [
        ct.c_uint32, ct.c_uint32, ct.POINTER(ct.c_int16),
    ]
    library.sm64_audio_tick.restype = ct.c_uint32
    if hasattr(library, "sm64_set_sound_volume"):
        library.sm64_set_sound_volume.argtypes = [ct.c_float]
        library.sm64_set_sound_volume.restype = None
    if hasattr(library, "sm64_register_play_sound_function"):
        library.sm64_register_play_sound_function.argtypes = [
            SM64PlaySoundFunctionPtr,
        ]
        library.sm64_register_play_sound_function.restype = None
    library._libsm64_audio_api_configured = True


def _register_play_sound_callback(session):
    if session.play_sound_callback_registered:
        return
    if not hasattr(session.library, "sm64_register_play_sound_function"):
        return
    owner_token = session.owner_token
    generation = session.generation

    def receive_sound(sound_bits, position):
        # Native callbacks may arrive on the audio worker.  Copy values into a
        # bounded Python deque only; Blender RNA remains main-thread-only.
        try:
            native_position = (
                tuple(float(position[index]) for index in range(3))
                if bool(position) else (0.0, 0.0, 0.0)
            )
            session.sound_events.append((
                owner_token,
                generation,
                int(sound_bits),
                native_position,
                time.monotonic(),
            ))
        except Exception:
            # Exceptions must never unwind through a C callback boundary.
            return

    callback = SM64PlaySoundFunctionPtr(receive_sound)
    _native_call(
        session, "sm64_register_play_sound_function", callback
    )
    session.play_sound_callback = callback
    session.play_sound_callback_registered = True


def _unregister_play_sound_callback(session):
    if not session.play_sound_callback_registered:
        session.play_sound_callback = None
        return
    if session.library is not None and hasattr(
        session.library, "sm64_register_play_sound_function"
    ):
        _native_call(
            session,
            "sm64_register_play_sound_function",
            SM64PlaySoundFunctionPtr(),
        )
    session.play_sound_callback_registered = False
    session.play_sound_callback = None


def _scene_audio_request(scene):
    settings = getattr(scene, "libsm64", None)
    if settings is None:
        return False, 1.0, False
    return (
        bool(getattr(settings, "enable_live_audio", False)),
        _require_finite_float("audio volume", getattr(settings, "audio_volume", 1.0)),
        bool(getattr(settings, "audio_mute", False)),
    )


def start_live_audio(session=None):
    session = require_owned_mario_operation(
        session, allowed_states=(LIVE_IDLE, RECORDING)
    )
    requested, volume, muted = _scene_audio_request(session.scene)
    if not requested:
        return False
    if not 0.0 <= volume <= 1.0:
        raise ValueError("Audio volume must be between 0 and 1")
    if session.rom_image is None:
        raise MarioFeatureUnavailableError(
            "Live audio cannot start because this session no longer owns its ROM image"
        )
    try:
        _configure_audio_api(session.library)
        _register_play_sound_callback(session)
        session.audio_runtime.initialize_and_start(
            session.library,
            session.rom_image,
            session.native_call_lock,
            volume,
            muted,
        )
    except (MarioFeatureUnavailableError, AudioBackendError, ValueError) as exc:
        session.audio_runtime.audio_failure = str(exc)
        session.audio_runtime.audio_requested = False
        try:
            _unregister_play_sound_callback(session)
        except Exception as cleanup_exc:
            session.native_ownership_uncertain = True
            _poison_session(
                session,
                "Audio callback cleanup failed; restart Blender: {}".format(
                    cleanup_exc
                ),
            )
            raise MarioLifecycleError(session.last_error) from cleanup_exc
        print("libsm64 optional audio disabled: {}".format(exc))
        return False
    except Exception as exc:
        if session.audio_runtime.native_failure:
            _poison_session(
                session,
                "Native audio initialization left execution state uncertain; restart "
                "Blender: {}".format(exc),
            )
            raise MarioLifecycleError(session.last_error) from exc
        session.audio_runtime.audio_failure = str(exc)
        session.audio_runtime.audio_requested = False
        _unregister_play_sound_callback(session)
        print("libsm64 optional audio disabled: {}".format(exc))
        return False
    session.optional_api_features.add("live_audio")
    return True


def stop_live_audio(session=None):
    session = session or _lifecycle
    stopped = session.audio_runtime.stop_worker()
    if not stopped:
        session.native_ownership_uncertain = True
        _poison_session(session, session.audio_runtime.audio_failure)
        return False
    if session.play_sound_callback_registered:
        try:
            _unregister_play_sound_callback(session)
        except Exception as exc:
            session.native_ownership_uncertain = True
            _poison_session(
                session,
                "Audio callback cleanup failed; restart Blender: {}".format(exc),
            )
            return False
    return stopped


def apply_scene_audio_settings(scene):
    """Apply UI audio changes without changing Mario or collision ownership."""
    session = _lifecycle
    if not is_mario_running() or session.scene is not scene:
        return False
    requested, volume, muted = _scene_audio_request(scene)
    if not 0.0 <= volume <= 1.0:
        session.audio_runtime.audio_failure = "Audio volume must be between 0 and 1"
        return False
    if not requested:
        stop_live_audio(session)
        return True
    state = session.audio_runtime.snapshot()
    if not state["audio_worker_started"]:
        return start_live_audio(session)
    try:
        session.audio_runtime.set_output(volume, muted)
    except Exception as exc:
        session.audio_runtime.audio_failure = str(exc)
        stop_live_audio(session)
        return False
    return True


def _poll_audio_runtime(session):
    state = session.audio_runtime.snapshot()
    if state["audio_failure"] and not state["audio_worker_started"]:
        session.audio_runtime.stop_worker()
        if state["native_failure"] and session.control_state != POISONED:
            session.native_ownership_uncertain = True
            _poison_session(session, state["audio_failure"])


def audio_diagnostics():
    session = _lifecycle
    result = session.audio_runtime.snapshot()
    result.update({
        "callback_registered": session.play_sound_callback_registered,
        "sound_event_count": len(session.sound_events),
    })
    return result


def _configure_debug_print_api(library):
    if getattr(library, "_libsm64_debug_print_api_configured", False):
        return
    export_name = "sm64_register_debug_print_function"
    if not hasattr(library, export_name):
        raise MarioFeatureUnavailableError(
            "Native debug messages are unavailable because the library is missing "
            "{}. Live Mario remains usable.".format(export_name)
        )
    library.sm64_register_debug_print_function.argtypes = [
        SM64DebugPrintFunctionPtr,
    ]
    library.sm64_register_debug_print_function.restype = None
    library._libsm64_debug_print_api_configured = True


def _register_debug_print_callback(session):
    if session.debug_print_callback_registered:
        return
    _configure_debug_print_api(session.library)
    owner_token = session.owner_token
    generation = session.generation

    def receive_debug(message):
        try:
            raw = message or b""
            text = raw.decode("utf-8", errors="replace").rstrip()
            if len(text) > 1024:
                text = text[:1021] + "..."
            record = NativeDebugMessage(
                owner_token, generation, text, time.monotonic()
            )
            session.native_debug_log.append(record)
            print("libsm64 native [{}] {}".format(generation, text))
        except Exception:
            return

    callback = SM64DebugPrintFunctionPtr(receive_debug)
    _native_call(
        session, "sm64_register_debug_print_function", callback
    )
    session.debug_print_callback = callback
    session.debug_print_callback_registered = True
    session.last_debug_error = ""
    session.optional_api_features.add("native_debug_callback")


def _unregister_debug_print_callback(session):
    if not session.debug_print_callback_registered:
        session.debug_print_callback = None
        return
    _native_call(
        session,
        "sm64_register_debug_print_function",
        SM64DebugPrintFunctionPtr(),
    )
    session.debug_print_callback_registered = False
    session.debug_print_callback = None


def _scene_debug_messages_requested(scene):
    settings = getattr(scene, "libsm64", None) if scene is not None else None
    return bool(
        settings is not None
        and getattr(settings, "enable_native_debug_messages", False)
    )


def apply_scene_debug_settings(scene):
    """Enable or disable the retained callback for one committed generation."""
    if not is_mario_running():
        return False
    session = require_owned_mario_operation(
        allowed_states=(LIVE_IDLE, RECORDING)
    )
    requested = _scene_debug_messages_requested(scene)
    try:
        if requested:
            _register_debug_print_callback(session)
        else:
            _unregister_debug_print_callback(session)
    except MarioFeatureUnavailableError as exc:
        session.last_debug_error = str(exc)
        return False
    except Exception as exc:
        session.native_ownership_uncertain = True
        _poison_session(
            session,
            "Native debug callback update failed; restart Blender: {}".format(exc),
        )
        session.last_debug_error = session.last_error
        raise MarioLifecycleError(session.last_error) from exc
    session.last_debug_error = ""
    return True


def _configure_directing_api(library):
    if getattr(library, "_libsm64_directing_api_configured", False):
        return
    exports = (
        "sm64_set_mario_health",
        "sm64_mario_heal",
        "sm64_mario_take_damage",
        "sm64_mario_kill",
        "sm64_set_mario_invincibility",
    )
    missing = [name for name in exports if not hasattr(library, name)]
    if missing:
        raise MarioFeatureUnavailableError(
            "Performance directing is unavailable because the native library is "
            "missing: {}. Live Mario remains usable.".format(", ".join(missing))
        )
    library.sm64_set_mario_health.argtypes = [ct.c_int32, ct.c_uint16]
    library.sm64_set_mario_health.restype = None
    library.sm64_mario_heal.argtypes = [ct.c_int32, ct.c_uint8]
    library.sm64_mario_heal.restype = None
    library.sm64_mario_take_damage.argtypes = [
        ct.c_int32, ct.c_uint32, ct.c_uint32,
        ct.c_float, ct.c_float, ct.c_float,
    ]
    library.sm64_mario_take_damage.restype = None
    library.sm64_mario_kill.argtypes = [ct.c_int32]
    library.sm64_mario_kill.restype = None
    library.sm64_set_mario_invincibility.argtypes = [ct.c_int32, ct.c_int16]
    library.sm64_set_mario_invincibility.restype = None
    library._libsm64_directing_api_configured = True


def _direct_mario(operation, export_name, arguments, details):
    session = require_owned_mario_operation(
        allowed_states=(LIVE_IDLE, RECORDING)
    )
    try:
        _configure_directing_api(session.library)
        _native_call(
            session, export_name, int(session.mario_id), *arguments
        )
    except MarioFeatureUnavailableError as exc:
        session.last_directing_error = str(exc)
        raise
    except Exception as exc:
        _poison_session(
            session,
            "Native directing operation {} failed; restart Blender: {}".format(
                operation, exc
            ),
        )
        session.last_directing_error = session.last_error
        raise MarioLifecycleError(session.last_error) from exc
    session.optional_api_features.add("directing_controls")
    session.last_directing_operation = {
        "operation": operation,
        "simulation_tick": max(0, int(tick_count)),
        "mario_id": int(session.mario_id),
        "details": dict(details),
    }
    session.last_directing_error = ""
    return session.last_directing_operation


def set_mario_health(health):
    health = _require_integer_range("health", health, 0, 0xFFFF)
    return _direct_mario(
        "set_health", "sm64_set_mario_health", (health,), {"health": health}
    )


def heal_mario(heal_counter):
    heal_counter = _require_integer_range(
        "heal counter", heal_counter, 1, 0xFF
    )
    return _direct_mario(
        "heal", "sm64_mario_heal", (heal_counter,),
        {"heal_counter": heal_counter},
    )


def damage_mario(damage, subtype, blender_source_position):
    damage = _require_integer_range("damage", damage, 1, 0xFFFFFFFF)
    subtype = _require_integer_range("damage subtype", subtype, 0, 0xFFFFFFFF)
    blender_source_position = tuple(blender_source_position)
    _require_float3("damage source", blender_source_position)
    native_source = blender_position_to_native(blender_source_position)
    return _direct_mario(
        "damage",
        "sm64_mario_take_damage",
        (damage, subtype, *native_source),
        {
            "damage": damage,
            "subtype": subtype,
            "blender_source": blender_source_position,
            "native_source": native_source,
        },
    )


def kill_mario():
    return _direct_mario("kill", "sm64_mario_kill", (), {})


def set_mario_invincibility(duration_ticks):
    duration_ticks = _require_integer_range(
        "invincibility duration", duration_ticks, 0, 0x7FFF
    )
    return _direct_mario(
        "set_invincibility",
        "sm64_set_mario_invincibility",
        (duration_ticks,),
        {"duration_ticks": duration_ticks},
    )


def _configure_collision_query_api(library):
    if getattr(library, "_libsm64_collision_query_api_configured", False):
        return
    exports = (
        "sm64_surface_find_floor_height",
        "sm64_surface_find_water_level",
        "sm64_surface_find_poison_gas_level",
    )
    missing = [name for name in exports if not hasattr(library, name)]
    if missing:
        raise MarioFeatureUnavailableError(
            "Collision probes are unavailable because the native library is missing: "
            "{}. Live Mario remains usable.".format(", ".join(missing))
        )
    library.sm64_surface_find_floor_height.argtypes = [
        ct.c_float, ct.c_float, ct.c_float,
    ]
    library.sm64_surface_find_floor_height.restype = ct.c_float
    library.sm64_surface_find_water_level.argtypes = [ct.c_float, ct.c_float]
    library.sm64_surface_find_water_level.restype = ct.c_float
    library.sm64_surface_find_poison_gas_level.argtypes = [
        ct.c_float, ct.c_float,
    ]
    library.sm64_surface_find_poison_gas_level.restype = ct.c_float
    library._libsm64_collision_query_api_configured = True


def probe_collision(blender_position):
    session = require_owned_mario_operation(
        allowed_states=(LIVE_IDLE, RECORDING)
    )
    blender_position = tuple(blender_position)
    _require_float3("collision probe position", blender_position)
    native_position = blender_position_to_native(blender_position)
    try:
        _configure_collision_query_api(session.library)
        with session.native_call_lock:
            floor_height = float(session.library.sm64_surface_find_floor_height(
                *native_position
            ))
            water_height = float(session.library.sm64_surface_find_water_level(
                native_position[0], native_position[2]
            ))
            gas_height = float(
                session.library.sm64_surface_find_poison_gas_level(
                    native_position[0], native_position[2]
                )
            )
    except MarioFeatureUnavailableError:
        raise
    except Exception as exc:
        _poison_session(
            session,
            "Native collision probe failed; restart Blender: {}".format(exc),
        )
        raise MarioLifecycleError(session.last_error) from exc
    for name, value in (
        ("floor", floor_height), ("water", water_height), ("gas", gas_height)
    ):
        if not math.isfinite(value):
            _poison_session(
                session, "Native collision probe returned non-finite {} height".format(name)
            )
            raise MarioLifecycleError(session.last_error)
    no_floor = math.isclose(
        floor_height, NO_FLOOR_NATIVE_HEIGHT, rel_tol=0.0, abs_tol=1.0e-4
    )
    no_water = math.isclose(
        water_height, float(ENVIRONMENT_DISABLED_NATIVE_LEVEL),
        rel_tol=0.0, abs_tol=1.0e-4,
    )
    no_gas = math.isclose(
        gas_height, float(ENVIRONMENT_DISABLED_NATIVE_LEVEL),
        rel_tol=0.0, abs_tol=1.0e-4,
    )
    key = chunk_coordinate(blender_position, CHUNK_SIZE_BLENDER)
    nearby_static = sum(
        int(record.surface_count)
        for chunk_key, record in session.native_surface_objects.items()
        if max(abs(chunk_key[0] - key[0]), abs(chunk_key[1] - key[1])) <= 1
        and record.state == SURFACE_CREATED
    )
    result = CollisionProbeResult(
        blender_position=blender_position,
        native_position=native_position,
        floor_native_height=floor_height,
        floor_blender_height=(
            None if no_floor else native_height_to_blender(floor_height)
        ),
        no_floor=no_floor,
        water_native_height=water_height,
        water_blender_height=(
            None if no_water else native_height_to_blender(water_height)
        ),
        gas_native_height=gas_height,
        gas_blender_height=(
            None if no_gas else native_height_to_blender(gas_height)
        ),
        chunk_key=key,
        chunk_active=key in session.active_chunk_keys,
        nearby_static_surface_count=nearby_static,
        active_static_surface_count=int(session.active_surface_count),
        active_moving_platform_surface_count=int(
            session.moving_platform_surface_count
        ),
    )
    session.last_collision_query = result
    session.optional_api_features.add("collision_queries")
    return result


def probe_collision_at_cursor(scene=None):
    scene = scene or bpy.context.scene
    return probe_collision(tuple(float(value) for value in scene.cursor.location))


def studio_diagnostics():
    session = _lifecycle
    state = None
    if session.mario_created:
        state = {
            "mario_id": int(session.mario_id),
            "position": tuple(float(value) for value in mario_state.position),
            "velocity": tuple(float(value) for value in mario_state.velocity),
            "health": int(mario_state.health),
            "action": int(mario_state.action),
            "animation_id": int(mario_state.animID),
            "animation_frame": int(mario_state.animFrame),
            "flags": int(mario_state.flags),
            "particle_flags": int(mario_state.particleFlags),
            "invincibility_timer": int(mario_state.invincTimer),
        }
    return {
        "mario_state": state,
        "start_mark_schema": START_MARK_SCHEMA_VERSION,
        "start_mark_restoration_mode": session.start_mark_restoration_mode,
        "moving_platform_count": len(session.moving_platform_objects),
        "moving_platform_ids": tuple(sorted(
            int(record.object_id)
            for record in session.moving_platform_objects.values()
            if record.object_id is not None
        )),
        "environment": environment_diagnostics(),
        "cap_request_history": tuple(session.cap_request_history),
        "audio": audio_diagnostics(),
        "runtime_metadata_schema": RUNTIME_METADATA_SCHEMA_VERSION,
        "debug_callback_registered": session.debug_print_callback_registered,
        "last_debug_error": session.last_debug_error,
        "debug_log": tuple(session.native_debug_log),
        "last_collision_query": session.last_collision_query,
        "last_directing_operation": session.last_directing_operation,
        "last_directing_error": session.last_directing_error,
    }


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
    _release_recording_timeline_playback(session)
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
        "moving_platforms": snapshot["moving_platform_count"],
        "moving_platform_surfaces": snapshot["moving_platform_surface_count"],
        "moving_platform_ids": snapshot["moving_platform_ids"],
        "platform_moves": snapshot["platform_move_count"],
        "last_platform_error": snapshot["last_platform_error"],
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
    if diagnostics["moving_platforms"]:
        message += " | {} moving platform(s)".format(diagnostics["moving_platforms"])
    if diagnostics["last_platform_error"]:
        message += " | Platform warning: {}".format(diagnostics["last_platform_error"])
    return message


def _collision_object_is_eligible(obj):
    return bool(
        getattr(obj, "type", None) == "MESH"
        and not obj.get("libsm64_is_bake", False)
        and not obj.get(LIVE_ROLE, "")
        and collision_role(obj) == COLLISION_ROLE_STATIC
    )


def _object_uses_material(obj, material):
    """Use pointer identity; Blender 5.2 collection membership accepts names only."""
    materials = getattr(getattr(obj, "data", None), "materials", ())
    try:
        target_pointer = int(material.as_pointer())
    except (AttributeError, ReferenceError, TypeError):
        return False
    for candidate in materials:
        if candidate is None:
            continue
        try:
            if int(candidate.as_pointer()) == target_pointer:
                return True
        except (AttributeError, ReferenceError, TypeError):
            continue
    return False


def _make_collision_update_handler(session):
    def collision_update(_scene, depsgraph):
        if not _session_is_registered_owner(session) or session.shutdown_in_progress:
            return
        affected = []
        for update in getattr(depsgraph, "updates", ()):
            data = getattr(update, "id", None)
            if isinstance(data, bpy.types.Object):
                if _moving_platform_object_is_eligible(data) and getattr(
                    update, "is_updated_geometry", False
                ):
                    session.dirty_moving_platforms.add(
                        _blender_object_identity(data)
                    )
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
                session.dirty_moving_platforms.update(
                    _blender_object_identity(obj) for obj in session.scene.objects
                    if getattr(obj, "data", None) is data
                    and _moving_platform_object_is_eligible(obj)
                )
            elif isinstance(data, bpy.types.Material) and session.scene is not None:
                affected.extend(
                    obj for obj in session.scene.objects
                    if _collision_object_is_eligible(obj)
                    and _object_uses_material(obj, data)
                )
                session.dirty_moving_platforms.update(
                    _blender_object_identity(obj) for obj in session.scene.objects
                    if _moving_platform_object_is_eligible(obj)
                    and _object_uses_material(obj, data)
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


def _surface_registry_key(record):
    if record.ownership_kind == SURFACE_KIND_STATIC_CHUNK:
        return record.chunk_key
    return record.object_key


def _register_native_surface_id(session, record):
    object_id = int(record.object_id)
    existing = session.native_surface_id_registry.get(object_id)
    if existing is not None:
        session.native_ownership_uncertain = True
        raise MarioLifecycleError(
            "Native surface ID {} was reused while still owned by {}".format(
                object_id, existing[0]
            )
        )
    session.native_surface_id_registry[object_id] = (
        record.ownership_kind,
        _surface_registry_key(record),
        record.owner_token,
        record.generation,
    )


def _validate_native_surface_id_owner(session, record):
    expected = (
        record.ownership_kind,
        _surface_registry_key(record),
        record.owner_token,
        record.generation,
    )
    actual = session.native_surface_id_registry.get(int(record.object_id))
    if actual != expected:
        session.native_ownership_uncertain = True
        raise MarioLifecycleError(
            "Native surface ID {} ownership is stale or ambiguous".format(
                record.object_id
            )
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
        object_id = int(_native_call(
            session, "sm64_surface_object_create", ct.byref(descriptor)
        ))
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
    _register_native_surface_id(session, record)
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
    _validate_native_surface_id_owner(session, record)
    if record.state in (SURFACE_DELETE_ATTEMPTED, SURFACE_DELETED):
        raise MarioLifecycleError("Collision object {} would be deleted twice".format(record.object_id))
    record.state = SURFACE_DELETE_ATTEMPTED
    try:
        _native_stage("before_surface_object_delete {}".format(chunk_key))
        _native_call(
            session, "sm64_surface_object_delete", record.object_id
        )
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
    session.native_surface_id_registry.pop(int(record.object_id), None)
    session.surface_delete_count += 1
    session.active_surface_count -= record.surface_count
    # IDs may be reused by upstream.  Remove successful ownership immediately
    # so this generation can never delete the old numeric ID twice.
    session.native_surface_objects.pop(chunk_key, None)
    return True


def _blender_object_identity(obj):
    original = getattr(obj, "original", obj)
    session_uid = getattr(original, "session_uid", None)
    if session_uid is not None:
        return ("UID", int(session_uid))
    return ("PTR", int(original.as_pointer()))


def _moving_platform_object_is_eligible(obj):
    if not (
        getattr(obj, "type", None) == "MESH"
        and not obj.get("libsm64_is_bake", False)
        and not obj.get(LIVE_ROLE, "")
        and collision_role(obj) == COLLISION_ROLE_MOVING_PLATFORM
    ):
        return False
    try:
        if obj.hide_get():
            return False
    except Exception:
        pass
    return not getattr(obj, "hide_viewport", False)


def _moving_platform_candidates(session):
    if session.scene is None:
        return {}
    return {
        _blender_object_identity(obj): obj
        for obj in session.scene.collection.all_objects
        if _moving_platform_object_is_eligible(obj)
    }


def _snapshot_collision_roles(session):
    session.initial_collision_roles = {
        _blender_object_identity(obj): collision_role(obj)
        for obj in session.scene.collection.all_objects
        if getattr(obj, "type", None) == "MESH"
        and not obj.get("libsm64_is_bake", False)
        and not obj.get(LIVE_ROLE, "")
    }


def _moving_platform_surface_type(mesh, triangle):
    default = int(COLLISION_TYPES["SURFACE_DEFAULT"])
    material_index = int(triangle.material_index)
    materials = getattr(mesh, "materials", ())
    if 0 <= material_index < len(materials):
        material = materials[material_index]
        collision_name = getattr(material, "collision_type_simple", None) if material else None
        if collision_name is not None:
            return int(COLLISION_TYPES.get(collision_name, default))
    return default


def _extract_moving_platform_geometry(obj, evaluated, depsgraph, transform):
    """Build object-local native surfaces with evaluated scale baked once."""
    mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
    try:
        mesh.calc_loop_triangles()
        terrain = int(_object_terrain(obj))
        scale = transform.blender_scale
        records = []
        digest = hashlib.sha256()
        digest.update(b"libsm64-moving-platform-v2")
        native_by_index = []
        for vertex in mesh.vertices:
            coordinate = vertex.co
            scaled_local = (
                float(coordinate.x) * scale[0],
                float(coordinate.y) * scale[1],
                float(coordinate.z) * scale[2],
            )
            mapped = blender_local_vector_to_native(scaled_local, SM64_SCALE_FACTOR)
            checked = []
            for value in mapped:
                integer, valid = _checked_int32_coordinate(value)
                if not valid:
                    raise OverflowError(
                        "Moving Platform {!r} exceeds signed 32-bit native coordinates".format(
                            obj.name
                        )
                    )
                checked.append(integer)
            native_by_index.append(tuple(checked))

        # Fingerprint evaluated polygon geometry independently of Blender's
        # transient loop-triangle order/diagonal choice for planar n-gons.
        polygon_fingerprints = []
        for polygon in mesh.polygons:
            coordinates = tuple(native_by_index[index] for index in polygon.vertices)
            rotations = tuple(
                coordinates[index:] + coordinates[:index]
                for index in range(len(coordinates))
            )
            canonical = min(rotations) if rotations else ()
            material_index = int(polygon.material_index)
            surface_type = int(COLLISION_TYPES["SURFACE_DEFAULT"])
            materials = getattr(mesh, "materials", ())
            if 0 <= material_index < len(materials):
                material = materials[material_index]
                collision_name = (
                    getattr(material, "collision_type_simple", None)
                    if material else None
                )
                if collision_name is not None:
                    surface_type = int(COLLISION_TYPES.get(collision_name, surface_type))
            polygon_fingerprints.append((surface_type, terrain, canonical))
        for surface_type, polygon_terrain, coordinates in sorted(polygon_fingerprints):
            digest.update(struct.pack("<hH", surface_type, polygon_terrain))
            digest.update(struct.pack("<I", len(coordinates)))
            for vertex in coordinates:
                digest.update(struct.pack("<iii", *vertex))

        for triangle in mesh.loop_triangles:
            native_vertices = [native_by_index[index] for index in triangle.vertices]
            surface_type = _moving_platform_surface_type(mesh, triangle)
            record = (surface_type, 0, terrain, tuple(native_vertices))
            records.append(record)
        surfaces = (SM64Surface * len(records))()
        for surface_index, record in enumerate(records):
            surface_type, force, terrain, vertices = record
            surface = surfaces[surface_index]
            surface.type = surface_type
            surface.force = force
            surface.terrain = terrain
            for vertex_index, vertex in enumerate(vertices):
                for axis_index, coordinate in enumerate(vertex):
                    surface.vertices[vertex_index][axis_index] = coordinate
        return surfaces, digest.hexdigest()
    finally:
        evaluated.to_mesh_clear()


def _ctypes_platform_transform(transform):
    native = SM64ObjectTransform()
    native.position[:] = transform.position
    native.eulerRotation[:] = transform.euler_rotation_degrees
    return native


def _evaluate_moving_platform(obj, depsgraph):
    evaluated = obj.evaluated_get(depsgraph)
    transform = blender_matrix_to_native_transform(
        evaluated.matrix_world, SM64_SCALE_FACTOR, origin_offset
    )
    surfaces, fingerprint = _extract_moving_platform_geometry(
        obj, evaluated, depsgraph, transform
    )
    return transform, surfaces, fingerprint


def _create_moving_platform_object(session, obj, depsgraph):
    object_key = _blender_object_identity(obj)
    if object_key in session.moving_platform_objects:
        raise MarioLifecycleError("Moving Platform already has a native owner")
    transform, surfaces, fingerprint = _evaluate_moving_platform(obj, depsgraph)
    surface_count = len(surfaces)
    if surface_count <= 0:
        raise ValueError("Moving Platform {!r} has no collision triangles".format(obj.name))
    record = MovingPlatformOwnership(
        object_key=object_key,
        object_name=obj.name,
        owner_token=session.owner_token,
        generation=session.generation,
        surface_count=surface_count,
        geometry_fingerprint=fingerprint,
        initial_scale=transform.blender_scale,
        previous_transform=transform,
        current_transform=transform,
        creation_order=session.surface_create_count + 1,
    )
    session.moving_platform_objects[object_key] = record
    descriptor = SM64SurfaceObject()
    descriptor.transform = _ctypes_platform_transform(transform)
    descriptor.surfaceCount = surface_count
    descriptor.surfaces = ct.cast(surfaces, ct.POINTER(SM64Surface))
    record.state = SURFACE_CREATE_ATTEMPTED
    try:
        _native_stage("before_moving_platform_create {}".format(obj.name))
        record.object_id = int(_native_call(
            session, "sm64_surface_object_create", ct.byref(descriptor)
        ))
        _native_stage("after_moving_platform_create {}".format(obj.name))
        _register_native_surface_id(session, record)
    except Exception as exc:
        record.state = SURFACE_FAILED
        record.diagnostic = str(exc)
        session.last_platform_error = (
            "Moving Platform {!r} create failed: {}".format(obj.name, exc)
        )
        raise
    record.state = SURFACE_CREATED
    session.surface_create_count += 1
    session.moving_platform_surface_count += surface_count
    return record


def _moving_platform_record_is_owned(session, record):
    return bool(
        record.owner_token == session.owner_token
        and record.generation == session.generation
        and session.moving_platform_objects.get(record.object_key) is record
    )


def _delete_moving_platform_object(session, object_key, operation="delete"):
    record = session.moving_platform_objects.get(object_key)
    if record is None:
        return False
    if not _moving_platform_record_is_owned(session, record):
        session.native_ownership_uncertain = True
        raise MarioLifecycleError("Moving Platform delete rejected for stale ownership")
    if record.object_id is None:
        session.moving_platform_objects.pop(object_key, None)
        return False
    _validate_native_surface_id_owner(session, record)
    if record.state in (SURFACE_DELETE_ATTEMPTED, SURFACE_DELETED):
        raise MarioLifecycleError(
            "Moving Platform {} would be deleted twice".format(record.object_id)
        )
    record.state = SURFACE_DELETE_ATTEMPTED
    try:
        _native_stage("before_moving_platform_delete {}".format(record.object_name))
        _native_call(
            session, "sm64_surface_object_delete", record.object_id
        )
        _native_stage("after_moving_platform_delete {}".format(record.object_name))
    except Exception as exc:
        record.state = SURFACE_FAILED
        record.diagnostic = str(exc)
        session.native_ownership_uncertain = True
        session.last_platform_error = (
            "Moving Platform {!r} {} failed; ownership is uncertain: {}".format(
                record.object_name, operation, exc
            )
        )
        raise
    record.state = SURFACE_DELETED
    session.native_surface_id_registry.pop(int(record.object_id), None)
    session.surface_delete_count += 1
    session.moving_platform_surface_count -= record.surface_count
    session.moving_platform_objects.pop(object_key, None)
    return True


def _platform_transform_changed(previous, current):
    position_tolerance = max(1.0, abs(float(SM64_SCALE_FACTOR))) * PLATFORM_TRANSFORM_TOLERANCE
    if any(
        abs(left - right) > position_tolerance
        for left, right in zip(previous.position, current.position)
    ):
        return True
    return any(
        abs(left - right) > PLATFORM_ROTATION_TOLERANCE
        for left, right in zip(previous.rotation_matrix, current.rotation_matrix)
    )


def _platform_scale_changed(initial, current):
    return any(
        not math.isclose(left, right, rel_tol=1e-7, abs_tol=1e-7)
        for left, right in zip(initial, current)
    )


def _disable_moving_platform(session, record, message):
    _delete_moving_platform_object(session, record.object_key, operation="disable")
    session.disabled_moving_platforms[record.object_key] = message
    session.last_platform_error = message


def _initialize_moving_platforms(session):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    for object_key, obj in sorted(
        _moving_platform_candidates(session).items(), key=lambda item: item[1].name
    ):
        try:
            _create_moving_platform_object(session, obj, depsgraph)
        except (ValueError, OverflowError) as exc:
            message = "Moving Platform {!r} disabled: {}".format(obj.name, exc)
            session.disabled_moving_platforms[object_key] = message
            session.last_platform_error = message
        except Exception as exc:
            _poison_session(session, session.last_platform_error or str(exc))
            raise MarioLifecycleError(session.last_error) from exc


def _update_moving_platforms(session):
    """Sample evaluated platform state and move native objects before Mario tick."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    candidates = _moving_platform_candidates(session)
    for object_key in tuple(session.moving_platform_objects):
        if object_key not in candidates:
            _delete_moving_platform_object(session, object_key, operation="removed")
    for object_key, obj in sorted(candidates.items(), key=lambda item: item[1].name):
        if object_key in session.disabled_moving_platforms:
            continue
        record = session.moving_platform_objects.get(object_key)
        if record is None:
            if session.initial_collision_roles.get(object_key) == COLLISION_ROLE_STATIC:
                message = (
                    "Moving Platform {!r} was Static when the session began; restart "
                    "Live Mario to change its collision role safely".format(obj.name)
                )
                session.disabled_moving_platforms[object_key] = message
                session.last_platform_error = message
                continue
            try:
                _create_moving_platform_object(session, obj, depsgraph)
            except (ValueError, OverflowError) as exc:
                message = "Moving Platform {!r} disabled: {}".format(obj.name, exc)
                session.disabled_moving_platforms[object_key] = message
                session.last_platform_error = message
            except Exception as exc:
                _poison_session(session, session.last_platform_error or str(exc))
                raise MarioLifecycleError(session.last_error) from exc
            continue
        try:
            evaluated = obj.evaluated_get(depsgraph)
            transform = blender_matrix_to_native_transform(
                evaluated.matrix_world, SM64_SCALE_FACTOR, origin_offset
            )
            fingerprint = record.geometry_fingerprint
            if object_key in session.dirty_moving_platforms:
                _surfaces, fingerprint = _extract_moving_platform_geometry(
                    obj, evaluated, depsgraph, transform
                )
        except (ValueError, OverflowError) as exc:
            _disable_moving_platform(
                session, record,
                "Moving Platform {!r} disabled until session restart: {}".format(
                    obj.name, exc
                ),
            )
            continue
        if _platform_scale_changed(record.initial_scale, transform.blender_scale):
            _disable_moving_platform(
                session, record,
                "Moving Platform {!r} disabled until session restart: animated scale is unsupported".format(
                    obj.name
                ),
            )
            continue
        if fingerprint != record.geometry_fingerprint:
            _disable_moving_platform(
                session, record,
                "Moving Platform {!r} disabled until session restart: evaluated geometry changed".format(
                    obj.name
                ),
            )
            continue
        session.dirty_moving_platforms.discard(object_key)
        record.current_transform = transform
        if not _platform_transform_changed(record.previous_transform, transform):
            continue
        native_transform = _ctypes_platform_transform(transform)
        try:
            _native_stage("before_moving_platform_move {}".format(obj.name))
            _native_call(
                session,
                "sm64_surface_object_move",
                record.object_id,
                ct.byref(native_transform),
            )
            _native_stage("after_moving_platform_move {}".format(obj.name))
        except Exception as exc:
            record.state = SURFACE_FAILED
            record.diagnostic = str(exc)
            session.last_platform_error = (
                "Moving Platform {!r} move failed: {}".format(obj.name, exc)
            )
            _poison_session(session, session.last_platform_error)
            raise MarioLifecycleError(session.last_error) from exc
        record.previous_transform = transform
        record.last_updated_tick = tick_count
        session.platform_move_count += 1


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
    # Retain only the validated bytes for optional mid-session audio startup.
    # The user-selected filesystem path is never stored in lifecycle state.
    session.rom_image = rom_bytes
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
        _native_call(
            session,
            "sm64_global_init",
            rom_chars.from_buffer(rom_bytes),
            texture_buff,
        )
        _native_stage("after_global_init")
        session.global_initialized = True
        _lifecycle_log(session, "global init succeeded")
        if _scene_debug_messages_requested(session.scene):
            try:
                _register_debug_print_callback(session)
            except MarioFeatureUnavailableError as exc:
                # Diagnostics are optional. An otherwise-compatible artifact
                # may omit this callback without blocking Live Mario.
                session.last_debug_error = str(exc)
                _lifecycle_log(session, "native debug callback unavailable")
            except Exception as exc:
                session.native_ownership_uncertain = True
                raise MarioLifecycleError(
                    "Native debug callback registration failed; restart Blender: {}".format(
                        exc
                    )
                ) from exc
        initialize_all_data(texture_buff)

        # Dynamic surface objects carry all Studio scene collision.  Keep the
        # pinned static API initialized with an explicitly empty set so no
        # geometry is represented in both systems.
        empty_surfaces = (SM64Surface * 0)()
        _native_stage("before_static_surface_load")
        _native_call(
            session, "sm64_static_surfaces_load", empty_surfaces, 0
        )
        _native_stage("after_static_surface_load")
        print("Preparing nearby streamed collision\u2026")
        _snapshot_collision_roles(session)
        _initialize_streamed_collision(session, tuple(original_cursor_pos))
        _initialize_moving_platforms(session)

        session.mario_create_attempted = True
        _lifecycle_log(session, "Mario create started")
        _native_stage("before_mario_create")
        print("Starting Live Mario\u2026")
        mario_id = int(_native_call(
            session, "sm64_mario_create", 0.0, 0.0, 0.0
        ))
        _native_stage("after_mario_create")
        if mario_id < 0:
            _lifecycle_log(session, "Mario create failed")
            raise MarioLifecycleError("There is no ground under the 3D cursor where Mario can spawn")
        session.mario_id = mario_id
        session.mario_created = True
        sm64_mario_id = mario_id
        _lifecycle_log(session, "Mario create succeeded")
        _apply_environment_after_mario_create(session)

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
        requested_audio, _volume, _muted = _scene_audio_request(session.scene)
        if requested_audio:
            start_live_audio(session)
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


def require_owned_mario_operation(session=None, allowed_states=None):
    """Return an exactly owned live session or reject before any native call."""
    session = session or _lifecycle
    if session is not _lifecycle or not _session_is_registered_owner(session):
        raise MarioLifecycleError("Native Mario operation rejected for stale ownership")
    if not session.session_committed:
        raise MarioLifecycleError("Native Mario operation rejected before session commit")
    if not session.library_loaded or session.library is None:
        raise MarioLifecycleError("Native Mario operation rejected without a loaded library")
    if not session.global_initialized:
        raise MarioLifecycleError("Native Mario operation rejected without global initialization")
    if not session.mario_created or session.mario_id < 0:
        raise MarioLifecycleError("Native Mario operation rejected without an owned Mario")
    if session.native_ownership_uncertain or session.control_state == POISONED:
        raise MarioLifecycleError("Native Mario operation rejected for a poisoned session")
    if session.shutdown_in_progress or session.shutdown_complete:
        raise MarioLifecycleError("Native Mario operation rejected during shutdown")
    if allowed_states is not None and session.control_state not in allowed_states:
        raise MarioLifecycleError(
            "Native Mario operation is unavailable while Studio is {}".format(
                session.control_state
            )
        )
    return session


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
    """Capture one immutable same-tick snapshot of modern public Mario state."""
    session = require_owned_mario_operation(
        allowed_states=(LIVE_IDLE, RECORDING, BAKING)
    )
    return MarioStartMark(
        schema_version=START_MARK_SCHEMA_VERSION,
        owner_token=session.owner_token,
        lifecycle_generation=session.generation,
        position=(
            float(mario_state.position[0]),
            float(mario_state.position[1]),
            float(mario_state.position[2]),
        ),
        velocity=(
            float(mario_state.velocity[0]),
            float(mario_state.velocity[1]),
            float(mario_state.velocity[2]),
        ),
        face_angle=float(mario_state.faceAngle),
        forward_velocity=float(mario_state.forwardVelocity),
        health=int(mario_state.health),
        action=int(mario_state.action),
        anim_id=int(mario_state.animID),
        anim_frame=int(mario_state.animFrame),
        flags=int(mario_state.flags),
        particle_flags=int(mario_state.particleFlags),
        invincibility_timer=int(mario_state.invincTimer),
    )


def _validate_start_mark_for_session(mark, session=None):
    session = session or _lifecycle
    if not isinstance(mark, MarioStartMark):
        raise ValueError(
            "Start Mark uses an unsupported legacy schema; set a new Start Mark"
        )
    # Re-run validation defensively in case a deserializer bypassed __init__.
    mark.__post_init__()
    if mark.owner_token != session.owner_token:
        raise MarioLifecycleError("Start Mark belongs to a different runtime owner")
    if mark.lifecycle_generation != session.generation:
        raise MarioLifecycleError("Start Mark belongs to a retired lifecycle generation")
    return mark


def _valid_persistent_start_mark(session=None):
    """Return this generation's mark data, or None for stale/unowned state."""
    session = session or _lifecycle
    mark = session.persistent_start_mark
    if not isinstance(mark, MarioStartMark):
        return None
    if mark.owner_token != session.owner_token:
        return None
    if mark.lifecycle_generation != session.generation:
        return None
    try:
        _validate_start_mark_for_session(mark, session)
    except (MarioLifecycleError, ValueError):
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
    session.persistent_start_mark = mark
    return mark


def clear_persistent_start_mark(session=None):
    """Forget a Start Mark without touching native Mario state."""
    (session or _lifecycle).persistent_start_mark = None


def _selected_start_mark_restoration_mode(session):
    settings = getattr(session.scene, "libsm64", None)
    mode = getattr(settings, "start_mark_restoration_mode", None)
    if mode not in START_MARK_RESTORATION_MODES:
        mode = session.start_mark_restoration_mode
    if mode not in START_MARK_RESTORATION_MODES:
        mode = START_MARK_PERFORMANCE
    return mode


def reset_to_persistent_start_mark(restoration_mode=None):
    """Safely recreate Live Mario at this generation's persistent mark."""
    session = _lifecycle
    if recorder.active or session.control_state == RECORDING:
        raise RuntimeError("Reset to Mark is unavailable while recording")
    if session.control_state != LIVE_IDLE:
        raise RuntimeError("Live Mario is not ready to reset")
    mark = _valid_persistent_start_mark(session)
    if mark is None:
        raise RuntimeError("Start Mark unavailable")
    mode = restoration_mode or _selected_start_mark_restoration_mode(session)
    if mode not in START_MARK_RESTORATION_MODES:
        raise ValueError("Unknown Start Mark restoration mode: {}".format(mode))

    session.control_state = RESETTING
    try:
        restore_mario_starting_mark(mark, mode)
        session.recording_tick_origin = None
        session.control_state = LIVE_IDLE
        resume_mario_for_recording()
    except Exception:
        if session.control_state != POISONED:
            session.control_state = LIVE_IDLE if is_mario_running() else STOPPED
        raise
    return mark


def _restore_supported_start_mark_state(session, mark, restoration_mode):
    """Apply supported setters in the order validated by the Phase-3A tests."""
    mario_id = session.mario_id
    if restoration_mode == START_MARK_PERFORMANCE:
        _native_call(session, "sm64_set_mario_action", mario_id, mark.action)
        _native_call(session, "sm64_set_mario_animation", mario_id, mark.anim_id)
        _native_call(session, "sm64_set_mario_anim_frame", mario_id, mark.anim_frame)
        _native_call(session, "sm64_set_mario_state", mario_id, mark.flags)
    _native_call(session, "sm64_set_mario_position", mario_id, *mark.position)
    _native_call(session, "sm64_set_mario_faceangle", mario_id, mark.face_angle)
    _native_call(session, "sm64_set_mario_velocity", mario_id, *mark.velocity)
    _native_call(
        session, "sm64_set_mario_forward_velocity", mario_id,
        mark.forward_velocity,
    )
    _native_call(session, "sm64_set_mario_health", mario_id, mark.health)
    _native_call(
        session, "sm64_set_mario_invincibility", mario_id,
        mark.invincibility_timer,
    )


def _start_mark_restoration_result(mark, restoration_mode):
    expected = {
        "position": mark.position,
        "velocity": mark.velocity,
        "face_angle": mark.face_angle,
        "forward_velocity": mark.forward_velocity,
        "health": mark.health,
        "invincibility_timer": mark.invincibility_timer,
    }
    if restoration_mode == START_MARK_PERFORMANCE:
        expected.update({
            "action": mark.action,
            "anim_id": mark.anim_id,
            "anim_frame": mark.anim_frame,
            "flags": mark.flags,
        })
    actual = {
        "position": tuple(float(value) for value in mario_state.position),
        "velocity": tuple(float(value) for value in mario_state.velocity),
        "face_angle": float(mario_state.faceAngle),
        "forward_velocity": float(mario_state.forwardVelocity),
        "health": int(mario_state.health),
        "action": int(mario_state.action),
        "anim_id": int(mario_state.animID),
        "anim_frame": int(mario_state.animFrame),
        "flags": int(mario_state.flags),
        "invincibility_timer": int(mario_state.invincTimer),
    }
    fidelity = {}
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
        fidelity[name] = "exact" if exact else (
            "approximate" if approximate else "changed_after_neutral_tick"
        )
    return {
        "schema_version": START_MARK_SCHEMA_VERSION,
        "mode": restoration_mode,
        "fidelity": fidelity,
        "observed_only": START_MARK_OBSERVED_ONLY_FIELDS,
    }


def restore_mario_starting_mark(mark, restoration_mode=START_MARK_PERFORMANCE):
    """Safely recreate Mario, then restore every ABI-supported mark field."""
    global sm64_mario_id, tick_count

    session = require_owned_mario_operation(
        allowed_states=(LIVE_IDLE, RESETTING, BAKING)
    )
    mark = _validate_start_mark_for_session(mark, session)
    if restoration_mode not in START_MARK_RESTORATION_MODES:
        raise ValueError("Unknown Start Mark restoration mode: {}".format(restoration_mode))
    # Optional capability validation happens before deleting the current Mario,
    # so a missing export is recoverable and cannot disturb native ownership.
    _configure_start_mark_api(session.library)
    session.optional_api_features.add("better_start_marks")
    session.start_mark_restoration_mode = restoration_mode
    spawn = tuple(float(value) for value in mark.position)
    old_id = session.mario_id
    try:
        _native_call(session, "sm64_mario_delete", old_id)
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
        replacement_id = int(_native_call(
            session, "sm64_mario_create", *spawn
        ))
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
        _restore_supported_start_mark_state(session, mark, restoration_mode)
        _apply_environment_after_mario_create(session)
        _clear_transient_input_state(session)
        _update_moving_platforms(session)
        _native_call(
            session,
            "sm64_mario_tick",
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
    session.last_start_mark_restoration = _start_mark_restoration_result(
        mark, restoration_mode
    )
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
        _release_recording_timeline_playback(session)
        recorder.cancel("Recording did not start")
        session.recording_tick_origin = None
        if session.control_state != POISONED and is_mario_running():
            session.control_state = LIVE_IDLE
        raise


def freeze_mario_recording_for_bake():
    session = _lifecycle
    if session.control_state != RECORDING and not recorder.has_pending_samples:
        raise RecordingError("Live Mario is not recording")
    _release_recording_timeline_playback(session)
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
    _release_recording_timeline_playback(session)
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
    _release_recording_timeline_playback(session)
    if is_mario_running() and session.control_state != POISONED:
        session.control_state = LIVE_IDLE
        _install_tick_timer(session)


def stop_tick_mario(_session=None, _cleanup_rejected=True):
    """Idempotently tear down only native resources this generation owns."""
    global sm64, sm64_mario_id, original_fps, original_fps_setting
    global original_fps_base, original_cursor_pos, simulation_scene

    session = _session or _lifecycle
    _release_recording_timeline_playback(session)
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
    _lifecycle_log(session, "audio stop invoked before native teardown")
    if not session.audio_runtime.stop_worker():
        errors.append(session.audio_runtime.audio_failure)
        session.native_ownership_uncertain = True
    if session.play_sound_callback_registered and not session.native_ownership_uncertain:
        try:
            _unregister_play_sound_callback(session)
        except Exception as exc:
            errors.append("Sound callback cleanup failed: {}".format(exc))
            session.native_ownership_uncertain = True
    try:
        surface_cleanup_safe = not session.native_ownership_uncertain
        if session.native_ownership_uncertain:
            errors.append("Surface-object cleanup skipped because native ownership is already uncertain")
            _lifecycle_log(session, "surface cleanup skipped: native ownership is uncertain")
        elif session.native_surface_objects or session.moving_platform_objects:
            if not (session.library_loaded and session.global_initialized and session.library):
                errors.append("Surface-object state is inconsistent; native cleanup skipped")
                session.native_ownership_uncertain = True
                surface_cleanup_safe = False
            else:
                owned_records = sorted(
                    tuple(session.native_surface_objects.values())
                    + tuple(session.moving_platform_objects.values()),
                    key=lambda record: record.creation_order,
                    reverse=True,
                )
                for record in owned_records:
                    try:
                        if record.ownership_kind == SURFACE_KIND_MOVING_PLATFORM:
                            _delete_moving_platform_object(
                                session, record.object_key, operation="shutdown"
                            )
                        else:
                            _delete_native_surface_object(
                                session, record.chunk_key, operation="shutdown"
                            )
                    except Exception as exc:
                        errors.append("Surface object delete failed: {}".format(exc))
                        surface_cleanup_safe = False
                        # Once one ID is uncertain, make no further native calls.
                        break
        if surface_cleanup_safe:
            session.active_chunk_keys.clear()
            session.active_surface_count = 0
            session.moving_platform_surface_count = 0
            session.native_surface_id_registry.clear()

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
                    _native_call(
                        session, "sm64_mario_delete", session.mario_id
                    )
                    session.mario_created = False
                    session.mario_id = -1
                except Exception as exc:
                    errors.append("Mario delete failed: {}".format(exc))
        elif session.mario_created:
            _lifecycle_log(session, "Mario delete skipped: deletion was already attempted")
        else:
            _lifecycle_log(session, "Mario delete skipped: no Mario instance was created")

        # Keep native debug reporting alive through Mario/surface cleanup, then
        # sever the retained Python callback before global memory is released.
        if session.debug_print_callback_registered:
            if session.native_ownership_uncertain:
                errors.append(
                    "Debug callback cleanup skipped because native ownership is uncertain"
                )
            else:
                try:
                    _unregister_debug_print_callback(session)
                except Exception as exc:
                    errors.append("Debug callback cleanup failed: {}".format(exc))
                    session.native_ownership_uncertain = True

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
                    _native_call(session, "sm64_global_terminate")
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
        session.rom_image = None
        session.play_sound_callback = None
        session.play_sound_callback_registered = False
        if not session.debug_print_callback_registered:
            session.debug_print_callback = None
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
    _poll_audio_runtime(session)
    if session.control_state == POISONED:
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

    # Upstream derives platform linear/angular velocity from consecutive move
    # calls and applies that displacement during Mario tick. Keep this order
    # deterministic so recording observes the same completed tick and geometry.
    _update_moving_platforms(session)
    _native_call(
        session,
        "sm64_mario_tick",
        session.mario_id,
        ct.byref(mario_inputs),
        ct.byref(mario_state),
        ct.byref(mario_geo),
    )
    runtime_metadata = MarioRuntimeMetadata(
        native_position=tuple(float(value) for value in mario_state.position),
        native_velocity=tuple(float(value) for value in mario_state.velocity),
        face_angle=float(mario_state.faceAngle),
        forward_velocity=float(mario_state.forwardVelocity),
        health=int(mario_state.health),
        action=int(mario_state.action),
        animation_id=int(mario_state.animID),
        animation_frame=int(mario_state.animFrame),
        flags=int(mario_state.flags),
        particle_flags=int(mario_state.particleFlags),
        invincibility_timer=int(mario_state.invincTimer),
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
    if tick_count < 15 or session.force_full_mesh_updates > 0:
        update_mesh_data(live_mesh)
        if session.force_full_mesh_updates > 0:
            session.force_full_mesh_updates -= 1
    else:
        update_mesh_data_fast(live_mesh)

    try:
        recorder.capture_mesh(
            live_mesh,
            tick_count,
            mario_world_location,
            float(mario_state.faceAngle),
            runtime_metadata,
        )
    except Exception as exc:
        recorder.fail("Recording stopped: {}".format(exc), preserve_samples=True)
        _release_recording_timeline_playback(session)
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
        if isinstance(obj.data, bpy.types.Mesh)
        and not obj.get('libsm64_is_bake', False)
        and collision_role(obj) == COLLISION_ROLE_STATIC
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
