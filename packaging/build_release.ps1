param(
    [string]$Python = "",
    [string]$Distribution = "Ubuntu-22.04",
    [string]$ReleaseVersion = ""
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BuildPython = Join-Path $ProjectRoot "build/windows-release-env/Scripts/python.exe"
if (-not (Test-Path -LiteralPath $BuildPython)) {
    if (-not $Python) {
        $Candidates = @(Get-ChildItem -Path "$env:APPDATA/uv/python/cpython-3.12.*-windows-x86_64-none/python.exe" -File -ErrorAction SilentlyContinue)
        if ($Candidates.Count) { $Python = $Candidates[-1].FullName }
        else { $Python = (Get-Command python -ErrorAction Stop).Source }
    }
    & $Python -m venv (Join-Path $ProjectRoot "build/windows-release-env")
    if ($LASTEXITCODE -ne 0) { throw "Cannot create the Windows build environment" }
}
$Requirement = Join-Path $PSScriptRoot "requirements-ubuntu-build.txt"
$RequirementHash = (Get-FileHash -LiteralPath $Requirement -Algorithm SHA256).Hash
$Marker = Join-Path $ProjectRoot "build/windows-release-env/requirements.sha256"
if (-not (Test-Path -LiteralPath $Marker) -or (Get-Content -LiteralPath $Marker -Raw).Trim() -ne $RequirementHash) {
    & $BuildPython -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot "requirements-windows-build.txt")
    if ($LASTEXITCODE -ne 0) { throw "Cannot install Windows release dependencies" }
    Set-Content -LiteralPath $Marker -Value $RequirementHash -Encoding ASCII
}
$ReleaseArgs = @((Join-Path $PSScriptRoot "build_release.py"), "--distribution", $Distribution)
if ($ReleaseVersion) { $ReleaseArgs += @("--release-version", $ReleaseVersion) }
& $BuildPython @ReleaseArgs
if ($LASTEXITCODE -ne 0) { throw "Paired Windows/Ubuntu release failed: $LASTEXITCODE" }
