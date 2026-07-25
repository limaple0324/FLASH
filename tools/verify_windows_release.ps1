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
$LatestPath = Join-Path $ReleaseDir "LATEST.txt"
$CommonManifestPaths = @(
    "FLASH.exe",
    "輔系統/BUILD_INFO.txt",
    "輔系統/verify_windows_release.ps1"
)
$MainReleaseManifestPaths = @(
    "FLASH.exe",
    "LATEST.txt",
    "更新輔.cmd",
    "輔系統/BUILD_INFO.txt",
    "輔系統/verify_windows_release.ps1",
    "輔系統/輔更新核心.ps1",
    "輔系統/檢查輔同步狀態.cmd",
    "輔系統/檢查輔同步狀態.ps1"
)

function Require-File([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required release file is missing: $Path"
    }
}

function Get-ReleasePath([string]$RelativePath) {
    $result = $ReleaseDir
    foreach ($segment in ($RelativePath -split "/")) {
        if ([string]::IsNullOrWhiteSpace($segment) -or $segment -in @(".", "..")) {
            throw "Release manifest contains an unsafe path: $RelativePath"
        }
        $result = Join-Path $result $segment
    }
    return $result
}

function Read-KeyValueFile([string]$Path, [string]$DisplayName) {
    Require-File $Path
    $result = @{}
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        if ([string]::IsNullOrWhiteSpace($line)) {
            continue
        }
        if ($line -notmatch "^([^=]+)=(.*)$") {
            throw "$DisplayName has an invalid line: $line"
        }
        $key = $Matches[1].Trim()
        $value = $Matches[2].Trim()
        if ([string]::IsNullOrWhiteSpace($key) -or $result.ContainsKey($key)) {
            throw "$DisplayName contains an empty or duplicate key: $key"
        }
        $result[$key] = $value
    }
    return $result
}

function Read-AndVerifyManifest([string[]]$ExpectedPaths) {
    Require-File $HashPath
    $manifest = @{}
    foreach ($line in Get-Content -LiteralPath $HashPath -Encoding UTF8) {
        if ([string]::IsNullOrWhiteSpace($line)) {
            continue
        }
        if ($line -notmatch "^([0-9a-fA-F]{64})  ([^\\]+)$") {
            throw "SHA256SUMS.txt has an invalid line: $line"
        }
        $hash = $Matches[1].ToLowerInvariant()
        $relativePath = $Matches[2]
        if ($ExpectedPaths -notcontains $relativePath) {
            throw "SHA256SUMS.txt contains an unexpected file: $relativePath"
        }
        if ($manifest.ContainsKey($relativePath)) {
            throw "SHA256SUMS.txt contains a duplicate file: $relativePath"
        }
        $manifest[$relativePath] = $hash
    }

    foreach ($relativePath in $ExpectedPaths) {
        if (-not $manifest.ContainsKey($relativePath)) {
            throw "SHA256SUMS.txt is missing required file: $relativePath"
        }
        $filePath = Get-ReleasePath -RelativePath $relativePath
        Require-File $filePath
        $actualHash = (Get-FileHash -LiteralPath $filePath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $manifest[$relativePath]) {
            throw "Release file hash mismatch: $relativePath"
        }
    }
    if ($manifest.Count -ne $ExpectedPaths.Count) {
        throw "SHA256SUMS.txt contains an invalid number of files."
    }
    return $manifest
}

Write-Host "FLASH SP1 release verification" -ForegroundColor Cyan

Require-File $ExePath
Require-File $HashPath
Require-File $InfoPath

$buildInfo = Read-KeyValueFile -Path $InfoPath -DisplayName "BUILD_INFO.txt"
foreach ($requiredKey in @(
    "product",
    "version",
    "milestone",
    "build_kind",
    "event_name",
    "source_ref",
    "source_branch",
    "publish_target",
    "commit",
    "run_id",
    "built_utc",
    "sha256"
)) {
    if (
        -not $buildInfo.ContainsKey($requiredKey) -or
        [string]::IsNullOrWhiteSpace($buildInfo[$requiredKey])
    ) {
        throw "BUILD_INFO.txt is missing required key: $requiredKey"
    }
}

