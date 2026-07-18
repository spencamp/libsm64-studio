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

    def test_every_explicit_init_import_exists_in_its_owner_module(self):
        for base in (ROOT, PACKAGE):
            init_tree = ast.parse((base / "__init__.py").read_text(encoding="utf-8"))
            for node in init_tree.body:
                if not isinstance(node, ast.ImportFrom) or node.level != 1 or not node.module:
                    continue
                module_path = base / (node.module + ".py")
                self.assertTrue(module_path.is_file(), node.module)
                symbols = top_level_assignments(module_path)
                module_tree = ast.parse(module_path.read_text(encoding="utf-8"))
                symbols.update(
                    item.name for item in module_tree.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                )
                for alias in node.names:
                    if alias.name != "*":
                        self.assertIn(alias.name, symbols, "{}.{}".format(node.module, alias.name))

    def test_collision_streaming_and_cache_runtime_are_absent(self):
        for base in (ROOT, PACKAGE):
            mario_source = (base / "mario.py").read_text(encoding="utf-8")
            init_source = (base / "__init__.py").read_text(encoding="utf-8")
            self.assertFalse((base / "collision_cache.py").exists())
            for forbidden in (
                "collision_cache", "chunk_coordinate", "_handle_collision_boundary",
                "_replace_collision_and_mario",
                "Could not recreate Mario after loading nearby collision",
            ):
                self.assertNotIn(forbidden, mario_source)
            self.assertNotIn("clear_collision_cache", init_source)
            self.assertNotIn("ClearCollisionCache", init_source)

    def test_static_surfaces_load_only_in_session_start(self):
        for base in (ROOT, PACKAGE):
            tree = ast.parse((base / "mario.py").read_text(encoding="utf-8"))
            owners = []
            for function in (node for node in tree.body if isinstance(node, ast.FunctionDef)):
                if any(
                    isinstance(node, ast.Attribute)
                    and node.attr == "sm64_static_surfaces_load"
                    for node in ast.walk(function)
                ):
                    owners.append(function.name)
            self.assertEqual(owners, ["_configure_native_api", "insert_mario"])

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

    def test_fast_mesh_update_uses_bulk_coordinates_without_bmesh(self):
        source = (PACKAGE / "mario.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        self.assertFalse(any(
            isinstance(node, (ast.Import, ast.ImportFrom))
            and any(alias.name == "bmesh" for alias in node.names)
            for node in ast.walk(tree)
        ))
        fast_update = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "update_mesh_data_fast"
        )
        calls = {
            node.func.attr for node in ast.walk(fast_update)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertIn("foreach_set", calls)
        self.assertIn("update", calls)


if __name__ == "__main__":
    unittest.main()
