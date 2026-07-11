@echo off
setlocal
chcp 65001 >nul
set "ROOT=%~dp0"
set "CORE=%TEMP%\update_flash_core.ps1"
set "URL=https://raw.githubusercontent.com/limaple0324/FLASH/feature/home-ui/tools/update_flash_core.ps1"

echo 正在準備更新輔...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri '%URL%' -OutFile '%CORE%' -UseBasicParsing"
if errorlevel 1 goto failed

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%CORE%" -Root "%ROOT%"
if errorlevel 1 goto failed

goto done

:failed
echo.
echo 更新輔執行失敗。
echo 請截圖這個畫面，或查看同資料夾的 更新紀錄.txt。

:done
echo.
pause
endlocal
