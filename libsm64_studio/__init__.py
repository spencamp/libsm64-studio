import importlib
import sys


_MARIO_REQUIRED_API = (
    "BAKING", "LIVE_IDLE", "POISONED", "RECORDING", "RESETTING", "STOPPED",
    "abandon_bake_transition", "begin_mario_recording",
    "freeze_mario_recording_for_bake", "resume_live_idle_after_transition",
    "clear_persistent_start_mark", "has_valid_start_mark",
    "reset_to_persistent_start_mark", "return_to_start_mark_after_transition",
    "set_persistent_start_mark",
)
_RUNTIME_API_VERSION = 6


def _reload_stale_runtime_modules():
    """Refresh an older cached submodule before importing the packaged API.

    Blender can execute a newly installed package ``__init__.py`` while keeping
    submodules from the previous add-on version in ``sys.modules``.  Reload only
    when the cached Mario module demonstrably lacks this package's required API.
    """
    mario_name = "{}.mario".format(__package__)
    cached_mario = sys.modules.get(mario_name)
    if cached_mario is None or (
        getattr(cached_mario, "RUNTIME_API_VERSION", 0) == _RUNTIME_API_VERSION
        and all(hasattr(cached_mario, symbol) for symbol in _MARIO_REQUIRED_API)
    ):
        return

    shutdown = getattr(cached_mario, "stop_tick_mario", None)
    if callable(shutdown):
        errors = shutdown()
        if errors:
            raise RuntimeError(
                "The previous libsm64 runtime could not shut down cleanly; "
                "restart Blender before enabling the updated add-on"
            )

    for suffix in ("recording", "input_reader", "input_reader_win", "mario"):
        module = sys.modules.get("{}.{}".format(__package__, suffix))
        if module is not None:
            importlib.reload(module)
    refreshed_mario = sys.modules.get(mario_name)
    if getattr(refreshed_mario, "RUNTIME_API_VERSION", 0) != _RUNTIME_API_VERSION:
        raise RuntimeError(
            "LibSM64 Studio has a mixed-file installation. Remove the existing "
            "libsm64_studio add-on directory and reinstall the current ZIP."
        )


_reload_stale_runtime_modules()


bl_info = {
    "name" : "LibSM64 Studio",
    "author" : "libsm64",
    "description" : "Record, bake, and edit Mario performances in Blender",
    "blender" : (2, 80, 0),
    "version" : (2, 7, 0),
    "location" : "View3D > Sidebar > LibSM64 Studio",
    "warning" : "",
    "category" : "Generic"
}

import bpy
import atexit
import math
import platform
from bpy.app.handlers import persistent
from . mario import (
    BAKING,
    LIVE_IDLE,
    POISONED,
    RECORDING,
    RESETTING,
    RUNTIME_API_VERSION,
    STOPPED,
    abandon_bake_transition,
    begin_mario_recording,
    clear_persistent_start_mark,
    collision_status_message,
    freeze_mario_recording_for_bake,
    get_live_mario_object,
    has_owned_native_session,
    has_valid_start_mark,
    insert_mario,
    is_mario_running,
    live_control_error,
    live_control_status,
    resume_live_idle_after_transition,
    reset_to_persistent_start_mark,
    return_to_start_mark_after_transition,
    set_persistent_start_mark,
    stop_tick_mario,
)
if RUNTIME_API_VERSION != _RUNTIME_API_VERSION:
    raise RuntimeError(
        "LibSM64 Studio has a mixed-file installation. Remove the existing "
        "libsm64_studio add-on directory and reinstall the current ZIP."
    )
