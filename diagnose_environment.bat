@echo off
setlocal
chcp 65001 >nul

set "PROJECT_ROOT=%~dp0"
set "PROJECT_PYTHON=%PROJECT_ROOT%.venv\Scripts\python.exe"
set "DIAGNOSTIC=%PROJECT_ROOT%scripts\diagnose_environment.py"
set "PYTHONUTF8=1"
cd /d "%PROJECT_ROOT%"

if not exist "%PROJECT_PYTHON%" (
    echo [PortScope] Project Python was not found: "%PROJECT_PYTHON%"
    echo [PortScope] Run setup_environment.bat manually.
    set "EXIT_CODE=2"
) else (
    "%PROJECT_PYTHON%" "%DIAGNOSTIC%"
    set "EXIT_CODE=%ERRORLEVEL%"
)

if defined PORTSCOPE_NO_PAUSE exit /b %EXIT_CODE%
echo.
pause
exit /b %EXIT_CODE%