if ($buildInfo["product"] -ne "FLASH" -or $buildInfo["milestone"] -ne "SP1") {
    throw "Release metadata does not describe FLASH SP1."
}
if ($buildInfo["version"] -notmatch "^\d+\.\d+\.\d+$") {
    throw "Release version has an invalid format: $($buildInfo['version'])"
}
if ($buildInfo["commit"] -notmatch "^[0-9a-fA-F]{40}$") {
    throw "Release commit SHA has an invalid format."
}

$buildKind = $buildInfo["build_kind"]
if ($buildKind -notin @("sp1_snapshot", "validation_build", "main_release")) {
    throw "Release build_kind is invalid: $buildKind"
}

if ($buildKind -eq "main_release") {
    $expectedManifestPaths = $MainReleaseManifestPaths
}
elseif ($buildKind -eq "sp1_snapshot") {
    $expectedManifestPaths = @($CommonManifestPaths + "SP1快照說明.txt")
}
else {
    $expectedManifestPaths = @($CommonManifestPaths + "分支驗證說明.txt")
}
$manifest = Read-AndVerifyManifest -ExpectedPaths $expectedManifestPaths
$actualExeHash = $manifest["FLASH.exe"]

if ($buildInfo["sha256"].ToLowerInvariant() -ne $actualExeHash) {
    throw "BUILD_INFO.txt hash does not match FLASH.exe."
}

if ($buildKind -in @("sp1_snapshot", "validation_build")) {
    if ($buildInfo["publish_target"] -ne "none") {
        throw "A non-release build must use publish_target=none."
    }
    foreach ($forbiddenRelativePath in @(
        "LATEST.txt",
        "更新輔.cmd",
        "輔系統/輔更新核心.ps1",
        "輔系統/檢查輔同步狀態.cmd",
        "輔系統/檢查輔同步狀態.ps1"
    )) {
        $forbiddenPath = Get-ReleasePath -RelativePath $forbiddenRelativePath
        if (Test-Path -LiteralPath $forbiddenPath) {
            throw "A non-release build must not contain live updater files."
        }
    }
}

if (
    $buildKind -eq "sp1_snapshot" -and
    (
        $buildInfo["event_name"] -notin @("push", "workflow_dispatch") -or
        $buildInfo["source_ref"] -ne "refs/heads/sp1/completion-2026-07-25" -or
        $buildInfo["source_branch"] -ne "sp1/completion-2026-07-25"
    )
) {
    throw "An SP1 snapshot must use the dedicated SP1 workflow source identity."
}

if ($buildKind -eq "main_release") {
    if ($buildInfo["event_name"] -ne "push") {
        throw "A main release must use event_name=push."
    }
    if ($buildInfo["source_ref"] -ne "refs/heads/main") {
        throw "A main release must use source_ref=refs/heads/main."
    }
    if ($buildInfo["source_branch"] -ne "main") {
        throw "A main release must use source_branch=main."
    }
    if ($buildInfo["publish_target"] -ne "release/latest") {
        throw "A main release must use publish_target=release/latest."
    }

    $latest = Read-KeyValueFile -Path $LatestPath -DisplayName "LATEST.txt"
    foreach ($requiredKey in @("branch", "commit", "run_id", "updated_utc")) {
        if (
            -not $latest.ContainsKey($requiredKey) -or
            [string]::IsNullOrWhiteSpace($latest[$requiredKey])
        ) {
            throw "LATEST.txt is missing required key: $requiredKey"
        }
    }
    if ($latest["branch"] -ne "main") {
        throw "LATEST.txt must use branch=main."
    }
    if ($latest["commit"] -ne $buildInfo["commit"]) {
        throw "LATEST.txt commit does not match BUILD_INFO.txt."
    }
    if ($latest["run_id"] -ne $buildInfo["run_id"]) {
        throw "LATEST.txt run_id does not match BUILD_INFO.txt."
    }
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
Write-Host "SHA256: $actualExeHash"

if ($NoLaunch) {
    Write-Host "NoLaunch was specified; FLASH.exe was not started." -ForegroundColor Yellow
}
else {
    Write-Host "Starting FLASH.exe for the final visual self-check..." -ForegroundColor Cyan
    Start-Process -FilePath $ExePath -WorkingDirectory $ReleaseDir
}
