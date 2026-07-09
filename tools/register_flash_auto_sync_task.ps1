# Register FLASH Auto Sync in Windows Task Scheduler
# This runs tools\sync_desktop_from_github.ps1 every 15 minutes after logon.

$ErrorActionPreference = "Stop"

$Desktop = [Environment]::GetFolderPath("Desktop")
$RepoPath = Join-Path $Desktop "FLASH"
$SyncScript = Join-Path $RepoPath "tools\sync_desktop_from_github.ps1"
$TaskName = "FLASH GitHub Desktop Auto Sync"

if (-not (Test-Path $SyncScript)) {
    throw "Sync script not found: $SyncScript. Run sync_desktop_from_github.ps1 once first."
}

$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$SyncScript`""
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 15)
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "Keep Desktop\FLASH synced with GitHub develop branch." -Force

Write-Host "Registered scheduled task: $TaskName"
Write-Host "Desktop repo path: $RepoPath"
