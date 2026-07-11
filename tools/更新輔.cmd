@echo off
setlocal
chcp 65001 >nul
set "ROOT=%~dp0"
set "BASE=https://raw.githubusercontent.com/limaple0324/FLASH/release/latest"
set "LOG=%ROOT%更新紀錄.txt"
set "TMP=%ROOT%下載暫存"

echo [%date% %time%] 開始更新輔 > "%LOG%"
echo 更新位置：%ROOT%>> "%LOG%"
echo 正在更新輔...

if not exist "%TMP%" mkdir "%TMP%"
del /q "%TMP%\*" >nul 2>nul

tasklist /FI "IMAGENAME eq FLASH.exe" | find /I "FLASH.exe" >nul
if not errorlevel 1 (
  echo FLASH 正在執行，請先關閉後再更新。>> "%LOG%"
  echo FLASH 正在執行，請先關閉後再更新。
  goto failed
)

call :download FLASH.exe
if errorlevel 1 goto failed
call :download SHA256SUMS.txt
if errorlevel 1 goto failed
call :download BUILD_INFO.txt
if errorlevel 1 goto failed
call :download verify_windows_release.ps1
if errorlevel 1 goto failed

echo 正在覆蓋檔案...
copy /y "%TMP%\FLASH.exe" "%ROOT%FLASH.exe" >> "%LOG%" 2>&1
if errorlevel 1 goto failed
copy /y "%TMP%\SHA256SUMS.txt" "%ROOT%SHA256SUMS.txt" >> "%LOG%" 2>&1
if errorlevel 1 goto failed
copy /y "%TMP%\BUILD_INFO.txt" "%ROOT%BUILD_INFO.txt" >> "%LOG%" 2>&1
if errorlevel 1 goto failed
copy /y "%TMP%\verify_windows_release.ps1" "%ROOT%verify_windows_release.ps1" >> "%LOG%" 2>&1
if errorlevel 1 goto failed

echo [%date% %time%] 更新完成。>> "%LOG%"
type "%ROOT%BUILD_INFO.txt" >> "%LOG%"
echo.
echo 更新完成。
echo 請打開同資料夾內的 FLASH。

goto done

:failed
echo.
echo 更新輔執行失敗。
echo 請截圖這個畫面，或查看同資料夾的 更新紀錄.txt。
echo [%date% %time%] 更新失敗。>> "%LOG%"

:done
echo.
pause
endlocal
exit /b

:download
echo 下載：%1
echo 下載：%1>> "%LOG%"
curl.exe -L --fail --output "%TMP%\%1" "%BASE%/%1?t=%random%%random%" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo 下載失敗：%1>> "%LOG%"
  exit /b 1
)
if not exist "%TMP%\%1" (
  echo 找不到下載檔案：%1>> "%LOG%"
  exit /b 1
)
exit /b 0
