"""Build the installable Blender add-on archive from the package source tree."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
import shutil
import zipfile


REQUIRED_MARIO_API = {
    "BAKING", "LIVE_IDLE", "POISONED", "RECORDING", "RESETTING", "STOPPED",
    "abandon_bake_transition", "begin_mario_recording",
    "freeze_mario_recording_for_bake", "resume_live_idle_after_transition",
}


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


def validate_package(root: Path) -> None:
    package = root / "libsm64_studio"
    if not package.is_dir():
        raise RuntimeError("Missing libsm64_studio package directory")

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