from . recording import (
    RecordingError,
    bake_shape_keys,
    discard_baked_take,
    recorder,
    sample_target_frame,
)
from .take_manager import (
    FAVORITE,
    REGULAR,
    REJECTED,
    TAKE_DISPOSITION,
    TAKE_ID,
    SCENE_SCHEMA_VERSION,
    TAKE_SCHEMA_VERSION,
    TakeError,
    current_take,
    favorite_take,
    find_take,
    iter_takes,
    reconcile_scene,
    register_baked_take,
    reject_take,
    restore_take,
    select_take,
    take_label,
    unfavorite_take,
)


_confirmation_message = ""
BUILD_ID = "2.7.0+surface-streaming-fd118132"


def _addon_preferences(context):
    """Return this add-on's preferences without assuming registration finished."""
    preferences = getattr(context, "preferences", None)
    addons = getattr(preferences, "addons", None)
    if addons is None:
        return None
    entry = addons.get(__package__)
    return getattr(entry, "preferences", None)


def _ensure_scene_take_state(context):
    """Explicitly migrate one scene from a safe operator/load context."""
    scene = getattr(context, "scene", None)
    if scene is None:
        return None
    if int(scene.get(SCENE_SCHEMA_VERSION, 0)) < TAKE_SCHEMA_VERSION:
        reconcile_scene(scene)
    return scene


def _migrate_all_scenes_once():
    for scene in bpy.data.scenes:
        if int(scene.get(SCENE_SCHEMA_VERSION, 0)) < TAKE_SCHEMA_VERSION:
            reconcile_scene(scene)
    print("LibSM64 Studio build {} loaded".format(BUILD_ID))
    return None


@persistent
def _migrate_take_state_on_load(_unused):
    _migrate_all_scenes_once()


@persistent
def _shutdown_native_session_on_load_pre(_unused):
    stop_tick_mario()


def _shutdown_owned_session_at_exit():
    try:
        stop_tick_mario()
    except Exception as exc:
        print("libsm64 lifecycle exit cleanup failed: {}".format(exc))


def _redraw_panels():
    for window in getattr(bpy.context.window_manager, "windows", ()):
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _clear_confirmation():
    global _confirmation_message
    _confirmation_message = ""
    _redraw_panels()
    return None


def _show_confirmation(message):
    global _confirmation_message
    _confirmation_message = message
    if bpy.app.timers.is_registered(_clear_confirmation):
        bpy.app.timers.unregister(_clear_confirmation)
    bpy.app.timers.register(_clear_confirmation, first_interval=2.0)
    _redraw_panels()

class LibSm64Properties(bpy.types.PropertyGroup):
    camera_follow : bpy.props.BoolProperty (
        name="Follow Live Mario with cursor + camera",
        description="Keep the 3D cursor and active camera following Live Mario",
        default=True
    )
    camera_shift : bpy.props.FloatVectorProperty (
        name='Camera Offset',
        description='Camera Offset from Mario Origin.',
        default=(0.0, 0.0, 1.0),
        soft_min =-10.0,
        soft_max=10.0,
        step=10,
        precision=3,
        subtype='XYZ',
        unit='LENGTH',
        size=3
    )
    mario_scale : bpy.props.FloatProperty(
        name="Blender-to-SM64 Scale",
        default=100
    )
    rejected_expanded : bpy.props.BoolProperty(
        name="Show rejected takes",
        default=False,
    )
    reset_to_mark_on_recording_start : bpy.props.BoolProperty(
        name="Reset to Mark when recording starts",
        description="Reset Live Mario to the persistent Start Mark before recording",
        default=False,
    )
    timeline_start_frame : bpy.props.IntProperty(
        name="Timeline Start Frame",
        description="Saved Blender timeline frame used to align recorded takes",
        default=1,
        min=-1048574,
        max=1048574,
    )
    timeline_start_frame_set : bpy.props.BoolProperty(
        name="Timeline Start Frame Set",
        description="Whether this scene has a saved timeline start frame",
        default=False,
        options={'HIDDEN'},
    )
    start_recording_from_saved_frame : bpy.props.BoolProperty(
        name="Start recording from saved frame",
        description="Return to the saved Timeline Start Frame when a recording starts or finishes",
        default=False,
    )

