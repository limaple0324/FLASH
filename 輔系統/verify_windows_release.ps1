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
$ChannelPath = Join-Path $SystemDir "UPDATE_CHANNEL.txt"
$ExpectedProduct = [string][char]0x8F14
$ExpectedTechnicalName = "FLASH"
$CommonManifestPaths = @(
    "FLASH.exe",
    "輔系統/BUILD_INFO.txt",
    "sync_plus_icon.ico",
    "輔系統/verify_windows_release.ps1"
)
$LiveReleaseManifestPaths = @(
    "FLASH.exe",
    "LATEST.txt",
    "安裝輔.cmd",
    "更新輔.cmd",
    "輔系統/BUILD_INFO.txt",
    "sync_plus_icon.ico",
    "輔系統/verify_windows_release.ps1",
    "輔系統/安裝輔.ps1",
    "輔系統/輔更新核心.ps1",
    "輔系統/UPDATE_CHANNEL.txt",
    "輔系統/檢查輔同步狀態.cmd",
    "輔系統/檢查輔同步狀態.ps1"
)

function Require-File([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required release file is missing: $Path"
    }
}

function Get-Sha256Hex([string]$Path) {
    Require-File $Path
    $stream = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
    )
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hashBytes = $sha256.ComputeHash($stream)
        return ([System.BitConverter]::ToString($hashBytes)).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha256.Dispose()
        $stream.Dispose()
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
        $actualHash = Get-Sha256Hex -Path $filePath
        if ($actualHash -ne $manifest[$relativePath]) {
            throw "Release file hash mismatch: $relativePath"
        }
    }
    if ($manifest.Count -ne $ExpectedPaths.Count) {
        throw "SHA256SUMS.txt contains an invalid number of files."
    }
    return $manifest
}

Write-Host "$ExpectedProduct Windows artifact verification" -ForegroundColor Cyan

Require-File $ExePath
Require-File $HashPath
Require-File $InfoPath

