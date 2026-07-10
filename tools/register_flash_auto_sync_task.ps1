# Register FLASH Auto Sync in Windows Task Scheduler
# This runs tools\sync_desktop_from_github.ps1 every 15 minutes.

$ErrorActionPreference = "Stop"

$Desktop = [Environment]::GetFolderPath("Desktop")
$RepoPath = Join-Path $Desktop "FLASH"
$SyncScript = Join-Path $RepoPath "tools\sync_desktop_from_github.ps1"
$TaskName = "FLASH GitHub Desktop Auto Sync"

if (-not (Test-Path $SyncScript)) {
    throw "Sync script not found: $SyncScript. Sync or clone the FLASH repository first."
}

$PowerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
$Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$SyncScript`""
$Action = New-ScheduledTaskAction -Execute $PowerShell -Argument $Arguments

# Start shortly after registration instead of using midnight, which may already
# be in the past when the task is installed.
$Trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 15)

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Keep Desktop\FLASH synchronized with GitHub main branch." `
    -Force | Out-Null

Write-Host "Registered scheduled task: $TaskName"
Write-Host "Desktop repo path: $RepoPath"
Write-Host "First sync will run in about one minute, then every 15 minutes."
