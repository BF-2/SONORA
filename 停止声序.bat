@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-sonora.ps1" %*
set "SONORA_STOP_EXIT=%errorlevel%"
echo.
pause
exit /b %SONORA_STOP_EXIT%
