[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Msys2Root,
    [string]$ZigPath = "",
    [string]$BuildRoot = (Join-Path ([System.IO.Path]::GetTempPath()) "libsm64-native-build"),
    [string]$PythonPath = "python",
    [switch]$WindowsOnly
)

$ErrorActionPreference = "Stop"
$commit = "fd11813208272b4271d92bd92feb8f3fdbe61be5"
$repository = "https://github.com/libsm64/libsm64.git"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$bash = Join-Path ([System.IO.Path]::GetFullPath($Msys2Root)) "usr\bin\bash.exe"
$mingwGcc = Join-Path ([System.IO.Path]::GetFullPath($Msys2Root)) "mingw64\bin\gcc.exe"

if (-not (Test-Path -LiteralPath $bash -PathType Leaf)) {
    throw "MSYS2 bash not found: $bash"
}
if (-not (Test-Path -LiteralPath $mingwGcc -PathType Leaf)) {
    throw "MSYS2 MinGW64 GCC not found: $mingwGcc. Install mingw-w64-x86_64-gcc."
}
$zig = $null
if (-not $WindowsOnly) {
    if (-not $ZigPath) {
        throw "-ZigPath is required unless -WindowsOnly is used"
    }
    $zig = [System.IO.Path]::GetFullPath($ZigPath)
    if (-not (Test-Path -LiteralPath $zig -PathType Leaf)) {
        throw "Zig not found: $zig"
    }
}

function Convert-ToMsysPath([string]$Path) {
    $full = [System.IO.Path]::GetFullPath($Path)
    return "/" + $full.Substring(0, 1).ToLowerInvariant() + $full.Substring(2).Replace("\", "/")
}

function Invoke-UpstreamBuild([string]$Name, [string]$Target, [bool]$Windows) {
    $source = Join-Path $BuildRoot $Name
    if (-not (Test-Path -LiteralPath $source)) {
        git clone $repository $source | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "Could not clone $repository" }
    } else {
        git -C $source fetch origin $commit | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "Could not fetch pinned commit for $Name" }
    }
    git -C $source checkout --detach $commit | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Could not check out $commit for $Name" }
    $actual = (git -C $source rev-parse HEAD).Trim()
    if ($actual -ne $commit) {
        throw "$Name checkout mismatch: expected $commit, actual $actual"
    }
    $dirty = git -C $source status --porcelain --untracked-files=all
    if ($dirty) {
        throw "$Name checkout is dirty; refusing to build:`n$dirty"
    }

    $sourceMsys = Convert-ToMsysPath $source
    & $bash -lc "cd '$sourceMsys' && make clean" | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Upstream make clean failed for $Name" }

    Push-Location $source
    try {
        & $PythonPath ".\import-mario-geo.py" | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "Upstream Mario geometry import failed for $Name" }
    } finally {
        Pop-Location
    }

    if ($Windows) {
        & $bash -lc "cd '$sourceMsys' && PATH=/mingw64/bin:/usr/bin OS=Windows_NT make lib -j4 CC=gcc" | Out-Host
    } else {
        $zigMsys = Convert-ToMsysPath $zig
        & $bash -lc "cd '$sourceMsys' && env -u OS make lib -j4 CC='$zigMsys cc -target $Target'" | Out-Host
    }
    if ($LASTEXITCODE -ne 0) { throw "Upstream make lib failed for $Name" }
    return $source
}

$windowsSource = Invoke-UpstreamBuild "windows" "x86_64-windows-gnu" $true
$windowsArtifact = Join-Path $windowsSource "dist\sm64.dll"
if (-not (Test-Path -LiteralPath $windowsArtifact -PathType Leaf)) {
    throw "Upstream Windows artifact missing: $windowsArtifact"
}

$packageLib = Join-Path $repoRoot "libsm64_studio\lib"
$runtimeLib = Join-Path $repoRoot "lib"
$existingManifest = Get-Content -LiteralPath (Join-Path $packageLib "libsm64-build.json") -Raw | ConvertFrom-Json
if ($WindowsOnly) {
    $linuxArtifact = Join-Path $packageLib "libsm64.so"
    $linuxToolchain = $existingManifest.artifacts.'libsm64.so'.toolchain
} else {
    $linuxSource = Invoke-UpstreamBuild "linux" "x86_64-linux-gnu" $false
    $linuxArtifact = Join-Path $linuxSource "dist\libsm64.so"
    $zigVersion = (& $zig version).Trim()
    $linuxToolchain = "Zig $zigVersion x86_64-linux-gnu"
}
if (-not (Test-Path -LiteralPath $linuxArtifact -PathType Leaf)) {
    throw "Linux artifact missing: $linuxArtifact"
}

$probeExe = Join-Path $BuildRoot "libsm64-abi-probe.exe"
$probeJson = Join-Path $BuildRoot "libsm64-abi-probe.json"
$probeSourceMsys = Convert-ToMsysPath (Join-Path $repoRoot "tools\libsm64_abi_probe.c")
$probeIncludeMsys = Convert-ToMsysPath (Join-Path $windowsSource "src")
$probeExeMsys = Convert-ToMsysPath $probeExe
& $bash -lc "PATH=/mingw64/bin:/usr/bin gcc '$probeSourceMsys' -I '$probeIncludeMsys' -o '$probeExeMsys'" | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Could not compile the pinned-header ABI probe" }
& $probeExe | Set-Content -LiteralPath $probeJson -Encoding ASCII
if ($LASTEXITCODE -ne 0) { throw "Pinned-header ABI probe failed" }

Copy-Item -LiteralPath $windowsArtifact -Destination (Join-Path $packageLib "sm64.dll") -Force
Copy-Item -LiteralPath $windowsArtifact -Destination (Join-Path $runtimeLib "sm64.dll") -Force
if (-not $WindowsOnly) {
    Copy-Item -LiteralPath $linuxArtifact -Destination (Join-Path $packageLib "libsm64.so") -Force
    Copy-Item -LiteralPath $linuxArtifact -Destination (Join-Path $runtimeLib "libsm64.so") -Force
}

$gccVersion = (& $mingwGcc -dumpfullversion).Trim()
$gccTarget = (& $mingwGcc -dumpmachine).Trim()
& $PythonPath (Join-Path $repoRoot "tools\update_native_manifest.py") `
    --package-lib $packageLib `
    --windows $windowsArtifact `
    --linux $linuxArtifact `
    --abi-probe $probeJson `
    --windows-toolchain "MSYS2 MinGW-w64 GCC $gccVersion $gccTarget" `
    --linux-toolchain $linuxToolchain
if ($LASTEXITCODE -ne 0) { throw "Could not write native build manifest" }
Copy-Item -LiteralPath (Join-Path $packageLib "libsm64-build.json") `
    -Destination (Join-Path $runtimeLib "libsm64-build.json") -Force

Get-FileHash -Algorithm SHA256 $windowsArtifact, $linuxArtifact
