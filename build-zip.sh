#!/usr/bin/env bash
set -euo pipefail

output="${1:-libsm64_studio.zip}"
python tools/build_addon_zip.py "$output"
