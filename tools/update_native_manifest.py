"""Write the pinned native build manifest from completed local artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


REPOSITORY = "libsm64/libsm64"
COMMIT = "fd11813208272b4271d92bd92feb8f3fdbe61be5"
HEADER = "src/libsm64.h"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-lib", type=Path, required=True)
    parser.add_argument("--windows", type=Path, required=True)
    parser.add_argument("--linux", type=Path, required=True)
    parser.add_argument("--abi-probe", type=Path, required=True)
    parser.add_argument("--windows-toolchain", required=True)
    parser.add_argument("--linux-toolchain", required=True)
    args = parser.parse_args()

    for path in (args.windows, args.linux, args.abi_probe):
        if not path.is_file():
            raise SystemExit("Missing build output: {}".format(path))
    probe = json.loads(args.abi_probe.read_text(encoding="utf-8"))
    manifest = {
        "schema_version": 1,
        "repository": REPOSITORY,
        "commit": COMMIT,
        "header": HEADER,
        "windows_artifact": "sm64.dll",
        "linux_artifact": "libsm64.so",
        "artifacts": {
            "sm64.dll": {
                "sha256": sha256(args.windows),
                "target": "x86_64-windows-gnu",
                "toolchain": args.windows_toolchain,
                "build_target": "make lib",
            },
            "libsm64.so": {
                "sha256": sha256(args.linux),
                "target": "x86_64-linux-gnu",
                "toolchain": args.linux_toolchain,
                "build_target": "make lib",
            },
        },
        "abi_probe": probe,
    }
    args.package_lib.mkdir(parents=True, exist_ok=True)
    destination = args.package_lib / "libsm64-build.json"
    destination.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(destination)


if __name__ == "__main__":
    main()
