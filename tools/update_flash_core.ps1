param(
    [Parameter(Mandatory = $true)]
    [string]$Root
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Repo = "limaple0324/FLASH"
$ReleaseBranch = "release/latest"
$Root = [System.IO.Path]::GetFullPath($Root)
$Desktop = [Environment]::GetFolderPath("Desktop")
$DownloadDir = Join-Path $Root "下載暫存"
$LogPath = Join-Path $Root "更新紀錄.txt"
$ShortcutPath = Join-Path $Desktop "輔.lnk"

function Log([string]$Message) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Message"
    Write-Host $line
    Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
}

function NeedFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "缺少必要檔案：$Path"
    }
}

try {
    New-Item -ItemType Directory -Force -Path $Root, $DownloadDir | Out-Null
    if (-not (Test-Path -LiteralPath $LogPath -PathType Leaf)) {
        New-Item -ItemType File -Force -Path $LogPath | Out-Null
    }

    Log "開始更新輔。"
    Log "更新位置：$Root"

    $targetExe = Join-Path $Root "FLASH.exe"
    $running = Get-Process | Where-Object { $_.ProcessName -eq "FLASH" }
    if ($running) {
        Log "偵測到 FLASH 正在執行，請先關閉後再更新。"
        throw "FLASH 正在執行，無法覆蓋。請關閉 FLASH 後再按一次。"
    }

    Get-ChildItem -LiteralPath $DownloadDir -Force -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force

    $base = "https://raw.githubusercontent.com/$Repo/$ReleaseBranch"
    $stamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $files = @("FLASH.exe", "SHA256SUMS.txt", "BUILD_INFO.txt", "verify_windows_release.ps1")

    foreach ($file in $files) {
        $url = "$base/$file" + "?t=$stamp"
        $out = Join-Path $DownloadDir $file
        Log "下載：$file"
        Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing
    }

    $downloadExe = Join-Path $DownloadDir "FLASH.exe"
    $hashFile = Join-Path $DownloadDir "SHA256SUMS.txt"
    $infoFile = Join-Path $DownloadDir "BUILD_INFO.txt"

    NeedFile $downloadExe
    NeedFile $hashFile
    NeedFile $infoFile

    Log "核對 FLASH.exe。"
    $expectedLine = (Get-Content -LiteralPath $hashFile -Raw).Trim()
    if ($expectedLine -notmatch '^([0-9a-fA-F]{64})\s+FLASH\.exe$') {
        throw "SHA256SUMS.txt 格式不正確。"
    }

    $expected = $Matches[1].ToLowerInvariant()
    $actual = (Get-FileHash -LiteralPath $downloadExe -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($expected -ne $actual) {
        throw "FLASH.exe 核對失敗。"
    }

    Log "核對通過，開始覆蓋目前資料夾。"
    foreach ($file in $files) {
        Copy-Item -LiteralPath (Join-Path $DownloadDir $file) -Destination (Join-Path $Root $file) -Force
    }

    NeedFile $targetExe

    Log "建立桌面捷徑：輔。"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($ShortcutPath)
    $shortcut.TargetPath = $targetExe
    $shortcut.WorkingDirectory = $Root
    $shortcut.IconLocation = "$targetExe,0"
    $shortcut.Save()

    $buildInfo = Get-Content -LiteralPath (Join-Path $Root "BUILD_INFO.txt") -Raw
    Log "更新完成。"
    foreach ($line in ($buildInfo -split "`r?`n")) {
        if ($line.Trim()) {
            Log $line
        }
    }

    Write-Host ""
    Write-Host "更新完成。請打開桌面的「輔」捷徑。" -ForegroundColor Green
}
catch {
    Log ("更新失敗：" + $_.Exception.Message)
    Write-Host ""
    Write-Host ("更新失敗：" + $_.Exception.Message) -ForegroundColor Red
    Write-Host ("請把這份內容貼給我：" + $LogPath) -ForegroundColor Yellow
}

Write-Host ""
Write-Host "按任意鍵關閉。"
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
