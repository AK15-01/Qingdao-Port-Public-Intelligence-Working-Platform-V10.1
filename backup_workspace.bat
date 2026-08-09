@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" backup_workspace.py
) else (
  python backup_workspace.py
)
if errorlevel 1 (
  echo [PortScope] 工作台备份失败。
  pause
  exit /b 1
)
echo [PortScope] 已备份数据库、原始HTML、工作空间报告和配置；未包含.venv或.env。
pause
