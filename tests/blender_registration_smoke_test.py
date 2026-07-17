"""Run with: blender --background --factory-startup --python tests/blender_registration_smoke_test.py"""

from pathlib import Path
import sys

import bpy


root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
import libsm64_studio as addon


def forbidden_reconciliation(*args, **kwargs):
    raise AssertionError("Take reconciliation ran during add-on registration")


original_reconcile = addon.reconcile_scene
addon.reconcile_scene = forbidden_reconciliation
handlers_before = tuple(bpy.app.handlers.frame_change_pre)
load_pre_before = tuple(bpy.app.handlers.load_pre)

# A new empty file must register without touching scene/take data. Repeating the
# cycle verifies that properties, handlers, and timers are cleaned up.
for _ in range(2):
    addon.register()
    assert hasattr(bpy.types.Scene, "libsm64")
    assert addon._addon_preferences(bpy.context) is None
    assert not bpy.app.timers.is_registered(addon._clear_confirmation)
    assert addon._shutdown_native_session_on_load_pre in bpy.app.handlers.load_pre
    addon.unregister()
    assert not hasattr(bpy.types.Scene, "libsm64")
    assert not bpy.app.timers.is_registered(addon._clear_confirmation)
    assert addon._shutdown_native_session_on_load_pre not in bpy.app.handlers.load_pre

assert tuple(bpy.app.handlers.frame_change_pre) == handlers_before
assert tuple(bpy.app.handlers.load_pre) == load_pre_before
addon.reconcile_scene = original_reconcile

# Once a normal scene context exists, lazy initialization migrates an existing
# legacy bake and recovers its take identity.
addon.register()
mesh = bpy.data.meshes.new("Lazy Migration Mesh")
legacy_bake = bpy.data.objects.new("Legacy Bake", mesh)
legacy_bake["libsm64_is_bake"] = True
bpy.context.scene.collection.objects.link(legacy_bake)
assert addon._ensure_scene_take_state(bpy.context) is bpy.context.scene
assert legacy_bake.get(addon.TAKE_ID)
addon.unregister()

print("libsm64 restricted registration smoke test passed")
