from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "monitor_windows_stability.ps1"


def test_monitor_records_confirmed_mixed_window_baseline() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "[int]$ExpectedGameWindows = 14" in source
    assert "[int]$ExpectedLoggedInWindows = 12" in source
    assert "[int]$ExpectedIntentionalLoginWindows = 2" in source
    assert (
        "$ExpectedLoggedInWindows + $ExpectedIntentionalLoginWindows"
        in source
    )


def test_monitor_is_passive_and_does_not_capture_game_credentials() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    forbidden = (
        "SendInput",
        "SetForegroundWindow",
        "PostMessage",
        "SendMessage",
        "CommandLine",
        "Win32_Process",
        "WScript.Shell",
    )
    assert all(value not in source for value in forbidden)
    assert "sends no keyboard, mouse, or focus input" in source
    assert source.isascii()


def test_monitor_writes_samples_events_and_completion_summary() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "windows-stability-$runId.csv" in source
    assert "windows-stability-$runId-events.txt" in source
    assert "windows-stability-$runId-summary.json" in source
    assert "windows-stability-$runId-complete.txt" in source
    assert "Export-Csv" in source


def test_monitor_supervises_product_runtime_and_reconnect_state() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'Get-Process -Name "FLASH"' in source
    assert "product_responsive" in source
    assert "product_working_set_mb" in source
    assert "smart_reconnect_enabled" in source
    assert "smart_reconnect_consent_v1" in source
    assert "product_log_new_error_markers" in source
    assert "smart_reconnect_state.json" in source
    assert "operation_records.json" in source
    assert "daily_record_last_write" in source


def test_monitor_reports_disconnect_without_reconnect_progress() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "reconnect_disconnect_events" in source
    assert "reconnect_progress_events" in source
    assert "reconnect_unresolved" in source
    assert "reconnect_unresolved_alerts" in source
    assert (
        "did not start an action within one monitor interval"
        in source
    )
