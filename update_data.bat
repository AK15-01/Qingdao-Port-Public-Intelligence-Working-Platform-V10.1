@echo off
chcp 65001 >nul
cd /d "%~dp0"
call ensure_environment.bat || goto :error
.venv\Scripts\python.exe update_data.py
set EXIT_CODE=%ERRORLEVEL%
pause
exit /b %EXIT_CODE%

:error
echo [PortScope] Dependency installation failed. Check the network and retry.
pause
exit /b 1
