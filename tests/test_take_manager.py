import importlib.util
from pathlib import Path
import sys
import types
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "take_manager.py"
SPEC = importlib.util.spec_from_file_location("libsm64_take_manager", MODULE_PATH)
take_manager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(take_manager)


class FakeObject(dict):
    def __init__(self, name):
        super().__init__(libsm64_is_bake=True)
        self.name = name
        self.data = None
        self.hide_render = False
        self.hide_viewport = False
        self.selected = False

    def hide_set(self, hidden):
        self.hide_viewport = hidden

    def select_set(self, selected):
        self.selected = selected


class TakeTransitionTests(unittest.TestCase):
    def setUp(self):
        self.previous_bpy = sys.modules.get("bpy")
        self.objects = []
        self.active_holder = types.SimpleNamespace(active=None)
        self.context = types.SimpleNamespace(
            scene={},
            selected_objects=[],
            view_layer=types.SimpleNamespace(objects=self.active_holder),
        )
        sys.modules["bpy"] = types.SimpleNamespace(
            data=types.SimpleNamespace(objects=self.objects),
            context=self.context,
        )

    def tearDown(self):
        if self.previous_bpy is None:
            sys.modules.pop("bpy", None)
        else:
            sys.modules["bpy"] = self.previous_bpy

    def new_take(self):
        obj = FakeObject("Bake")
        self.objects.append(obj)
        take_manager.register_baked_take(self.context.scene, obj)
        return obj

    def assert_visible(self, obj, visible):
        self.assertEqual(obj.hide_render, not visible)
        self.assertEqual(obj.hide_viewport, not visible)

    def test_new_bake_and_favorite_visibility(self):
        first = self.new_take()
        take_manager.favorite_take(self.context.scene, first)
        second = self.new_take()
        self.assertEqual(second[take_manager.TAKE_NUMBER], 2)
        self.assertIs(take_manager.current_take(self.context.scene), second)
        self.assert_visible(first, True)
        self.assert_visible(second, True)

    def test_new_bake_hides_previous_regular(self):
        first = self.new_take()
        second = self.new_take()
        self.assert_visible(first, False)
        self.assert_visible(second, True)

    def test_unfavorite_current_remains_current_and_visible(self):
        obj = self.new_take()
        take_manager.favorite_take(self.context.scene, obj)
        take_manager.unfavorite_take(self.context.scene, obj)
        self.assertIs(take_manager.current_take(self.context.scene), obj)
        self.assertEqual(obj[take_manager.TAKE_DISPOSITION], take_manager.REGULAR)
        self.assert_visible(obj, True)

    def test_unfavorite_noncurrent_returns_to_normal_visibility(self):
        first = self.new_take()
        take_manager.favorite_take(self.context.scene, first)
        self.new_take()
        take_manager.unfavorite_take(self.context.scene, first)
        self.assertEqual(first[take_manager.TAKE_DISPOSITION], take_manager.REGULAR)
        self.assert_visible(first, False)

    def test_select_does_not_touch_scene_timeline_values(self):
        first = self.new_take()
        second = self.new_take()
        self.context.scene["frame_current"] = 42.25
        self.context.scene["playing"] = True
        self.context.selected_objects = [second]
        take_manager.select_take(self.context, first)
        self.assertEqual(self.context.scene["frame_current"], 42.25)
        self.assertTrue(self.context.scene["playing"])
        self.assert_visible(first, True)
        self.assert_visible(second, False)

    def test_reject_protection_restore_and_clear_current(self):
        obj = self.new_take()
        take_manager.favorite_take(self.context.scene, obj)
        with self.assertRaises(take_manager.TakeError):
            take_manager.reject_take(self.context.scene, obj)
        take_manager.unfavorite_take(self.context.scene, obj)
        take_manager.reject_take(self.context.scene, obj)
        self.assertEqual(self.context.scene[take_manager.SCENE_CURRENT_TAKE], "")
        self.assert_visible(obj, False)
        take_manager.restore_take(self.context, obj)
        self.assertEqual(obj[take_manager.TAKE_DISPOSITION], take_manager.REGULAR)
        self.assertIs(take_manager.current_take(self.context.scene), obj)
        self.assert_visible(obj, True)

    def test_deleted_numbers_are_not_reused(self):
        first = self.new_take()
        self.objects.remove(first)
        second = self.new_take()
        self.assertEqual(second[take_manager.TAKE_NUMBER], 2)


if __name__ == "__main__":
    unittest.main()
