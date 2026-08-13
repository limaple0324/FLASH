# 輔更新核心
# 用途：由固定的「更新輔.cmd」複製到 TEMP 後執行。
# 所有正式檔案都從安裝包鎖定的更新分支同一個 commit 下載，先驗證，再交易式套用。

param(
    [string]$InstallDirectory = "",
    [string]$ReleaseSourceDirectory = "",
    [string]$ResolvedReleaseCommit = "",
    [ValidateRange(0, 1000)]
    [int]$TestFailAfterReplacement = 0,
    [ValidateRange(0, 60000)]
    [int]$TestHoldLockMilliseconds = 0,
    [ValidateRange(0, 1000)]
    [int]$TestFailDuringRollbackAt = 0,
    [ValidateRange(5, 300)]
    [int]$ConnectionTimeoutSeconds = 15,
    [ValidateRange(10, 1800)]
    [int]$DownloadTimeoutSeconds = 90,
    [ValidateRange(1, 8)]
    [int]$NetworkRetryCount = 4,
    [ValidateRange(0, 8)]
    [int]$TestTransientFailuresBeforeSuccess = 0,
    [ValidateSet("", "offline", "dns", "tls", "403", "404", "429", "500", "timeout", "github_limit")]
    [string]$TestNetworkFailureKind = ""
)

$ErrorActionPreference = "Stop"

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Repo = "limaple0324/FLASH"
$ReleaseBranch = ""
$ManifestRelativePath = "輔系統/SHA256SUMS.txt"
$ChannelRelativePath = "輔系統/UPDATE_CHANNEL.txt"
$PayloadPaths = @(
    "FLASH.exe",
    "LATEST.txt",
    "安裝輔.cmd",
    "更新輔.cmd",
    "輔系統/BUILD_INFO.txt",
    "sync_plus_icon.ico",
    "輔系統/verify_windows_release.ps1",
    "輔系統/安裝輔.ps1",
    "輔系統/輔更新核心.ps1",
    $ChannelRelativePath,
    "輔系統/檢查輔同步狀態.cmd",
    "輔系統/檢查輔同步狀態.ps1"
)
$FixedIdentityPaths = @("更新輔.cmd", $ChannelRelativePath)
$DownloadPaths = @($PayloadPaths + $ManifestRelativePath)

if ([string]::IsNullOrWhiteSpace($InstallDirectory)) {
    $scriptSystemDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $InstallDirectory = Split-Path -Parent $scriptSystemDir
}

$InstallDir = [IO.Path]::GetFullPath($InstallDirectory).TrimEnd(
    [IO.Path]::DirectorySeparatorChar,
    [IO.Path]::AltDirectorySeparatorChar
)
$SystemDir = Join-Path $InstallDir "輔系統"
$LogPath = Join-Path $InstallDir "更新紀錄.txt"
$LockPath = Join-Path $SystemDir "更新鎖定.lock"
$TransactionBase = Join-Path $SystemDir "更新交易"
$TransactionId = [Guid]::NewGuid().ToString("N")
$TransactionRoot = Join-Path $TransactionBase $TransactionId
$StageRoot = Join-Path $TransactionRoot "stage"
$BackupRoot = Join-Path $TransactionRoot "backup"
$UsingLocalSource = -not [string]::IsNullOrWhiteSpace($ReleaseSourceDirectory)
$LockStream = $null
$AppliedRecords = New-Object System.Collections.ArrayList
$TemporaryTargetPaths = New-Object System.Collections.ArrayList
$UpdateSucceeded = $false
$PreserveTransaction = $false
$ExitCode = 1
$script:TransientFailuresRemaining = $TestTransientFailuresBeforeSuccess

function Write-Step([string]$Message) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Message"
    Write-Host $line
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
            return
        }
        catch {
            if ($attempt -eq 5) {
                # 連記錄檔都無法寫入時，仍保留主畫面的中文錯誤。
                return
            }
            Start-Sleep -Milliseconds 40
        }
    }
}

