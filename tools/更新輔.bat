@echo off
chcp 65001 >nul
title 更新輔
cd /d "%~dp0"

echo 更新輔已啟動。
echo 如果你看得到這個畫面，代表工具有成功開始執行。
echo.

set "BASE=https://raw.githubusercontent.com/limaple0324/FLASH/release/latest"
set "LOG=%~dp0更新紀錄.txt"
set "STAMP=%RANDOM%%RANDOM%"

echo [%date% %time%] 開始更新輔 > "%LOG%"
echo 更新位置：%~dp0>> "%LOG%"

echo 下載 FLASH.exe...
curl.exe -L --fail -o "FLASH.exe.new" "%BASE%/FLASH.exe?t=%STAMP%" >> "%LOG%" 2>&1
if errorlevel 1 goto fail

echo 下載 BUILD_INFO...
curl.exe -L --fail -o "BUILD_INFO.new" "%BASE%/BUILD_INFO.txt?t=%STAMP%" >> "%LOG%" 2>&1
if errorlevel 1 goto fail

echo 下載 SHA256SUMS...
curl.exe -L --fail -o "SHA256SUMS.new" "%BASE%/SHA256SUMS.txt?t=%STAMP%" >> "%LOG%" 2>&1
if errorlevel 1 goto fail

echo 下載 verify_windows_release...
curl.exe -L --fail -o "verify_windows_release.new" "%BASE%/verify_windows_release.ps1?t=%STAMP%" >> "%LOG%" 2>&1
if errorlevel 1 goto fail

echo 正在覆蓋檔案...
copy /y "FLASH.exe.new" "FLASH.exe" >> "%LOG%" 2>&1
if errorlevel 1 goto fail
copy /y "BUILD_INFO.new" "BUILD_INFO.txt" >> "%LOG%" 2>&1
if errorlevel 1 goto fail
copy /y "SHA256SUMS.new" "SHA256SUMS.txt" >> "%LOG%" 2>&1
if errorlevel 1 goto fail
copy /y "verify_windows_release.new" "verify_windows_release.ps1" >> "%LOG%" 2>&1
if errorlevel 1 goto fail

del /q "FLASH.exe.new" "BUILD_INFO.new" "SHA256SUMS.new" "verify_windows_release.new" >nul 2>nul

echo [%date% %time%] 更新完成 >> "%LOG%"
type "BUILD_INFO.txt" >> "%LOG%"
echo.
echo 更新完成。
echo 請確認 BUILD_INFO 和 FLASH 的修改時間有變新。
echo.
pause
exit /b 0

:fail
echo [%date% %time%] 更新失敗 >> "%LOG%"
echo.
echo 更新失敗。
echo 請把這個畫面截圖，或貼同資料夾的 更新紀錄.txt。
echo.
pause
exit /b 1
