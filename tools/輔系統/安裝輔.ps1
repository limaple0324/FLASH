# 輔 SP1 完整首次安裝器
# 先驗證整包，再以同磁碟暫存與備份完成可回復安裝。

param(
    [string]$SourceDirectory = "",
    [string]$InstallDirectory = "",
    [string]$DesktopDirectory = "",
    [switch]$NoShortcut,
    [ValidateRange(0, 1)]
    [int]$TestFailAfterSwap = 0
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Get-NormalizedDirectory([string]$Path) {
    return [IO.Path]::GetFullPath($Path).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
}

function Invoke-BundleVerifier([string]$Root, [string]$Description) {
    $verifier = Join-Path $Root "輔系統\verify_windows_release.ps1"
    if (-not (Test-Path -LiteralPath $verifier -PathType Leaf)) {
        throw "$Description 缺少成品驗證器：$verifier"
    }
    Write-Host "$Description：執行 verify_windows_release.ps1 -NoLaunch。"
    & $verifier -NoLaunch
}

function Copy-DirectoryContents([string]$Source, [string]$Destination) {
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    foreach ($item in Get-ChildItem -LiteralPath $Source -Force) {
        Copy-Item `
            -LiteralPath $item.FullName `
            -Destination $Destination `
            -Recurse `
            -Force
    }
}

function New-DesktopShortcut(
    [string]$ShortcutPath,
    [string]$ExecutablePath,
    [string]$WorkingDirectory
) {
    $shell = New-Object -ComObject WScript.Shell
    try {
        $iconPath = Join-Path $WorkingDirectory "sync_plus_icon.ico"
        if (-not (Test-Path -LiteralPath $iconPath -PathType Leaf)) {
            throw "缺少桌面捷徑圖示：$iconPath"
        }
        $shortcut = $shell.CreateShortcut($ShortcutPath)
        $shortcut.TargetPath = $ExecutablePath
        $shortcut.WorkingDirectory = $WorkingDirectory
        $shortcut.IconLocation = "$iconPath,0"
        $shortcut.Description = "輔"
        $shortcut.Save()
    }
    finally {
        if ($shortcut) {
            [Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut) | Out-Null
        }
        [Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) | Out-Null
    }
}

function Get-DesktopShortcutPath([string]$DesktopDirectory) {
    $defaultShortcut = Join-Path $DesktopDirectory "輔.lnk"
    $alternateShortcut = Join-Path $DesktopDirectory "啟動輔.lnk"
    $visibleNameConflict = Join-Path $DesktopDirectory "輔"

    # Preserve the name selected by an earlier installation. When this is the
    # first installation and Explorer already shows a file, directory, or
    # junction named "輔", avoid creating a visually indistinguishable second
    # item while file-name extensions are hidden.
    if (Test-Path -LiteralPath $defaultShortcut -PathType Leaf) {
        return $defaultShortcut
    }
    if (Test-Path -LiteralPath $alternateShortcut -PathType Leaf) {
        return $alternateShortcut
    }
    if (Test-Path -LiteralPath $visibleNameConflict) {
        return $alternateShortcut
    }
    return $defaultShortcut
}

if ([string]::IsNullOrWhiteSpace($SourceDirectory)) {
    $scriptSystemDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $SourceDirectory = Split-Path -Parent $scriptSystemDir
}
if ([string]::IsNullOrWhiteSpace($InstallDirectory)) {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw "找不到 LOCALAPPDATA，無法決定安全的使用者安裝位置。"
    }
    $InstallDirectory = Join-Path $env:LOCALAPPDATA "Programs\輔\SP1"
}
if ([string]::IsNullOrWhiteSpace($DesktopDirectory)) {
    $DesktopDirectory = [Environment]::GetFolderPath("Desktop")
}

$SourceDir = Get-NormalizedDirectory $SourceDirectory
$InstallDir = Get-NormalizedDirectory $InstallDirectory
$DesktopDir = Get-NormalizedDirectory $DesktopDirectory

