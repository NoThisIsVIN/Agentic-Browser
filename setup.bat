@echo off
echo Starting setup...
set SCRIPT_DIR=%~dp0
powershell -ExecutionPolicy Bypass -File "%SCRIPT_DIR%setup.ps1"
pause
