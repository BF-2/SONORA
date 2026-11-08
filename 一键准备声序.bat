@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\prepare-sonora.ps1" %*
if errorlevel 1 echo 准备未完成，重新运行可继续下载。
pause
