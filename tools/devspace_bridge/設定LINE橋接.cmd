@echo off
setlocal
cd /d "%~dp0\..\.."
where pyw >nul 2>nul
if %errorlevel%==0 (
  start "" pyw -3 -m tools.devspace_bridge.line_setup
  exit /b 0
)
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw -m tools.devspace_bridge.line_setup
  exit /b 0
)
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m tools.devspace_bridge.line_setup
  exit /b %errorlevel%
)
echo 找不到 Python。
pause
exit /b 1
