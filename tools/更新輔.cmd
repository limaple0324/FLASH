@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "& { $ErrorActionPreference = 'Stop'; $root = $args[0]; $url = 'https://raw.githubusercontent.com/limaple0324/FLASH/feature/home-ui/tools/update_flash_core.ps1?t=' + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); $script = Join-Path $env:TEMP 'update_flash_core.ps1'; Invoke-WebRequest -Uri $url -OutFile $script -UseBasicParsing; & $script -Root $root }" "%~dp0"
endlocal
