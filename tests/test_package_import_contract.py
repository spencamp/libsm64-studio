import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "libsm64_studio"
LIFECYCLE_SYMBOLS = {"BAKING", "RESETTING", "LIVE_IDLE", "RECORDING", "STOPPED", "POISONED"}


def top_level_assignments(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        target.id
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else (node.target,))
        if isinstance(target, ast.Name)
    }


def mario_imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "mario"
        for alias in node.names
    }


class PackageImportContractTests(unittest.TestCase):
    def test_installable_package_matches_runtime_sources(self):
        """Prevent the installable package from shipping a stale module copy."""
        for packaged_source in sorted(PACKAGE.glob("*.py")):
            runtime_source = ROOT / packaged_source.name
            self.assertTrue(runtime_source.is_file())
            self.assertEqual(
                runtime_source.read_bytes(),
                packaged_source.read_bytes(),
                "Installable package has a stale {}".format(packaged_source.name),
            )

    def test_lifecycle_symbols_match_definitions_and_imports_in_both_trees(self):
        for base in (ROOT, PACKAGE):
            self.assertEqual(
                top_level_assignments(base / "mario.py") & LIFECYCLE_SYMBOLS,
                LIFECYCLE_SYMBOLS,
            )
            self.assertEqual(
                mario_imports(base / "__init__.py") & LIFECYCLE_SYMBOLS,
                LIFECYCLE_SYMBOLS,
            )

    def test_start_mark_api_is_explicit_and_recording_does_not_capture_it(self):
        mario_tree = ast.parse((PACKAGE / "mario.py").read_text(encoding="utf-8"))
        definitions = {
            node.name for node in mario_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertIn("capture_mario_starting_mark", definitions)
        self.assertIn("begin_mario_recording", definitions)
        for symbol in (
            "set_persistent_start_mark", "has_valid_start_mark",
            "reset_to_persistent_start_mark", "clear_persistent_start_mark",
        ):
            self.assertIn(symbol, definitions)

        begin = next(
            node for node in mario_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "begin_mario_recording"
        )
        called_names = {
            node.func.id for node in ast.walk(begin)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertNotIn("capture_mario_starting_mark", called_names)

        init_tree = ast.parse((PACKAGE / "__init__.py").read_text(encoding="utf-8"))
        mario_imports = {
            alias.name
            for node in init_tree.body
            if isinstance(node, ast.ImportFrom) and node.module == "mario"
            for alias in node.names
        }
        self.assertIn("begin_mario_recording", mario_imports)

    def test_register_does_not_enumerate_blender_scenes(self):
        init_tree = ast.parse((PACKAGE / "__init__.py").read_text(encoding="utf-8"))
        register = next(
            node for node in init_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "register"
        )

        def is_bpy_data_scenes(node):
            return (
                isinstance(node, ast.Attribute)
                and node.attr == "scenes"
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "data"
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "bpy"
            )

        self.assertFalse(any(is_bpy_data_scenes(node) for node in ast.walk(register)))

    def test_live_simulation_is_not_coupled_to_timeline_playback(self):
        source = (PACKAGE / "mario.py").read_text(encoding="utf-8")
        self.assertNotIn("animation_play", source)
        self.assertNotIn("animation_cancel", source)
        self.assertNotIn("frame_change_pre.append", source)
        self.assertNotIn("render.fps = 30", source)
        self.assertIn("bpy.app.timers.register", source)
        self.assertIn("_libsm64_generation", source)


if __name__ == "__main__":
    unittest.main()
