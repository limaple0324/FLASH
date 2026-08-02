# 輔正式版完整首次安裝器
# 正式版先驗證整包，再以同磁碟暫存與備份完成可回復安裝。

param(
    [string]$SourceDirectory = "",
    [string]$InstallDirectory = "",
    [string]$DesktopDirectory = "",
    [switch]$NoShortcut,
    [ValidateRange(0, 1)]
    [int]$TestFailAfterSwap = 0,
    [ValidateRange(0, 2)]
    [int]$TestFailAfterShortcut = 0
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if (-not ("FlashNativeShortcutWriter" -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

[ComImport]
[Guid("00021401-0000-0000-C000-000000000046")]
internal class ShellLinkObject
{
}

[ComImport]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
[Guid("000214F9-0000-0000-C000-000000000046")]
internal interface IShellLinkW
{
    void GetPath(
        [Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder file,
        int maxPath,
        IntPtr findData,
        uint flags
    );
    void GetIDList(out IntPtr itemIdList);
    void SetIDList(IntPtr itemIdList);
    void GetDescription(
        [Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder description,
        int maxName
    );
    void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string description);
    void GetWorkingDirectory(
        [Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder directory,
        int maxPath
    );
    void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string directory);
    void GetArguments(
        [Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder arguments,
        int maxPath
    );
    void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string arguments);
    void GetHotkey(out short hotkey);
    void SetHotkey(short hotkey);
    void GetShowCmd(out int showCommand);
    void SetShowCmd(int showCommand);
    void GetIconLocation(
        [Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder iconPath,
        int iconPathLength,
        out int iconIndex
    );
    void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string iconPath, int iconIndex);
    void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
    void Resolve(IntPtr windowHandle, uint flags);
    void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
}

public static class FlashNativeShortcutWriter
{
    public static void Create(
        string shortcutPath,
        string executablePath,
        string workingDirectory,
        string iconPath,
        string description
    )
    {
        object shellLinkObject = new ShellLinkObject();
        try
        {
            IShellLinkW shellLink = (IShellLinkW)shellLinkObject;
            shellLink.SetPath(executablePath);
            shellLink.SetWorkingDirectory(workingDirectory);
            shellLink.SetIconLocation(iconPath, 0);
            shellLink.SetDescription(description);
            shellLink.SetShowCmd(1);
            ((IPersistFile)shellLink).Save(shortcutPath, true);
        }
        finally
        {
            Marshal.FinalReleaseComObject(shellLinkObject);
        }
    }
}
"@
}

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

function Read-KeyValueFile([string]$Path, [string]$Description) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Description 不存在：$Path"
    }
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith("#")) {
            continue
        }
        $separator = $line.IndexOf("=")
        if ($separator -le 0) {
            throw "$Description 格式錯誤。"
        }
        $key = $line.Substring(0, $separator).Trim()
        $value = $line.Substring($separator + 1).Trim()
        if ([string]::IsNullOrWhiteSpace($key) -or $values.ContainsKey($key)) {
            throw "$Description 包含無效或重複欄位。"
        }
        $values[$key] = $value
    }
    return $values
}

function Write-InstallLog([string]$Path, [string]$Message) {
    $line = "[{0}] {1}" -f [DateTime]::Now.ToString("yyyy-MM-dd HH:mm:ss"), $Message
    try {
        Add-Content -LiteralPath $Path -Value $line -Encoding UTF8
    }
    catch {
        Write-Host "安裝紀錄無法寫入：$Path" -ForegroundColor Yellow
    }
}

function New-DesktopShortcut(
    [string]$ShortcutPath,
    [string]$ExecutablePath,
    [string]$WorkingDirectory,
    [string]$Description
) {
    $iconPath = Join-Path $WorkingDirectory "sync_plus_icon.ico"
    if (-not (Test-Path -LiteralPath $iconPath -PathType Leaf)) {
        throw "缺少桌面捷徑圖示：$iconPath"
    }
    [FlashNativeShortcutWriter]::Create(
        $ShortcutPath,
        $ExecutablePath,
        $WorkingDirectory,
        $iconPath,
        $Description
    )
}

