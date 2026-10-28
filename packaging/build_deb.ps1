param(
    [string]$Distribution = "Ubuntu-22.04",
    [string]$OutputDir = "",
    [string]$ReleaseVersion = "",
    [switch]$FullFeatures
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw "WSL is required. Use an existing Ubuntu 22.04 x86_64 distribution."
}
$LinuxRoot = (& wsl.exe -d $Distribution --exec wslpath -a $ProjectRoot)
if ($LASTEXITCODE -ne 0) { throw "Cannot access WSL distribution: $Distribution" }
$LinuxRoot = ($LinuxRoot -join "").Trim()
$BuildArgs = @("-d", $Distribution, "--cd", $LinuxRoot, "--exec", "python3",
               "packaging/build_deb.py")
if ($FullFeatures) { $BuildArgs += "--full-features" }
if ($ReleaseVersion) { $BuildArgs += @("--release-version", $ReleaseVersion) }
if ($OutputDir) {
    $AbsoluteOutput = [System.IO.Path]::GetFullPath($OutputDir)
    $LinuxOutput = (& wsl.exe -d $Distribution --exec wslpath -a $AbsoluteOutput)
    if ($LASTEXITCODE -ne 0) { throw "Cannot convert output path: $AbsoluteOutput" }
    $BuildArgs += @("--output-dir", ($LinuxOutput -join "").Trim())
}
& wsl.exe @BuildArgs
if ($LASTEXITCODE -ne 0) { throw "Ubuntu DEB build failed: $LASTEXITCODE" }
