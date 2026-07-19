"""Build the installable Blender add-on archive from the package source tree."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import shutil
import zipfile


REQUIRED_MARIO_API = {
    "BAKING", "LIVE_IDLE", "POISONED", "RECORDING", "RESETTING", "STOPPED",
    "abandon_bake_transition", "begin_mario_recording",
    "freeze_mario_recording_for_bake", "resume_live_idle_after_transition",
    "apply_scene_debug_settings", "damage_mario", "heal_mario", "kill_mario",
    "probe_collision_at_cursor", "set_mario_health",
    "set_mario_invincibility", "studio_diagnostics",
}
PINNED_LIBSM64_COMMIT = "fd11813208272b4271d92bd92feb8f3fdbe61be5"
NATIVE_FILES = {"libsm64-build.json", "sm64.dll", "libsm64.so"}


def _top_level_symbols(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    symbols = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            symbols.update(target.id for target in targets if isinstance(target, ast.Name))
    return symbols


def _mario_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "mario"
        for alias in node.names
    }


def relative_import_contract(path: Path) -> dict[str, set[str]]:
    """Return every explicit ``from .module import name`` used by the package."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    contract = {}
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.level != 1 or not node.module:
            continue
        names = {alias.name for alias in node.names if alias.name != "*"}
        contract.setdefault(node.module, set()).update(names)
    return contract


def validate_package(root: Path) -> None:
    package = root / "libsm64_studio"
    if not package.is_dir():
        raise RuntimeError("Missing libsm64_studio package directory")

    package_lib = package / "lib"
    manifest_path = package_lib / "libsm64-build.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("Invalid native build manifest: {}".format(exc)) from exc
    expected_manifest = {
        "repository": "libsm64/libsm64",
        "commit": PINNED_LIBSM64_COMMIT,
        "header": "src/libsm64.h",
        "windows_artifact": "sm64.dll",
        "linux_artifact": "libsm64.so",
    }
    for field_name, expected in expected_manifest.items():
        if manifest.get(field_name) != expected:
            raise RuntimeError(
                "Native manifest {} must be {!r}, got {!r}".format(
                    field_name, expected, manifest.get(field_name)
                )
            )
    for artifact_name in ("sm64.dll", "libsm64.so"):
        artifact = package_lib / artifact_name
        if not artifact.is_file():
            raise RuntimeError("Missing packaged native artifact: {}".format(artifact_name))
        expected_hash = manifest.get("artifacts", {}).get(artifact_name, {}).get("sha256")
        actual_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if expected_hash != actual_hash:
            raise RuntimeError(
                "Native artifact hash mismatch for {}: expected {}, actual {}".format(
                    artifact_name, expected_hash, actual_hash
                )
            )
        runtime_artifact = root / "lib" / artifact_name
        if not runtime_artifact.is_file() or runtime_artifact.read_bytes() != artifact.read_bytes():
            raise RuntimeError("Runtime/package native mismatch: {}".format(artifact_name))
    runtime_manifest = root / "lib" / "libsm64-build.json"
    if not runtime_manifest.is_file() or runtime_manifest.read_bytes() != manifest_path.read_bytes():
        raise RuntimeError("Runtime/package native manifest mismatch")
    sm64_named_files = {
        path.name for path in package_lib.iterdir()
        if path.is_file() and "sm64" in path.name.lower()
    }
    if sm64_named_files != NATIVE_FILES:
        raise RuntimeError(
            "Unexpected or missing libsm64 native files: {}".format(
                ", ".join(sorted(sm64_named_files ^ NATIVE_FILES))
            )
        )

    for packaged_source in sorted(package.glob("*.py")):
        runtime_source = root / packaged_source.name
        if not runtime_source.is_file():
            raise RuntimeError("Missing runtime mirror for {}".format(packaged_source.name))
        if runtime_source.read_bytes() != packaged_source.read_bytes():
            raise RuntimeError(
                "Runtime/package source mismatch: {}".format(packaged_source.name)
            )

    mario_symbols = _top_level_symbols(package / "mario.py")
    init_imports = _mario_imports(package / "__init__.py")
    missing_exports = REQUIRED_MARIO_API - mario_symbols
    missing_imports = REQUIRED_MARIO_API - init_imports
    if missing_exports:
        raise RuntimeError(
            "Packaged mario.py is missing: {}".format(", ".join(sorted(missing_exports)))
        )
    if missing_imports:
        raise RuntimeError(
            "Packaged __init__.py does not import: {}".format(", ".join(sorted(missing_imports)))
        )

    for module_name, imported_names in relative_import_contract(package / "__init__.py").items():
        module_path = package / (module_name + ".py")
        if not module_path.is_file():
            raise RuntimeError("Packaged __init__.py imports missing module: {}".format(module_name))
        missing_names = imported_names - _top_level_symbols(module_path)
        if missing_names:
            raise RuntimeError(
                "Packaged __init__.py imports missing symbols from {}: {}".format(
                    module_name, ", ".join(sorted(missing_names))
                )
            )


def build_archive(root: Path, output: Path) -> None:
    validate_package(root)
    package = root / "libsm64_studio"
    temporary = output.with_suffix(output.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in sorted(package.rglob("*")):
            if not source.is_file():
                continue
            if "__pycache__" in source.parts or source.suffix in {".pyc", ".pyo"}:
                continue
            archive.write(source, source.relative_to(root).as_posix())
    with zipfile.ZipFile(temporary) as archive:
        names = archive.namelist()
        if not names or any(
            not name.startswith("libsm64_studio/")
            or name.startswith("libsm64_studio/libsm64_studio/")
            for name in names
        ):
            temporary.unlink()
            raise RuntimeError("Archive has an invalid or nested add-on package layout")
    try:
        temporary.replace(output)
    except PermissionError:
        # OneDrive-backed Windows workspaces can deny replace-over-existing
        # even when the destination is writable. Copying the completed archive
        # still avoids exposing a partially written ZIP.
        shutil.copyfile(temporary, output)
        temporary.unlink()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", nargs="?", default="libsm64_studio.zip")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = Path(args.output)
    if not output.is_absolute():
        output = root / output
    build_archive(root, output)
    print(output)


if __name__ == "__main__":
    main()
