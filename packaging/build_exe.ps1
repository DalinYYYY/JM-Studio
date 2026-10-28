param(
    [string]$Python = "",
    [string]$Name = "JointMotorController",
    [string]$Entry = "main.py",
    [switch]$OneFile,
    [switch]$SkipInstall,
    [switch]$Clean,
    [string]$ReleaseVersion = "",
    [string]$OutputDir = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot
if (-not $Python) {
    $CachedPython = Join-Path $ProjectRoot "build/windows-release-env/Scripts/python.exe"
    if (Test-Path -LiteralPath $CachedPython) { $Python = $CachedPython }
    else { $Python = (Get-Command python -ErrorAction Stop).Source }
}
$PythonExe = (Resolve-Path -LiteralPath $Python).Path
function Invoke-Step([string]$Title, [scriptblock]$Action) {
    Write-Host "==> $Title"
    & $Action
    if ($LASTEXITCODE -ne 0) { throw "$Title failed: $LASTEXITCODE" }
}
if (-not $SkipInstall) {
    Invoke-Step "Install Windows build/runtime dependencies" {
        & $PythonExe -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot "requirements-windows-build.txt")
    }
}
Invoke-Step "Check runtime and 64-bit Python" {
    & $PythonExe -c 'import struct, PyQt6, pyqtgraph, serial, numpy, can, PyInstaller; assert struct.calcsize(chr(80)) == 8'
}
$VersionTool = Join-Path $PSScriptRoot "release_version.py"
if ($ReleaseVersion) { & $PythonExe $VersionTool --expect $ReleaseVersion | Out-Null }
else { $ReleaseVersion = (& $PythonExe $VersionTool --bump).Trim() }
if ($LASTEXITCODE -ne 0) { throw "Cannot allocate or verify the release version" }
$Metadata = (& $PythonExe $VersionTool --metadata) | ConvertFrom-Json
$VersionFile = Join-Path $ProjectRoot "build/windows-version-info.txt"
& $PythonExe $VersionTool --expect $ReleaseVersion --windows-version-file $VersionFile | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Cannot write Windows file version information" }
$Slug = if ($Name -eq "JointMotorController") { $Metadata.slug } else { $Name }
$BuildName = "${Slug}_${ReleaseVersion}_Windows-x64"
if (-not $OutputDir) { $OutputDir = Join-Path $ProjectRoot "dist/windows" }
$OutputDir = [System.IO.Path]::GetFullPath($OutputDir)
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$Staging = Join-Path $ProjectRoot "build/windows-staging"
Invoke-Step "Prepare CAN-enabled release resources" {
    & $PythonExe (Join-Path $PSScriptRoot "prepare_build.py") --out $Staging
}
$Resources = Join-Path $Staging "resources"
$Features = Get-Content -LiteralPath (Join-Path $Resources "feature_flags.json") -Raw | ConvertFrom-Json
if ($Features.features.can_transport -ne $true) { throw "Windows release must enable can_transport" }
$Utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $Resources "ui_layout.json"), '{"version":1}', $Utf8)
[System.IO.File]::WriteAllText((Join-Path $Resources "user_motor_presets.json"), '{"version":1,"presets":{}}', $Utf8)
$PcanDll = Join-Path $env:WINDIR "System32/PCANBasic.dll"
if (-not (Test-Path -LiteralPath $PcanDll)) { throw "Missing 64-bit PCANBasic.dll; install the PEAK PCAN driver/SDK first" }
$SavedEnvironment = @{}
foreach ($Key in @("JM_STAGING_RES", "JM_BUILD_NAME", "JM_ENTRY", "JM_PCAN_DLL", "JM_VERSION_FILE")) {
    $SavedEnvironment[$Key] = [Environment]::GetEnvironmentVariable($Key, "Process")
}
try {
    $env:JM_STAGING_RES = $Resources
    $env:JM_BUILD_NAME = $BuildName
    $env:JM_ENTRY = if ($Entry -eq "main.py") { "packaging/linux_entry.py" } else { $Entry }
    $env:JM_PCAN_DLL = $PcanDll
    $env:JM_VERSION_FILE = $VersionFile
    if ($OneFile) {
        $PyiArgs = @((Join-Path $PSScriptRoot "JointMotorController.spec"), "--noconfirm",
                     "--distpath", $OutputDir, "--workpath", (Join-Path $ProjectRoot "build/windows-pyinstaller"))
    } else {
        $PyiArgs = @("--noconfirm", "--windowed", "--onedir", "--name", $BuildName,
                     "--distpath", $OutputDir, "--workpath", (Join-Path $ProjectRoot "build/windows-pyinstaller"),
                     "--specpath", (Join-Path $ProjectRoot "build"), "--paths", $ProjectRoot,
                     "--add-data", "$Resources;resources", "--add-binary", "$PcanDll;.",
                     "--collect-submodules", "pyqtgraph.graphicsItems",
                     "--collect-submodules", "pyqtgraph.widgets",
                     "--collect-submodules", "pyqtgraph.exporters",
                     "--collect-submodules", "pyqtgraph.parametertree",
                     "--collect-submodules", "transport.virtual_engine",
                     "--collect-submodules", "can.interfaces.pcan",
                     "--collect-submodules", "can.interfaces.virtual",
                     "--hidden-import", "serial.tools.list_ports",
                     "--hidden-import", "PyQt6.sip", "--hidden-import", "PyQt6.QtOpenGLWidgets",
                     "--version-file", $VersionFile,
                     $env:JM_ENTRY)
    }
    if ($Clean) { $PyiArgs += "--clean" }
    Invoke-Step "Build $BuildName" { & $PythonExe -m PyInstaller @PyiArgs }
} finally {
    foreach ($Key in $SavedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($Key, $SavedEnvironment[$Key], "Process")
    }
}
$OutputExe = if ($OneFile) { Join-Path $OutputDir "$BuildName.exe" }
             else { Join-Path (Join-Path $OutputDir $BuildName) "$BuildName.exe" }
if (-not (Test-Path -LiteralPath $OutputExe)) { throw "Build output missing: $OutputExe" }
Write-Host "Windows $ReleaseVersion build complete: $OutputExe"
