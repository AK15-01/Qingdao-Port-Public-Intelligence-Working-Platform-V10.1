@echo off
setlocal
chcp 65001 >nul

rem Compatibility checker for update_data.bat and rebuild_index.bat.
rem This script never creates an environment and never installs packages.
set "PROJECT_ROOT=%~dp0"
set "PROJECT_PYTHON=%PROJECT_ROOT%.venv\Scripts\python.exe"
set "LAUNCHER=%PROJECT_ROOT%scripts\launch_portscope.py"
set "PYTHONUTF8=1"

cd /d "%PROJECT_ROOT%"

if not exist "%PROJECT_PYTHON%" (
    echo [PortScope] Project Python was not found: "%PROJECT_PYTHON%"
    echo [PortScope] Run setup_environment.bat or repair_environment.bat manually.
    exit /b 2
)

"%PROJECT_PYTHON%" "%LAUNCHER%" --check-only --skip-port-check
exit /b %ERRORLEVEL%
