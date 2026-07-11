@echo off
setlocal
set "SCRIPT=%~dp0檢查輔同步狀態.ps1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%"
endlocal
