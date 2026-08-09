@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"

set "PORTSCOPE_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PORTSCOPE_PYTHON%" set "PORTSCOPE_PYTHON=python"

"%PORTSCOPE_PYTHON%" "%~dp0diagnose_deepseek.py"
set "PORTSCOPE_EXIT_CODE=%ERRORLEVEL%"
if not "%PORTSCOPE_EXIT_CODE%"=="0" echo.
if not "%PORTSCOPE_EXIT_CODE%"=="0" echo Diagnosis did not pass. Review the stage, HTTP status, and redacted error above.
exit /b %PORTSCOPE_EXIT_CODE%
