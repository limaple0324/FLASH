@echo off
setlocal
set "SCRIPT=%~dp0更新輔.ps1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%"
endlocal
