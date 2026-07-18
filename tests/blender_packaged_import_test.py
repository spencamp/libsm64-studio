"""Import the ZIP-packaged add-on exactly as Blender does.

Run with:
  blender --background --factory-startup --python tests/blender_packaged_import_test.py
"""

from pathlib import Path
import ast
import hashlib
import importlib
import os
import sys
import tempfile
import zipfile

import bpy


REQUIRED_MARIO_API = (
    "BAKING", "LIVE_IDLE", "POISONED", "RECORDING", "RESETTING", "STOPPED",
    "abandon_bake_transition", "begin_mario_recording",
    "freeze_mario_recording_for_bake", "resume_live_idle_after_transition",
)


def assert_init_import_contract(package_path):
    tree = ast.parse((package_path / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.level != 1 or not node.module:
            continue
        module = importlib.import_module("libsm64_studio.{}".format(node.module))
        for alias in node.names:
            if alias.name != "*":
                assert hasattr(module, alias.name), "{}.{}".format(node.module, alias.name)

root = Path(__file__).resolve().parents[1]

configured_install = os.environ.get("LIBSM64_EXPECTED_INSTALL_ROOT")
if configured_install:
    install_root = Path(configured_install).resolve().parent
    addon = importlib.import_module("libsm64_studio")
    mario = importlib.import_module("libsm64_studio.mario")
    expected_package = Path(configured_install).resolve()
    assert expected_package in Path(addon.__file__).resolve().parents
    assert expected_package in Path(mario.__file__).resolve().parents
    expected_mario = expected_package / "mario.py"
    assert hashlib.sha256(Path(mario.__file__).read_bytes()).digest() == hashlib.sha256(
        expected_mario.read_bytes()
    ).digest()
    assert all(hasattr(mario, symbol) for symbol in REQUIRED_MARIO_API)
    assert_init_import_contract(expected_package)
    assert not (expected_package / "collision_cache.py").exists()
    assert not hasattr(addon, "clear_collision_cache")
    assert not hasattr(bpy.types, "ClearCollisionCache_OT_Operator")

    # The bootstrap enabled (registered) the package. Exercise unregistration
    # and registration once more through Blender's real add-on lifecycle.
    bpy.ops.preferences.addon_disable(module="libsm64_studio")
    bpy.ops.preferences.addon_enable(module="libsm64_studio")
    addon = importlib.import_module("libsm64_studio")
    mario = importlib.import_module("libsm64_studio.mario")
    assert all(hasattr(mario, symbol) for symbol in REQUIRED_MARIO_API)
    assert mario.RUNTIME_API_VERSION == 4
    assert_init_import_contract(expected_package)
    assert addon.BAKING == mario.BAKING
    assert addon.POISONED == mario.POISONED
    print("libsm64 packaged enable/import/register/unregister smoke passed")
else:
    with tempfile.TemporaryDirectory() as directory:
        temporary_root = Path(directory)
        configured_archive = os.environ.get("LIBSM64_ADDON_ZIP")
        if configured_archive:
            archive_path = Path(configured_archive)
        else:
            sys.path.insert(0, str(root))
            try:
                from tools.build_addon_zip import build_archive
                archive_path = temporary_root / "libsm64_studio.zip"
                build_archive(root, archive_path)
            finally:
                sys.path.remove(str(root))

        install_root = temporary_root / "installed"
        install_root.mkdir()
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(install_root)

        sys.path.insert(0, str(install_root))
        try:
            addon = importlib.import_module("libsm64_studio")
            mario = importlib.import_module("libsm64_studio.mario")
            assert install_root in Path(addon.__file__).resolve().parents
            assert install_root in Path(mario.__file__).resolve().parents
            assert all(hasattr(mario, symbol) for symbol in REQUIRED_MARIO_API)
            expected_mario = install_root / "libsm64_studio" / "mario.py"
            assert hashlib.sha256(Path(mario.__file__).read_bytes()).digest() == hashlib.sha256(
                expected_mario.read_bytes()
            ).digest()

            # Reproduce an overlay update in one Blender process: the new package
            # initializer sees an older cached mario submodule missing its new API.
            mario.RUNTIME_API_VERSION = 0
            addon = importlib.reload(addon)
            mario = importlib.import_module("libsm64_studio.mario")
            assert all(hasattr(mario, symbol) for symbol in REQUIRED_MARIO_API)
            assert mario.RUNTIME_API_VERSION == 4
            assert addon.BAKING == mario.BAKING
            assert addon.POISONED == mario.POISONED
        finally:
            sys.path.remove(str(install_root))
            for name in list(sys.modules):
                if name == "libsm64_studio" or name.startswith("libsm64_studio."):
                    del sys.modules[name]

    print("libsm64 packaged import and stale-module reload passed")
