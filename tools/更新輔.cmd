@echo off
setlocal
chcp 65001 >nul
set "SCRIPT=%~dp0輔系統\輔更新核心.ps1"
if not exist "%SCRIPT%" (
  echo 找不到更新腳本：%SCRIPT%
  echo.
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%"
set "EXITCODE=%ERRORLEVEL%"
if not "%EXITCODE%"=="0" (
  echo.
  echo 更新輔沒有正常完成。錯誤碼：%EXITCODE%
  echo 請截圖這個畫面，或查看同資料夾的 更新紀錄.txt。
)
echo.
echo 按任意鍵關閉。
pause >nul
endlocal
