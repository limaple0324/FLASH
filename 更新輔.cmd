@echo off
setlocal
chcp 65001 >nul
set "SCRIPT=%~dp0輔系統\輔更新核心.ps1"
set "TEMPUPDATER=%TEMP%\FLASH-SP1-Updater-%RANDOM%-%RANDOM%.ps1"
if not exist "%SCRIPT%" (
  echo 找不到更新腳本：%SCRIPT%
  echo.
  echo 按任意鍵關閉。
  pause
  exit /b 1
)
copy /y "%SCRIPT%" "%TEMPUPDATER%" >nul
if errorlevel 1 (
  echo 無法建立安全的暫存更新程序：%TEMPUPDATER%
  echo.
  echo 按任意鍵關閉。
  pause >nul
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%TEMPUPDATER%" -InstallDirectory "%~dp0"
set "EXITCODE=%ERRORLEVEL%"
del /f /q "%TEMPUPDATER%" >nul 2>nul
if not "%EXITCODE%"=="0" (
  echo.
  echo 更新輔沒有正常完成。錯誤碼：%EXITCODE%
  echo 請截圖這個畫面，或查看同資料夾的 更新紀錄.txt。
)
echo.
echo 按任意鍵關閉。
pause >nul
endlocal & exit /b %EXITCODE%