class LibSm64Preferences(bpy.types.AddonPreferences):
    bl_idname = __name__
    rom_path : bpy.props.StringProperty(
        name="SM64 US ROM",
        description="Path to an unmodified US SM64 ROM",
        subtype='FILE_PATH',
        default=('c:\\sm64.us.z64' if platform.system() == 'Windows' else '~/sm64.us.z64')
    )
    def draw(self, context):
        layout = self.layout
        col = layout.column()
        col.label(text="SM64 US ROM (Unmodified, 8 MB, z64)")
        col.prop(self, 'rom_path')

class Main_PT_Panel(bpy.types.Panel):
    bl_idname = "LIBSM64_PT_main_panel"
    bl_label = "Mario Performance Studio"
    bl_category = "LibSM64 Studio"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"

    def draw(self, context):
        layout = self.layout
        scene = getattr(context, "scene", None)
        settings = getattr(scene, "libsm64", None) if scene is not None else None
        preferences = _addon_preferences(context)
        if scene is None or settings is None:
            layout.label(text="LibSM64 Studio is still initializing", icon='INFO')
            return

        col = layout.column()
        prop_split(col, settings, "mario_scale", "Blender-to-SM64 Scale")
        if preferences is not None:
            col.prop(preferences, "rom_path")
        else:
            col.label(text="Add-on preferences unavailable", icon='INFO')
        col.prop(settings, "camera_follow")
        insert_row = col.row()
        insert_row.enabled = preferences is not None
        insert_row.operator(InsertMario_OT_Operator.bl_idname, text='Start Live Mario')
        col.prop(settings, "camera_shift")
        col.operator(ControlMario_OT_Operator.bl_idname, text='Control Live Mario with keyboard')
        col.label(text="WASD + JKL to move. ESC to stop.")

        layout.separator()
        box = layout.box()
        box.label(text="Record a Mario Performance")
        box.label(text="Build {}".format(BUILD_ID))
        control_status = live_control_status()
        if control_status == RECORDING:
            box.label(text="Live Mario: Recording", icon='REC')
        elif control_status == LIVE_IDLE:
            box.label(text="Live Mario: Rehearsing", icon='CHECKMARK')
        elif control_status in (BAKING, RESETTING):
            box.label(text="Live Mario: Working", icon='TIME')
        elif control_status == POISONED:
            box.label(text="Live Mario: Error â€” end control or restart", icon='ERROR')
            if live_control_error():
                box.label(text=live_control_error(), icon='INFO')
        else:
            box.label(text="Live Mario: Unavailable", icon='ERROR')
        if has_owned_native_session():
            box.label(text=collision_status_message(), icon='MESH_GRID')

        mark_row = box.row(align=True)
        mark_row.enabled = control_status == LIVE_IDLE
        mark_row.operator(SetStartMark_OT_Operator.bl_idname, text="Set Start Mark", icon='BOOKMARKS')
        reset_mark = mark_row.row(align=True)
        reset_mark.enabled = control_status == LIVE_IDLE and has_valid_start_mark()
        reset_mark.operator(ResetToStartMark_OT_Operator.bl_idname, text="Reset to Mark", icon='LOOP_BACK')
        mark_is_valid = has_valid_start_mark()
        box.label(
            text="Mario Start Mark: {}".format("Set" if mark_is_valid else "Not Set"),
            icon='CHECKMARK' if mark_is_valid else 'INFO',
        )
        auto_reset = box.row()
        auto_reset.enabled = mark_is_valid and control_status == LIVE_IDLE
        auto_reset.prop(settings, "reset_to_mark_on_recording_start")

        timeline_row = box.row(align=True)
        timeline_row.enabled = not recorder.active
        timeline_row.operator(
            SetTimelineStartFrame_OT_Operator.bl_idname,
            text="Set Start Frame",
            icon='KEYFRAME_HLT',
        )
        go_to_frame = timeline_row.row(align=True)
        go_to_frame.enabled = settings.timeline_start_frame_set
        go_to_frame.operator(
            GoToTimelineStartFrame_OT_Operator.bl_idname,
            text="Go to Start Frame",
            icon='PLAY_REVERSE',
        )
        timeline_label = (
            "Frame {}".format(settings.timeline_start_frame)
            if settings.timeline_start_frame_set else "Not Set"
        )
        box.label(
            text="Timeline Start Frame: {}".format(timeline_label),
            icon='CHECKMARK' if settings.timeline_start_frame_set else 'INFO',
        )
        auto_timeline = box.row()
        auto_timeline.enabled = settings.timeline_start_frame_set and not recorder.active
        auto_timeline.prop(settings, "start_recording_from_saved_frame")

        primary = box.row()
        primary.scale_y = 1.6
        if recorder.active or recorder.has_pending_samples:
            primary.enabled = recorder.has_pending_samples or recorder.active
            primary.operator(StopAndBake_OT_Operator.bl_idname, text="Stop & Bake", icon='KEY_HLT')
        else:
            primary.enabled = control_status == LIVE_IDLE
            primary.operator(StartRecording_OT_Operator.bl_idname, text="Start Recording", icon='REC')
        if recorder.active or recorder.has_pending_samples:
            box.operator(CancelRecording_OT_Operator.bl_idname, text="Cancel Recording", icon='CANCEL')

        if recorder.active:
            box.label(text="{} samples Â· {:.2f} seconds".format(
                recorder.sample_count, recorder.duration_seconds
            ))
        if recorder.sample_count >= 300:
            box.label(text="Large take: shape-key baking may take time", icon='ERROR')
        if _confirmation_message:
            box.label(text=_confirmation_message, icon='CHECKMARK')
        end_row = box.row()
        end_row.enabled = has_owned_native_session()
        end_row.operator(EndMarioControl_OT_Operator.bl_idname, text="End Studio Session")

        takes = iter_takes()
        current = current_take(scene)

        layout.separator()
        layout.label(text="Active Take")
        if current is None:
            layout.label(text="No active take")
        else:
            draw_take_row(layout, current, is_current=True)

        favorites = sorted(
            (obj for obj in takes if obj.get(TAKE_DISPOSITION) == FAVORITE),
            key=lambda obj: int(obj.get("libsm64_take_number", 0)), reverse=True,
        )
        layout.separator()
        layout.label(text="Favorite Takes")
        if favorites:
            for obj in favorites:
                draw_take_row(layout, obj, is_current=(obj is current))
        else:
            layout.label(text="No favorites")

        regular = sorted(
            (obj for obj in takes
             if obj.get(TAKE_DISPOSITION) == REGULAR and obj is not current),
            key=lambda obj: int(obj.get("libsm64_take_number", 0)), reverse=True,
        )
        layout.separator()
        layout.label(text="Other Takes")
        if regular:
            for obj in regular:
                draw_take_row(layout, obj)
        else:
            layout.label(text="No other takes")

        rejected = sorted(
            (obj for obj in takes if obj.get(TAKE_DISPOSITION) == REJECTED),
            key=lambda obj: int(obj.get("libsm64_take_number", 0)), reverse=True,
        )
        layout.separator()
        row = layout.row()
        icon = 'TRIA_DOWN' if settings.rejected_expanded else 'TRIA_RIGHT'
        row.prop(
            settings, "rejected_expanded",
            text="Rejected Takes ({})".format(len(rejected)), icon=icon, emboss=False,
        )
        if settings.rejected_expanded:
            for obj in rejected:
                rejected_row = layout.row(align=True)
                rejected_row.label(text=take_label(obj))
                op = rejected_row.operator(RestoreTake_OT_Operator.bl_idname, text="Restore")
                op.take_id = obj[TAKE_ID]


