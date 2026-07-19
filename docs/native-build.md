# Reproducing the pinned libsm64 binaries

LibSM64 Studio Phase 3 continues the Phase 1/2 pin of `libsm64/libsm64` at the full commit
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

## Phase 3 optional runtime exports

Phase 3 does not change or rebuild either native artifact. Core Live Mario keeps
the Phase 2 required export set; user-invoked features bind their additional
exports lazily so an unavailable disabled feature cannot block startup. The
small ABI probe compile-checks every signature as its pass is added.

The complete Phase 3 optional bindings are:

- Better Start Marks: action, animation/frame, state, position, facing,
  velocity/forward velocity, health, and invincibility setters.
- Moving platforms: `sm64_surface_object_move(uint32_t, const struct
  SM64ObjectTransform *)`.
- Environment levels: `sm64_set_mario_water_level(int32_t, signed int)` and
  `sm64_set_mario_gas_level(int32_t, signed int)`.
- Cap controls: `sm64_mario_interact_cap(int32_t, uint32_t, uint16_t,
  uint8_t)` and `sm64_mario_extend_cap(int32_t, uint16_t)`.
- Live audio: `sm64_audio_init(const uint8_t *)`,
  `sm64_audio_tick(uint32_t, uint32_t, int16_t *)`, optional
  `sm64_set_sound_volume(float)`, and
  `sm64_register_play_sound_function(SM64PlaySoundFunctionPtr)`.
- Directing: `sm64_mario_take_damage(int32_t, uint32_t, uint32_t, float,
  float, float)`, `sm64_mario_heal(int32_t, uint8_t)`,
  `sm64_mario_kill(int32_t)`, and the health/invincibility setters already
  shared with Better Start Marks.
- Value-returning collision diagnostics: `sm64_surface_find_floor_height(float,
  float, float)`, `sm64_surface_find_water_level(float, float)`, and
  `sm64_surface_find_poison_gas_level(float, float)`.
- Native diagnostics: `sm64_register_debug_print_function(
  SM64DebugPrintFunctionPtr)`.

Pass 3E adds no native export. Runtime metadata is copied from the complete
modern `SM64MarioState` returned by the exact `sm64_mario_tick()` that produced
each recorded geometry buffer.

The audio init/tick exports remain optional for basic startup and are configured
only when Live Audio is requested. Studio retains the exact callback type and
callback object for its owning generation, unregisters it before releasing the
Python reference, and serializes every native call with the generation's audio
lock while the worker exists. The pinned implementation returns one per-channel
block size of 528 or 544 and writes two stereo signed-16-bit blocks per tick.
It has no `sm64_audio_terminate()`; the worker therefore stops before any native
teardown, and global termination remains the final audio/global cleanup boundary.

The debug callback is an optional diagnostic feature. Studio retains its exact
`CFUNCTYPE(None, c_char_p)` object for the owning lifecycle generation, copies
messages into a bounded plain-Python queue without Blender RNA access, keeps it
registered through surface/Mario cleanup, and registers a null callback before
global termination and before releasing the Python reference. Missing directing,
collision-query, debug, or audio exports are reported only when their optional
feature is configured or invoked; they are not added to the core Live Mario
startup export set.

At the pinned revision, `src/decomp/engine/surface_collision.c` returns
`-10000.0f` from both `find_water_level()` and
`find_poison_gas_level()`. Studio therefore uses signed native `-10000` as the
canonical value when either global level control is disabled; it does not invent
a separate sentinel.

The same pinned collision implementation defines `FLOOR_LOWER_LIMIT` as
`-110000.0f`; Studio treats only that return value as the explicit no-floor
sentinel and never exposes the accompanying native surface pointer APIs.
