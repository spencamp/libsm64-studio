"""Short-take geometry recording and Blender shape-key baking.

The recorder and timing helpers deliberately do not import bpy so their state
handling can be tested with ordinary Python.  Blender-specific baking is kept
behind :func:`bake_shape_keys`.
"""

from array import array
from dataclasses import dataclass
import json
import math


SAMPLE_FPS = 30.0
SHORT_TAKE_WARNING_SAMPLES = 300
BAKE_SCHEMA_VERSION = "libsm64_bake_schema_version"
BAKE_LAYOUT = "libsm64_bake_layout"
CURRENT_BAKE_SCHEMA_VERSION = 2
OBJECT_MOTION_LOCAL_POSE = "OBJECT_MOTION_LOCAL_POSE"
RUNTIME_METADATA_SCHEMA_VERSION = 1
RUNTIME_METADATA_TEXT_PROPERTY = "libsm64_runtime_metadata_text"
RUNTIME_METADATA_OWNER_PROPERTY = "libsm64_take_owner"
RUNTIME_METADATA_SCHEMA_PROPERTY = "libsm64_runtime_metadata_schema"


class RecordingError(RuntimeError):
    pass


def scene_rate_matches_sample_rate(target_fps, sample_fps=SAMPLE_FPS):
    """Return whether scene playback and Mario capture have the same cadence."""
    try:
        target_fps = float(target_fps)
        sample_fps = float(sample_fps)
    except (TypeError, ValueError, OverflowError):
        return False
    return (
        math.isfinite(target_fps)
        and math.isfinite(sample_fps)
        and target_fps > 0.0
        and sample_fps > 0.0
        and math.isclose(target_fps, sample_fps, rel_tol=0.0, abs_tol=1e-6)
    )


class TimelinePlaybackOwner:
    """Own at most one externally implemented timeline playback session.

    Blender-specific callbacks are supplied only when playback is acquired, so
    this state machine stays unit-testable without importing ``bpy``.  Release
    clears ownership before invoking callbacks to make reentrant cleanup safe.
    """

    def __init__(self):
        self._owned = False
        self._is_playing = None
        self._stop_playback = None

    @property
    def owns_playback(self):
        return self._owned

    def acquire(self, is_playing, start_playback, stop_playback):
        if self._owned or is_playing():
            return False
        start_playback()
        self._owned = True
        self._is_playing = is_playing
        self._stop_playback = stop_playback
        return True

    def release(self):
        if not self._owned:
            return False
        is_playing = self._is_playing
        stop_playback = self._stop_playback
        self._owned = False
        self._is_playing = None
        self._stop_playback = None
        if is_playing is not None and is_playing():
            stop_playback()
        return True


timeline_playback = TimelinePlaybackOwner()


def _copy_coordinates(coordinates):
    try:
        copied = array('f', coordinates)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RecordingError("Performance coordinates must be finite numeric values") from exc
    _validate_coordinates(copied)
    return copied


def _validate_coordinates(coordinates, expected_values=None):
    if not isinstance(coordinates, array) or coordinates.typecode != 'f':
        raise RecordingError("Performance coordinates must be an array('f')")
    if len(coordinates) % 3:
        raise RecordingError("Performance coordinates must contain complete XYZ vertices")
    if expected_values is not None and len(coordinates) != expected_values:
        raise RecordingError(
            "Performance sample has {} coordinates; expected {}".format(
                len(coordinates), expected_values
            )
        )
    if any(not math.isfinite(float(value)) for value in coordinates):
        raise RecordingError("Performance coordinates must contain only finite values")


def _validate_world_location(world_location):
    try:
        values = tuple(float(value) for value in world_location)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RecordingError("Mario world location must contain three finite values") from exc
    if len(values) != 3 or any(not math.isfinite(value) for value in values):
        raise RecordingError("Mario world location must contain three finite values")
    return values


def _validate_face_angle(face_angle):
    try:
        value = float(face_angle)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RecordingError("Mario face angle must be a finite value in radians") from exc
    if not math.isfinite(value):
        raise RecordingError("Mario face angle must be a finite value in radians")
    return value


