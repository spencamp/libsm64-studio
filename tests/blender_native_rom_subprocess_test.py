"""Parent regression that contains native access violations in a child Blender."""

from pathlib import Path
import os
import subprocess

import bpy


root = Path(__file__).resolve().parents[1]
child_script = root / "tests" / "blender_native_rom_child.py"
rom_path = os.environ.get("LIBSM64_TEST_ROM", "")
if not rom_path:
    raise RuntimeError("LIBSM64_TEST_ROM must be supplied for the ROM-backed test")

environment = os.environ.copy()
environment["PYTHONUNBUFFERED"] = "1"
command = [
    bpy.app.binary_path,
    "--background",
    "--factory-startup",
    "--disable-crash-handler",
    "--python",
    str(child_script),
]
print("Launching crash-isolated native child: {}".format(command), flush=True)
try:
    completed = subprocess.run(
        command,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
except subprocess.TimeoutExpired as exc:
    print(exc.stdout or "", end="", flush=True)
    print(exc.stderr or "", end="", flush=True)
    raise RuntimeError("ROM-backed native child exceeded the 120-second limit") from exc
print(completed.stdout, end="", flush=True)
print(completed.stderr, end="", flush=True)

stages = [
    line.split(" ", 1)[1]
    for line in completed.stdout.splitlines()
    if line.startswith("LIBSM64_NATIVE_STAGE ")
]
last_stage = stages[-1] if stages else "no native stage emitted"
windows_status = completed.returncode & 0xFFFFFFFF
if completed.returncode != 0:
    raise RuntimeError(
        "ROM-backed native child crashed or failed after {!r}: return code {} "
        "(Windows status 0x{:08X})".format(
            last_stage, completed.returncode, windows_status
        )
    )
if "LIBSM64_NATIVE_RESULT PASS" not in completed.stdout:
    raise RuntimeError(
        "ROM-backed native child returned without PASS after {!r}".format(last_stage)
    )

required_stages = [
    "before_dll_load",
    "after_dll_load",
    "after_abi_configuration",
    "before_global_init",
    "after_global_init",
    "before_static_surface_load",
    "after_static_surface_load",
    "before_mario_create",
    "after_mario_create",
    "before_mario_tick",
    "after_mario_tick",
    "before_mario_delete",
    "after_mario_delete",
    "before_global_terminate",
    "after_global_terminate",
]
cursor = 0
for stage in stages:
    if cursor < len(required_stages) and stage == required_stages[cursor]:
        cursor += 1
if cursor != len(required_stages):
    raise AssertionError(
        "Native child stage sequence incomplete; expected {}, observed {}".format(
            required_stages, stages
        )
    )

print("ROM-backed native subprocess lifecycle passed", flush=True)
