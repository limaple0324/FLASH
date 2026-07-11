@echo off
setlocal
set "SCRIPT=%~dp0檢查輔同步狀態.ps1"
if not exist "%SCRIPT%" (
  echo 找不到檢查腳本：%SCRIPT%
  echo.
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%"
set "EXITCODE=%ERRORLEVEL%"
if not "%EXITCODE%"=="0" (
  echo.
  echo 檢查沒有正常完成。錯誤碼：%EXITCODE%
  echo 請截圖這個畫面。
)
echo.
echo 按任意鍵關閉。
pause >nul
endlocal
