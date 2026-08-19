@echo off
setlocal
set "STATE=%LOCALAPPDATA%\輔\Devspace"
if not exist "%STATE%" mkdir "%STATE%"
type nul > "%STATE%\stop.request"
exit /b 0