def draw_take_row(layout, obj, is_current=False):
    row = layout.row(align=True)
    op = row.operator(
        SelectTake_OT_Operator.bl_idname,
        text=take_label(obj),
        depress=is_current,
    )
    op.take_id = obj[TAKE_ID]
    favorite = obj.get(TAKE_DISPOSITION) == FAVORITE
    op = row.operator(
        ToggleFavoriteTake_OT_Operator.bl_idname,
        text="", icon='SOLO_ON' if favorite else 'SOLO_OFF',
    )
    op.take_id = obj[TAKE_ID]
    reject_row = row.row(align=True)
    reject_row.enabled = not favorite
    reject = reject_row.operator(RejectTake_OT_Operator.bl_idname, text="", icon='X')
    reject.take_id = obj[TAKE_ID]

class InsertMario_OT_Operator(bpy.types.Operator):
    bl_idname = "view3d.libsm64_insert_mario"
    bl_label = "Start Live Mario"
    bl_description = "Place Live Mario in the scene and begin a performance-capture session"

    def execute(self, context):
        scene = context.scene
        preferences = _addon_preferences(context)
        if preferences is None:
            self.report({"ERROR"}, "LibSM64 Studio preferences are unavailable")
            return {'CANCELLED'}
        err = insert_mario(preferences.rom_path, scene.libsm64.mario_scale, scene.libsm64.camera_follow)
        if err != None:
            self.report({"ERROR"}, err)
            return {'CANCELLED'}
        return {'FINISHED'}