function Get-NetworkFailureMessage([System.Exception]$Exception) {
    $response = $Exception.Response
    $statusCode = $null
    if ($response -and $response.StatusCode) {
        try {
            $statusCode = [int]$response.StatusCode
        }
        catch {
            $statusCode = $null
        }
    }
    if ($statusCode -eq 403) {
        $remaining = $null
        try {
            $remaining = [string]$response.Headers["X-RateLimit-Remaining"]
        }
        catch {
            $remaining = $null
        }
        if ($remaining -eq "0") {
            return "GitHub 下載次數已達限制，請稍後再試。"
        }
        return "GitHub 拒絕存取（403），請稍後再試。"
    }
    if ($statusCode -eq 404) {
        return "找不到更新檔案（404），目前版本沒有被修改。"
    }
    if ($statusCode -eq 429) {
        return "下載過於頻繁（429），請稍後再試。"
    }
    if ($statusCode -ge 500 -and $statusCode -le 599) {
        return "更新伺服器暫時無法使用，請稍後再試。"
    }
    $webStatus = $null
    if ($Exception -is [System.Net.WebException]) {
        $webStatus = $Exception.Status
    }
    if ($webStatus -eq [System.Net.WebExceptionStatus]::NameResolutionFailure) {
        return "無法解析網路位址，請檢查網路或 DNS 設定。"
    }
    if (
        $webStatus -eq [System.Net.WebExceptionStatus]::TrustFailure -or
        $webStatus -eq [System.Net.WebExceptionStatus]::SecureChannelFailure
    ) {
        return "安全連線驗證失敗，請確認系統時間與網路憑證。"
    }
    if ($webStatus -eq [System.Net.WebExceptionStatus]::Timeout) {
        return "連線逾時，請確認網路後再試。"
    }
    if (
        $webStatus -eq [System.Net.WebExceptionStatus]::ConnectFailure -or
        $webStatus -eq [System.Net.WebExceptionStatus]::ProxyNameResolutionFailure
    ) {
        return "目前無法連上網路，請確認網路連線後再試。"
    }
    return "下載更新時發生網路錯誤，請稍後再試。"
}

function Test-TransientNetworkFailure([System.Exception]$Exception) {
    $response = $Exception.Response
    $statusCode = $null
    if ($response -and $response.StatusCode) {
        try {
            $statusCode = [int]$response.StatusCode
        }
        catch {
            $statusCode = $null
        }
    }
    if ($statusCode -in @(408, 429)) {
        return $true
    }
    if ($statusCode -ge 500 -and $statusCode -le 599) {
        return $true
    }
    if ($Exception -is [System.Net.WebException]) {
        return $Exception.Status -in @(
            [System.Net.WebExceptionStatus]::ConnectFailure,
            [System.Net.WebExceptionStatus]::ConnectionClosed,
            [System.Net.WebExceptionStatus]::KeepAliveFailure,
            [System.Net.WebExceptionStatus]::NameResolutionFailure,
            [System.Net.WebExceptionStatus]::ProxyNameResolutionFailure,
            [System.Net.WebExceptionStatus]::ReceiveFailure,
            [System.Net.WebExceptionStatus]::SendFailure,
            [System.Net.WebExceptionStatus]::Timeout
        )
    }
    return $false
}

function Invoke-WithNetworkRetry(
    [string]$Description,
    [scriptblock]$Action
) {
    for ($attempt = 1; $attempt -le $NetworkRetryCount; $attempt++) {
        try {
            return & $Action
        }
        catch {
            $transient = Test-TransientNetworkFailure $_.Exception
            if (-not $transient -or $attempt -eq $NetworkRetryCount) {
                throw (Get-NetworkFailureMessage $_.Exception)
            }
            $delay = [Math]::Min(8000, 500 * [Math]::Pow(2, $attempt - 1))
            Write-Step "$Description 暫時失敗；即將進行第 $($attempt + 1) 次嘗試。"
            Start-Sleep -Milliseconds ([int]$delay)
        }
    }
}

