param(
    [ValidateRange(0.01, 168.0)]
    [double]$DurationHours = 8.0,

    [ValidateRange(5, 3600)]
    [int]$IntervalSeconds = 60,

    [ValidateRange(1, 100)]
    [int]$ExpectedGameWindows = 14,

    [ValidateRange(0, 100)]
    [int]$ExpectedLoggedInWindows = 12,

    [ValidateRange(0, 100)]
    [int]$ExpectedIntentionalLoginWindows = 2,

    [string]$ProductDataRoot = "",

    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"

if (($ExpectedLoggedInWindows + $ExpectedIntentionalLoginWindows) -ne $ExpectedGameWindows) {
    throw "Expected logged-in and intentional-login window counts must equal the expected game window count."
}

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $repositoryRoot = Split-Path -Parent $PSScriptRoot
    $OutputDirectory = Join-Path $repositoryRoot "dist\acceptance"
}
if ([string]::IsNullOrWhiteSpace($ProductDataRoot)) {
    $ProductDataRoot = Join-Path $env:LOCALAPPDATA "FLASH"
}

$resolvedOutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
[IO.Directory]::CreateDirectory($resolvedOutputDirectory) | Out-Null
$resolvedProductDataRoot = [IO.Path]::GetFullPath($ProductDataRoot)
$settingsPath = Join-Path $resolvedProductDataRoot "config\settings.json"
$productLogPath = Join-Path $resolvedProductDataRoot "logs\flash.log"
$reconnectStatePath = Join-Path $resolvedProductDataRoot "data\smart_reconnect_state.json"
$operationRecordsPath = Join-Path $resolvedProductDataRoot "data\operation_records.json"
$productDataDirectory = Join-Path $resolvedProductDataRoot "data"

$startedAt = Get-Date
$runId = $startedAt.ToString("yyyyMMdd-HHmmss")
$samplesPath = Join-Path $resolvedOutputDirectory "windows-stability-$runId.csv"
$eventsPath = Join-Path $resolvedOutputDirectory "windows-stability-$runId-events.txt"
$summaryPath = Join-Path $resolvedOutputDirectory "windows-stability-$runId-summary.json"
$completionPath = Join-Path $resolvedOutputDirectory "windows-stability-$runId-complete.txt"
$deadline = $startedAt.AddHours($DurationHours)

$samples = [Collections.Generic.List[object]]::new()
$events = [Collections.Generic.List[string]]::new()
$seenGameProcessStarts = [Collections.Generic.HashSet[string]]::new(
    [StringComparer]::OrdinalIgnoreCase
)
$seenProductProcessStarts = [Collections.Generic.HashSet[string]]::new(
    [StringComparer]::OrdinalIgnoreCase
)
$lastGameCount = $null
$lastProductCount = $null
$unexpectedGameSamples = 0
$unexpectedProductSamples = 0
$productUnresponsiveSamples = 0
$smartReconnectDisabledSamples = 0
$settingsReadFailures = 0
$newProductLogBytes = 0L
$newProductErrorMarkers = 0
$reconnectDisconnectEvents = 0
$reconnectProgressEvents = 0
$unresolvedReconnectAlerts = 0
$pendingReconnectDetectedAt = $null
$pendingReconnectAlerted = $false
$productLogOffset = if ([IO.File]::Exists($productLogPath)) {
    [IO.FileInfo]::new($productLogPath).Length
}
else {
    0L
}

function Add-StabilityEvent {
    param([Parameter(Mandatory = $true)][string]$Message)

    $line = "[{0}] {1}" -f (Get-Date).ToString("yyyy-MM-dd HH:mm:ss"), $Message
    $events.Add($line)
    [IO.File]::AppendAllText(
        $eventsPath,
        $line + [Environment]::NewLine,
        [Text.UTF8Encoding]::new($false)
    )
}