class ControlMario_OT_Operator(bpy.types.Operator):
    bl_idname = "view3d.libsm64_control_mario"
    bl_label = "Control Live Mario with Keyboard"
    bl_description = "Control Live Mario with the keyboard during rehearsal or recording"

    def invoke(self, context, event):
        global config
        config["keyboard_control"] = True
        if not is_mario_running():
            self.report({"ERROR"}, 'Start Live Mario first.')
            return {'CANCELLED'}
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if not config["keyboard_control"]:
            return {'FINISHED'}
        if event.type == 'ESC':
            config["keyboard_control"] = False
            return {'FINISHED'}

        process_input(event)

        return {'RUNNING_MODAL'}


def _restore_timeline_start_frame(scene):
    """Recall the saved take frame when the scene's automatic option is enabled."""
    settings = getattr(scene, "libsm64", None)
    if not (
        settings is not None
        and settings.start_recording_from_saved_frame
        and settings.timeline_start_frame_set
    ):
        return False
    scene.frame_set(int(settings.timeline_start_frame))
    return True


class StartRecording_OT_Operator(bpy.types.Operator):
    bl_idname = "view3d.libsm64_start_recording"
    bl_label = "Start Recording"
    bl_description = "Start capturing 30 Hz geometry from Live Mario's exact current position"

    def execute(self, context):
        if not is_mario_running():
            self.report({'ERROR'}, "Live Mario is not available for recording")
            return {'CANCELLED'}
        had_pending_samples = recorder.has_pending_samples
        settings = getattr(context.scene, "libsm64", None)
        auto_reset = bool(
            settings is not None
            and settings.reset_to_mark_on_recording_start
            and has_valid_start_mark()
        )
        try:
            _restore_timeline_start_frame(context.scene)
            begin_mario_recording(context.scene, reset_to_mark=auto_reset)
        except Exception as exc:
            abandon_bake_transition()
            recorder.fail(
                "Could not start recording: {}".format(exc),
                preserve_samples=had_pending_samples,
            )
            self.report({'ERROR'}, recorder.message)
            return {'CANCELLED'}
        self.report({'INFO'}, "Recording Mario performance")
        return {'FINISHED'}


