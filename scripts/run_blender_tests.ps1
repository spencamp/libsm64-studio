[CmdletBinding()]
param(
    [string]$BlenderPath = "C:\Program Files (x86)\Steam\steamapps\common\Blender\5.2\blender.exe",
    [string]$RomPath = $env:LIBSM64_TEST_ROM,
    [switch]$KeepTemp,
    [switch]$SmokeOnly
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$blenderExe = [System.IO.Path]::GetFullPath($BlenderPath)

if (-not (Test-Path -LiteralPath $blenderExe -PathType Leaf)) {
    # Steam currently places the executable beside its version-data directory,
    # although older/documented layouts put it inside that directory.
    $steamLayoutCandidate = Join-Path (Split-Path (Split-Path $blenderExe -Parent) -Parent) "blender.exe"
    if (Test-Path -LiteralPath $steamLayoutCandidate -PathType Leaf) {
        Write-Warning "Blender was not found at $blenderExe; using Steam executable $steamLayoutCandidate"
        $blenderExe = $steamLayoutCandidate
    } else {
        throw "Blender executable not found: $blenderExe. Pass -BlenderPath with the installed blender.exe path."
    }
}

$runId = "libsm64-blender-tests-{0}-{1}" -f $PID, ([Guid]::NewGuid().ToString("N"))
$runRoot = Join-Path ([System.IO.Path]::GetTempPath()) $runId
$stageRoot = Join-Path $runRoot "package-stage"
$stagedPackage = Join-Path $stageRoot "libsm64_studio"
$archivePath = Join-Path $runRoot "libsm64_studio.zip"
$userConfig = Join-Path $runRoot "user-config"
$userScripts = Join-Path $runRoot "user-scripts"
$userData = Join-Path $runRoot "user-datafiles"
$userExtensions = Join-Path $runRoot "user-extensions"
$addonRoot = Join-Path $userScripts "addons"
$installedPackage = Join-Path $addonRoot "libsm64_studio"
$blendRoot = Join-Path $runRoot "blend-files"

$savedEnvironment = @{}
$isolatedVariables = @(
    "BLENDER_USER_CONFIG", "BLENDER_USER_SCRIPTS", "BLENDER_USER_DATAFILES",
    "BLENDER_USER_EXTENSIONS", "LIBSM64_ADDON_ZIP",
    "LIBSM64_EXPECTED_INSTALL_ROOT", "LIBSM64_TEST_INSTALLED",
    "LIBSM64_BLENDER_TEST", "LIBSM64_TEST_ROM", "TEMP", "TMP"
)

function Assert-MirrorFile([string]$Name) {
    $runtime = Join-Path $repoRoot $Name
    $packaged = Join-Path (Join-Path $repoRoot "libsm64_studio") $Name
    if (-not (Test-Path -LiteralPath $runtime -PathType Leaf)) {
        throw "Missing mirrored runtime file: $runtime"
    }
    if (-not (Test-Path -LiteralPath $packaged -PathType Leaf)) {
        throw "Missing packaged file: $packaged"
    }
    $runtimeHash = (Get-FileHash -LiteralPath $runtime -Algorithm SHA256).Hash
    $packagedHash = (Get-FileHash -LiteralPath $packaged -Algorithm SHA256).Hash
    if ($runtimeHash -ne $packagedHash) {
        throw "Runtime/package source mismatch for $Name. Synchronize the mirrored files before testing."
    }
}

function Invoke-BlenderTest([string]$Label, [string]$TestScript) {
    $testBlend = Join-Path $blendRoot (([System.IO.Path]::GetFileNameWithoutExtension($TestScript)) + ".blend")
    $resultFile = Join-Path $runRoot (([System.IO.Path]::GetFileNameWithoutExtension($TestScript)) + ".passed")
    Remove-Item -LiteralPath $resultFile -Force -ErrorAction SilentlyContinue
    $env:LIBSM64_TEST_SCRIPT = $TestScript
    $env:LIBSM64_TEST_BLEND = $testBlend
    $env:LIBSM64_TEST_RESULT = $resultFile
    $arguments = @(
        "--background", "--factory-startup",
        "--python", (Join-Path $repoRoot "tests\blender_packaged_test_bootstrap.py")
    )
    Write-Host ""
    Write-Host "[$Label] $blenderExe $($arguments -join ' ')"
    & $blenderExe @arguments
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "$Label failed with Blender exit code $exitCode"
    }
    if (-not (Test-Path -LiteralPath $resultFile -PathType Leaf)) {
        throw "$Label failed: Blender returned 0 but the test did not write its success sentinel; inspect the traceback above"
    }
    Write-Host "[$Label] PASS"
}