function Get-ProcessStartKey {
    param([Parameter(Mandatory = $true)]$Process)

    try {
        return "{0}|{1:O}" -f $Process.Id, $Process.StartTime.ToUniversalTime()
    }
    catch {
        return "{0}|unknown" -f $Process.Id
    }
}

function Get-ResponsiveCount {
    param([Parameter(Mandatory = $true)][object[]]$Processes)

    $count = 0
    foreach ($process in $Processes) {
        try {
            if ($process.Responding) {
                $count++
            }
        }
        catch {
            # A process can close between enumeration and inspection.
        }
    }
    return $count
}

function Get-WorkingSetMegabytes {
    param([Parameter(Mandatory = $true)][object[]]$Processes)

    $bytes = 0L
    foreach ($process in $Processes) {
        try {
            $bytes += [int64]$process.WorkingSet64
        }
        catch {
            # A process can close between enumeration and inspection.
        }
    }
    return [Math]::Round($bytes / 1MB, 2)
}

function Get-CpuSeconds {
    param([Parameter(Mandatory = $true)][object[]]$Processes)

    $seconds = 0.0
    foreach ($process in $Processes) {
        try {
            $seconds += [double]$process.CPU
        }
        catch {
            # A process can close between enumeration and inspection.
        }
    }
    return [Math]::Round($seconds, 2)
}

function Get-AppFileState {
    param([Parameter(Mandatory = $true)][string]$Path)

    try {
        $file = [IO.FileInfo]::new($Path)
        if (-not $file.Exists) {
            return [pscustomobject]@{
                exists = $false
                length = 0L
                last_write = ""
            }
        }
        return [pscustomobject]@{
            exists = $true
            length = [int64]$file.Length
            last_write = $file.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss")
        }
    }
    catch {
        return [pscustomobject]@{
            exists = $false
            length = 0L
            last_write = ""
        }
    }
}

function Get-SmartReconnectSettings {
    try {
        if (-not [IO.File]::Exists($settingsPath)) {
            throw "Settings file is missing."
        }
        $settings = Get-Content -LiteralPath $settingsPath -Raw -Encoding utf8 |
            ConvertFrom-Json
        return [pscustomobject]@{
            read_ok = $true
            enabled = $settings.smart_reconnect_enabled -eq $true
            consent = $settings.smart_reconnect_consent_v1 -eq $true
        }
    }
    catch {
        return [pscustomobject]@{
            read_ok = $false
            enabled = $null
            consent = $null
        }
    }
}

function Read-NewProductLogSummary {
    $result = [ordered]@{
        bytes = 0L
        error_markers = 0
        reconnect_disconnect_events = 0
        reconnect_progress_events = 0
        reconnect_terminal_events = 0
    }
    try {
        if (-not [IO.File]::Exists($productLogPath)) {
            return [pscustomobject]$result
        }
        $length = [IO.FileInfo]::new($productLogPath).Length
        if ($length -lt $script:productLogOffset) {
            Add-StabilityEvent "Product log rotated or shrank; monitoring restarted at the new file beginning."
            $script:productLogOffset = 0L
        }
        if ($length -eq $script:productLogOffset) {
            return [pscustomobject]$result
        }

        $stream = [IO.File]::Open(
            $productLogPath,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
        )
        try {
            [void]$stream.Seek($script:productLogOffset, [IO.SeekOrigin]::Begin)
            $reader = [IO.StreamReader]::new(
                $stream,
                [Text.UTF8Encoding]::new($false),
                $true,
                4096,
                $true
            )
            try {
                $newText = $reader.ReadToEnd()
            }
            finally {
                $reader.Dispose()
            }
        }
        finally {
            $stream.Dispose()
        }

        $result.bytes = $length - $script:productLogOffset
        $result.error_markers = @(
            [regex]::Matches(
                $newText,
                "(?im)^.*(?:ERROR|CRITICAL|Traceback).*$"
            )
        ).Count
        foreach ($line in ($newText -split "\r?\n")) {
            if ($line -notmatch "Smart reconnect state changed;") {
                continue
            }
            if (
                $line -match "states=[^;]*disconnected:[1-9][0-9]*" -and
                $line -notmatch "(?:clicked|restarted)=[1-9][0-9]*"
            ) {
                $result.reconnect_disconnect_events++
            }
            if (
                $line -match "(?:clicked|restarted)=[1-9][0-9]*" -or
                $line -match "code=reconnect\.progressed"
            ) {
                $result.reconnect_progress_events++
            }
            if ($line -match "code=reconnect\.connected") {
                $result.reconnect_terminal_events++
            }
        }
        $script:productLogOffset = $length
    }
    catch {
        Add-StabilityEvent "Failed to read appended product log content."
    }
    return [pscustomobject]$result
}

