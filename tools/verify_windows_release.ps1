# Verify a downloaded FLASH Windows SP1 release bundle.
# Place this script beside FLASH.exe, SHA256SUMS.txt, and BUILD_INFO.txt.

$ErrorActionPreference = "Stop"

$ReleaseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ExePath = Join-Path $ReleaseDir "FLASH.exe"
$HashPath = Join-Path $ReleaseDir "SHA256SUMS.txt"
$InfoPath = Join-Path $ReleaseDir "BUILD_INFO.txt"

function Require-File([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required release file is missing: $Path"
    }
}

Write-Host "FLASH SP1 release verification" -ForegroundColor Cyan

Require-File $ExePath
Require-File $HashPath
Require-File $InfoPath

$expectedLine = (Get-Content -LiteralPath $HashPath -Raw).Trim()
if ($expectedLine -notmatch '^([0-9a-fA-F]{64})\s+FLASH\.exe$') {
    throw "SHA256SUMS.txt has an invalid format."
}

$expectedHash = $Matches[1].ToLowerInvariant()
$actualHash = (Get-FileHash -LiteralPath $ExePath -Algorithm SHA256).Hash.ToLowerInvariant()

if ($actualHash -ne $expectedHash) {
    throw "FLASH.exe hash mismatch. Expected $expectedHash but got $actualHash."
}

$buildInfo = @{}
foreach ($line in Get-Content -LiteralPath $InfoPath) {
    if ($line -match '^([^=]+)=(.*)$') {
        $buildInfo[$Matches[1].Trim()] = $Matches[2].Trim()
    }
}

foreach ($requiredKey in @('product', 'version', 'milestone', 'commit', 'run_id', 'built_utc', 'sha256')) {
    if (-not $buildInfo.ContainsKey($requiredKey) -or [string]::IsNullOrWhiteSpace($buildInfo[$requiredKey])) {
        throw "BUILD_INFO.txt is missing required key: $requiredKey"
    }
}

if ($buildInfo['product'] -ne 'FLASH' -or $buildInfo['milestone'] -ne 'SP1') {
    throw "Release metadata does not describe FLASH SP1."
}

if ($buildInfo['version'] -notmatch '^\d+\.\d+\.\d+$') {
    throw "Release version has an invalid format: $($buildInfo['version'])"
}

if ($buildInfo['commit'] -notmatch '^[0-9a-fA-F]{40}$') {
    throw "Release commit SHA has an invalid format."
}

if ($buildInfo['sha256'].ToLowerInvariant() -ne $actualHash) {
    throw "BUILD_INFO.txt hash does not match FLASH.exe."
}

Write-Host "Verification passed." -ForegroundColor Green
Write-Host "Version: $($buildInfo['version'])"
Write-Host "Milestone: $($buildInfo['milestone'])"
Write-Host "Commit: $($buildInfo['commit'])"
Write-Host "Run ID: $($buildInfo['run_id'])"
Write-Host "Built UTC: $($buildInfo['built_utc'])"
Write-Host "SHA256: $actualHash"

Write-Host "Starting FLASH.exe for the final visual self-check..." -ForegroundColor Cyan
Start-Process -FilePath $ExePath -WorkingDirectory $ReleaseDir