@echo off
setlocal
chcp 65001 >nul
set "INSTALLER=%~dp0輔系統\安裝輔.ps1"
if not exist "%INSTALLER%" (
  echo 找不到安裝腳本：%INSTALLER%
  echo.
  echo 按任意鍵關閉。
  pause >nul
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%INSTALLER%" -SourceDirectory "%~dp0."
set "EXITCODE=%ERRORLEVEL%"
if not "%EXITCODE%"=="0" (
  echo.
  echo 安裝輔沒有正常完成。錯誤碼：%EXITCODE%
)
echo.
echo 按任意鍵關閉。
pause >nul
endlocal & exit /b %EXITCODE%
