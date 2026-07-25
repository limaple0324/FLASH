# Verify a downloaded FLASH Windows SP1 release bundle.
# Place this script inside 輔系統, beside SHA256SUMS.txt and BUILD_INFO.txt.

param(
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"

$SystemDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ReleaseDir = Split-Path -Parent $SystemDir
$ExePath = Join-Path $ReleaseDir "FLASH.exe"
$HashPath = Join-Path $SystemDir "SHA256SUMS.txt"
$InfoPath = Join-Path $SystemDir "BUILD_INFO.txt"
$AssistantName = [string][char]0x8F14
$UpdateVerb = ([string][char]0x66F4) + [char]0x65B0
$UpdaterCommandName = $UpdateVerb + $AssistantName + ".cmd"
$UpdaterCoreName = (
    $AssistantName +
    $UpdateVerb +
    [char]0x6838 +
    [char]0x5FC3 +
    ".ps1"
)
$UpdaterCommandPath = Join-Path $ReleaseDir $UpdaterCommandName
$UpdaterCorePath = Join-Path $SystemDir $UpdaterCoreName

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

foreach ($requiredKey in @(
    'product',
    'version',
    'milestone',
    'build_kind',
    'event_name',
    'source_ref',
    'source_branch',
    'publish_target',
    'commit',
    'run_id',
    'built_utc',
    'sha256'
)) {
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

$buildKind = $buildInfo['build_kind']
if ($buildKind -notin @('sp1_snapshot', 'validation_build', 'main_release')) {
    throw "Release build_kind is invalid: $buildKind"
}

if ($buildKind -in @('sp1_snapshot', 'validation_build')) {
    if ($buildInfo['publish_target'] -ne 'none') {
        throw "A non-release build must use publish_target=none."
    }
    foreach ($forbiddenPath in @($UpdaterCommandPath, $UpdaterCorePath)) {
        if (Test-Path -LiteralPath $forbiddenPath) {
            throw "A non-release build must not contain live updater files."
        }
    }
}

if (
    $buildKind -eq 'sp1_snapshot' -and
    (
        $buildInfo['event_name'] -notin @('push', 'workflow_dispatch') -or
        $buildInfo['source_ref'] -ne 'refs/heads/sp1/completion-2026-07-25' -or
        $buildInfo['source_branch'] -ne 'sp1/completion-2026-07-25'
    )
) {
    throw "An SP1 snapshot must use the dedicated SP1 workflow source identity."
}

if ($buildKind -eq 'main_release') {
    if ($buildInfo['event_name'] -ne 'push') {
        throw "A main release must use event_name=push."
    }
    if ($buildInfo['source_ref'] -ne 'refs/heads/main') {
        throw "A main release must use source_ref=refs/heads/main."
    }
    if ($buildInfo['source_branch'] -ne 'main') {
        throw "A main release must use source_branch=main."
    }
    if ($buildInfo['publish_target'] -ne 'release/latest') {
        throw "A main release must use publish_target=release/latest."
    }
    Require-File $UpdaterCommandPath
    Require-File $UpdaterCorePath
}

if ($buildInfo['sha256'].ToLowerInvariant() -ne $actualHash) {
    throw "BUILD_INFO.txt hash does not match FLASH.exe."
}

Write-Host "Verification passed." -ForegroundColor Green
Write-Host "Version: $($buildInfo['version'])"
Write-Host "Milestone: $($buildInfo['milestone'])"
Write-Host "Build kind: $buildKind"
Write-Host "Event name: $($buildInfo['event_name'])"
Write-Host "Source ref: $($buildInfo['source_ref'])"
Write-Host "Source branch: $($buildInfo['source_branch'])"
Write-Host "Publish target: $($buildInfo['publish_target'])"
Write-Host "Commit: $($buildInfo['commit'])"
Write-Host "Run ID: $($buildInfo['run_id'])"
Write-Host "Built UTC: $($buildInfo['built_utc'])"
Write-Host "SHA256: $actualHash"

if ($NoLaunch) {
    Write-Host "NoLaunch was specified; FLASH.exe was not started." -ForegroundColor Yellow
}
else {
    Write-Host "Starting FLASH.exe for the final visual self-check..." -ForegroundColor Cyan
    Start-Process -FilePath $ExePath -WorkingDirectory $ReleaseDir
}