def _validate_int(name, value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecordingError("{} must be an integer".format(name))
    if value < minimum or value > maximum:
        raise RecordingError(
            "{} must be between {} and {}".format(name, minimum, maximum)
        )
    return value


def _validate_float3(name, value):
    try:
        values = tuple(float(component) for component in value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RecordingError("{} must contain three finite values".format(name)) from exc
    if len(values) != 3 or any(not math.isfinite(component) for component in values):
        raise RecordingError("{} must contain three finite values".format(name))
    return values


@dataclass(frozen=True)
class MarioRuntimeMetadata:
    """Immutable public libsm64 state captured from one geometry-producing tick."""

    native_position: tuple
    native_velocity: tuple
    face_angle: float
    forward_velocity: float
    health: int
    action: int
    animation_id: int
    animation_frame: int
    flags: int
    particle_flags: int
    invincibility_timer: int

    def __post_init__(self):
        object.__setattr__(
            self, "native_position", _validate_float3("native_position", self.native_position)
        )
        object.__setattr__(
            self, "native_velocity", _validate_float3("native_velocity", self.native_velocity)
        )
        object.__setattr__(self, "face_angle", _validate_face_angle(self.face_angle))
        try:
            forward_velocity = float(self.forward_velocity)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RecordingError("forward_velocity must be finite") from exc
        if not math.isfinite(forward_velocity):
            raise RecordingError("forward_velocity must be finite")
        object.__setattr__(self, "forward_velocity", forward_velocity)
        _validate_int("health", self.health, -0x8000, 0x7FFF)
        _validate_int("action", self.action, 0, 0xFFFFFFFF)
        _validate_int("animation_id", self.animation_id, -0x80000000, 0x7FFFFFFF)
        _validate_int("animation_frame", self.animation_frame, -0x8000, 0x7FFF)
        _validate_int("flags", self.flags, 0, 0xFFFFFFFF)
        _validate_int("particle_flags", self.particle_flags, 0, 0xFFFFFFFF)
        _validate_int(
            "invincibility_timer", self.invincibility_timer, -0x8000, 0x7FFF
        )

    def to_json_record(self):
        return {
            "native_position": list(self.native_position),
            "native_velocity": list(self.native_velocity),
            "face_angle": self.face_angle,
            "forward_velocity": self.forward_velocity,
            "health": self.health,
            "action": self.action,
            "animation_id": self.animation_id,
            "animation_frame": self.animation_frame,
            "flags": self.flags,
            "particle_flags": self.particle_flags,
            "invincibility_timer": self.invincibility_timer,
        }

    @classmethod
    def from_json_record(cls, record):
        if not isinstance(record, dict):
            raise RecordingError("Runtime metadata sample must be a JSON object")
        fields = (
            "native_position", "native_velocity", "face_angle", "forward_velocity",
            "health", "action", "animation_id", "animation_frame", "flags",
            "particle_flags", "invincibility_timer",
        )
        missing = [name for name in fields if name not in record]
        if missing:
            raise RecordingError(
                "Runtime metadata sample is missing: {}".format(", ".join(missing))
            )
        return cls(**{name: record[name] for name in fields})


class PerformanceSample:
    """One immutable-transform, fixed-topology performance snapshot."""

    __slots__ = ("_coordinates", "_world_location", "_face_angle", "_runtime_metadata")

    def __init__(self, coordinates, world_location, face_angle, runtime_metadata=None):
        self._coordinates = _copy_coordinates(coordinates)
        self._world_location = _validate_world_location(world_location)
        self._face_angle = _validate_face_angle(face_angle)
        if runtime_metadata is not None and not isinstance(
                runtime_metadata, MarioRuntimeMetadata):
            raise RecordingError("Runtime metadata must be a MarioRuntimeMetadata record")
        self._runtime_metadata = runtime_metadata

    @classmethod
    def _from_owned_coordinates(
            cls, coordinates, world_location, face_angle, runtime_metadata=None):
        """Build from a fresh recorder buffer without making a second copy."""
        _validate_coordinates(coordinates)
        instance = cls.__new__(cls)
        instance._coordinates = coordinates
        instance._world_location = _validate_world_location(world_location)
        instance._face_angle = _validate_face_angle(face_angle)
        if runtime_metadata is not None and not isinstance(
                runtime_metadata, MarioRuntimeMetadata):
            raise RecordingError("Runtime metadata must be a MarioRuntimeMetadata record")
        instance._runtime_metadata = runtime_metadata
        return instance

    @property
    def coordinates(self):
        return self._coordinates

    @property
    def world_location(self):
        return self._world_location

    @property
    def face_angle(self):
        return self._face_angle

    @property
    def runtime_metadata(self):
        return self._runtime_metadata


def validate_performance_sample(sample, expected_values=None):
    """Validate a structured sample without accepting legacy coordinate arrays."""
    if not isinstance(sample, PerformanceSample):
        raise RecordingError(
            "A structured PerformanceSample with transform metadata is required"
        )
    _validate_coordinates(sample.coordinates, expected_values)
    _validate_world_location(sample.world_location)
    _validate_face_angle(sample.face_angle)
    return sample


def unwrap_face_angles(face_angles):
    """Return nearest-equivalent angles with continuous turns across +/- pi."""
    validated = [_validate_face_angle(angle) for angle in face_angles]
    if not validated:
        return ()
    unwrapped = [validated[0]]
    previous_raw = validated[0]
    for angle in validated[1:]:
        delta = (angle - previous_raw + math.pi) % (2.0 * math.pi) - math.pi
        unwrapped.append(unwrapped[-1] + delta)
        previous_raw = angle
    return tuple(unwrapped)


def world_to_mario_local(coordinates, world_location, face_angle):
    """Remove Mario's world translation and Z-facing rotation from vertices."""
    coordinates = _copy_coordinates(coordinates)
    location_x, location_y, location_z = _validate_world_location(world_location)
    angle = _validate_face_angle(face_angle)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    localized = array('f', [0.0]) * len(coordinates)
    for base in range(0, len(coordinates), 3):
        delta_x = coordinates[base] - location_x
        delta_y = coordinates[base + 1] - location_y
        delta_z = coordinates[base + 2] - location_z
        localized[base] = cosine * delta_x + sine * delta_y
        localized[base + 1] = -sine * delta_x + cosine * delta_y
        localized[base + 2] = delta_z
    return localized


def mario_local_to_world(coordinates, world_location, face_angle):
    """Reconstruct world vertices from a Mario-local pose and object transform."""
    coordinates = _copy_coordinates(coordinates)
    location_x, location_y, location_z = _validate_world_location(world_location)
    angle = _validate_face_angle(face_angle)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    reconstructed = array('f', [0.0]) * len(coordinates)
    for base in range(0, len(coordinates), 3):
        local_x = coordinates[base]
        local_y = coordinates[base + 1]
        local_z = coordinates[base + 2]
        reconstructed[base] = cosine * local_x - sine * local_y + location_x
        reconstructed[base + 1] = sine * local_x + cosine * local_y + location_y
        reconstructed[base + 2] = local_z + location_z
    return reconstructed


def sample_target_frame(start_frame, sample_index, target_fps, sample_fps=SAMPLE_FPS):
    """Map a sequential simulation sample onto the target Blender timeline."""
    if sample_index < 0:
        raise ValueError("sample_index must be non-negative")
    if target_fps <= 0 or sample_fps <= 0:
        raise ValueError("frame rates must be positive")
    return float(start_frame) + float(sample_index) * float(target_fps) / float(sample_fps)


def held_runtime_sample_index(
        frame, start_frame, target_fps, sample_count, sample_fps=SAMPLE_FPS):
    """Map a Blender frame to the constant-held runtime sample used by playback."""
    frame = float(frame)
    start_frame = float(start_frame)
    target_fps = float(target_fps)
    sample_fps = float(sample_fps)
    if not all(math.isfinite(value) for value in (
            frame, start_frame, target_fps, sample_fps)):
        raise ValueError("Frame mapping values must be finite")
    if target_fps <= 0.0 or sample_fps <= 0.0:
        raise ValueError("Frame rates must be positive")
    if isinstance(sample_count, bool) or not isinstance(sample_count, int):
        raise ValueError("sample_count must be an integer")
    if sample_count <= 0:
        raise ValueError("sample_count must be positive")
    relative_sample = (frame - start_frame) * sample_fps / target_fps
    held = int(math.floor(relative_sample + 1.0e-9))
    return min(sample_count - 1, max(0, held))


def _runtime_metadata_document(samples, start_frame, target_fps, take_id):
    if not isinstance(take_id, str) or not take_id:
        raise RecordingError("Runtime metadata requires a take owner ID")
    metadata = []
    for index, sample in enumerate(samples):
        validate_performance_sample(sample)
        if sample.runtime_metadata is None:
            raise RecordingError(
                "Sample {} has no same-tick Mario runtime metadata".format(index)
            )
        metadata.append(sample.runtime_metadata.to_json_record())
    if not metadata:
        raise RecordingError("Runtime metadata requires at least one sample")
    start_frame = float(start_frame)
    target_fps = float(target_fps)
    if not math.isfinite(start_frame) or not math.isfinite(target_fps) or target_fps <= 0:
        raise RecordingError("Runtime metadata frame mapping is invalid")
    sample_frames = [
        sample_target_frame(start_frame, index, target_fps)
        for index in range(len(metadata))
    ]
    return {
        "schema_version": RUNTIME_METADATA_SCHEMA_VERSION,
        "sample_rate": SAMPLE_FPS,
        "target_fps": target_fps,
        "sample_count": len(metadata),
        "sample_to_frame_mapping": {
            "mode": "constant_hold",
            "start_frame": start_frame,
            "sample_frames": sample_frames,
        },
        "coordinate_conventions": {
            "native_axes": "libsm64 X/Y(up)/Z",
            "blender_axes": "Blender X/native X, Blender Z/native Y, -Blender Y/native Z",
            "native_position_units": "libsm64 units relative to the recording session origin",
            "angles": "radians",
            "metadata_effect": "inspection_only; geometry playback remains authoritative",
        },
        "source_take_owner_id": take_id,
        "samples": metadata,
    }


def create_take_runtime_metadata_text(
        baked_object, samples, take_id, take_number, start_frame, target_fps):
    """Create one exclusively referenced, compact JSON Text datablock."""
    import bpy

    if baked_object.get(RUNTIME_METADATA_TEXT_PROPERTY):
        raise RecordingError("The candidate take already references runtime metadata")
    document = _runtime_metadata_document(
        samples, start_frame, target_fps, take_id
    )
    text = bpy.data.texts.new(
        "LibSM64 Studio Take {:03d} Runtime Metadata".format(int(take_number))
    )
    try:
        text[RUNTIME_METADATA_OWNER_PROPERTY] = take_id
        text[RUNTIME_METADATA_SCHEMA_PROPERTY] = RUNTIME_METADATA_SCHEMA_VERSION
        text.write(json.dumps(
            document, sort_keys=True, separators=(",", ":"), allow_nan=False
        ))
        baked_object[RUNTIME_METADATA_TEXT_PROPERTY] = text.name
        validate_take_runtime_metadata(baked_object, expected_take_id=take_id)
        return text
    except Exception:
        if baked_object.get(RUNTIME_METADATA_TEXT_PROPERTY) == text.name:
            del baked_object[RUNTIME_METADATA_TEXT_PROPERTY]
        if bpy.data.texts.get(text.name) is text:
            bpy.data.texts.remove(text)
        raise


def take_runtime_metadata_text(baked_object, require=False):
    """Resolve one take's exclusive Text reference without needing native runtime."""
    import bpy

    name = baked_object.get(RUNTIME_METADATA_TEXT_PROPERTY, "")
    if not name:
        if require:
            raise RecordingError("This take has no runtime metadata Text datablock")
        return None
    text = bpy.data.texts.get(name)
    if text is None:
        if require:
            raise RecordingError("The take's runtime metadata Text datablock is missing")
        return None
    references = [
        obj for obj in bpy.data.objects
        if obj.get(RUNTIME_METADATA_TEXT_PROPERTY, "") == text.name
    ]
    if len(references) != 1 or references[0] is not baked_object:
        raise RecordingError("Runtime metadata Text ownership is not exclusive")
    return text


def validate_take_runtime_metadata(baked_object, expected_take_id=None):
    """Parse and validate one persisted runtime document; legacy takes return None."""
    text = take_runtime_metadata_text(baked_object, require=False)
    if text is None:
        return None
    take_id = expected_take_id or baked_object.get("libsm64_take_id", "")
    if not take_id:
        raise RecordingError("Runtime metadata owner cannot be validated")
    if text.get(RUNTIME_METADATA_OWNER_PROPERTY) != take_id:
        raise RecordingError("Runtime metadata Text owner does not match the take")
    try:
        document = json.loads(text.as_string())
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RecordingError("Runtime metadata Text is not valid JSON") from exc
    if not isinstance(document, dict):
        raise RecordingError("Runtime metadata document must be a JSON object")
    if document.get("schema_version") != RUNTIME_METADATA_SCHEMA_VERSION:
        raise RecordingError("Unsupported runtime metadata schema")
    if document.get("source_take_owner_id") != take_id:
        raise RecordingError("Runtime metadata document owner does not match the take")
    sample_count = document.get("sample_count")
    _validate_int("sample_count", sample_count, 1, 0x7FFFFFFF)
    samples = document.get("samples")
    if not isinstance(samples, list) or len(samples) != sample_count:
        raise RecordingError("Runtime metadata sample count is inconsistent")
    parsed_samples = tuple(
        MarioRuntimeMetadata.from_json_record(sample) for sample in samples
    )
    sample_rate = float(document.get("sample_rate", 0.0))
    target_fps = float(document.get("target_fps", 0.0))
    mapping = document.get("sample_to_frame_mapping")
    if (
        not math.isfinite(sample_rate) or sample_rate <= 0.0
        or not math.isfinite(target_fps) or target_fps <= 0.0
        or not isinstance(mapping, dict)
        or mapping.get("mode") != "constant_hold"
    ):
        raise RecordingError("Runtime metadata timing is invalid")
    start_frame = float(mapping.get("start_frame", float("nan")))
    frames = mapping.get("sample_frames")
    expected_frames = [
        sample_target_frame(start_frame, index, target_fps, sample_rate)
        for index in range(sample_count)
    ] if math.isfinite(start_frame) else []
    if (
        not math.isfinite(start_frame)
        or not isinstance(frames, list)
        or len(frames) != sample_count
        or any(
            not math.isclose(float(actual), expected, rel_tol=0.0, abs_tol=1.0e-9)
            for actual, expected in zip(frames, expected_frames)
        )
    ):
        raise RecordingError("Runtime metadata sample-to-frame mapping is invalid")
    return {
        "document": document,
        "samples": parsed_samples,
        "sample_rate": sample_rate,
        "target_fps": target_fps,
        "start_frame": start_frame,
        "text": text,
    }


def runtime_metadata_at_frame(baked_object, frame):
    validated = validate_take_runtime_metadata(baked_object)
    if validated is None:
        return None
    index = held_runtime_sample_index(
        frame,
        validated["start_frame"],
        validated["target_fps"],
        len(validated["samples"]),
        validated["sample_rate"],
    )
    return index, validated["samples"][index], validated


def remove_take_runtime_metadata_text(baked_object, expected_take_id=None):
    """Delete only an exclusively referenced Text proven to belong to this take."""
    import bpy

    text = take_runtime_metadata_text(baked_object, require=False)
    if text is None:
        return False
    take_id = expected_take_id or baked_object.get("libsm64_take_id", "")
    if not take_id or text.get(RUNTIME_METADATA_OWNER_PROPERTY) != take_id:
        return False
    if RUNTIME_METADATA_TEXT_PROPERTY in baked_object:
        del baked_object[RUNTIME_METADATA_TEXT_PROPERTY]
    if bpy.data.texts.get(text.name) is text:
        bpy.data.texts.remove(text)
    return True


class GeometryRecorder:
    """Owns one pending recording without depending on Blender types."""

    IDLE = "Idle"
    RECORDING = "Recording"
    BAKING = "Baking"
    COMPLETE = "Complete"
    ERROR = "Error"

    def __init__(self):
        self.reset()

    def reset(self, message="Ready"):
        self.active = False
        self.samples = []
        self.start_frame = 0.0
        self.target_fps = 0.0
        self.vertex_count = 0
        self.last_sample_id = None
        self.status = self.IDLE
        self.message = message
        self.completed_sample_count = 0

    @property
    def sample_count(self):
        return len(self.samples) if self.samples else self.completed_sample_count

    @property
    def duration_seconds(self):
        return float(self.sample_count) / SAMPLE_FPS

    @property
    def has_pending_samples(self):
        return bool(self.samples)

    def start(self, start_frame, target_fps):
        if target_fps <= 0:
            raise RecordingError("The target scene FPS must be greater than zero")
        self.reset()
        self.active = True
        self.start_frame = float(start_frame)
        self.target_fps = float(target_fps)
        self.status = self.RECORDING
        self.message = "Capturing one mesh snapshot per libsm64 tick"

    def capture_mesh(
            self, mesh, sample_id, world_location, face_angle, runtime_metadata=None):
        """Capture geometry and same-tick Mario transform metadata."""
        if not self.active:
            return False
        if sample_id == self.last_sample_id:
            return False

        vertex_count = len(mesh.vertices)
        if self.vertex_count and vertex_count != self.vertex_count:
            self.fail("Mario mesh topology changed while recording", preserve_samples=True)
            raise RecordingError(self.message)

        coordinates = array('f', [0.0]) * (vertex_count * 3)
        mesh.vertices.foreach_get("co", coordinates)
        try:
            sample = PerformanceSample._from_owned_coordinates(
                coordinates, world_location, face_angle, runtime_metadata
            )
        except RecordingError as exc:
            self.fail(str(exc), preserve_samples=True)
            raise
        self.samples.append(sample)
        self.vertex_count = vertex_count
        self.last_sample_id = sample_id

        if len(self.samples) == SHORT_TAKE_WARNING_SAMPLES:
            self.message = "300 samples captured; short takes are recommended"
        return True

    def freeze_for_bake(self):
        """Stop capture immediately and return a stable snapshot list."""
        self.active = False
        if not self.samples:
            self.status = self.ERROR
            self.message = "No Mario samples were captured"
            raise RecordingError(self.message)
        self.status = self.BAKING
        self.message = "Building shape keys"
        return tuple(self.samples)

    def complete(self, object_name):
        count = len(self.samples)
        self.active = False
        self.completed_sample_count = count
        self.samples = []
        self.status = self.COMPLETE
        self.message = "Baked {} samples to {}".format(count, object_name)

    def cancel(self, message="Recording cancelled"):
        self.reset(message)

    def fail(self, message, preserve_samples=True):
        self.active = False
        if not preserve_samples:
            self.samples = []
            self.vertex_count = 0
            self.last_sample_id = None
        self.status = self.ERROR
        self.message = str(message)


recorder = GeometryRecorder()


def _set_coordinates(collection, coordinates):
    """Use bulk assignment where available, with an old-Blender fallback."""
    foreach_set = getattr(collection, "foreach_set", None)
    if foreach_set is not None:
        foreach_set("co", coordinates)
        return
    for index, point in enumerate(collection):
        base = index * 3
        point.co = coordinates[base:base + 3]


def _copy_object_display_settings(source, destination):
    for attribute in (
        "color", "display_type", "show_all_edges", "show_axis", "show_bounds",
        "show_in_front", "show_name", "show_texture_space", "show_transparent",
        "show_wire", "visible_camera", "visible_diffuse", "visible_glossy",
        "visible_shadow", "visible_transmission", "visible_volume_scatter",
    ):
        if hasattr(source, attribute) and hasattr(destination, attribute):
            try:
                setattr(destination, attribute, getattr(source, attribute))
            except (AttributeError, TypeError):
                pass


def iter_action_fcurves(action):
    """Yield curves from legacy actions and Blender 4.4+ layered actions."""
    legacy_fcurves = getattr(action, "fcurves", None)
    if legacy_fcurves is not None:
        for fcurve in legacy_fcurves:
            yield fcurve
        return

    for layer in getattr(action, "layers", ()):
        for strip in getattr(layer, "strips", ()):
            for channelbag in getattr(strip, "channelbags", ()):
                for fcurve in channelbag.fcurves:
                    yield fcurve


def _remove_take_datablocks(
        baked_object, baked_mesh=None, pose_action=None, transform_action=None):
    """Remove a partial take and only the datablocks created for that take."""
    import bpy

    if baked_object is not None and bpy.data.objects.get(baked_object.name) is baked_object:
        bpy.data.objects.remove(baked_object, do_unlink=True)
    if baked_mesh is not None and baked_mesh.users == 0 and bpy.data.meshes.get(baked_mesh.name) is baked_mesh:
        bpy.data.meshes.remove(baked_mesh)
    for action in (pose_action, transform_action):
        if (
            action is not None
            and action.users == 0
            and bpy.data.actions.get(action.name) is action
        ):
            bpy.data.actions.remove(action)


def discard_baked_take(baked_object):
    """Roll back a fully built but uncommitted baked take."""
    remove_take_runtime_metadata_text(baked_object)
    mesh = getattr(baked_object, "data", None)
    key_data = getattr(mesh, "shape_keys", None) if mesh is not None else None
    pose_animation = getattr(key_data, "animation_data", None)
    pose_action = getattr(pose_animation, "action", None)
    object_animation = getattr(baked_object, "animation_data", None)
    transform_action = getattr(object_animation, "action", None)
    _remove_take_datablocks(baked_object, mesh, pose_action, transform_action)


def _is_schema_2_bake(baked_object):
    try:
        version = int(baked_object.get(BAKE_SCHEMA_VERSION, 1))
    except (TypeError, ValueError):
        version = 1
    return version >= CURRENT_BAKE_SCHEMA_VERSION


def validate_take_ownership(baked_object, source_object=None, require_schema_2=False):
    """Prove that a bake exclusively owns every mutable animation datablock."""
    import bpy

    if baked_object is None or getattr(baked_object, "type", None) != 'MESH':
        raise RecordingError("The baked take object is unavailable")
    mesh = baked_object.data
    key_data = getattr(mesh, "shape_keys", None)
    pose_animation = getattr(key_data, "animation_data", None)
    pose_action = getattr(pose_animation, "action", None)
    object_animation = getattr(baked_object, "animation_data", None)
    transform_action = getattr(object_animation, "action", None)
    schema_2 = _is_schema_2_bake(baked_object)
    if require_schema_2 and not schema_2:
        raise RecordingError("A new baked take must use bake schema version 2")
    if schema_2 and baked_object.get(BAKE_LAYOUT) != OBJECT_MOTION_LOCAL_POSE:
        raise RecordingError("The schema-2 baked take has an unsupported bake layout")
    if source_object is not None:
        if baked_object is source_object:
            raise RecordingError("The baked take reused Live Mario's object")
        if mesh is source_object.data:
            raise RecordingError("The baked take reused Live Mario's mesh")
        if key_data is not None and key_data is getattr(source_object.data, "shape_keys", None):
            raise RecordingError("The baked take reused Live Mario's shape keys")
        source_object_animation = getattr(source_object, "animation_data", None)
        source_transform_action = getattr(source_object_animation, "action", None)
        source_keys = getattr(source_object.data, "shape_keys", None)
        source_pose_animation = getattr(source_keys, "animation_data", None)
        source_pose_action = getattr(source_pose_animation, "action", None)
        if transform_action is not None and transform_action in (
                source_transform_action, source_pose_action):
            raise RecordingError("The baked take reused a Live Mario action")
        if pose_action is not None and pose_action in (
                source_transform_action, source_pose_action):
            raise RecordingError("The baked take reused a Live Mario action")
    if mesh.users != 1:
        raise RecordingError("The baked take mesh has {} users; expected 1".format(mesh.users))
    if key_data is None:
        raise RecordingError("The baked take has no shape-key datablock")
    if key_data.users != 1:
        raise RecordingError(
            "The baked take shape-key datablock has {} users; expected 1".format(key_data.users)
        )
    if pose_action is None:
        raise RecordingError("The baked take has no pose action")
    if pose_action.users != 1:
        raise RecordingError(
            "The baked take pose action has {} users; expected 1".format(pose_action.users)
        )
    if schema_2:
        if transform_action is None:
            raise RecordingError("The schema-2 baked take has no transform action")
        if transform_action is pose_action:
            raise RecordingError("The baked take transform and pose actions must be distinct")
        if transform_action.users != 1:
            raise RecordingError(
                "The baked take transform action has {} users; expected 1".format(
                    transform_action.users
                )
            )

    for other in bpy.data.objects:
        if other is not baked_object and getattr(other, "data", None) is mesh:
            raise RecordingError("Another object shares the baked take mesh")
        other_mesh = getattr(other, "data", None)
        other_keys = getattr(other_mesh, "shape_keys", None)
        if other is not baked_object and other_keys is key_data:
            raise RecordingError("Another object shares the baked take shape keys")
        other_pose_animation = getattr(other_keys, "animation_data", None)
        other_pose_action = getattr(other_pose_animation, "action", None)
        other_object_animation = getattr(other, "animation_data", None)
        other_transform_action = getattr(other_object_animation, "action", None)
        if other is not baked_object and pose_action in (
                other_pose_action, other_transform_action):
            raise RecordingError("Another object shares the baked take pose action")
        if other is not baked_object and transform_action is not None and transform_action in (
                other_pose_action, other_transform_action):
            raise RecordingError("Another object shares the baked take transform action")
    return mesh, key_data, pose_action, transform_action


def _require_identity_source_transform(source_object, tolerance=1e-6):
    expected = (
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
    def matrix_is_identity(matrix):
        try:
            return all(
                abs(float(matrix[row][column]) - expected[row][column]) <= tolerance
                for row in range(4)
                for column in range(4)
            )
        except (AttributeError, IndexError, TypeError):
            return False

    is_identity = (
        getattr(source_object, "parent", None) is None
        and matrix_is_identity(getattr(source_object, "matrix_basis", None))
        and matrix_is_identity(getattr(source_object, "matrix_world", None))
    )
    if not is_identity:
        raise RecordingError(
            "Live Mario must have an identity world transform before baking; "
            "its mesh coordinates already contain Blender-world positions"
        )


def bake_shape_keys(context, source_object, samples, start_frame, target_fps):
    """Create synchronized object motion and Mario-local shape-key animation."""
    import bpy

    if source_object is None or getattr(source_object, "type", None) != 'MESH':
        raise RecordingError("The Live Mario mesh is unavailable")
    if not samples:
        raise RecordingError("No Mario samples were captured")
    _require_identity_source_transform(source_object)

    vertex_count = len(source_object.data.vertices)
    expected_values = vertex_count * 3
    validated_samples = []
    for index, sample in enumerate(samples):
        try:
            validated_samples.append(validate_performance_sample(sample, expected_values))
        except RecordingError as exc:
            raise RecordingError("Sample {} is invalid: {}".format(index, exc)) from exc
    unwrapped_angles = unwrap_face_angles(
        sample.face_angle for sample in validated_samples
    )
    localized_samples = [
        world_to_mario_local(sample.coordinates, sample.world_location, angle)
        for sample, angle in zip(validated_samples, unwrapped_angles)
    ]

    source_mesh = source_object.data
    baked_mesh = source_mesh.copy()
    baked_mesh.name = "LibSM64 Studio Performance Take Mesh"
    _set_coordinates(baked_mesh.vertices, localized_samples[0])
    baked_mesh.update()

    baked_object = bpy.data.objects.new("LibSM64 Studio Performance Take", baked_mesh)
    pose_action = None
    transform_action = None
    try:
        collections = list(source_object.users_collection)
        target_collection = collections[0] if collections else context.scene.collection
        target_collection.objects.link(baked_object)
        baked_object.parent = None
        baked_object.location = validated_samples[0].world_location
        baked_object.rotation_mode = 'XYZ'
        baked_object.rotation_euler = (0.0, 0.0, unwrapped_angles[0])
        baked_object.scale = (1.0, 1.0, 1.0)
        _copy_object_display_settings(source_object, baked_object)

        # Mesh.copy() must not leave a nested Key datablock shared with Live
        # Mario. Clear only a proven-exclusive copied Key before creating the
        # take's own shape-key stack.
        copied_keys = getattr(baked_mesh, "shape_keys", None)
        if copied_keys is not None:
            if copied_keys is getattr(source_mesh, "shape_keys", None) or copied_keys.users != 1:
                raise RecordingError("Could not create exclusive shape keys for the new take")
            baked_object.shape_key_clear()

        baked_object.shape_key_add(name="Basis", from_mix=False)
        pose_keys = []
        for index, coordinates in enumerate(localized_samples):
            key = baked_object.shape_key_add(
                name="Mario Pose {:04d}".format(index + 1), from_mix=False
            )
            _set_coordinates(key.data, coordinates)
            key.value = 0.0
            pose_keys.append(key)

        key_data = baked_object.data.shape_keys
        key_data.name = "{} Shape Keys".format(baked_object.name)
        key_data.animation_data_create()
        pose_action = bpy.data.actions.new(
            "LibSM64 Studio Performance Take Pose Action"
        )
        key_data.animation_data.action = pose_action

        baked_object.animation_data_create()
        transform_action = bpy.data.actions.new(
            "LibSM64 Studio Performance Take Transform Action"
        )
        baked_object.animation_data.action = transform_action

        interval = float(target_fps) / SAMPLE_FPS
        for index, key in enumerate(pose_keys):
            target = sample_target_frame(start_frame, index, target_fps)
            key.value = 0.0
            key.keyframe_insert(data_path="value", frame=target - interval)
            key.value = 1.0
            key.keyframe_insert(data_path="value", frame=target)
            key.value = 0.0
            key.keyframe_insert(data_path="value", frame=target + interval)

        for fcurve in iter_action_fcurves(pose_action):
            for point in fcurve.keyframe_points:
                point.interpolation = 'CONSTANT'

        for index, (sample, angle) in enumerate(zip(validated_samples, unwrapped_angles)):
            target = sample_target_frame(start_frame, index, target_fps)
            baked_object.location = sample.world_location
            for component in range(3):
                baked_object.keyframe_insert(
                    data_path="location", index=component, frame=target
                )
            baked_object.rotation_euler = (0.0, 0.0, angle)
            baked_object.keyframe_insert(
                data_path="rotation_euler", index=2, frame=target
            )

        for fcurve in iter_action_fcurves(transform_action):
            for point in fcurve.keyframe_points:
                point.interpolation = 'CONSTANT'

        baked_object.location = validated_samples[0].world_location
        baked_object.rotation_euler = (0.0, 0.0, unwrapped_angles[0])
        baked_object.scale = (1.0, 1.0, 1.0)

        baked_object["libsm64_source_addon"] = "LibSM64 Studio"
        baked_object["libsm64_is_bake"] = True
        baked_object[BAKE_SCHEMA_VERSION] = CURRENT_BAKE_SCHEMA_VERSION
        baked_object[BAKE_LAYOUT] = OBJECT_MOTION_LOCAL_POSE
        baked_object["libsm64_sample_count"] = len(validated_samples)
        baked_object["libsm64_sample_fps"] = SAMPLE_FPS
        baked_object["libsm64_target_fps"] = float(target_fps)
        baked_object["libsm64_recording_start_frame"] = float(start_frame)

        # Keep the candidate invisible until registration has validated it and
        # committed the current-take visibility transition.
        baked_object.hide_set(True)
        baked_object.hide_render = True
        validate_take_ownership(baked_object, source_object, require_schema_2=True)
        return baked_object
    except Exception:
        _remove_take_datablocks(
            baked_object, baked_mesh, pose_action, transform_action
        )
        raise