class SetTimelineStartFrame_OT_Operator(bpy.types.Operator):
    bl_idname = "view3d.libsm64_set_timeline_start_frame"
    bl_label = "Set Start Frame"
    bl_description = "Save the current Blender timeline frame for repeatable takes"

    @classmethod
    def poll(cls, context):
        return (
            getattr(getattr(context, "scene", None), "libsm64", None) is not None
            and not recorder.active
        )

    def execute(self, context):
        settings = context.scene.libsm64
        settings.timeline_start_frame = int(context.scene.frame_current)
        settings.timeline_start_frame_set = True
        _show_confirmation("Timeline Start Frame set")
        self.report(
            {'INFO'},
            "Timeline Start Frame set to {}".format(settings.timeline_start_frame),
        )
        return {'FINISHED'}


class GoToTimelineStartFrame_OT_Operator(bpy.types.Operator):
    bl_idname = "view3d.libsm64_go_to_timeline_start_frame"
    bl_label = "Go to Start Frame"
    bl_description = "Move the Blender timeline to the saved start frame"

    @classmethod
    def poll(cls, context):
        settings = getattr(getattr(context, "scene", None), "libsm64", None)
        return bool(
            settings is not None
            and settings.timeline_start_frame_set
            and not recorder.active
        )

    def execute(self, context):
        settings = context.scene.libsm64
        context.scene.frame_set(int(settings.timeline_start_frame))
        self.report(
            {'INFO'},
            "Timeline moved to frame {}".format(settings.timeline_start_frame),
        )
        return {'FINISHED'}


class SetStartMark_OT_Operator(bpy.types.Operator):
    bl_idname = "view3d.libsm64_set_start_mark"
    bl_label = "Set Start Mark"
    bl_description = "Save Live Mario's current native position for repeatable takes"

    @classmethod
    def poll(cls, _context):
        return is_mario_running() and live_control_status() == LIVE_IDLE

    def execute(self, context):
        try:
            set_persistent_start_mark()
        except Exception as exc:
            _show_confirmation("Start Mark unavailable")
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        _show_confirmation("✓ Start Mark set")
        self.report({'INFO'}, "Start Mark set")
        return {'FINISHED'}


class ResetToStartMark_OT_Operator(bpy.types.Operator):
    bl_idname = "view3d.libsm64_reset_to_start_mark"
    bl_label = "Reset to Mark"
    bl_description = "Recreate Live Mario at the persistent Start Mark"

    @classmethod
    def poll(cls, _context):
        return (
            is_mario_running()
            and live_control_status() == LIVE_IDLE
            and has_valid_start_mark()
            and not recorder.active
        )

    def execute(self, context):
        try:
            reset_to_persistent_start_mark()
        except Exception as exc:
            _show_confirmation("Start Mark unavailable")
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        _show_confirmation("✓ Reset to Start Mark")
        self.report({'INFO'}, "Reset to Start Mark")
        return {'FINISHED'}


