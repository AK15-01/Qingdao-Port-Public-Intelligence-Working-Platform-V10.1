@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo [PortScope] 未找到项目虚拟环境，请先运行 run.bat 完成安装。
    pause
    exit /b 2
)
.venv\Scripts\python.exe commercial_preflight.py
set EXIT_CODE=%ERRORLEVEL%
echo.
if not "%EXIT_CODE%"=="0" (
    echo [PortScope] 商用部署前检查存在阻断项，请先修复。
) else (
    echo [PortScope] 未发现代码和本机环境阻断项；外部许可及客户验收仍需人工签署。
)
pause
exit /b %EXIT_CODE%
