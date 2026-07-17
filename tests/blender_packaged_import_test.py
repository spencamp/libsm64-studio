"""Import the ZIP-packaged add-on exactly as Blender does.

Run with:
  blender --background --factory-startup --python tests/blender_packaged_import_test.py
"""

from pathlib import Path
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

root = Path(__file__).resolve().parents[1]

configured_install = os.environ.get("LIBSM64_EXPECTED_INSTALL_ROOT")
if configured_install:
    install_root = Path(configured_install).resolve().parent
    addon = importlib.import_module("libsm64_studio")
    mario = importlib.import_module("libsm64_studio.mario")
    expected_package = Path(configured_install).resolve()
    assert expected_package in Path(addon.__file__).resolve().parents
    assert expected_package in Path(mario.__file__).resolve().parents
    assert all(hasattr(mario, symbol) for symbol in REQUIRED_MARIO_API)

    # The bootstrap enabled (registered) the package. Exercise unregistration
    # and registration once more through Blender's real add-on lifecycle.
    bpy.ops.preferences.addon_disable(module="libsm64_studio")
    bpy.ops.preferences.addon_enable(module="libsm64_studio")
    addon = importlib.import_module("libsm64_studio")
    mario = importlib.import_module("libsm64_studio.mario")
    assert all(hasattr(mario, symbol) for symbol in REQUIRED_MARIO_API)
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

            # Reproduce an overlay update in one Blender process: the new package
            # initializer sees an older cached mario submodule missing its new API.
            for symbol in REQUIRED_MARIO_API:
                mario.__dict__.pop(symbol, None)
            addon = importlib.reload(addon)
            mario = importlib.import_module("libsm64_studio.mario")
            assert all(hasattr(mario, symbol) for symbol in REQUIRED_MARIO_API)
            assert addon.BAKING == mario.BAKING
            assert addon.POISONED == mario.POISONED
        finally:
            sys.path.remove(str(install_root))
            for name in list(sys.modules):
                if name == "libsm64_studio" or name.startswith("libsm64_studio."):
                    del sys.modules[name]

    print("libsm64 packaged import and stale-module reload passed")