function Get-DailyRecordState {
    try {
        if (-not [IO.Directory]::Exists($productDataDirectory)) {
            return [pscustomobject]@{
                exists = $false
                length = 0L
                last_write = ""
            }
        }
        $today = Get-Date -Format "yyyy-MM-dd"
        $record = Get-ChildItem -LiteralPath $productDataDirectory -File -Recurse |
            Where-Object { $_.Extension -eq ".txt" } |
            Where-Object { $_.Name -like "*$today*" } |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
        if ($null -eq $record) {
            return [pscustomobject]@{
                exists = $false
                length = 0L
                last_write = ""
            }
        }
        return [pscustomobject]@{
            exists = $true
            length = [int64]$record.Length
            last_write = $record.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss")
        }
    }
    catch {
        return [pscustomobject]@{
            exists = $false
            length = 0L
            last_write = ""
        }
    }
}

function Save-StabilitySamples {
    $samples |
        Export-Csv -LiteralPath $samplesPath -NoTypeInformation -Encoding utf8
}

Add-StabilityEvent (
    "Started background stability acceptance: expected game windows {0}, logged in {1}, intentionally logged out {2}, duration {3} hours." -f
    $ExpectedGameWindows,
    $ExpectedLoggedInWindows,
    $ExpectedIntentionalLoginWindows,
    $DurationHours
)
Add-StabilityEvent "This monitor reads process and window state only and sends no keyboard, mouse, or focus input."

