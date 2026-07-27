from pathlib import Path


def test_main_connects_confirmed_legacy_window_size_controls():
    source = (
        Path(__file__).resolve().parents[1] / "main.py"
    ).read_text(encoding="utf-8")

    assert "WindowSizeAdjustmentService(" in source
    assert "Win32WindowClientSizeBackend()" in source
    assert "on_read_main_window_size=read_main_window_size" in source
    assert "on_apply_group_window_size=apply_group_window_size" in source
    assert "on_apply_all_window_size=apply_all_window_size" in source