if ([string]::IsNullOrWhiteSpace($SourceDirectory)) {
    $scriptSystemDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $SourceDirectory = Split-Path -Parent $scriptSystemDir
}
$SourceDir = Get-NormalizedDirectory $SourceDirectory
if (-not (Test-Path -LiteralPath $SourceDir -PathType Container)) {
    throw "找不到完整安裝包：$SourceDir"
}
$buildInfo = Read-KeyValueFile `
    -Path (Join-Path $SourceDir "輔系統\BUILD_INFO.txt") `
    -Description "成品身分資料"
$buildKind = [string]$buildInfo["build_kind"]
$milestone = [string]$buildInfo["milestone"]
if ($buildKind -eq "sp1_release" -and $milestone -eq "SP1") {
    $installFlavor = "SP1"
    $installLabel = "SP1 獨立版"
}
elseif ($buildKind -eq "main_release" -and $milestone -eq "SP3") {
    $installFlavor = "完整累積版"
    $installLabel = "完整累積版"
}
else {
    throw "這個安裝包不是可安裝的 SP1 獨立版或完整累積版。"
}
if ([string]::IsNullOrWhiteSpace($InstallDirectory)) {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw "找不到 LOCALAPPDATA，無法決定安全的使用者安裝位置。"
    }
    $InstallDirectory = Join-Path $env:LOCALAPPDATA "Programs\輔\$installFlavor"
}
if ([string]::IsNullOrWhiteSpace($DesktopDirectory)) {
    $DesktopDirectory = [Environment]::GetFolderPath("Desktop")
}

$InstallDir = Get-NormalizedDirectory $InstallDirectory
$DesktopDir = Get-NormalizedDirectory $DesktopDirectory

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

$installParent = Split-Path -Parent $InstallDir
New-Item -ItemType Directory -Force -Path $installParent | Out-Null
$persistentInstallLog = Join-Path $installParent "輔-安裝紀錄.txt"
Write-InstallLog -Path $persistentInstallLog -Message "開始安裝：$installLabel"

$running = Get-Process -Name "FLASH" -ErrorAction SilentlyContinue
if ($running) {
    Write-InstallLog `
        -Path $persistentInstallLog `
        -Message "安裝失敗：輔正在執行，請先關閉後再安裝。"
    throw "輔正在執行，請先關閉後再安裝。"
}

try {
    Invoke-BundleVerifier -Root $SourceDir -Description "來源安裝包驗證"
}
catch {
    Write-InstallLog `
        -Path $persistentInstallLog `
        -Message "安裝失敗：$($_.Exception.Message)"
    throw
}

$transactionId = [Guid]::NewGuid().ToString("N")
$stageDir = Join-Path $installParent ".輔-stage-$transactionId"
$backupDir = Join-Path $installParent ".輔-backup-$transactionId"
$failedDir = Join-Path $installParent ".輔-failed-$transactionId"
$shortcutPath = Join-Path $DesktopDir "輔.lnk"
$updateShortcutPath = Join-Path $DesktopDir "更新輔.lnk"
$shortcutTemp = Join-Path $DesktopDir ".FLASH-$transactionId.lnk"
$updateShortcutTemp = Join-Path $DesktopDir ".FLASH-Update-$transactionId.lnk"
$shortcutBackup = Join-Path $installParent ".FLASH-shortcut-$transactionId.lnk"
$updateShortcutBackup = Join-Path $installParent ".FLASH-update-shortcut-$transactionId.lnk"

