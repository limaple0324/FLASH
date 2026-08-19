@echo off
setlocal
cd /d "%~dp0\..\.."
where pyw >nul 2>nul
if %errorlevel%==0 (
  start "" pyw -3 "%~dp0bridge.py" --repo "%CD%"
  exit /b 0
)
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw "%~dp0bridge.py" --repo "%CD%"
  exit /b 0
)
echo 找不到可用的 Python 背景執行器。
pause
exit /b 1
