import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "libsm64_studio"


class PackageImportContractTests(unittest.TestCase):
    def test_installable_package_matches_runtime_sources(self):
        """Prevent the installable package from shipping a stale module copy."""
        for name in ("__init__.py", "mario.py", "recording.py", "take_manager.py"):
            self.assertEqual(
                (ROOT / name).read_bytes(),
                (PACKAGE / name).read_bytes(),
                "Installable package has a stale {}".format(name),
            )

    def test_starting_mark_symbol_is_defined_and_imported_exactly(self):
        mario_tree = ast.parse((PACKAGE / "mario.py").read_text(encoding="utf-8"))
        definitions = {
            node.name for node in mario_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertIn("capture_mario_starting_mark", definitions)

        init_tree = ast.parse((PACKAGE / "__init__.py").read_text(encoding="utf-8"))
        mario_imports = {
            alias.name
            for node in init_tree.body
            if isinstance(node, ast.ImportFrom) and node.module == "mario"
            for alias in node.names
        }
        self.assertIn("capture_mario_starting_mark", mario_imports)

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


if __name__ == "__main__":
    unittest.main()
