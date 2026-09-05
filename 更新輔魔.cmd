@echo off
chcp 65001 >nul
setlocal
set "ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%??????.ps1" -InstallDirectory "%ROOT%"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
exit /b %RC%
