# FLASH Desktop Auto Sync
# Purpose: keep Desktop\FLASH synced with GitHub develop branch.
# Run once manually, or register it with Windows Task Scheduler.

$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/limaple0324/FLASH.git"
$Branch = "develop"
$Desktop = [Environment]::GetFolderPath("Desktop")
$RepoPath = Join-Path $Desktop "FLASH"
$LogPath = Join-Path $RepoPath "sync.log"

function Write-Log($Message) {
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$timestamp] $Message"
    Write-Host $line
    if (Test-Path $RepoPath) {
        Add-Content -Path $LogPath -Value $line -Encoding UTF8
    }
}

try {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "Git is not installed or not available in PATH."
    }

    if (-not (Test-Path $RepoPath)) {
        Write-Host "Cloning FLASH to $RepoPath ..."
        git clone --branch $Branch $RepoUrl $RepoPath
        Write-Log "Initial clone complete."
        exit 0
    }

    Set-Location $RepoPath

    if (-not (Test-Path ".git")) {
        throw "$RepoPath exists but is not a Git repository. Rename or remove it first."
    }

    git fetch origin
    git checkout $Branch
    git pull --ff-only origin $Branch

    Write-Log "Sync complete from origin/$Branch."
}
catch {
    Write-Host "FLASH sync failed: $($_.Exception.Message)"
    if (Test-Path $RepoPath) {
        Add-Content -Path $LogPath -Value "[$(Get-Date -Format "yyyy-MM-dd HH:mm:ss")] ERROR: $($_.Exception.Message)" -Encoding UTF8
    }
    exit 1
}