try {
    while ((Get-Date) -lt $deadline) {
        $sampledAt = Get-Date
        $gameProcesses = @(
            Get-Process -Name "flashplayer11-5_sa_win_32" -ErrorAction SilentlyContinue
        )
        $gameWindowProcesses = @(
            $gameProcesses | Where-Object { $_.MainWindowHandle -ne 0 }
        )
        $productProcesses = @(
            Get-Process -Name "FLASH" -ErrorAction SilentlyContinue
        )
        $productWindowProcesses = @(
            $productProcesses | Where-Object { $_.MainWindowHandle -ne 0 }
        )
        $productResponsive = Get-ResponsiveCount $productWindowProcesses
        $reconnectSettings = Get-SmartReconnectSettings
        $newLog = Read-NewProductLogSummary
        $productLogState = Get-AppFileState $productLogPath
        $reconnectState = Get-AppFileState $reconnectStatePath
        $operationRecordsState = Get-AppFileState $operationRecordsPath
        $dailyRecordState = Get-DailyRecordState

        foreach ($process in $gameProcesses) {
            [void]$seenGameProcessStarts.Add((Get-ProcessStartKey $process))
        }
        foreach ($process in $productProcesses) {
            [void]$seenProductProcessStarts.Add((Get-ProcessStartKey $process))
        }

        $gameCount = $gameWindowProcesses.Count
        $productCount = $productWindowProcesses.Count
        if ($gameCount -ne $ExpectedGameWindows) {
            $unexpectedGameSamples++
        }
        if ($productCount -ne 1) {
            $unexpectedProductSamples++
        }
        if ($productCount -ne $productResponsive) {
            $productUnresponsiveSamples++
        }
        if (-not $reconnectSettings.read_ok) {
            $settingsReadFailures++
        }
        elseif (
            -not $reconnectSettings.enabled -or
            -not $reconnectSettings.consent
        ) {
            $smartReconnectDisabledSamples++
        }
        $newProductLogBytes += [int64]$newLog.bytes
        $newProductErrorMarkers += [int]$newLog.error_markers
        $reconnectDisconnectEvents += [int]$newLog.reconnect_disconnect_events
        $reconnectProgressEvents += [int]$newLog.reconnect_progress_events
        if ($newLog.reconnect_disconnect_events -gt 0) {
            $pendingReconnectDetectedAt = $sampledAt
            $pendingReconnectAlerted = $false
        }
        if (
            $newLog.reconnect_progress_events -gt 0 -or
            $newLog.reconnect_terminal_events -gt 0
        ) {
            $pendingReconnectDetectedAt = $null
            $pendingReconnectAlerted = $false
        }
        if (
            $null -ne $pendingReconnectDetectedAt -and
            -not $pendingReconnectAlerted -and
            ($sampledAt - $pendingReconnectDetectedAt).TotalSeconds -ge
                $IntervalSeconds
        ) {
            $unresolvedReconnectAlerts++
            $pendingReconnectAlerted = $true
            Add-StabilityEvent (
                "ERROR Smart reconnect detected a disconnected window but did not start an action within one monitor interval."
            )
        }

        if ($null -ne $lastGameCount -and $lastGameCount -ne $gameCount) {
            Add-StabilityEvent (
                "Game window count changed from {0} to {1}." -f $lastGameCount, $gameCount
            )
        }
        if ($null -ne $lastProductCount -and $lastProductCount -ne $productCount) {
            Add-StabilityEvent (
                "Product window count changed from {0} to {1}." -f $lastProductCount, $productCount
            )
        }
        $lastGameCount = $gameCount
        $lastProductCount = $productCount

        $samples.Add(
            [pscustomobject]@{
                timestamp = $sampledAt.ToString("yyyy-MM-dd HH:mm:ss")
                elapsed_seconds = [Math]::Round(
                    ($sampledAt - $startedAt).TotalSeconds,
                    2
                )
                expected_game_windows = $ExpectedGameWindows
                expected_logged_in_windows = $ExpectedLoggedInWindows
                expected_intentional_login_windows = $ExpectedIntentionalLoginWindows
                game_processes = $gameProcesses.Count
                game_windows = $gameCount
                game_responsive = Get-ResponsiveCount $gameWindowProcesses
                game_working_set_mb = Get-WorkingSetMegabytes $gameProcesses
                game_cpu_seconds = Get-CpuSeconds $gameProcesses
                product_processes = $productProcesses.Count
                product_windows = $productCount
                product_responsive = $productResponsive
                product_working_set_mb = Get-WorkingSetMegabytes $productProcesses
                product_cpu_seconds = Get-CpuSeconds $productProcesses
                smart_reconnect_settings_read_ok = $reconnectSettings.read_ok
                smart_reconnect_enabled = $reconnectSettings.enabled
                smart_reconnect_consent = $reconnectSettings.consent
                product_log_exists = $productLogState.exists
                product_log_size_bytes = $productLogState.length
                product_log_last_write = $productLogState.last_write
                product_log_new_bytes = $newLog.bytes
                product_log_new_error_markers = $newLog.error_markers
                reconnect_disconnect_events = $newLog.reconnect_disconnect_events
                reconnect_progress_events = $newLog.reconnect_progress_events
                reconnect_unresolved = (
                    $null -ne $pendingReconnectDetectedAt
                )
                reconnect_unresolved_alerts = $unresolvedReconnectAlerts
                reconnect_state_exists = $reconnectState.exists
                reconnect_state_size_bytes = $reconnectState.length
                reconnect_state_last_write = $reconnectState.last_write
                operation_records_exists = $operationRecordsState.exists
                operation_records_size_bytes = $operationRecordsState.length
                operation_records_last_write = $operationRecordsState.last_write
                daily_record_exists = $dailyRecordState.exists
                daily_record_size_bytes = $dailyRecordState.length
                daily_record_last_write = $dailyRecordState.last_write
            }
        )
        Save-StabilitySamples

        $remainingSeconds = [Math]::Max(
            0,
            [Math]::Floor(($deadline - (Get-Date)).TotalSeconds)
        )
        if ($remainingSeconds -le 0) {
            break
        }
        Start-Sleep -Seconds ([Math]::Min($IntervalSeconds, $remainingSeconds))
    }
}
finally {
    $finishedAt = Get-Date
    Save-StabilitySamples

    $gameCounts = @($samples | ForEach-Object { [int]$_.game_windows })
    $productCounts = @($samples | ForEach-Object { [int]$_.product_windows })
    $summary = [ordered]@{
        started_at = $startedAt.ToString("yyyy-MM-dd HH:mm:ss")
        finished_at = $finishedAt.ToString("yyyy-MM-dd HH:mm:ss")
        requested_duration_hours = $DurationHours
        actual_duration_seconds = [Math]::Round(
            ($finishedAt - $startedAt).TotalSeconds,
            2
        )
        interval_seconds = $IntervalSeconds
        expected_game_windows = $ExpectedGameWindows
        expected_logged_in_windows = $ExpectedLoggedInWindows
        expected_intentional_login_windows = $ExpectedIntentionalLoginWindows
        sample_count = $samples.Count
        minimum_game_windows = if ($gameCounts.Count) {
            ($gameCounts | Measure-Object -Minimum).Minimum
        }
        else {
            0
        }
        maximum_game_windows = if ($gameCounts.Count) {
            ($gameCounts | Measure-Object -Maximum).Maximum
        }
        else {
            0
        }
        minimum_product_windows = if ($productCounts.Count) {
            ($productCounts | Measure-Object -Minimum).Minimum
        }
        else {
            0
        }
        maximum_product_windows = if ($productCounts.Count) {
            ($productCounts | Measure-Object -Maximum).Maximum
        }
        else {
            0
        }
        unexpected_game_samples = $unexpectedGameSamples
        unexpected_product_samples = $unexpectedProductSamples
        product_unresponsive_samples = $productUnresponsiveSamples
        smart_reconnect_disabled_samples = $smartReconnectDisabledSamples
        settings_read_failures = $settingsReadFailures
        product_log_new_bytes = $newProductLogBytes
        product_log_new_error_markers = $newProductErrorMarkers
        reconnect_disconnect_events = $reconnectDisconnectEvents
        reconnect_progress_events = $reconnectProgressEvents
        reconnect_unresolved_alerts = $unresolvedReconnectAlerts
        distinct_game_process_starts = $seenGameProcessStarts.Count
        distinct_product_process_starts = $seenProductProcessStarts.Count
        samples_file = [IO.Path]::GetFileName($samplesPath)
        events_file = [IO.Path]::GetFileName($eventsPath)
    }
    $summaryJson = $summary | ConvertTo-Json -Depth 5
    [IO.File]::WriteAllText(
        $summaryPath,
        $summaryJson + [Environment]::NewLine,
        [Text.UTF8Encoding]::new($false)
    )
    [IO.File]::WriteAllText(
        $completionPath,
        (
            "Background stability acceptance completed.`r`n" +
            "Started: $($summary.started_at)`r`n" +
            "Finished: $($summary.finished_at)`r`n" +
            "Samples: $($summary.sample_count)`r`n" +
            "Game window range: $($summary.minimum_game_windows)-$($summary.maximum_game_windows)`r`n" +
            "Product window range: $($summary.minimum_product_windows)-$($summary.maximum_product_windows)`r`n"
        ),
        [Text.UTF8Encoding]::new($false)
    )
    Add-StabilityEvent "Background stability acceptance ended; summary and samples were saved."
}
