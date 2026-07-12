# 輔更新核心
# 用途：由「更新輔.cmd」呼叫，只更新目前資料夾內的最新成品。
# 桌面捷徑由玩家自行保留；更新程序不得更改名稱或圖示。

$ErrorActionPreference = "Stop"

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Repo = "limaple0324/FLASH"
$ReleaseBranch = "release/latest"
$SystemDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallDir = Split-Path -Parent $SystemDir
$DownloadDir = Join-Path $SystemDir "下載暫存"
$ReleaseDir = $InstallDir
$ExePath = Join-Path $ReleaseDir "FLASH.exe"
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

function Download-File([string]$Url, [string]$Target, [string]$Name) {
    Write-Step "下載：$Name"
    Invoke-WebRequest -Uri $Url -OutFile $Target
    Require-File $Target
}

function Copy-RequiredFile([string]$Source, [string]$Target, [string]$Name) {
    Require-File $Source
    Write-Step "更新：$Name"
    Copy-Item -LiteralPath $Source -Destination $Target -Force
    Require-File $Target
}

New-Item -ItemType Directory -Force -Path $InstallDir, $DownloadDir | Out-Null
if (-not (Test-Path -LiteralPath $LogPath -PathType Leaf)) {
    New-Item -ItemType File -Force -Path $LogPath | Out-Null
}

try {
    Write-Step "開始更新輔。"

    $running = Get-Process | Where-Object { $_.ProcessName -eq "FLASH" }
    if ($running) {
        throw "輔正在執行，請先關閉後再更新。"
    }

    $baseUrl = "https://raw.githubusercontent.com/$Repo/$ReleaseBranch"
    $files = @(
        @{ Remote = "FLASH.exe"; Local = Join-Path $InstallDir "FLASH.exe"; Name = "FLASH.exe" },
        @{ Remote = "更新輔.cmd"; Local = Join-Path $InstallDir "更新輔.cmd"; Name = "更新輔.cmd" },
        @{ Remote = "輔系統/SHA256SUMS.txt"; Local = Join-Path $SystemDir "SHA256SUMS.txt"; Name = "SHA256SUMS.txt" },
        @{ Remote = "輔系統/BUILD_INFO.txt"; Local = Join-Path $SystemDir "BUILD_INFO.txt"; Name = "BUILD_INFO.txt" },
        @{ Remote = "輔系統/verify_windows_release.ps1"; Local = Join-Path $SystemDir "verify_windows_release.ps1"; Name = "verify_windows_release.ps1" },
        @{ Remote = "輔系統/輔更新核心.ps1"; Local = Join-Path $SystemDir "輔更新核心.ps1"; Name = "輔更新核心.ps1" },
        @{ Remote = "輔系統/檢查輔同步狀態.cmd"; Local = Join-Path $SystemDir "檢查輔同步狀態.cmd"; Name = "檢查輔同步狀態.cmd" },
        @{ Remote = "輔系統/檢查輔同步狀態.ps1"; Local = Join-Path $SystemDir "檢查輔同步狀態.ps1"; Name = "檢查輔同步狀態.ps1" }
    )
    $cacheBreaker = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()

    if (Test-Path -LiteralPath $DownloadDir) {
        Get-ChildItem -LiteralPath $DownloadDir -Force | Remove-Item -Recurse -Force
    }

    foreach ($file in $files) {
        $source = "$baseUrl/$($file.Remote)`?t=$cacheBreaker"
        $target = Join-Path $DownloadDir $file.Name
        Download-File -Url $source -Target $target -Name $file.Name
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
    $selfUpdateNames = @("更新輔.cmd", "輔更新核心.ps1")
    $regularFiles = $files | Where-Object { $selfUpdateNames -notcontains $_.Name }

    foreach ($file in $regularFiles) {
        Copy-RequiredFile `
            -Source (Join-Path $DownloadDir $file.Name) `
            -Target $file.Local `
            -Name $file.Name
    }

    $buildInfo = Get-Content -LiteralPath (Join-Path $SystemDir "BUILD_INFO.txt") -Raw
    Write-Step "更新完成。目前資料夾：$ReleaseDir"
    Write-Step "已保留原本桌面捷徑的名稱與圖示。"
    Write-Step "最新建置資訊："
    foreach ($line in ($buildInfo -split "`r?`n")) {
        if ($line.Trim()) {
            Write-Step $line
        }
    }

    Write-Step "更新工具本身。"
    foreach ($file in ($files | Where-Object { $selfUpdateNames -contains $_.Name })) {
        Copy-RequiredFile `
            -Source (Join-Path $DownloadDir $file.Name) `
            -Target $file.Local `
            -Name $file.Name
    }

    Write-Host ""
    Write-Host "更新完成，可以直接打開原本桌面的「輔 V0.2」。" -ForegroundColor Green
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