class StopAndBake_OT_Operator(bpy.types.Operator):
    bl_idname = "view3d.libsm64_stop_and_bake"
    bl_label = "Stop & Bake"
    bl_description = (
        "Stop recording and bake Mario's path/facing to object transforms and body pose to local shape keys; dynamic UV and color changes are not captured"
    )

    def execute(self, context):
        source_object = get_live_mario_object()
        try:
            samples = freeze_mario_recording_for_bake()
        except RecordingError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        start_frame = recorder.start_frame
        target_fps = recorder.target_fps
        baked_object = None
        try:
            baked_object = bake_shape_keys(
                context, source_object, samples, start_frame, target_fps
            )
            take_number = register_baked_take(context.scene, baked_object)
        except Exception as exc:
            if baked_object is not None:
                discard_baked_take(baked_object)
            abandon_bake_transition()
            recorder.fail("Bake failed: {}".format(exc), preserve_samples=True)
            self.report({'ERROR'}, recorder.message)
            return {'CANCELLED'}

        final_frame = sample_target_frame(start_frame, len(samples) - 1, target_fps)
        if final_frame > context.scene.frame_end:
            context.scene.frame_end = int(math.ceil(final_frame))
        try:
            select_take(context, baked_object)
        except (TakeError, RuntimeError):
            # The take is already committed and may be outside the active view
            # layer; selection is convenience, not part of data ownership.
            pass

        sample_count = len(samples)
        label = "Take {:03d}".format(take_number)
        recorder.complete(label)
        try:
            return_to_start_mark_after_transition()
        except Exception as exc:
            abandon_bake_transition()
            self.report(
                {'ERROR'},
                "{} was captured, but reset to Start Mark failed: {}".format(label, exc),
            )
            return {'FINISHED'}
        try:
            _restore_timeline_start_frame(context.scene)
        except Exception as exc:
            self.report(
                {'ERROR'},
                "{} was captured, but Timeline Start Frame restore failed: {}".format(label, exc),
            )
            return {'FINISHED'}
        _show_confirmation("âœ“ Take {:03d} captured".format(take_number))
        self.report(
            {'INFO'},
            "Baked {} performance samples to {}".format(sample_count, label),
        )
        return {'FINISHED'}


class CancelRecording_OT_Operator(bpy.types.Operator):
    bl_idname = "view3d.libsm64_cancel_recording"
    bl_label = "Cancel Recording"
    bl_description = "Discard the pending take and return Live Mario to the persistent Start Mark"

    def execute(self, context):
        recorder.cancel()
        try:
            return_to_start_mark_after_transition()
        except Exception as exc:
            abandon_bake_transition()
            self.report({'ERROR'}, "Recording discarded, but reset to Start Mark failed: {}".format(exc))
            return {'FINISHED'}
        try:
            _restore_timeline_start_frame(context.scene)
        except Exception as exc:
            self.report({'ERROR'}, "Recording discarded, but Timeline Start Frame restore failed: {}".format(exc))
            return {'FINISHED'}
        self.report({'INFO'}, "Mario recording discarded")
        return {'FINISHED'}


class SelectTake_OT_Operator(bpy.types.Operator):
    bl_idname = "view3d.libsm64_select_take"
    bl_label = "Select Take"
    bl_description = "Make this the current visible take without changing the timeline"

    take_id : bpy.props.StringProperty()

    def execute(self, context):
        obj = find_take(self.take_id)
        if obj is None:
            self.report({'ERROR'}, "Take not found")
            return {'CANCELLED'}
        try:
            select_take(context, obj)
        except TakeError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class ToggleFavoriteTake_OT_Operator(bpy.types.Operator):
    bl_idname = "view3d.libsm64_toggle_favorite_take"
    bl_label = "Favorite Take"
    bl_description = "Keep this take visible, or return it to regular visibility"

    take_id : bpy.props.StringProperty()

    def execute(self, context):
        obj = find_take(self.take_id)
        if obj is None:
            self.report({'ERROR'}, "Take not found")
            return {'CANCELLED'}
        try:
            if obj.get(TAKE_DISPOSITION) == FAVORITE:
                unfavorite_take(context.scene, obj)
            else:
                favorite_take(context.scene, obj)
        except TakeError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class RejectTake_OT_Operator(bpy.types.Operator):
    bl_idname = "view3d.libsm64_reject_take"
    bl_label = "Reject Take"
    bl_description = "Hide this take and keep it until Mario control ends"

    take_id : bpy.props.StringProperty()

    def execute(self, context):
        obj = find_take(self.take_id)
        if obj is None:
            self.report({'ERROR'}, "Take not found")
            return {'CANCELLED'}
        try:
            reject_take(context.scene, obj)
        except TakeError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class RestoreTake_OT_Operator(bpy.types.Operator):
    bl_idname = "view3d.libsm64_restore_take"
    bl_label = "Restore Take"

    take_id : bpy.props.StringProperty()

    def execute(self, context):
        obj = find_take(self.take_id)
        if obj is None:
            self.report({'ERROR'}, "Take not found")
            return {'CANCELLED'}
        restore_take(context, obj)
        return {'FINISHED'}


