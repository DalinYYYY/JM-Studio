@echo off
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "ROOT=%%~fI"
set "DIST=%ROOT%\dist"
set "BASE=JointMotorController"

if not exist "%DIST%" (
    echo Dist directory not found: "%DIST%"
    exit /b 1
)

set "TARGET="

for /f "delims=" %%D in ('dir /b /ad /o-d "%DIST%\%BASE%*" 2^>nul') do (
    if exist "%DIST%\%%D\%BASE%.exe" (
        set "TARGET=%DIST%\%%D\%BASE%.exe"
        goto :launch
    )
)

for /f "delims=" %%E in ('dir /b /a-d /o-d "%DIST%\%BASE%*.exe" 2^>nul') do (
    set "TARGET=%DIST%\%%E"
    goto :launch
)

if exist "%DIST%\%BASE%\%BASE%.exe" (
    set "TARGET=%DIST%\%BASE%\%BASE%.exe"
    goto :launch
)

echo No runnable build found under "%DIST%".
exit /b 1

:launch
echo Launching "%TARGET%"
start "" "%TARGET%"
exit /b 0