$buildInfo = Read-KeyValueFile -Path $InfoPath -DisplayName "BUILD_INFO.txt"
foreach ($requiredKey in @(
    "product",
    "technical_name",
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

if (
    $buildInfo["product"] -cne $ExpectedProduct -or
    $buildInfo["technical_name"] -cne $ExpectedTechnicalName -or
    $buildInfo["milestone"] -notin @("SP1", "SP2", "SP3")
) {
    throw "Release metadata does not describe a supported 輔 milestone."
}
if ($buildInfo["version"] -notmatch "^\d+\.\d+\.\d+$") {
    throw "Release version has an invalid format: $($buildInfo['version'])"
}
if ($buildInfo["commit"] -notmatch "^[0-9a-fA-F]{40}$") {
    throw "Release commit SHA has an invalid format."
}

$buildKind = $buildInfo["build_kind"]
if (
    $buildKind -notin @(
        "sp1_snapshot",
        "sp2_snapshot",
        "sp3_snapshot",
        "validation_build",
        "main_release",
        "sp1_release"
    )
) {
    throw "Release build_kind is invalid: $buildKind"
}
if (
    $buildKind -in @("sp1_snapshot", "sp1_release") -and
    $buildInfo["milestone"] -ne "SP1"
) {
    throw "An SP1 delivery must use milestone=SP1."
}
if (
    $buildKind -eq "sp2_snapshot" -and
    $buildInfo["milestone"] -ne "SP2"
) {
    throw "An SP2 delivery must use milestone=SP2."
}
if (
    $buildKind -eq "sp3_snapshot" -and
    $buildInfo["milestone"] -ne "SP3"
) {
    throw "An SP3 delivery must use milestone=SP3."
}
if (
    $buildKind -eq "main_release" -and
    $buildInfo["milestone"] -ne "SP3"
) {
    throw "The complete cumulative release must use milestone=SP3."
}

if ($buildKind -in @("main_release", "sp1_release")) {
    $expectedManifestPaths = $LiveReleaseManifestPaths
}
elseif ($buildKind -eq "sp1_snapshot") {
    $expectedManifestPaths = @($CommonManifestPaths + "SP1快照說明.txt")
}
elseif ($buildKind -eq "sp2_snapshot") {
    $expectedManifestPaths = @($CommonManifestPaths + "SP1+SP2累積快照說明.txt")
}
elseif ($buildKind -eq "sp3_snapshot") {
    $expectedManifestPaths = @($CommonManifestPaths + "SP1+SP2+SP3完整累積快照說明.txt")
}
else {
    $expectedManifestPaths = @($CommonManifestPaths + "分支驗證說明.txt")
}
$manifest = Read-AndVerifyManifest -ExpectedPaths $expectedManifestPaths
$actualExeHash = $manifest["FLASH.exe"]

if ($buildInfo["sha256"].ToLowerInvariant() -ne $actualExeHash) {
    throw "BUILD_INFO.txt hash does not match FLASH.exe."
}

if ($buildKind -in @("sp1_snapshot", "sp2_snapshot", "sp3_snapshot", "validation_build")) {
    if ($buildInfo["publish_target"] -ne "none") {
        throw "A non-release build must use publish_target=none."
    }
    foreach ($forbiddenRelativePath in @(
        "LATEST.txt",
        "安裝輔.cmd",
        "更新輔.cmd",
        "輔系統/安裝輔.ps1",
        "輔系統/輔更新核心.ps1",
        "輔系統/UPDATE_CHANNEL.txt",
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

if (
    $buildKind -eq "sp2_snapshot" -and
    (
        $buildInfo["event_name"] -notin @("push", "workflow_dispatch") -or
        $buildInfo["source_ref"] -ne "refs/heads/sp2/completion-2026-07-26" -or
        $buildInfo["source_branch"] -ne "sp2/completion-2026-07-26"
    )
) {
    throw "An SP2 snapshot must use the dedicated SP2 workflow source identity."
}
if (
    $buildKind -eq "sp3_snapshot" -and
    (
        $buildInfo["event_name"] -notin @("push", "workflow_dispatch") -or
        $buildInfo["source_ref"] -ne "refs/heads/sp3/completion-2026-07-26" -or
        $buildInfo["source_branch"] -ne "sp3/completion-2026-07-26"
    )
) {
    throw "An SP3 snapshot must use the dedicated SP3 workflow source identity."
}

if ($buildKind -in @("main_release", "sp1_release")) {
    if ($buildKind -eq "main_release") {
        $expectedEventName = "push"
        $expectedSourceRef = "refs/heads/main"
        $expectedSourceBranch = "main"
        $expectedPublishTarget = "release/latest"
    }
    else {
        $expectedEventName = ""
        $expectedSourceRef = "refs/heads/sp1/completion-2026-07-25"
        $expectedSourceBranch = "sp1/completion-2026-07-25"
        $expectedPublishTarget = "release/sp1"
    }
    if (
        $buildKind -eq "sp1_release" -and
        $buildInfo["event_name"] -notin @("push", "workflow_dispatch")
    ) {
        throw "An sp1_release build must use event_name=push or workflow_dispatch."
    }
    if (
        $buildKind -eq "main_release" -and
        $buildInfo["event_name"] -ne $expectedEventName
    ) {
        throw "A $buildKind build must use event_name=$expectedEventName."
    }
    if ($buildInfo["source_ref"] -ne $expectedSourceRef) {
        throw "A $buildKind build must use source_ref=$expectedSourceRef."
    }
    if ($buildInfo["source_branch"] -ne $expectedSourceBranch) {
        throw "A $buildKind build must use source_branch=$expectedSourceBranch."
    }
    if ($buildInfo["publish_target"] -ne $expectedPublishTarget) {
        throw "A $buildKind build must use publish_target=$expectedPublishTarget."
    }

    $channel = Read-KeyValueFile -Path $ChannelPath -DisplayName "UPDATE_CHANNEL.txt"
    foreach ($requiredKey in @("release_branch", "source_branch", "build_kind", "publish_target")) {
        if (
            -not $channel.ContainsKey($requiredKey) -or
            [string]::IsNullOrWhiteSpace($channel[$requiredKey])
        ) {
            throw "UPDATE_CHANNEL.txt is missing required key: $requiredKey"
        }
    }
    if ($channel["release_branch"] -ne $expectedPublishTarget) {
        throw "UPDATE_CHANNEL.txt must use release_branch=$expectedPublishTarget."
    }
    foreach ($matchingKey in @("source_branch", "build_kind", "publish_target")) {
        if ($channel[$matchingKey] -ne $buildInfo[$matchingKey]) {
            throw "UPDATE_CHANNEL.txt $matchingKey does not match BUILD_INFO.txt."
        }
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
    if ($latest["branch"] -ne $expectedSourceBranch) {
        throw "LATEST.txt must use branch=$expectedSourceBranch."
    }
    if ($latest["commit"] -ne $buildInfo["commit"]) {
        throw "LATEST.txt commit does not match BUILD_INFO.txt."
    }
    if ($latest["run_id"] -ne $buildInfo["run_id"]) {
        throw "LATEST.txt run_id does not match BUILD_INFO.txt."
    }
}

Write-Host "Verification passed." -ForegroundColor Green
Write-Host "Product: $($buildInfo['product'])"
Write-Host "Technical name: $($buildInfo['technical_name'])"
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
