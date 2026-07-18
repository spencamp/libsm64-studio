"""Short-take geometry recording and Blender shape-key baking.

The recorder and timing helpers deliberately do not import bpy so their state
handling can be tested with ordinary Python.  Blender-specific baking is kept
behind :func:`bake_shape_keys`.
"""

from array import array
import math


SAMPLE_FPS = 30.0
SHORT_TAKE_WARNING_SAMPLES = 300
BAKE_SCHEMA_VERSION = "libsm64_bake_schema_version"
BAKE_LAYOUT = "libsm64_bake_layout"
CURRENT_BAKE_SCHEMA_VERSION = 2
OBJECT_MOTION_LOCAL_POSE = "OBJECT_MOTION_LOCAL_POSE"


class RecordingError(RuntimeError):
    pass


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


class PerformanceSample:
    """One immutable-transform, fixed-topology performance snapshot."""

    __slots__ = ("_coordinates", "_world_location", "_face_angle")

    def __init__(self, coordinates, world_location, face_angle):
        self._coordinates = _copy_coordinates(coordinates)
        self._world_location = _validate_world_location(world_location)
        self._face_angle = _validate_face_angle(face_angle)

    @classmethod
    def _from_owned_coordinates(cls, coordinates, world_location, face_angle):
        """Build from a fresh recorder buffer without making a second copy."""
        _validate_coordinates(coordinates)
        instance = cls.__new__(cls)
        instance._coordinates = coordinates
        instance._world_location = _validate_world_location(world_location)
        instance._face_angle = _validate_face_angle(face_angle)
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

    def capture_mesh(self, mesh, sample_id, world_location, face_angle):
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
                coordinates, world_location, face_angle
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