class EndMarioControl_OT_Operator(bpy.types.Operator):
    bl_idname = "view3d.libsm64_end_mario_control"
    bl_label = "End Studio Session"
    bl_description = "End Live Mario control and permanently remove all rejected takes"

    def execute(self, context):
        recorder.cancel("Studio session ended")
        clear_persistent_start_mark()
        stop_tick_mario()
        config["keyboard_control"] = False
        for key in input_value:
            input_value[key] = False
        live_object = get_live_mario_object()
        if live_object is not None:
            bpy.data.objects.remove(live_object, do_unlink=True)
        self.report({'INFO'}, "Studio session ended; rejected takes cleaned up")
        return {'FINISHED'}

config = {
    'keyboard_control': False
}

input_value = {
    'UP': False,
    'DOWN': False,
    'LEFT': False,
    'RIGHT': False,
    'A': False,
    'B': False,
    'C': False,
}

input_config = {
    'UP': 'W',
    'DOWN': 'S',
    'LEFT': 'A',
    'RIGHT': 'D',
    'A': 'J',
    'B': 'K',
    'C': 'L',
}

def process_input(event):
    for k, v in input_config.items():
        if event.type == v:
            if event.value == 'PRESS':
                input_value[k] = True
            else:
                input_value[k] = False


register_classes, unregister_classes = bpy.utils.register_classes_factory((
    LibSm64Properties,
    LibSm64Preferences,
    Main_PT_Panel,
    InsertMario_OT_Operator,
    ControlMario_OT_Operator,
    SetStartMark_OT_Operator,
    ResetToStartMark_OT_Operator,
    SetTimelineStartFrame_OT_Operator,
    GoToTimelineStartFrame_OT_Operator,
    StartRecording_OT_Operator,
    StopAndBake_OT_Operator,
    CancelRecording_OT_Operator,
    SelectTake_OT_Operator,
    ToggleFavoriteTake_OT_Operator,
    RejectTake_OT_Operator,
    RestoreTake_OT_Operator,
    EndMarioControl_OT_Operator,
))

def register():
    register_classes()
    bpy.types.Scene.libsm64 = bpy.props.PointerProperty(type=LibSm64Properties)
    handlers = bpy.app.handlers.load_post
    if _migrate_take_state_on_load not in handlers:
        handlers.append(_migrate_take_state_on_load)
    if not bpy.app.timers.is_registered(_migrate_all_scenes_once):
        bpy.app.timers.register(_migrate_all_scenes_once, first_interval=0.0)
    if _shutdown_native_session_on_load_pre not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_shutdown_native_session_on_load_pre)
    atexit.unregister(_shutdown_owned_session_at_exit)
    atexit.register(_shutdown_owned_session_at_exit)

def unregister():
    global _confirmation_message
    recorder.cancel("Add-on unregistered")
    clear_persistent_start_mark()
    _confirmation_message = ""
    if bpy.app.timers.is_registered(_clear_confirmation):
        bpy.app.timers.unregister(_clear_confirmation)
    if bpy.app.timers.is_registered(_migrate_all_scenes_once):
        bpy.app.timers.unregister(_migrate_all_scenes_once)
    if _migrate_take_state_on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_migrate_take_state_on_load)
    if _shutdown_native_session_on_load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_shutdown_native_session_on_load_pre)
    atexit.unregister(_shutdown_owned_session_at_exit)
    stop_tick_mario()
    unregister_classes()
    if hasattr(bpy.types.Scene, "libsm64"):
        del bpy.types.Scene.libsm64

def prop_split(layout, data, field, name):
    split = layout.split(factor = 0.5)
    split.label(text = name)
    split.prop(data, field, text = '')
