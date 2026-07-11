# 更新輔
# 用途：下載最新通過建置的 Windows 成品，核對後更新目前資料夾與桌面捷徑。

$ErrorActionPreference = "Stop"

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Repo = "limaple0324/FLASH"
$ReleaseBranch = "release/latest"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Desktop = [Environment]::GetFolderPath("Desktop")
$InstallDir = $ScriptDir
$DownloadDir = Join-Path $InstallDir "下載暫存"
$ReleaseDir = $InstallDir
$ExePath = Join-Path $ReleaseDir "FLASH.exe"
$DesktopExe = $ExePath
$DesktopShortcut = Join-Path $Desktop "輔.lnk"
$LogPath = Join-Path $InstallDir "更新紀錄.txt"

function Write-Step([string]$Message) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Message"
    Write-Host $line
    Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
}

function Require-File([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "缺少必要檔案：$Path"
    }
}

New-Item -ItemType Directory -Force -Path $InstallDir, $DownloadDir | Out-Null
if (-not (Test-Path -LiteralPath $LogPath -PathType Leaf)) {
    New-Item -ItemType File -Force -Path $LogPath | Out-Null
}

try {
    Write-Step "開始更新輔。"

    $baseUrl = "https://raw.githubusercontent.com/$Repo/$ReleaseBranch"
    $files = @("FLASH.exe", "SHA256SUMS.txt", "BUILD_INFO.txt", "verify_windows_release.ps1")
    $cacheBreaker = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()

    if (Test-Path -LiteralPath $DownloadDir) {
        Get-ChildItem -LiteralPath $DownloadDir -Force | Remove-Item -Recurse -Force
    }

    foreach ($file in $files) {
        $source = "$baseUrl/$file`?t=$cacheBreaker"
        $target = Join-Path $DownloadDir $file
        Write-Step "下載：$file"
        Invoke-WebRequest -Uri $source -OutFile $target
    }

    $downloadedExe = Join-Path $DownloadDir "FLASH.exe"
    $hashPath = Join-Path $DownloadDir "SHA256SUMS.txt"
    $infoPath = Join-Path $DownloadDir "BUILD_INFO.txt"

    Require-File $downloadedExe
    Require-File $hashPath
    Require-File $infoPath

    Write-Step "核對 FLASH.exe。"
    $expectedLine = (Get-Content -LiteralPath $hashPath -Raw).Trim()
    if ($expectedLine -notmatch '^([0-9a-fA-F]{64})\s+FLASH\.exe$') {
        throw "SHA256SUMS.txt 格式不正確。"
    }

    $expectedHash = $Matches[1].ToLowerInvariant()
    $actualHash = (Get-FileHash -LiteralPath $downloadedExe -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $expectedHash) {
        throw "FLASH.exe 核對失敗。"
    }

    Write-Step "核對通過，更新目前資料夾。"
    foreach ($file in $files) {
        Copy-Item -LiteralPath (Join-Path $DownloadDir $file) -Destination (Join-Path $ReleaseDir $file) -Force
    }

    Write-Step "建立桌面捷徑：輔。"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($DesktopShortcut)
    $shortcut.TargetPath = $ExePath
    $shortcut.WorkingDirectory = $ReleaseDir
    $shortcut.IconLocation = "$ExePath,0"
    $shortcut.Save()

    $buildInfo = Get-Content -LiteralPath (Join-Path $ReleaseDir "BUILD_INFO.txt") -Raw
    Write-Step "更新完成。目前資料夾：$ReleaseDir"
    Write-Step "捷徑位置：$DesktopShortcut"
    Write-Step "最新建置資訊："
    foreach ($line in ($buildInfo -split "`r?`n")) {
        if ($line.Trim()) {
            Write-Step $line
        }
    }
    Write-Host ""
    Write-Host "更新完成，可以直接打開桌面的「輔」。" -ForegroundColor Green
}
catch {
    Write-Step "更新失敗：$($_.Exception.Message)"
    Write-Host ""
    Write-Host "更新失敗：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "紀錄位置：$LogPath"
    exit 1
}

Write-Host ""
Write-Host "按任意鍵關閉。"
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