function Require-File([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "缺少必要檔案：$Path"
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

function Get-PayloadPath([string]$Root, [string]$RelativePath) {
    $result = $Root
    foreach ($segment in ($RelativePath -split "/")) {
        if ([string]::IsNullOrWhiteSpace($segment) -or $segment -in @(".", "..")) {
            throw "發布檔案路徑不安全：$RelativePath"
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
            throw "$DisplayName 格式不正確：$line"
        }
        $key = $Matches[1].Trim()
        $value = $Matches[2].Trim()
        if ([string]::IsNullOrWhiteSpace($key) -or $result.ContainsKey($key)) {
            throw "$DisplayName 含有空白或重複欄位：$key"
        }
        $result[$key] = $value
    }
    return $result
}

function Require-MetadataValue(
    [hashtable]$Metadata,
    [string]$Key,
    [string]$Expected,
    [string]$DisplayName
) {
    if (-not $Metadata.ContainsKey($Key)) {
        throw "$DisplayName 缺少欄位：$Key"
    }
    if ($Metadata[$Key] -ne $Expected) {
        throw "$DisplayName 的 $Key 必須是 $Expected，實際是 $($Metadata[$Key])。"
    }
}

function Read-InstalledUpdateChannel {
    $channelPath = Get-PayloadPath -Root $InstallDir -RelativePath $ChannelRelativePath
    $channel = Read-KeyValueFile -Path $channelPath -DisplayName "UPDATE_CHANNEL.txt"
    foreach ($requiredKey in @("release_branch", "source_branch", "build_kind", "publish_target")) {
        if (
            -not $channel.ContainsKey($requiredKey) -or
            [string]::IsNullOrWhiteSpace($channel[$requiredKey])
        ) {
            throw "UPDATE_CHANNEL.txt 缺少欄位：$requiredKey"
        }
    }

    $allowedIdentity = @{
        "release/latest" = @{
            source_branch = "main"
            build_kind = "main_release"
            publish_target = "release/latest"
        }
        "release/sp1" = @{
            source_branch = "sp1/completion-2026-07-25"
            build_kind = "sp1_release"
            publish_target = "release/sp1"
        }
    }
    $releaseBranch = $channel["release_branch"]
    if (-not $allowedIdentity.ContainsKey($releaseBranch)) {
        throw "UPDATE_CHANNEL.txt 的 release_branch 不受支援：$releaseBranch"
    }
    $expected = $allowedIdentity[$releaseBranch]
    foreach ($key in @("source_branch", "build_kind", "publish_target")) {
        if ($channel[$key] -ne $expected[$key]) {
            throw "UPDATE_CHANNEL.txt 的 $key 與 $releaseBranch 不一致。"
        }
    }
    return $channel
}

function Resolve-TargetUpdateChannel([hashtable]$InstalledChannel) {
    if ($InstalledChannel["release_branch"] -eq "release/sp1") {
        Write-Step "偵測到 SP1 獨立版；本次將安全升級為完整累積版。"
        return @{
            release_branch = "release/latest"
            source_branch = "main"
            build_kind = "main_release"
            publish_target = "release/latest"
        }
    }
    return $InstalledChannel
}

function Assert-InstalledMigrationIdentity(
    [hashtable]$InstalledChannel
) {
    if ($InstalledChannel["release_branch"] -ne "release/sp1") {
        return
    }
    $buildInfoPath = Get-PayloadPath `
        -Root $InstallDir `
        -RelativePath "輔系統/BUILD_INFO.txt"
    $buildInfo = Read-KeyValueFile `
        -Path $buildInfoPath `
        -DisplayName "已安裝 BUILD_INFO.txt"
    foreach ($key in @("source_branch", "build_kind", "publish_target")) {
        if (
            -not $buildInfo.ContainsKey($key) -or
            $buildInfo[$key] -ne $InstalledChannel[$key]
        ) {
            throw "已安裝 BUILD_INFO.txt 的 $key 與 SP1 更新頻道不一致；未進行遷移。"
        }
    }
}

function Read-AndVerifyManifest([string]$Root) {
    $manifestPath = Get-PayloadPath -Root $Root -RelativePath $ManifestRelativePath
    Require-File $manifestPath

    $manifest = @{}
    foreach ($line in Get-Content -LiteralPath $manifestPath -Encoding UTF8) {
        if ([string]::IsNullOrWhiteSpace($line)) {
            continue
        }
        if ($line -notmatch "^([0-9a-fA-F]{64})  ([^\\]+)$") {
            throw "SHA256SUMS.txt 格式不正確：$line"
        }
        $hash = $Matches[1].ToLowerInvariant()
        $relativePath = $Matches[2]
        if ($PayloadPaths -notcontains $relativePath) {
            throw "SHA256SUMS.txt 含有未允許的檔案：$relativePath"
        }
        if ($manifest.ContainsKey($relativePath)) {
            throw "SHA256SUMS.txt 含有重複檔案：$relativePath"
        }
        $manifest[$relativePath] = $hash
    }

    foreach ($relativePath in $PayloadPaths) {
        if (-not $manifest.ContainsKey($relativePath)) {
            throw "SHA256SUMS.txt 缺少必要檔案：$relativePath"
        }
        $filePath = Get-PayloadPath -Root $Root -RelativePath $relativePath
        Require-File $filePath
        $actualHash = Get-Sha256Hex -Path $filePath
        if ($actualHash -ne $manifest[$relativePath]) {
            throw "檔案雜湊核對失敗：$relativePath"
        }
    }

    if ($manifest.Count -ne $PayloadPaths.Count) {
        throw "SHA256SUMS.txt 的檔案數量不正確。"
    }
    return $manifest
}

function Assert-ReleaseIdentity(
    [string]$Root,
    [hashtable]$Manifest,
    [hashtable]$ExpectedChannel
) {
    $buildInfoPath = Get-PayloadPath -Root $Root -RelativePath "輔系統/BUILD_INFO.txt"
    $latestPath = Get-PayloadPath -Root $Root -RelativePath "LATEST.txt"
    $channelPath = Get-PayloadPath -Root $Root -RelativePath $ChannelRelativePath
    $buildInfo = Read-KeyValueFile -Path $buildInfoPath -DisplayName "BUILD_INFO.txt"
    $latest = Read-KeyValueFile -Path $latestPath -DisplayName "LATEST.txt"
    $channel = Read-KeyValueFile -Path $channelPath -DisplayName "UPDATE_CHANNEL.txt"

    Require-MetadataValue $buildInfo "product" ([string][char]0x8F14) "BUILD_INFO.txt"
    Require-MetadataValue $buildInfo "technical_name" "FLASH" "BUILD_INFO.txt"
    if (
        -not $buildInfo.ContainsKey("milestone") -or
        $buildInfo["milestone"] -notin @("SP1", "SP2", "SP3")
    ) {
        throw "BUILD_INFO.txt 的 milestone 不受支援。"
    }
    if (
        $ExpectedChannel["build_kind"] -eq "sp1_release" -and
        $buildInfo["milestone"] -ne "SP1"
    ) {
        throw "SP1 獨立版發布必須使用 SP1 里程碑。"
    }
    if (
        $ExpectedChannel["build_kind"] -eq "main_release" -and
        $buildInfo["milestone"] -ne "SP3"
    ) {
        throw "完整累積版發布必須使用 SP3 里程碑。"
    }
    foreach ($key in @("source_branch", "build_kind", "publish_target")) {
        Require-MetadataValue $buildInfo $key $ExpectedChannel[$key] "BUILD_INFO.txt"
        Require-MetadataValue $channel $key $ExpectedChannel[$key] "UPDATE_CHANNEL.txt"
    }
    Require-MetadataValue $channel "release_branch" $ExpectedChannel["release_branch"] "UPDATE_CHANNEL.txt"
    Require-MetadataValue $latest "branch" $ExpectedChannel["source_branch"] "LATEST.txt"
    if ($ExpectedChannel["build_kind"] -eq "main_release") {
        Require-MetadataValue $buildInfo "event_name" "push" "BUILD_INFO.txt"
        Require-MetadataValue $buildInfo "source_ref" "refs/heads/main" "BUILD_INFO.txt"
    }
    else {
        if ($buildInfo["event_name"] -notin @("push", "workflow_dispatch")) {
            throw "BUILD_INFO.txt 的 event_name 必須是 push 或 workflow_dispatch。"
        }
        Require-MetadataValue `
            $buildInfo `
            "source_ref" `
            "refs/heads/sp1/completion-2026-07-25" `
            "BUILD_INFO.txt"
    }

    foreach ($requiredKey in @("commit", "run_id", "sha256")) {
        if (
            -not $buildInfo.ContainsKey($requiredKey) -or
            [string]::IsNullOrWhiteSpace($buildInfo[$requiredKey])
        ) {
            throw "BUILD_INFO.txt 缺少欄位：$requiredKey"
        }
    }
    foreach ($requiredKey in @("commit", "run_id", "updated_utc")) {
        if (
            -not $latest.ContainsKey($requiredKey) -or
            [string]::IsNullOrWhiteSpace($latest[$requiredKey])
        ) {
            throw "LATEST.txt 缺少欄位：$requiredKey"
        }
    }

    if ($buildInfo["commit"] -notmatch "^[0-9a-fA-F]{40}$") {
        throw "BUILD_INFO.txt 的 commit 格式不正確。"
    }
    if ($latest["commit"] -ne $buildInfo["commit"]) {
        throw "LATEST.txt 與 BUILD_INFO.txt 的來源 commit 不一致。"
    }
    if ($latest["run_id"] -ne $buildInfo["run_id"]) {
        throw "LATEST.txt 與 BUILD_INFO.txt 的 run_id 不一致。"
    }
    if ($buildInfo["sha256"].ToLowerInvariant() -ne $Manifest["FLASH.exe"]) {
        throw "BUILD_INFO.txt 與完整 manifest 的 FLASH.exe 雜湊不一致。"
    }
    return $buildInfo
}

function Convert-ToUrlPath([string]$RelativePath) {
    $encodedSegments = @()
    foreach ($segment in ($RelativePath -split "/")) {
        $encodedSegments += [Uri]::EscapeDataString($segment)
    }
    return ($encodedSegments -join "/")
}

function Resolve-ReleaseCommit {
    if (-not [string]::IsNullOrWhiteSpace($TestNetworkFailureKind)) {
        $testMessages = @{
            offline = "目前無法連上網路，請確認網路連線後再試。"
            dns = "無法解析網路位址，請檢查網路或 DNS 設定。"
            tls = "安全連線驗證失敗，請確認系統時間與網路憑證。"
            "403" = "GitHub 拒絕存取（403），請稍後再試。"
            "404" = "找不到更新檔案（404），目前版本沒有被修改。"
            "429" = "下載過於頻繁（429），請稍後再試。"
            "500" = "更新伺服器暫時無法使用，請稍後再試。"
            timeout = "連線逾時，請確認網路後再試。"
            github_limit = "GitHub 下載次數已達限制，請稍後再試。"
        }
        throw $testMessages[$TestNetworkFailureKind]
    }
    if ($TestTransientFailuresBeforeSuccess -gt 0) {
        $resolved = Invoke-WithNetworkRetry "測試暫時性網路錯誤" {
            if ($script:TransientFailuresRemaining -gt 0) {
                $script:TransientFailuresRemaining--
                $testException = [System.Net.WebException]::new(
                    "測試暫時性逾時",
                    [System.Net.WebExceptionStatus]::Timeout
                )
                throw $testException
            }
            return $ResolvedReleaseCommit
        }
        if ($resolved -notmatch "^[0-9a-fA-F]{40}$") {
            throw "測試暫時性網路錯誤沒有回傳有效 commit。"
        }
        return $resolved.ToLowerInvariant()
    }
    if ($UsingLocalSource) {
        if ($ResolvedReleaseCommit -notmatch "^[0-9a-fA-F]{40}$") {
            throw "本機測試來源必須指定 40 碼的固定發布 commit。"
        }
        return $ResolvedReleaseCommit.ToLowerInvariant()
    }

    Write-Step "解析 $ReleaseBranch 的固定發布版本。"
    $headers = @{
        "Accept" = "application/vnd.github+json"
        "User-Agent" = "FLASH-Windows-Updater"
    }
    $apiUrl = "https://api.github.com/repos/$Repo/commits/$ReleaseBranch"
    $response = Invoke-WithNetworkRetry "解析固定發布版本" {
        Invoke-RestMethod `
            -Uri $apiUrl `
            -Headers $headers `
            -Method Get `
            -TimeoutSec $ConnectionTimeoutSeconds `
            -UseBasicParsing
    }
    $commit = [string]$response.sha
    if ($commit -notmatch "^[0-9a-fA-F]{40}$") {
        throw "GitHub 沒有回傳有效的 $ReleaseBranch commit。"
    }
    return $commit.ToLowerInvariant()
}

function Copy-OrDownloadPayload(
    [string]$RelativePath,
    [string]$TargetPath,
    [string]$ReleaseCommit
) {
    $targetParent = Split-Path -Parent $TargetPath
    New-Item -ItemType Directory -Force -Path $targetParent | Out-Null
    Write-Step "取得：$RelativePath"

    if ($UsingLocalSource) {
        $sourceRoot = [IO.Path]::GetFullPath($ReleaseSourceDirectory)
        $sourcePath = Get-PayloadPath -Root $sourceRoot -RelativePath $RelativePath
        Require-File $sourcePath
        Copy-Item -LiteralPath $sourcePath -Destination $TargetPath -Force
    }
    else {
        $urlPath = Convert-ToUrlPath -RelativePath $RelativePath
        $url = "https://raw.githubusercontent.com/$Repo/$ReleaseCommit/$urlPath"
        Invoke-WithNetworkRetry "下載 $RelativePath" {
            Invoke-WebRequest `
                -Uri $url `
                -OutFile $TargetPath `
                -TimeoutSec $DownloadTimeoutSeconds `
                -UseBasicParsing
        } | Out-Null
    }
    Require-File $TargetPath
}

function Invoke-StagedVerifier([string]$Root, [string]$Description) {
    $verifierPath = Get-PayloadPath -Root $Root -RelativePath "輔系統/verify_windows_release.ps1"
    Require-File $verifierPath
    Write-Step "$Description：執行 verify_windows_release.ps1 -NoLaunch。"
    & $verifierPath -NoLaunch
}

function Install-FileAtomically(
    [string]$RelativePath,
    [string]$SourcePath,
    [int]$Sequence
) {
    $targetPath = Get-PayloadPath -Root $InstallDir -RelativePath $RelativePath
    $targetParent = Split-Path -Parent $targetPath
    New-Item -ItemType Directory -Force -Path $targetParent | Out-Null

    $temporaryTarget = Join-Path $targetParent (
        ([IO.Path]::GetFileName($targetPath)) + ".flash-new-" + $TransactionId
    )
    $null = $TemporaryTargetPaths.Add($temporaryTarget)
    Copy-Item -LiteralPath $SourcePath -Destination $temporaryTarget -Force

    $existed = Test-Path -LiteralPath $targetPath -PathType Leaf
    $backupPath = Get-PayloadPath -Root $BackupRoot -RelativePath $RelativePath
    if ($existed) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $backupPath) | Out-Null
        [IO.File]::Replace($temporaryTarget, $targetPath, $backupPath, $true)
    }
    else {
        [IO.File]::Move($temporaryTarget, $targetPath)
    }

    $record = [PSCustomObject]@{
        RelativePath = $RelativePath
        TargetPath = $targetPath
        BackupPath = $backupPath
        Existed = $existed
    }
    $null = $AppliedRecords.Add($record)
    Write-Step "已套用：$RelativePath"

    if ($TestFailAfterReplacement -gt 0 -and $Sequence -eq $TestFailAfterReplacement) {
        throw "測試指定在第 $Sequence 個檔案後中斷。"
    }
}

function Restore-AppliedFiles {
    if ($AppliedRecords.Count -eq 0) {
        Write-Step "尚未修改正式安裝內容，不需要回復。"
        return
    }

    Write-Step "開始回復原本安裝內容。"
    $rollbackSequence = 0
    for ($index = $AppliedRecords.Count - 1; $index -ge 0; $index--) {
        $record = $AppliedRecords[$index]
        $rollbackSequence++
        if (
            $TestFailDuringRollbackAt -gt 0 -and
            $rollbackSequence -eq $TestFailDuringRollbackAt
        ) {
            throw "測試指定在第 $rollbackSequence 個回復檔案前中斷。"
        }
        if ($record.Existed) {
            Require-File $record.BackupPath
            if (Test-Path -LiteralPath $record.TargetPath -PathType Leaf) {
                $discardPath = $record.TargetPath + ".flash-failed-" + $TransactionId
                $null = $TemporaryTargetPaths.Add($discardPath)
                [IO.File]::Replace(
                    $record.BackupPath,
                    $record.TargetPath,
                    $discardPath,
                    $true
                )
                Remove-Item -LiteralPath $discardPath -Force -ErrorAction SilentlyContinue
            }
            else {
                [IO.File]::Move($record.BackupPath, $record.TargetPath)
            }
        }
        elseif (Test-Path -LiteralPath $record.TargetPath -PathType Leaf) {
            Remove-Item -LiteralPath $record.TargetPath -Force
        }
        Write-Step "已回復：$($record.RelativePath)"
    }
    Write-Step "回復完成；正式安裝內容已還原。"
}

try {
    New-Item -ItemType Directory -Force -Path $InstallDir, $SystemDir | Out-Null
    if (-not (Test-Path -LiteralPath $LogPath -PathType Leaf)) {
        New-Item -ItemType File -Force -Path $LogPath | Out-Null
    }

    try {
        $LockStream = [IO.File]::Open(
            $LockPath,
            [IO.FileMode]::OpenOrCreate,
            [IO.FileAccess]::ReadWrite,
            [IO.FileShare]::None
        )
    }
    catch {
        throw "另一個更新程序正在執行；本次更新已安全停止。"
    }

    Write-Step "已取得單一更新鎖。"
    if ($TestHoldLockMilliseconds -gt 0) {
        Start-Sleep -Milliseconds $TestHoldLockMilliseconds
    }

    $installedChannel = Read-InstalledUpdateChannel
    Assert-InstalledMigrationIdentity $installedChannel
    $updateChannel = Resolve-TargetUpdateChannel $installedChannel
    $ReleaseBranch = $updateChannel["release_branch"]
    Write-Step "固定更新來源：$ReleaseBranch"

    if (-not $UsingLocalSource) {
        $running = Get-Process -Name "FLASH" -ErrorAction SilentlyContinue
        if ($running) {
            throw "輔正在執行，請先關閉後再更新。"
        }
    }

    New-Item -ItemType Directory -Force -Path $StageRoot, $BackupRoot | Out-Null
    Write-Step "開始更新輔；正式安裝尚未修改。"
    $releaseCommit = Resolve-ReleaseCommit
    Write-Step "固定發布版本：$releaseCommit"

    foreach ($relativePath in $DownloadPaths) {
        $targetPath = Get-PayloadPath -Root $StageRoot -RelativePath $relativePath
        Copy-OrDownloadPayload `
            -RelativePath $relativePath `
            -TargetPath $targetPath `
            -ReleaseCommit $releaseCommit
    }

    Write-Step "逐檔核對完整 SHA-256 manifest。"
    $manifest = Read-AndVerifyManifest -Root $StageRoot
    $buildInfo = Assert-ReleaseIdentity `
        -Root $StageRoot `
        -Manifest $manifest `
        -ExpectedChannel $updateChannel
    Invoke-StagedVerifier -Root $StageRoot -Description "安裝前驗證"

    $migratingChannel = (
        $installedChannel["release_branch"] -ne
        $updateChannel["release_branch"]
    )
    foreach ($fixedIdentityPath in $FixedIdentityPaths) {
        if ($migratingChannel) {
            continue
        }
        $installedIdentity = Get-PayloadPath -Root $InstallDir -RelativePath $fixedIdentityPath
        $stagedIdentity = Get-PayloadPath -Root $StageRoot -RelativePath $fixedIdentityPath
        Require-File $installedIdentity
        $installedIdentityHash = Get-Sha256Hex -Path $installedIdentity
        $stagedIdentityHash = Get-Sha256Hex -Path $stagedIdentity
        if ($installedIdentityHash -ne $stagedIdentityHash) {
            throw "固定更新身分檔案版本不相容：$fixedIdentityPath；未修改任何檔案，請改用完整安裝包更新。"
        }
    }

    Write-Step "所有安裝前檢查通過，開始交易式套用。"
    $sequence = 0
    $installPaths = (
        $PayloadPaths |
        Where-Object {
            $migratingChannel -or $FixedIdentityPaths -notcontains $_
        }
    )
    foreach ($relativePath in $installPaths) {
        $sequence++
        $sourcePath = Get-PayloadPath -Root $StageRoot -RelativePath $relativePath
        Install-FileAtomically `
            -RelativePath $relativePath `
            -SourcePath $sourcePath `
            -Sequence $sequence
    }
    $sequence++
    $manifestSource = Get-PayloadPath -Root $StageRoot -RelativePath $ManifestRelativePath
    Install-FileAtomically `
        -RelativePath $ManifestRelativePath `
        -SourcePath $manifestSource `
        -Sequence $sequence

    Invoke-StagedVerifier -Root $InstallDir -Description "安裝後驗證"

    $installedInfo = Read-KeyValueFile `
        -Path (Get-PayloadPath -Root $InstallDir -RelativePath "輔系統/BUILD_INFO.txt") `
        -DisplayName "已安裝的 BUILD_INFO.txt"
    if ($installedInfo["commit"] -ne $buildInfo["commit"]) {
        throw "安裝後來源 commit 與下載版本不一致。"
    }

    $UpdateSucceeded = $true
    $ExitCode = 0
    Write-Step "更新成功；全部檔案已套用並通過再次驗證。"
    Write-Step "來源 commit：$($buildInfo['commit'])"
    Write-Step "發布 commit：$releaseCommit"
    Write-Step "已保留原本桌面捷徑的名稱與圖示。"
    Write-Host ""
    Write-Host "更新完成，可以直接使用桌面的「輔」或「更新輔」。" -ForegroundColor Green
}
catch {
    $failureMessage = $_.Exception.Message
    Write-Step "更新失敗：$failureMessage"
    Write-Host ""
    Write-Host "更新失敗：$failureMessage" -ForegroundColor Red

    try {
        Restore-AppliedFiles
    }
    catch {
        $ExitCode = 2
        $PreserveTransaction = $true
        Write-Step "回復失敗：$($_.Exception.Message)"
        Write-Step "已保留救援資料：$TransactionRoot"
        Write-Host "回復未完整完成，請保留更新紀錄並改用完整安裝包。" -ForegroundColor Red
        Write-Host "救援資料：$TransactionRoot" -ForegroundColor Yellow
    }
    Write-Host "紀錄位置：$LogPath"
}
finally {
    foreach ($temporaryTarget in $TemporaryTargetPaths) {
        if (Test-Path -LiteralPath $temporaryTarget -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryTarget -Force -ErrorAction SilentlyContinue
        }
    }

    if ($LockStream) {
        $LockStream.Dispose()
        $LockStream = $null
        Remove-Item -LiteralPath $LockPath -Force -ErrorAction SilentlyContinue
    }

    if (
        -not $PreserveTransaction -and
        (Test-Path -LiteralPath $TransactionRoot -PathType Container)
    ) {
        $resolvedTransaction = [IO.Path]::GetFullPath(
            (Resolve-Path -LiteralPath $TransactionRoot).Path
        )
        $resolvedBase = [IO.Path]::GetFullPath($TransactionBase).TrimEnd(
            [IO.Path]::DirectorySeparatorChar
        )
        if (
            $resolvedTransaction.StartsWith(
                $resolvedBase + [IO.Path]::DirectorySeparatorChar,
                [StringComparison]::OrdinalIgnoreCase
            )
        ) {
            Remove-Item -LiteralPath $resolvedTransaction -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    if ($UpdateSucceeded) {
        Write-Host "更新紀錄：$LogPath"
    }
}

exit $ExitCode
