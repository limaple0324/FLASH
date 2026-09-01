@echo off
setlocal
cd /d "%~dp0\..\.."
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m tools.devspace_bridge.line_bridge_main --stop
  exit /b %errorlevel%
)
where python >nul 2>nul
if %errorlevel%==0 (
  python -m tools.devspace_bridge.line_bridge_main --stop
  exit /b %errorlevel%
)
echo 找不到 Python。
pause
exit /b 1
