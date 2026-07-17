"""Short-take geometry recording and Blender shape-key baking.

The recorder and timing helpers deliberately do not import bpy so their state
handling can be tested with ordinary Python.  Blender-specific baking is kept
behind :func:`bake_shape_keys`.
"""

from array import array
import math


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


def _set_scene_frame(scene, frame):
    whole = int(math.floor(frame))
    subframe = float(frame) - whole
    try:
        scene.frame_set(whole, subframe=subframe)
    except TypeError:
        scene.frame_set(whole)


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


def bake_shape_keys(context, source_object, samples, start_frame, target_fps):
    """Create a self-contained, fixed-topology Blender shape-key animation."""
    import bpy

    if source_object is None or getattr(source_object, "type", None) != 'MESH':
        raise RecordingError("The live LibSM64 Mario mesh is unavailable")
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

    baked_mesh = source_object.data.copy()
    baked_mesh.name = "LibSM64 Mario Bake Mesh"
    baked_mesh_name = baked_mesh.name
    _set_coordinates(baked_mesh.vertices, samples[0])
    baked_mesh.update()

    baked_object = bpy.data.objects.new("LibSM64 Mario Bake", baked_mesh)
    baked_object_name = baked_object.name
    action = None
    source_hide_render = source_object.hide_render
    try:
        source_hidden = source_object.hide_get()
    except AttributeError:
        source_hidden = getattr(source_object, "hide_viewport", False)
    try:
        collections = list(source_object.users_collection)
        target_collection = collections[0] if collections else context.scene.collection
        target_collection.objects.link(baked_object)
        baked_object.matrix_world = source_object.matrix_world.copy()
        _copy_object_display_settings(source_object, baked_object)

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

        baked_object["libsm64_source_addon"] = "libsm64-blender"
        baked_object["libsm64_is_bake"] = True
        baked_object["libsm64_sample_count"] = len(samples)
        baked_object["libsm64_sample_fps"] = SAMPLE_FPS
        baked_object["libsm64_target_fps"] = float(target_fps)
        baked_object["libsm64_recording_start_frame"] = float(start_frame)

        final_frame = sample_target_frame(start_frame, len(samples) - 1, target_fps)
        if final_frame > context.scene.frame_end:
            context.scene.frame_end = int(math.ceil(final_frame))

        source_object.hide_render = True
        try:
            source_object.hide_set(True)
        except AttributeError:
            source_object.hide_viewport = True

        for selected in list(context.selected_objects):
            selected.select_set(False)
        baked_object.hide_set(False)
        baked_object.hide_render = False
        baked_object.select_set(True)
        context.view_layer.objects.active = baked_object
        _set_scene_frame(context.scene, float(start_frame))
        return baked_object
    except Exception:
        # A failed bake must not leave a misleading partial object in the scene.
        source_object.hide_render = source_hide_render
        try:
            source_object.hide_set(source_hidden)
        except AttributeError:
            source_object.hide_viewport = source_hidden
        if bpy.data.objects.get(baked_object_name) is baked_object:
            bpy.data.objects.remove(baked_object, do_unlink=True)
        if bpy.data.meshes.get(baked_mesh_name) is baked_mesh and baked_mesh.users == 0:
            bpy.data.meshes.remove(baked_mesh)
        if action is not None and action.users == 0:
            bpy.data.actions.remove(action)
        raise
