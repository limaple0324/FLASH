from pathlib import Path


def test_group_configuration_transfer_is_wired_to_current_group_page() -> None:
    source = Path("main.py").read_text(encoding="utf-8")

    assert "def export_group_configuration()" in source
    assert "def import_group_configuration()" in source
    assert 'title="匯出組別設定"' in source
    assert 'title="匯入組別設定"' in source
    assert (
        "group_configuration_service.export_configuration" in source
    )
    assert (
        "group_configuration_service.import_configuration" in source
    )
    assert (
        "on_export_group_configuration=export_group_configuration"
        in source
    )
    assert (
        "on_import_group_configuration=import_group_configuration"
        in source
    )
    assert "def reorder_group_entries(" in source
    assert (
        "on_reorder_group_entries=reorder_group_entries"
        in source
    )
    stop_start = source.index("def stop_all_managed_games(")
    gate_call = source.index(
        "stop_group_automation_for_configuration_change()",
        stop_start,
    )
    launch_stop = source.index(
        "group_window_launch_service.stop(",
        stop_start,
    )
    managed_stop = source.index(
        "group_window_launch_service.start_stop_all(",
        stop_start,
    )
    assert gate_call < launch_stop < managed_stop
    assert "on_stop_all_managed_games=stop_all_managed_games" in source
    assert "def change_group_launch_hotkey(" in source
    assert "GroupLaunchHotkeyMonitor(" in source
    assert (
        "group_launch_hotkey_provider=group_launch_hotkey"
        in source
    )
    assert (
        "on_group_launch_hotkey_change=change_group_launch_hotkey"
        in source
    )
