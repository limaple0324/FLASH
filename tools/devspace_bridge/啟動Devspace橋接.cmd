@echo off
setlocal
cd /d "%~dp0\..\.."
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0bridge_runner.py" --repo "%CD%"
  exit /b %errorlevel%
)
where python >nul 2>nul
if %errorlevel%==0 (
  python "%~dp0bridge_runner.py" --repo "%CD%"
  exit /b %errorlevel%
)
echo ERROR NOPYTHON
pause
exit /b 1
