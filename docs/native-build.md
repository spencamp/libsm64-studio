# Reproducing the pinned libsm64 binaries

LibSM64 Studio Phase 2 continues the Phase 1 pin of `libsm64/libsm64` at the full commit
`fd11813208272b4271d92bd92feb8f3fdbe61be5`. Both packaged artifacts must be
built from that same revision through upstream's `make lib` target. Never copy
a ROM, `run-test`, generated `build/` directories, or unrelated upstream files
into the add-on.

The Windows DLL is built with the upstream-supported MSYS2 MinGW64 GCC and GNU
Make. Do not use Zig for `sm64.dll`: a Zig 0.16.0 `x86_64-windows-gnu` build
loaded and exported the expected API but crashed inside `sm64_global_init`.
The Linux artifact was cross-built with GNU Make 4.4.1 and Zig 0.16.0. The Zig archive was the official
`zig-x86_64-windows-0.16.0.zip` distribution with SHA-256
`68659eb5f1e4eb1437a722f1dd889c5a322c9954607f5edcf337bc3684a75a7e`.
GNU Make ran in MSYS2 base `20260611` (archive SHA-256
`c105946e64e08f099ac0e4647461ce762b95333ad211777666476a9a41451d65`).

Install `mingw-w64-x86_64-gcc` and GNU Make in MSYS2. To reproduce only the
Windows artifact while preserving the already-pinned Linux artifact:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_libsm64_native.ps1 `
  -Msys2Root C:\path\to\msys64 `
  -PythonPath C:\path\to\python.exe `
  -WindowsOnly
```

Omit `-WindowsOnly` and provide `-ZigPath` to reproduce both artifacts.

The script clones or fetches the official repository, checks out the exact
detached SHA, refuses a dirty or different checkout, runs upstream's Mario
geometry importer, and invokes these upstream targets in separate clean source
trees:

```text
PATH=/mingw64/bin:/usr/bin OS=Windows_NT make lib -j4 CC=gcc
env -u OS make lib -j4 CC="zig cc -target x86_64-linux-gnu"
```

In full-build mode it requires both `dist/sm64.dll` and `dist/libsm64.so` before
replacement. In `-WindowsOnly` mode it replaces only `sm64.dll` and carries the
existing verified Linux artifact and toolchain entry forward unchanged. It then
builds the small `tools/libsm64_abi_probe.c` program against the pinned header,
calculates SHA-256 hashes, and rewrites `libsm64-build.json`. The manifest is the runtime source of
truth for repository, commit, artifact name, target, toolchain, ABI-probe data,
and artifact hash.

On a native Linux builder the equivalent source steps are `make clean`,
`python3 import-mario-geo.py`, and `make lib`; copy only `dist/libsm64.so` after
confirming `git rev-parse HEAD` and an empty `git status --porcelain`. Do not
update or distribute a one-platform package while its Python bindings or other
platform artifact target a different revision.
