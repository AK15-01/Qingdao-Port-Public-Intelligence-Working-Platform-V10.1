@echo off
setlocal
chcp 65001 >nul

set "PROJECT_ROOT=%~dp0"
set "PROJECT_PYTHON=%PROJECT_ROOT%.venv\Scripts\python.exe"
set "LAUNCHER=%PROJECT_ROOT%scripts\launch_portscope.py"
set "LOG_DIR=%PROJECT_ROOT%output\environment_repair"
set "PYTHONUTF8=1"
cd /d "%PROJECT_ROOT%"

if not exist "%PROJECT_PYTHON%" (
    echo [PortScope] The project environment does not exist.
    echo [PortScope] Repair will not create or replace it. Run setup_environment.bat.
    exit /b 2
)

echo [PortScope] Running read-only diagnostics first:
"%PROJECT_PYTHON%" "%LAUNCHER%" --diagnose

for /f "delims=" %%M in ('""%PROJECT_PYTHON%" "%LAUNCHER%" --list-missing-core"') do set "MISSING_PACKAGES=%%M"
if not defined MISSING_PACKAGES (
    echo [PortScope] Core dependencies are complete. pip was not run.
    exit /b 0
)

echo.
echo [PortScope] Missing core packages: %MISSING_PACKAGES%
echo [PortScope] Only these packages will be installed; the full lock file will not be reinstalled.
set /p "CONFIRM=Type REPAIR to install; any other input cancels: "
if /I not "%CONFIRM%"=="REPAIR" (
    echo [PortScope] Cancelled. pip was not run.
    exit /b 3
)

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "LOG_FILE=%LOG_DIR%\repair-latest.log"
"%PROJECT_PYTHON%" -m pip install --disable-pip-version-check %MISSING_PACKAGES% > "%LOG_FILE%" 2>&1
set "REPAIR_EXIT=%ERRORLEVEL%"
type "%LOG_FILE%"

if not "%REPAIR_EXIT%"=="0" (
    echo [PortScope] Repair failed with pip exit code %REPAIR_EXIT%.
    echo [PortScope] Full output: "%LOG_FILE%"
    exit /b %REPAIR_EXIT%
)

echo [PortScope] Repair completed. Log: "%LOG_FILE%"
"%PROJECT_PYTHON%" "%LAUNCHER%" --check-only
exit /b %ERRORLEVEL%
