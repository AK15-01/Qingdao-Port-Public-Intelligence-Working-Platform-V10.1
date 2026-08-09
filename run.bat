@echo off
setlocal
chcp 65001 >nul

set "PROJECT_ROOT=%~dp0"
set "PROJECT_PYTHON=%PROJECT_ROOT%.venv\Scripts\python.exe"
set "LAUNCHER=%PROJECT_ROOT%scripts\launch_portscope.py"
set "PYTHONUTF8=1"

cd /d "%PROJECT_ROOT%"

if not exist "%PROJECT_PYTHON%" (
    echo [PortScope] Project Python was not found:
    echo   "%PROJECT_PYTHON%"
    echo [PortScope] Nothing was installed or rebuilt.
    echo [PortScope] Run setup_environment.bat, or repair_environment.bat for an existing environment.
    set "EXIT_CODE=2"
    goto :finish
)

if not exist "%LAUNCHER%" (
    echo [PortScope] Launcher was not found:
    echo   "%LAUNCHER%"
    set "EXIT_CODE=3"
    goto :finish
)

"%PROJECT_PYTHON%" "%LAUNCHER%" %*
set "EXIT_CODE=%ERRORLEVEL%"

:finish
if "%EXIT_CODE%"=="" set "EXIT_CODE=1"
if /I "%~1"=="--check-only" exit /b %EXIT_CODE%
if defined PORTSCOPE_NO_PAUSE exit /b %EXIT_CODE%

echo.
echo [PortScope] Streamlit exited with code %EXIT_CODE%.
if not "%EXIT_CODE%"=="0" (
    echo [PortScope] Run diagnose_environment.bat for details.
)
pause
exit /b %EXIT_CODE%
