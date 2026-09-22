@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "wecom-voice-blaster-gui.exe" (
    start "" "%~dp0wecom-voice-blaster-gui.exe"
    exit /b 0
)
echo 未找到 wecom-voice-blaster-gui.exe
echo 请把本文件放在打包目录里，与 exe 在同一层。
pause
