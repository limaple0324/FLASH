# Verify a downloaded 輔 Windows artifact bundle.
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
$ExpectedProduct = [string][char]0x8F14

function Require-File([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required release file is missing: $Path"
    }
}

Write-Host "$ExpectedProduct Windows artifact verification" -ForegroundColor Cyan

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
    'technical_name',
    'version',
    'milestone',
    'delivery_scope',
    'build_kind',
    'validation_state',
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

if ($buildInfo['product'] -cne $ExpectedProduct) {
    throw "Release product must be $ExpectedProduct."
}

if ($buildInfo['technical_name'] -cne 'FLASH') {
    throw "Release technical_name must be FLASH."
}

$semVerPattern = '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$'
if ($buildInfo['version'] -notmatch $semVerPattern) {
    throw "Release version has an invalid format: $($buildInfo['version'])"
}

if ($buildInfo['commit'] -notmatch '^[0-9a-fA-F]{40}$') {
    throw "Release commit SHA has an invalid format."
}

$buildKind = $buildInfo['build_kind']
if (@('integration_snapshot', 'main_snapshot', 'main_release') -cnotcontains $buildKind) {
    throw "Release build_kind is invalid: $buildKind"
}

if ($buildKind -ceq 'integration_snapshot') {
    if ($buildInfo['publish_target'] -cne 'none') {
        throw "An integration_snapshot must use publish_target=none."
    }
    if ($buildInfo['source_branch'] -ieq 'main') {
        throw "An integration_snapshot must not use source_branch=main."
    }
}

if ($buildKind -ceq 'main_snapshot') {
    if ($buildInfo['publish_target'] -cne 'none') {
        throw "A main_snapshot must use publish_target=none."
    }
    if ($buildInfo['source_branch'] -cne 'main') {
        throw "A main_snapshot must use source_branch=main."
    }
}

if ($buildKind -ceq 'main_release') {
    if ($buildInfo['publish_target'] -cne 'release/latest') {
        throw "A main_release must use publish_target=release/latest."
    }
    if ($buildInfo['source_branch'] -cne 'main') {
        throw "A main_release must use source_branch=main."
    }
}

if ($buildInfo['sha256'].ToLowerInvariant() -ne $actualHash) {
    throw "BUILD_INFO.txt hash does not match FLASH.exe."
}

Write-Host "Verification passed." -ForegroundColor Green
Write-Host "Product: $($buildInfo['product'])"
Write-Host "Technical name: $($buildInfo['technical_name'])"
Write-Host "Version: $($buildInfo['version'])"
Write-Host "Milestone: $($buildInfo['milestone'])"
Write-Host "Delivery scope: $($buildInfo['delivery_scope'])"
Write-Host "Build kind: $buildKind"
Write-Host "Validation state: $($buildInfo['validation_state'])"
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