$installSwapped = $false
$installBackedUp = $false
$shortcutInstalled = $false
$shortcutBackedUp = $false
$updateShortcutInstalled = $false
$updateShortcutBackedUp = $false
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
            -WorkingDirectory $InstallDir `
            -Description "輔"
        New-DesktopShortcut `
            -ShortcutPath $updateShortcutTemp `
            -ExecutablePath (Join-Path $InstallDir "更新輔.cmd") `
            -WorkingDirectory $InstallDir `
            -Description "更新輔"

        if (Test-Path -LiteralPath $shortcutPath -PathType Leaf) {
            Move-Item -LiteralPath $shortcutPath -Destination $shortcutBackup
            $shortcutBackedUp = $true
        }
        Move-Item -LiteralPath $shortcutTemp -Destination $shortcutPath
        $shortcutInstalled = $true
        if ($TestFailAfterShortcut -eq 1) {
            throw "測試指定在建立第一個桌面捷徑後中斷。"
        }
        if (Test-Path -LiteralPath $updateShortcutPath -PathType Leaf) {
            Move-Item `
                -LiteralPath $updateShortcutPath `
                -Destination $updateShortcutBackup
            $updateShortcutBackedUp = $true
        }
        Move-Item `
            -LiteralPath $updateShortcutTemp `
            -Destination $updateShortcutPath
        $updateShortcutInstalled = $true
        if ($TestFailAfterShortcut -eq 2) {
            throw "測試指定在建立第二個桌面捷徑後中斷。"
        }
    }

    @(
        "installed_utc=$([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))"
        "install_directory=$InstallDir"
        "source_directory=$SourceDir"
        "shortcut=$(-not $NoShortcut)"
        "shortcut_path=$(if ($NoShortcut) { '' } else { $shortcutPath })"
        "update_shortcut_path=$(if ($NoShortcut) { '' } else { $updateShortcutPath })"
    ) | Set-Content (Join-Path $InstallDir "安裝紀錄.txt") -Encoding UTF8

    Write-InstallLog -Path $persistentInstallLog -Message "安裝成功：$installLabel"
    $success = $true
    Write-Host ""
    Write-Host "輔 $installLabel 安裝完成。" -ForegroundColor Green
    Write-Host "安裝位置：$InstallDir"
    if (-not $NoShortcut) {
        Write-Host "桌面捷徑：$shortcutPath"
        Write-Host "更新捷徑：$updateShortcutPath"
    }
}
catch {
    $failureMessage = $_.Exception.Message
    Write-Host "安裝失敗：$failureMessage" -ForegroundColor Red
    Write-InstallLog -Path $persistentInstallLog -Message "安裝失敗：$failureMessage"

    if ($shortcutInstalled -and (Test-Path -LiteralPath $shortcutPath -PathType Leaf)) {
        Remove-Item -LiteralPath $shortcutPath -Force
    }
    if ($shortcutBackedUp -and (Test-Path -LiteralPath $shortcutBackup -PathType Leaf)) {
        Move-Item -LiteralPath $shortcutBackup -Destination $shortcutPath
    }
    if (Test-Path -LiteralPath $shortcutTemp -PathType Leaf) {
        Remove-Item -LiteralPath $shortcutTemp -Force
    }
    if (
        $updateShortcutInstalled -and
        (Test-Path -LiteralPath $updateShortcutPath -PathType Leaf)
    ) {
        Remove-Item -LiteralPath $updateShortcutPath -Force
    }
    if (
        $updateShortcutBackedUp -and
        (Test-Path -LiteralPath $updateShortcutBackup -PathType Leaf)
    ) {
        Move-Item `
            -LiteralPath $updateShortcutBackup `
            -Destination $updateShortcutPath
    }
    if (Test-Path -LiteralPath $updateShortcutTemp -PathType Leaf) {
        Remove-Item -LiteralPath $updateShortcutTemp -Force
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
    if (
        $success -and
        (Test-Path -LiteralPath $updateShortcutBackup -PathType Leaf)
    ) {
        Remove-Item -LiteralPath $updateShortcutBackup -Force
    }
}
