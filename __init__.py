bl_info = {
    "name" : "libsm64-blender",
    "author" : "libsm64",
    "description" : "Add a playble Mario to your Blender Scene",
    "blender" : (2, 80, 0),
    "version" : (2, 1, 0),
    "location" : "View3D",
    "warning" : "",
    "category" : "Generic"
}

import bpy
import platform
from . mario import (
    get_simulation_target_fps,
    insert_mario,
    is_mario_running,
    stop_tick_mario,
)
from . recording import RecordingError, bake_shape_keys, recorder

class LibSm64Properties(bpy.types.PropertyGroup):
    camera_follow : bpy.props.BoolProperty (
        name="Follow Mario with 3D cursor + camera",
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
        name="Blender to SM64 Scale",
        default=100
    )

class LibSm64Preferences(bpy.types.AddonPreferences):
    bl_idname = __name__
    rom_path : bpy.props.StringProperty(
        name="Path",
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
    bl_label = "Insert Mario"
    bl_category = "LibSM64"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        preferences = context.preferences.addons[__package__].preferences

        col = layout.column()
        prop_split(col, scene.libsm64, "mario_scale", "Blender to SM64 Scale")
        col.prop(preferences, "rom_path")
        col.prop(scene.libsm64, "camera_follow")
        col.operator(InsertMario_OT_Operator.bl_idname, text='Insert Mario')
        col.prop(scene.libsm64, "camera_shift")
        col.operator(ControlMario_OT_Operator.bl_idname, text='Control Mario with keyboard')
        col.label(text="WASD + JKL to move. ESC to stop.")

        layout.separator()
        box = layout.box()
        box.label(text="Animation Recording")
        box.label(text="Status: {}".format(recorder.status))
        box.label(text="Samples: {}".format(recorder.sample_count))
        box.label(text="Duration: {:.2f} seconds".format(recorder.duration_seconds))
        if recorder.message:
            box.label(text=recorder.message, icon='INFO')
        if recorder.sample_count >= 300:
            box.label(text="Large take: shape-key baking may take time", icon='ERROR')
        else:
            box.label(text="Designed for short takes (about 10 seconds or less)")
        box.label(text="Bake captures positions; UV/color changes are not recorded")

        row = box.row(align=True)
        row.enabled = is_mario_running() and not recorder.active
        row.operator(StartRecording_OT_Operator.bl_idname, text="Start Recording", icon='REC')
        row = box.row(align=True)
        row.enabled = recorder.active or recorder.has_pending_samples
        row.operator(StopAndBake_OT_Operator.bl_idname, text="Stop & Bake", icon='KEY_HLT')
        row.operator(CancelRecording_OT_Operator.bl_idname, text="Cancel", icon='CANCEL')

class InsertMario_OT_Operator(bpy.types.Operator):
    bl_idname = "view3d.libsm64_insert_mario"
    bl_label = "Insert Mario"
    bl_description = "Inserts a Mario into the scene"

    def execute(self, context):
        scene = context.scene
        preferences = context.preferences.addons[__package__].preferences
        err = insert_mario(preferences.rom_path, scene.libsm64.mario_scale, scene.libsm64.camera_follow)
        if err != None:
            self.report({"ERROR"}, err)
        return {'FINISHED'}


class ControlMario_OT_Operator(bpy.types.Operator):
    bl_idname = "view3d.libsm64_control_mario"
    bl_label = "Control with keyboard"
    bl_description = "Control Mario with keyboard"

    def invoke(self, context, event):
        global config
        config["keyboard_control"] = True
        if 'LibSM64 Mario' not in bpy.data.objects:
            self.report({"ERROR"}, 'Insert Mario first.')
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


class StartRecording_OT_Operator(bpy.types.Operator):
    bl_idname = "view3d.libsm64_start_recording"
    bl_label = "Start Recording"
    bl_description = "Capture one complete Mario mesh snapshot per libsm64 tick"

    def execute(self, context):
        if not is_mario_running():
            self.report({'ERROR'}, "Insert Mario and keep the simulation running first")
            return {'CANCELLED'}
        try:
            recorder.start(
                float(context.scene.frame_current),
                get_simulation_target_fps(context.scene),
            )
        except Exception as exc:
            recorder.fail("Could not start recording: {}".format(exc), preserve_samples=False)
            self.report({'ERROR'}, recorder.message)
            return {'CANCELLED'}
        self.report({'INFO'}, "Recording Mario geometry")
        return {'FINISHED'}


class StopAndBake_OT_Operator(bpy.types.Operator):
    bl_idname = "view3d.libsm64_stop_and_bake"
    bl_label = "Stop & Bake"
    bl_description = (
        "Stop recording and bake positions to shape keys; dynamic UV and color changes are not captured"
    )

    def execute(self, context):
        source_object = bpy.data.objects.get('LibSM64 Mario')
        try:
            samples = recorder.freeze_for_bake()
        except RecordingError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        start_frame = recorder.start_frame
        target_fps = recorder.target_fps
        try:
            stop_tick_mario()
            baked_object = bake_shape_keys(
                context, source_object, samples, start_frame, target_fps
            )
        except Exception as exc:
            recorder.fail("Bake failed: {}".format(exc), preserve_samples=True)
            self.report({'ERROR'}, recorder.message)
            return {'CANCELLED'}

        sample_count = len(samples)
        recorder.complete(baked_object.name)
        self.report(
            {'INFO'},
            "Baked {} Mario samples to {}".format(sample_count, baked_object.name),
        )
        return {'FINISHED'}


class CancelRecording_OT_Operator(bpy.types.Operator):
    bl_idname = "view3d.libsm64_cancel_recording"
    bl_label = "Cancel Recording"
    bl_description = "Discard the pending take without stopping the live Mario simulation"

    def execute(self, context):
        recorder.cancel()
        self.report({'INFO'}, "Mario recording discarded")
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
    StartRecording_OT_Operator,
    StopAndBake_OT_Operator,
    CancelRecording_OT_Operator,
))

def register():
    register_classes()
    bpy.types.Scene.libsm64 = bpy.props.PointerProperty(type=LibSm64Properties)

def unregister():
    recorder.cancel("Add-on unregistered")
    stop_tick_mario()
    unregister_classes()
    del bpy.types.Scene.libsm64

def prop_split(layout, data, field, name):
    split = layout.split(factor = 0.5)
    split.label(text = name)
    split.prop(data, field, text = '')
