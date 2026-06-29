param(
    [string]$Python = "",
    [string]$Name = "JointMotorController",
    [switch]$OneFile,
    [switch]$SkipInstall,
    [switch]$Clean
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..")).Path
Set-Location $ProjectRoot

function Invoke-Step {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Title,
        [Parameter(Mandatory = $true)]
        [scriptblock]$Script
    )
    Write-Host ""
    Write-Host "==> $Title" -ForegroundColor Cyan
    & $Script
    if ($LASTEXITCODE -ne 0) {
        throw "$Title failed, exit code $LASTEXITCODE"
    }
}

if ([string]::IsNullOrWhiteSpace($Python)) {
    $PythonExe = $null
    $CondaPrefix = $env:CONDA_PREFIX
    if (-not [string]::IsNullOrWhiteSpace($CondaPrefix)) {
        $CondaPython = Join-Path $CondaPrefix "python.exe"
        if (Test-Path $CondaPython) {
            $PythonExe = (Resolve-Path $CondaPython).Path
        }
    }
    if (-not $PythonExe) {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if ($null -eq $cmd) {
            throw "python was not found. Pass -Python C:\path\to\python.exe"
        }
        $PythonExe = $cmd.Source
    }
} else {
    $PythonExe = (Resolve-Path $Python).Path
}

$Entry = "main.py"
$Resources = Join-Path $ProjectRoot "resources"
$IconIco = Join-Path $Resources "pic\log_ioc.ico"
$RuntimeReq = "requirements.txt"
$BuildReq = Join-Path $ScriptDir "requirements-build.txt"
$AddData = "$Resources;resources"
$DateTag = Get-Date -Format "yyyyMMdd"
$BuildName = $Name
$BuildDir = "${Name}_${DateTag}"

if (!(Test-Path $Entry)) {
    throw "Entry file not found: $ProjectRoot\$Entry"
}
if (!(Test-Path $Resources)) {
    throw "Resources directory not found: $Resources"
}

Write-Host "Project : ."
Write-Host "Python  : $PythonExe"
Write-Host "Target  : $BuildDir"
Write-Host "Mode    : $(if ($OneFile) { 'onefile' } else { 'onedir' })"

if (!$SkipInstall) {
    Invoke-Step "Install build/runtime dependencies" {
        & $PythonExe -m pip install --upgrade pip
        & $PythonExe -m pip install -r $RuntimeReq
        & $PythonExe -m pip install -r $BuildReq
    }
}

Invoke-Step "Check PyInstaller" {
    & $PythonExe -m PyInstaller --version
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller is not installed in $PythonExe. Run without -SkipInstall, or install packaging\requirements-build.txt."
    }
}

$PyiArgs = @(
    "--noconfirm",
    "--windowed",
    "--name", $BuildName,
    "--distpath", "dist",
    "--workpath", "build",
    "--specpath", "build",
    "--paths", ".",
    "--add-data", $AddData,
    "--collect-all", "PyQt6",
    "--collect-all", "pyqtgraph",
    "--collect-submodules", "serial",
    "--collect-submodules", "transport.virtual_engine",
    "--hidden-import", "serial.tools.list_ports"
)

if ($Clean) {
    $PyiArgs += "--clean"
}
if ($OneFile) {
    $PyiArgs += "--onefile"
} else {
    $PyiArgs += "--onedir"
}
if (Test-Path $IconIco) {
    $PyiArgs += @("--icon", $IconIco)
}

$PyiArgs += $Entry

Invoke-Step "Build EXE" {
    & $PythonExe -m PyInstaller @PyiArgs
}

$Output = $null
if ($OneFile) {
    $Output = "dist\$BuildName.exe"
} else {
    $DistRoot = "dist"
    $SourceDir = Join-Path $DistRoot $BuildName
    $TargetDir = Join-Path $DistRoot $BuildDir
    if (Test-Path $TargetDir) {
        Remove-Item $TargetDir -Recurse -Force
    }
    if (Test-Path $SourceDir) {
        Move-Item $SourceDir $TargetDir
    } else {
        throw "Build output directory not found: $SourceDir"
    }
    $Output = Join-Path $TargetDir "$BuildName.exe"
}

Write-Host ""
Write-Host "Build complete:" -ForegroundColor Green
Write-Host $Output
Write-Host ""
Write-Host "Run:" -ForegroundColor Green
Write-Host "  `"$Output`""
