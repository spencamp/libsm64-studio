"""Run one Blender test against the isolated, installed add-on package."""

from pathlib import Path
import os
import runpy
import sys
import traceback

import bpy


install_root = Path(os.environ["LIBSM64_EXPECTED_INSTALL_ROOT"]).resolve()
test_script = Path(os.environ["LIBSM64_TEST_SCRIPT"]).resolve()
test_blend = Path(os.environ["LIBSM64_TEST_BLEND"]).resolve()
result_file = Path(os.environ["LIBSM64_TEST_RESULT"]).resolve()

if not install_root.is_dir():
    raise RuntimeError("Isolated add-on install is missing: {}".format(install_root))
if not test_script.is_file():
    raise RuntimeError("Blender test script is missing: {}".format(test_script))

# Give every process a unique, disposable .blend and never inspect an existing file.
bpy.ops.wm.save_as_mainfile(filepath=str(test_blend), check_existing=False)

is_smoke = test_script.name == "blender_packaged_import_test.py"
enabled = False
try:
    if is_smoke:
        bpy.ops.preferences.addon_enable(module="libsm64_studio")
        enabled = True
        import libsm64_studio as addon

        if install_root not in Path(addon.__file__).resolve().parents:
            raise AssertionError(
                "Loaded add-on from {!r}, expected isolated install {!r}".format(
                    addon.__file__, str(install_root)
                )
            )
    else:
        # Import only from the isolated add-ons directory. Regression scripts
        # own register()/unregister() where their lifecycle assertions require it.
        sys.path.insert(0, str(install_root.parent))
    runpy.run_path(str(test_script), run_name="__main__")
    result_file.write_text("PASS\n", encoding="ascii")
except BaseException:
    traceback.print_exc()
finally:
    if enabled:
        bpy.ops.preferences.addon_disable(module="libsm64_studio")
