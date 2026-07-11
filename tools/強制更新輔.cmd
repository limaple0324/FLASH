@echo off
setlocal
chcp 65001 >nul
set "ROOT=%~dp0"
set "PS1=%TEMP%\flash_force_update_%RANDOM%%RANDOM%.ps1"

> "%PS1%" (
echo $ErrorActionPreference = "Stop"
echo [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
echo $Repo = "limaple0324/FLASH"
echo $ReleaseBranch = "release/latest"
echo $Root = "%ROOT%"
echo $Root = [System.IO.Path]::GetFullPath($Root)
echo $Desktop = [Environment]::GetFolderPath("Desktop")
echo $DownloadDir = Join-Path $Root "下載暫存"
echo $LogPath = Join-Path $Root "更新紀錄.txt"
echo $ShortcutPath = Join-Path $Desktop "輔.lnk"
echo function Log([string]$Message^) {
echo     $line = "[" + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"^) + "] " + $Message
echo     Write-Host $line
echo     Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
echo }
echo function NeedFile([string]$Path^) {
echo     if (-not (Test-Path -LiteralPath $Path -PathType Leaf^)^) { throw "缺少必要檔案：" + $Path }
echo }
echo try {
echo     New-Item -ItemType Directory -Force -Path $Root, $DownloadDir ^| Out-Null
echo     if (-not (Test-Path -LiteralPath $LogPath -PathType Leaf^)^) { New-Item -ItemType File -Force -Path $LogPath ^| Out-Null }
echo     Log "開始強制更新輔。"
echo     Log "更新位置：$Root"
echo     $targetExe = Join-Path $Root "FLASH.exe"
echo     $running = Get-Process ^| Where-Object { $_.ProcessName -eq "FLASH" }
echo     if ($running^) {
echo         Log "偵測到 FLASH 正在執行，請先關閉後再更新。"
echo         throw "FLASH 正在執行，無法覆蓋。請關閉 FLASH 後再按一次。"
echo     }
echo     Get-ChildItem -LiteralPath $DownloadDir -Force -ErrorAction SilentlyContinue ^| Remove-Item -Recurse -Force
echo     $base = "https://raw.githubusercontent.com/$Repo/$ReleaseBranch"
echo     $stamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
echo     $files = @("FLASH.exe", "SHA256SUMS.txt", "BUILD_INFO.txt", "verify_windows_release.ps1"^)
echo     foreach ($file in $files^) {
echo         $url = "$base/$file" + "?t=$stamp"
echo         $out = Join-Path $DownloadDir $file
echo         Log "下載：$file"
echo         Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing
echo     }
echo     $downloadExe = Join-Path $DownloadDir "FLASH.exe"
echo     $hashFile = Join-Path $DownloadDir "SHA256SUMS.txt"
echo     $infoFile = Join-Path $DownloadDir "BUILD_INFO.txt"
echo     NeedFile $downloadExe
echo     NeedFile $hashFile
echo     NeedFile $infoFile
echo     Log "核對 FLASH.exe。"
echo     $expectedLine = (Get-Content -LiteralPath $hashFile -Raw^).Trim()
echo     if ($expectedLine -notmatch '^([0-9a-fA-F]{64})\s+FLASH\.exe$'^) { throw "SHA256SUMS.txt 格式不正確。" }
echo     $expected = $Matches[1].ToLowerInvariant()
echo     $actual = (Get-FileHash -LiteralPath $downloadExe -Algorithm SHA256^).Hash.ToLowerInvariant()
echo     if ($expected -ne $actual^) { throw "FLASH.exe 核對失敗。" }
echo     Log "核對通過，開始覆蓋目前資料夾。"
echo     foreach ($file in $files^) {
echo         Copy-Item -LiteralPath (Join-Path $DownloadDir $file^) -Destination (Join-Path $Root $file^) -Force
echo     }
echo     NeedFile $targetExe
echo     Log "建立桌面捷徑：輔。"
echo     $shell = New-Object -ComObject WScript.Shell
echo     $shortcut = $shell.CreateShortcut($ShortcutPath^)
echo     $shortcut.TargetPath = $targetExe
echo     $shortcut.WorkingDirectory = $Root
echo     $shortcut.IconLocation = "$targetExe,0"
echo     $shortcut.Save()
echo     $buildInfo = Get-Content -LiteralPath (Join-Path $Root "BUILD_INFO.txt"^) -Raw
echo     Log "更新完成。"
echo     foreach ($line in ($buildInfo -split "`r?`n"^)^) { if ($line.Trim()^) { Log $line } }
echo     Write-Host ""
echo     Write-Host "更新完成。請打開桌面的「輔」捷徑。" -ForegroundColor Green
echo } catch {
echo     Log ("更新失敗：" + $_.Exception.Message^)
echo     Write-Host ""
echo     Write-Host ("更新失敗：" + $_.Exception.Message^) -ForegroundColor Red
echo     Write-Host ("請把這份內容貼給我：" + $LogPath^) -ForegroundColor Yellow
echo }
echo Write-Host ""
echo Write-Host "按任意鍵關閉。"
echo $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown"^)
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
del "%PS1%" >nul 2>nul
endlocal
