@echo off
setlocal
chcp 65001 >nul

set "PROJECT_ROOT=%~dp0"
set "VENV_DIR=%PROJECT_ROOT%.venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "LOCK_FILE=%PROJECT_ROOT%requirements-lock.txt"
set "PYTHONUTF8=1"
cd /d "%PROJECT_ROOT%"

echo [PortScope] First-install target: "%VENV_DIR%"
echo [PortScope] Required Python version: 3.9

if exist "%VENV_PYTHON%" (
    echo [PortScope] The project environment already exists.
    echo [PortScope] This script will not overwrite, upgrade, or rebuild it.
    echo [PortScope] Run repair_environment.bat only if a core module is missing.
    exit /b 0
)

where py >nul 2>&1
if errorlevel 1 (
    echo [PortScope] Windows Python Launcher py.exe was not found.
    echo [PortScope] Install Python 3.9 and verify that py -3.9 works.
    exit /b 2
)

py -3.9 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 9) else 1)"
if errorlevel 1 (
    echo [PortScope] Python 3.9 was not found. No other Python version will be used.
    exit /b 3
)

echo [PortScope] A new environment will be created only because .venv does not exist.
set /p "CONFIRM=Type YES to continue; any other input cancels: "
if /I not "%CONFIRM%"=="YES" (
    echo [PortScope] Cancelled. No environment was changed.
    exit /b 4
)

py -3.9 -m venv "%VENV_DIR%"
if errorlevel 1 (
    echo [PortScope] Environment creation failed. The real Python error is shown above.
    exit /b 5
)

if not exist "%LOCK_FILE%" (
    echo [PortScope] requirements-lock.txt is missing. Installation stopped.
    exit /b 6
)

echo [PortScope] Running the user-requested first dependency installation.
"%VENV_PYTHON%" -m pip install --disable-pip-version-check -r "%LOCK_FILE%"
set "INSTALL_EXIT=%ERRORLEVEL%"
if not "%INSTALL_EXIT%"=="0" (
    echo [PortScope] Dependency installation failed with pip exit code %INSTALL_EXIT%.
    echo [PortScope] The output above is the original pip error.
    exit /b %INSTALL_EXIT%
)

set "PORTSCOPE_LOCK_FILE=%LOCK_FILE%"
set "PORTSCOPE_HASH_FILE=%VENV_DIR%\.portscope-requirements.sha256"
"%VENV_PYTHON%" -c "import hashlib,os,pathlib; src=pathlib.Path(os.environ['PORTSCOPE_LOCK_FILE']); dst=pathlib.Path(os.environ['PORTSCOPE_HASH_FILE']); dst.write_text(hashlib.sha256(src.read_bytes()).hexdigest()+'\\n',encoding='ascii')"
if errorlevel 1 (
    echo [PortScope] Packages were installed, but the dependency hash could not be written.
    echo [PortScope] The environment was retained. Run diagnose_environment.bat.
    exit /b 7
)

"%VENV_PYTHON%" "%PROJECT_ROOT%scripts\launch_portscope.py" --check-only
exit /b %ERRORLEVEL%
