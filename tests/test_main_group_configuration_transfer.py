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
    import_source = source[
        source.index("    def import_group_configuration("):
        source.index("    def group_launch_hotkey(")
    ]
    assert "candidate.import_configuration(" in import_source
    assert "finish_group_management(mutation)" in import_source
    assert "group_configuration_service.import_configuration" not in import_source
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
    group_hotkey_source = source[
        source.index("    def change_group_launch_hotkey("):
        source.index("    def save_feature_card_settings(")
    ]
    feature_hotkey_source = source[
        source.index("    def change_feature_hotkey("):
        source.index("    def change_ui_theme(")
    ]
    assert (
        "feature_card_settings_batch_service.change_group_launch_hotkey("
        in group_hotkey_source
    )
    assert "group_configuration_service.set_launch_hotkey(" not in group_hotkey_source
    assert (
        "feature_card_settings_batch_service.change_feature_hotkey("
        in feature_hotkey_source
    )
    assert "configured_feature_hotkeys[feature] =" not in feature_hotkey_source
    assert "config.set(" not in feature_hotkey_source
    assert "GroupLaunchHotkeyMonitor(" in source
    assert (
        "group_launch_hotkey_provider=group_launch_hotkey"
        in source
    )
    assert (
        "on_group_launch_hotkey_change=change_group_launch_hotkey"
        in source
    )
