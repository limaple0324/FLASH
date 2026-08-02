from pathlib import Path


def test_smart_reconnect_verifier_defaults_to_read_only_and_requires_clear_flag():
    source = Path("scripts/verify_smart_reconnect_sp1.py").read_text(
        encoding="utf-8"
    )

    assert "--execute-approved-reconnect" in source
    assert "controller.check_connection()" in source
    assert "controller.reconnect()" in source
    assert "controller.set_execution_enabled(True)" in source
    assert "--watch-seconds requires --execute-approved-reconnect" in source
    assert '"input_sent"' in source
    assert '"monitor_cycles"' in source
    assert '"captured_pixels_persisted"' not in source


def test_input_verifier_holds_minimized_windows_before_restore():
    source = Path("scripts/verify_input_sync_sp1.py").read_text(
        encoding="utf-8"
    )

    assert "MINIMIZED_INPUT_SETTLE_SECONDS = 2.0" in source
    assert 'payload["minimized_hold_seconds"]' in source
    assert "time.sleep(MINIMIZED_INPUT_SETTLE_SECONDS)" in source
