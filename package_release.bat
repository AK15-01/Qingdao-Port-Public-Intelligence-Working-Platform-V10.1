@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" package_release.py
) else (
  python package_release.py
)
if errorlevel 1 (
  echo [PortScope] 交付包生成失败。
  pause
  exit /b 1
)
echo [PortScope] 干净交付包已生成到 dist 目录。
pause
