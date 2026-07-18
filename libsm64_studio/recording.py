"""Short-take geometry recording and Blender shape-key baking.

The recorder and timing helpers deliberately do not import bpy so their state
handling can be tested with ordinary Python.  Blender-specific baking is kept
behind :func:`bake_shape_keys`.
"""

from array import array


SAMPLE_FPS = 30.0
SHORT_TAKE_WARNING_SAMPLES = 300


class RecordingError(RuntimeError):
    pass


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

    def capture_mesh(self, mesh, sample_id):
        """Capture the complete fixed-topology mesh using Blender's bulk API."""
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
        self.samples.append(coordinates)
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


def _remove_take_datablocks(baked_object, baked_mesh=None, action=None):
    """Remove a partial take and only the datablocks created for that take."""
    import bpy

    if baked_object is not None and bpy.data.objects.get(baked_object.name) is baked_object:
        bpy.data.objects.remove(baked_object, do_unlink=True)
    if action is not None and action.users == 0 and bpy.data.actions.get(action.name) is action:
        bpy.data.actions.remove(action)
    if baked_mesh is not None and baked_mesh.users == 0 and bpy.data.meshes.get(baked_mesh.name) is baked_mesh:
        bpy.data.meshes.remove(baked_mesh)


def discard_baked_take(baked_object):
    """Roll back a fully built but uncommitted baked take."""
    mesh = getattr(baked_object, "data", None)
    key_data = getattr(mesh, "shape_keys", None) if mesh is not None else None
    animation_data = getattr(key_data, "animation_data", None)
    action = getattr(animation_data, "action", None)
    _remove_take_datablocks(baked_object, mesh, action)


def validate_take_ownership(baked_object, source_object=None):
    """Prove that a bake exclusively owns every mutable animation datablock."""
    import bpy

    if baked_object is None or getattr(baked_object, "type", None) != 'MESH':
        raise RecordingError("The baked take object is unavailable")
    mesh = baked_object.data
    key_data = getattr(mesh, "shape_keys", None)
    animation_data = getattr(key_data, "animation_data", None)
    action = getattr(animation_data, "action", None)
    if source_object is not None:
        if baked_object is source_object:
            raise RecordingError("The baked take reused Live Mario's object")
        if mesh is source_object.data:
            raise RecordingError("The baked take reused Live Mario's mesh")
        if key_data is not None and key_data is getattr(source_object.data, "shape_keys", None):
            raise RecordingError("The baked take reused Live Mario's shape keys")
    if mesh.users != 1:
        raise RecordingError("The baked take mesh has {} users; expected 1".format(mesh.users))
    if key_data is None:
        raise RecordingError("The baked take has no shape-key datablock")
    if key_data.users != 1:
        raise RecordingError(
            "The baked take shape-key datablock has {} users; expected 1".format(key_data.users)
        )
    if action is None:
        raise RecordingError("The baked take has no shape-key action")
    if action.users != 1:
        raise RecordingError("The baked take action has {} users; expected 1".format(action.users))

    for other in bpy.data.objects:
        if other is not baked_object and getattr(other, "data", None) is mesh:
            raise RecordingError("Another object shares the baked take mesh")
        other_mesh = getattr(other, "data", None)
        other_keys = getattr(other_mesh, "shape_keys", None)
        if other is not baked_object and other_keys is key_data:
            raise RecordingError("Another object shares the baked take shape keys")
        other_animation = getattr(other_keys, "animation_data", None)
        if other is not baked_object and getattr(other_animation, "action", None) is action:
            raise RecordingError("Another object shares the baked take action")
    return mesh, key_data, action


def bake_shape_keys(context, source_object, samples, start_frame, target_fps):
    """Create a self-contained, fixed-topology Blender shape-key animation."""
    import bpy

    if source_object is None or getattr(source_object, "type", None) != 'MESH':
        raise RecordingError("The Live Mario mesh is unavailable")
    if not samples:
        raise RecordingError("No Mario samples were captured")

    vertex_count = len(source_object.data.vertices)
    expected_values = vertex_count * 3
    for index, coordinates in enumerate(samples):
        if len(coordinates) != expected_values:
            raise RecordingError(
                "Sample {} has {} coordinates; expected {}".format(
                    index, len(coordinates), expected_values
                )
            )

    source_mesh = source_object.data
    baked_mesh = source_mesh.copy()
    baked_mesh.name = "LibSM64 Studio Performance Take Mesh"
    _set_coordinates(baked_mesh.vertices, samples[0])
    baked_mesh.update()

    baked_object = bpy.data.objects.new("LibSM64 Studio Performance Take", baked_mesh)
    action = None
    try:
        collections = list(source_object.users_collection)
        target_collection = collections[0] if collections else context.scene.collection
        target_collection.objects.link(baked_object)
        baked_object.matrix_world = source_object.matrix_world.copy()
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
        for index, coordinates in enumerate(samples):
            key = baked_object.shape_key_add(
                name="Mario Pose {:04d}".format(index + 1), from_mix=False
            )
            _set_coordinates(key.data, coordinates)
            key.value = 0.0
            pose_keys.append(key)

        key_data = baked_object.data.shape_keys
        key_data.name = "{} Shape Keys".format(baked_object.name)
        key_data.animation_data_create()
        action = bpy.data.actions.new("{} Action".format(baked_object.name))
        key_data.animation_data.action = action

        interval = float(target_fps) / SAMPLE_FPS
        for index, key in enumerate(pose_keys):
            target = sample_target_frame(start_frame, index, target_fps)
            key.value = 0.0
            key.keyframe_insert(data_path="value", frame=target - interval)
            key.value = 1.0
            key.keyframe_insert(data_path="value", frame=target)
            key.value = 0.0
            key.keyframe_insert(data_path="value", frame=target + interval)

        for fcurve in iter_action_fcurves(action):
            for point in fcurve.keyframe_points:
                point.interpolation = 'CONSTANT'

        baked_object["libsm64_source_addon"] = "LibSM64 Studio"
        baked_object["libsm64_is_bake"] = True
        baked_object["libsm64_sample_count"] = len(samples)
        baked_object["libsm64_sample_fps"] = SAMPLE_FPS
        baked_object["libsm64_target_fps"] = float(target_fps)
        baked_object["libsm64_recording_start_frame"] = float(start_frame)

        # Keep the candidate invisible until registration has validated it and
        # committed the current-take visibility transition.
        baked_object.hide_set(True)
        baked_object.hide_render = True
        validate_take_ownership(baked_object, source_object)
        return baked_object
    except Exception:
        _remove_take_datablocks(baked_object, baked_mesh, action)
        raise
