@echo off
rem 测试员版打包入口: 以 tester_entry.py 为入口构建 JointMotorTester.exe
rem 用法: build_tester.bat [-OneFile] [-SkipInstall] [-Clean]
rem   详见 build_exe.ps1 (通用构建脚本, 本脚本仅注入 Entry/Name)
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_exe.ps1" -Entry "tester_entry.py" -Name "JointMotorTester" %*
exit /b %ERRORLEVEL%