function Install-StagedPackageClean {
    $resolvedAddonRoot = [System.IO.Path]::GetFullPath($addonRoot)
    $resolvedTarget = [System.IO.Path]::GetFullPath($installedPackage)
    $requiredPrefix = $resolvedAddonRoot.TrimEnd('\') + '\'
    if (-not $resolvedTarget.StartsWith($requiredPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to replace an add-on outside the isolated test root: $resolvedTarget"
    }
    if (Test-Path -LiteralPath $resolvedTarget) {
        Remove-Item -LiteralPath $resolvedTarget -Recurse -Force
    }
    Copy-Item -LiteralPath $stagedPackage -Destination $addonRoot -Recurse -Force -ErrorAction Stop
}

try {
    New-Item -ItemType Directory -Path $stagedPackage, $addonRoot, $userConfig, $userData, $userExtensions, $blendRoot -Force | Out-Null

    foreach ($name in @("__init__.py", "mario.py", "collision_cache.py", "recording.py", "take_manager.py", "input_reader.py", "input_reader_win.py", "collision_types.py", "zeth_inputs.py")) {
        Assert-MirrorFile $name
    }

    try {
        Get-ChildItem -LiteralPath (Join-Path $repoRoot "libsm64_studio") -Force | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $stagedPackage -Recurse -Force -ErrorAction Stop
        }
        Get-ChildItem -LiteralPath $stagedPackage -Directory -Filter "__pycache__" -Recurse -Force | ForEach-Object {
            if (-not $_.FullName.StartsWith($stageRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Refusing to clean a cache directory outside this run's staging root: $($_.FullName)"
            }
            Remove-Item -LiteralPath $_.FullName -Recurse -Force
        }
        Get-ChildItem -LiteralPath $stagedPackage -File -Recurse -Force | Where-Object {
            $_.Extension -in @(".pyc", ".pyo")
        } | Remove-Item -Force
    } catch {
        throw "Could not stage packaged add-on files (a file may be locked): $($_.Exception.Message)"
    }

    foreach ($required in @("__init__.py", "mario.py", "collision_cache.py", "recording.py", "take_manager.py", "lib\libsm64-build.json", "lib\sm64.dll", "lib\libsm64.so", "lib\SDL2.dll")) {
        if (-not (Test-Path -LiteralPath (Join-Path $stagedPackage $required) -PathType Leaf)) {
            throw "Staged add-on is missing required file: libsm64_studio\$required"
        }
    }

    $nativeManifestPath = Join-Path $stagedPackage "lib\libsm64-build.json"
    $nativeManifest = Get-Content -LiteralPath $nativeManifestPath -Raw | ConvertFrom-Json
    $pinnedCommit = "fd11813208272b4271d92bd92feb8f3fdbe61be5"
    if ($nativeManifest.repository -ne "libsm64/libsm64" -or $nativeManifest.commit -ne $pinnedCommit -or $nativeManifest.header -ne "src/libsm64.h") {
        throw "Staged native build manifest does not name the pinned repository/header/commit"
    }
    foreach ($artifactName in @("sm64.dll", "libsm64.so")) {
        $artifactPath = Join-Path $stagedPackage ("lib\" + $artifactName)
        $expectedHash = $nativeManifest.artifacts.$artifactName.sha256
        $actualHash = (Get-FileHash -LiteralPath $artifactPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $expectedHash) {
            throw "Staged native hash mismatch for $artifactName`: expected $expectedHash, actual $actualHash"
        }
    }
    $sm64NativeNames = @(Get-ChildItem -LiteralPath (Join-Path $stagedPackage "lib") -File | Where-Object {
        $_.Name.ToLowerInvariant().Contains("sm64")
    } | ForEach-Object { $_.Name })
    $expectedNativeNames = @("libsm64-build.json", "libsm64.so", "sm64.dll")
    if (Compare-Object -ReferenceObject $expectedNativeNames -DifferenceObject $sm64NativeNames) {
        throw "Staged package contains an unexpected or stale libsm64 native artifact"
    }

    $marioSource = Get-Content -LiteralPath (Join-Path $stagedPackage "mario.py") -Raw
    $initSource = Get-Content -LiteralPath (Join-Path $stagedPackage "__init__.py") -Raw
    foreach ($symbol in @("STOPPED", "LIVE_IDLE", "RECORDING", "BAKING", "RESETTING", "POISONED")) {
        if ($marioSource -notmatch "(?m)^$symbol\s*=") {
            throw "Packaged mario.py does not export lifecycle symbol $symbol"
        }
        if ($initSource -notmatch "(?m)^\s+$symbol,") {
            throw "Packaged __init__.py does not import lifecycle symbol $symbol"
        }
    }

    Compress-Archive -LiteralPath $stagedPackage -DestinationPath $archivePath -CompressionLevel Optimal
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($archivePath)
    try {
        $entries = @($zip.Entries | Where-Object { -not $_.FullName.EndsWith("/") } | ForEach-Object {
            $_.FullName.Replace("\", "/")
        })
        $expectedEntries = @(Get-ChildItem -LiteralPath $stagedPackage -File -Recurse -Force | ForEach-Object {
            "libsm64_studio/" + $_.FullName.Substring($stagedPackage.Length + 1).Replace("\", "/")
        })
        $entryDifference = Compare-Object -ReferenceObject $expectedEntries -DifferenceObject $entries
        if ($entryDifference) {
            throw "Generated add-on ZIP contents differ from the staged install: $($entryDifference | Out-String)"
        }
        foreach ($required in @("libsm64_studio/__init__.py", "libsm64_studio/mario.py", "libsm64_studio/collision_cache.py", "libsm64_studio/lib/libsm64-build.json", "libsm64_studio/lib/sm64.dll", "libsm64_studio/lib/libsm64.so", "libsm64_studio/lib/SDL2.dll")) {
            if ($entries -notcontains $required) {
                throw "Generated add-on ZIP is missing $required"
            }
        }
    } finally {
        $zip.Dispose()
    }

    try {
        # Exercise upgrading over an existing directory. A clean replacement is
        # required because Blender's overlay installer can otherwise leave old
        # modules and bytecode beside a new __init__.py.
        New-Item -ItemType Directory -Path $installedPackage -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $installedPackage "stale-upgrade-file.py") -Value "old"
        Install-StagedPackageClean
        if (Test-Path -LiteralPath (Join-Path $installedPackage "stale-upgrade-file.py")) {
            throw "Clean package upgrade retained a stale file"
        }
    } catch {
        throw "Could not install the staged add-on into the isolated test directory: $($_.Exception.Message)"
    }

    foreach ($name in $isolatedVariables) {
        $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
    }
    $env:BLENDER_USER_CONFIG = $userConfig
    $env:BLENDER_USER_SCRIPTS = $userScripts
    $env:BLENDER_USER_DATAFILES = $userData
    $env:BLENDER_USER_EXTENSIONS = $userExtensions
    $env:LIBSM64_ADDON_ZIP = $archivePath
    $env:LIBSM64_EXPECTED_INSTALL_ROOT = $installedPackage
    $env:LIBSM64_TEST_INSTALLED = "1"
    $env:LIBSM64_BLENDER_TEST = "1"
    if ($RomPath) {
        $resolvedRom = [System.IO.Path]::GetFullPath($RomPath)
        if (-not (Test-Path -LiteralPath $resolvedRom -PathType Leaf)) {
            throw "ROM-backed native test ROM was not found: $resolvedRom"
        }
        $romSha1 = (Get-FileHash -LiteralPath $resolvedRom -Algorithm SHA1).Hash.ToLowerInvariant()
        if ($romSha1 -ne "9bef1128717f958171a4afac3ed78ee2bb4e86ce") {
            throw "-RomPath is not the supported unmodified SM64 US ROM"
        }
        $env:LIBSM64_TEST_ROM = $resolvedRom
    } else {
        Remove-Item Env:LIBSM64_TEST_ROM -ErrorAction SilentlyContinue
    }
    $env:TEMP = $runRoot
    $env:TMP = $runRoot

    Invoke-BlenderTest "packaged add-on smoke" (Join-Path $repoRoot "tests\blender_packaged_import_test.py")
    Invoke-BlenderTest "real native ABI smoke" (Join-Path $repoRoot "tests\blender_native_abi_smoke_test.py")
    if ($RomPath) {
        Invoke-BlenderTest "ROM-backed native subprocess lifecycle" (Join-Path $repoRoot "tests\blender_native_rom_subprocess_test.py")
    } else {
        Write-Warning "ROM-backed native subprocess lifecycle was not run; pass -RomPath or set LIBSM64_TEST_ROM."
    }
    if (-not $SmokeOnly) {
        Invoke-BlenderTest "bulk mesh update regression" (Join-Path $repoRoot "tests\blender_mesh_update_test.py")
        Invoke-BlenderTest "static collision regression" (Join-Path $repoRoot "tests\blender_static_collision_test.py")
        Invoke-BlenderTest "evaluated collision cache regression" (Join-Path $repoRoot "tests\blender_collision_cache_test.py")
        Invoke-BlenderTest "live control regression" (Join-Path $repoRoot "tests\blender_live_control_test.py")
        Invoke-BlenderTest "Start Mark regression" (Join-Path $repoRoot "tests\blender_start_mark_test.py")
        Invoke-BlenderTest "Timeline Start Frame regression" (Join-Path $repoRoot "tests\blender_timeline_start_frame_test.py")
        Invoke-BlenderTest "native lifecycle regression" (Join-Path $repoRoot "tests\blender_native_lifecycle_test.py")
        Invoke-BlenderTest "surface-object streaming regression" (Join-Path $repoRoot "tests\blender_collision_streaming_test.py")
        Invoke-BlenderTest "three-take regression" (Join-Path $repoRoot "tests\blender_three_take_regression_test.py")
        Invoke-BlenderTest "local-pose bake reconstruction regression" (Join-Path $repoRoot "tests\blender_local_pose_bake_test.py")
    }

    Write-Host ""
    Write-Host "All requested Blender tests passed."
    if ($KeepTemp) {
        Write-Host "Temporary test environment retained at: $runRoot"
    }
} finally {
    Remove-Item Env:LIBSM64_TEST_SCRIPT -ErrorAction SilentlyContinue
    Remove-Item Env:LIBSM64_TEST_BLEND -ErrorAction SilentlyContinue
    Remove-Item Env:LIBSM64_TEST_RESULT -ErrorAction SilentlyContinue
    foreach ($name in $savedEnvironment.Keys) {
        $value = $savedEnvironment[$name]
        if ($null -eq $value) {
            [Environment]::SetEnvironmentVariable($name, $null, "Process")
        } else {
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
    if ((-not $KeepTemp) -and (Test-Path -LiteralPath $runRoot)) {
        Remove-Item -LiteralPath $runRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
