param(
    [string]$Python = "",
    [string]$Name = "JointMotorController",
    [string]$Entry = "main.py",
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

$SpecFile = Join-Path $ScriptDir "JointMotorController.spec"

# ---- 功能裁剪: packaging/build_config.json -> build/staging/resources ----
# 配置存在时生成裁剪版 feature_flags.json 的 staging 资源目录并注入打包;
# 缺失时保持全功能构建, 与开发运行 main.py 用的 resources/feature_flags.json 互不影响。
$BuildConfig = Join-Path $ScriptDir "build_config.json"
$StagingRes  = Join-Path $ProjectRoot "build\staging\resources"
Remove-Item Env:JM_STAGING_RES -ErrorAction SilentlyContinue
if (Test-Path $BuildConfig) {
    Invoke-Step "Prepare feature-trim staging" {
        & $PythonExe (Join-Path $ScriptDir "prepare_build.py") `
            --config $BuildConfig --out (Split-Path -Parent $StagingRes)
    }
    $env:JM_STAGING_RES = $StagingRes      # onefile/spec 路径消费
    $AddData = "$StagingRes;resources"     # onedir 路径消费
    Write-Host "Features : trimmed by build_config.json" -ForegroundColor Yellow
} else {
    Write-Host "Features : build_config.json not found, full-feature build" -ForegroundColor Yellow
}

if ($OneFile -and (Test-Path $SpecFile)) {
    # 单文件构建走 spec: 内含 hiddenimports/excludes, 并对 binaries/datas 二次过滤
    # (剔除未用 Qt DLL/插件、精简翻译), 产物体积最小。
    $SpecArgs = @($SpecFile, "--noconfirm", "--distpath", "dist", "--workpath", "build")
    if ($Clean) { $SpecArgs += "--clean" }
    Invoke-Step "Build EXE (spec)" {
        & $PythonExe -m PyInstaller @SpecArgs
    }
} else {
    $PyiArgs = @(
        "--noconfirm",
        "--windowed",
        "--name", $BuildName,
        "--distpath", "dist",
        "--workpath", "build",
        "--specpath", "build",
        "--paths", ".",
        "--add-data", $AddData,
        "--collect-submodules", "pyqtgraph",
        "--collect-submodules", "serial",
        "--collect-submodules", "transport.virtual_engine",
        "--collect-submodules", "can.interfaces.pcan",
        "--hidden-import", "serial.tools.list_ports",
        "--hidden-import", "PyQt6.sip",
        "--hidden-import", "PyQt6.QtOpenGLWidgets"
    )
    if ($Clean) { $PyiArgs += "--clean" }
    if ($OneFile) { $PyiArgs += "--onefile" } else { $PyiArgs += "--onedir" }
    if (Test-Path $IconIco) { $PyiArgs += @("--icon", $IconIco) }
    $PyiArgs += $Entry
    Invoke-Step "Build EXE" {
        & $PythonExe -m PyInstaller @PyiArgs
    }
}

$Output = $null
if ($OneFile) {
    $Output = "dist\$BuildName.exe"
} else {
    $DistRoot = "dist"
    $SourceDir = Join-Path $DistRoot $BuildName
    $TargetDir = Join-Path $DistRoot $BuildDir
    if (Test-Path $TargetDir) {
        $TimeTag = Get-Date -Format "HHmmss"
        $TargetDir = Join-Path $DistRoot "${BuildDir}_$TimeTag"
        $Seq = 1
        while (Test-Path $TargetDir) {
            $TargetDir = Join-Path $DistRoot ("{0}_{1:00}" -f "${BuildDir}_$TimeTag", $Seq)
            $Seq++
        }
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