if (-not (Test-Path -LiteralPath $SourceDir -PathType Container)) {
    throw "找不到完整安裝包：$SourceDir"
}
if ($InstallDir -eq $SourceDir) {
    throw "安裝位置不能與安裝包來源相同。"
}
$installRoot = [IO.Path]::GetPathRoot($InstallDir).TrimEnd(
    [IO.Path]::DirectorySeparatorChar,
    [IO.Path]::AltDirectorySeparatorChar
)
if ($InstallDir.TrimEnd("\") -eq $installRoot) {
    throw "安裝位置不能是磁碟根目錄。"
}
if (-not $NoShortcut -and [string]::IsNullOrWhiteSpace($DesktopDir)) {
    throw "找不到桌面資料夾。"
}

$running = Get-Process -Name "FLASH" -ErrorAction SilentlyContinue
if ($running) {
    throw "輔正在執行，請先關閉後再安裝。"
}

Invoke-BundleVerifier -Root $SourceDir -Description "來源安裝包驗證"

$installParent = Split-Path -Parent $InstallDir
New-Item -ItemType Directory -Force -Path $installParent | Out-Null

$transactionId = [Guid]::NewGuid().ToString("N")
$stageDir = Join-Path $installParent ".輔-SP1-stage-$transactionId"
$backupDir = Join-Path $installParent ".輔-SP1-backup-$transactionId"
$failedDir = Join-Path $installParent ".輔-SP1-failed-$transactionId"
$shortcutPath = Get-DesktopShortcutPath -DesktopDirectory $DesktopDir
$shortcutTemp = Join-Path $DesktopDir ".FLASH-SP1-$transactionId.lnk"
$shortcutBackup = Join-Path $installParent ".FLASH-SP1-shortcut-$transactionId.lnk"

$installSwapped = $false
$installBackedUp = $false
$shortcutInstalled = $false
$shortcutBackedUp = $false
$success = $false

try {
    Write-Host "建立完整安裝暫存：$stageDir"
    Copy-DirectoryContents -Source $SourceDir -Destination $stageDir
    Invoke-BundleVerifier -Root $stageDir -Description "安裝暫存驗證"

    if (Test-Path -LiteralPath $InstallDir) {
        Write-Host "備份原本安裝：$backupDir"
        Move-Item -LiteralPath $InstallDir -Destination $backupDir
        $installBackedUp = $true
    }

    Move-Item -LiteralPath $stageDir -Destination $InstallDir
    $installSwapped = $true
    if ($TestFailAfterSwap -eq 1) {
        throw "測試指定在安裝內容交換後中斷。"
    }

    Invoke-BundleVerifier -Root $InstallDir -Description "安裝後驗證"

    if (-not $NoShortcut) {
        New-Item -ItemType Directory -Force -Path $DesktopDir | Out-Null
        $installedExe = Join-Path $InstallDir "FLASH.exe"
        New-DesktopShortcut `
            -ShortcutPath $shortcutTemp `
            -ExecutablePath $installedExe `
            -WorkingDirectory $InstallDir

        if (Test-Path -LiteralPath $shortcutPath -PathType Leaf) {
            Move-Item -LiteralPath $shortcutPath -Destination $shortcutBackup
            $shortcutBackedUp = $true
        }
        Move-Item -LiteralPath $shortcutTemp -Destination $shortcutPath
        $shortcutInstalled = $true
    }

    @(
        "installed_utc=$([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))"
        "install_directory=$InstallDir"
        "source_directory=$SourceDir"
        "shortcut=$(-not $NoShortcut)"
        "shortcut_path=$(if ($NoShortcut) { '' } else { $shortcutPath })"
    ) | Set-Content (Join-Path $InstallDir "安裝紀錄.txt") -Encoding UTF8

    $success = $true
    Write-Host ""
    Write-Host "輔 SP1 安裝完成。" -ForegroundColor Green
    Write-Host "安裝位置：$InstallDir"
    if (-not $NoShortcut) {
        Write-Host "桌面捷徑：$shortcutPath"
    }
}
catch {
    $failureMessage = $_.Exception.Message
    Write-Host "安裝失敗：$failureMessage" -ForegroundColor Red

    if ($shortcutInstalled -and (Test-Path -LiteralPath $shortcutPath -PathType Leaf)) {
        Remove-Item -LiteralPath $shortcutPath -Force
    }
    if ($shortcutBackedUp -and (Test-Path -LiteralPath $shortcutBackup -PathType Leaf)) {
        Move-Item -LiteralPath $shortcutBackup -Destination $shortcutPath
    }
    if (Test-Path -LiteralPath $shortcutTemp -PathType Leaf) {
        Remove-Item -LiteralPath $shortcutTemp -Force
    }

    if ($installSwapped -and (Test-Path -LiteralPath $InstallDir)) {
        Move-Item -LiteralPath $InstallDir -Destination $failedDir
    }
    if ($installBackedUp -and (Test-Path -LiteralPath $backupDir)) {
        Move-Item -LiteralPath $backupDir -Destination $InstallDir
    }
    if (Test-Path -LiteralPath $failedDir) {
        Remove-Item -LiteralPath $failedDir -Recurse -Force
    }
    throw
}
finally {
    if (Test-Path -LiteralPath $stageDir) {
        Remove-Item -LiteralPath $stageDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($success -and (Test-Path -LiteralPath $backupDir)) {
        Remove-Item -LiteralPath $backupDir -Recurse -Force
    }
    if ($success -and (Test-Path -LiteralPath $shortcutBackup -PathType Leaf)) {
        Remove-Item -LiteralPath $shortcutBackup -Force
    }
}
