"""Run with: blender --background --factory-startup --python tests/blender_take_persistence_test.py"""

from pathlib import Path
import sys
import tempfile

import bpy


root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from libsm64_studio import take_manager as takes
from libsm64_studio import recording


def make_bake(name):
    mesh = bpy.data.meshes.new(name + " Mesh")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    obj = bpy.data.objects.new(name, mesh)
    obj["libsm64_is_bake"] = True
    obj[recording.BAKE_SCHEMA_VERSION] = recording.CURRENT_BAKE_SCHEMA_VERSION
    obj[recording.BAKE_LAYOUT] = recording.OBJECT_MOTION_LOCAL_POSE
    bpy.context.scene.collection.objects.link(obj)
    obj.shape_key_add(name="Basis", from_mix=False)
    obj.shape_key_add(name="Pose", from_mix=False)
    keys = obj.data.shape_keys
    keys.animation_data_create()
    keys.animation_data.action = bpy.data.actions.new(name + " Pose Action")
    obj.animation_data_create()
    obj.animation_data.action = bpy.data.actions.new(name + " Transform Action")
    return obj


scene = bpy.context.scene
first = make_bake("First")
takes.register_baked_take(scene, first)
second = make_bake("Second")
takes.register_baked_take(scene, second)
takes.favorite_take(scene, second)
third = make_bake("Third")
takes.register_baked_take(scene, third)
takes.reject_take(scene, first)

first_id = first[takes.TAKE_ID]
second_id = second[takes.TAKE_ID]
third_id = third[takes.TAKE_ID]
second.name = "Manually Renamed Keeper"

with tempfile.TemporaryDirectory() as directory:
    blend_path = str(Path(directory) / "take-persistence.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend_path)
    bpy.ops.wm.open_mainfile(filepath=blend_path)

    scene = bpy.context.scene
    takes.reconcile_scene(scene)
    first = takes.find_take(first_id)
    second = takes.find_take(second_id)
    third = takes.find_take(third_id)
    assert first[takes.TAKE_DISPOSITION] == takes.REJECTED
    assert second[takes.TAKE_DISPOSITION] == takes.FAVORITE
    assert third[takes.TAKE_DISPOSITION] == takes.REGULAR
    assert takes.current_take(scene) is third
    assert first.hide_render
    assert not second.hide_render
    assert not third.hide_render
    assert second.name == "Manually Renamed Keeper"
    assert scene[takes.SCENE_NEXT_TAKE] == 4

    fourth = make_bake("Fourth")
    takes.register_baked_take(scene, fourth)
    assert fourth[takes.TAKE_NUMBER] == 4

print("libsm64 take persistence Blender test passed")
