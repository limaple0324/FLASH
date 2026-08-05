import ast
from pathlib import Path


def test_main_window_uses_home_view():
    source = Path("main.py").read_text(encoding="utf-8")

    assert "from ui.home import HomeView" in source
    assert "on_input_policy_change=change_input_policy" in source
    assert "on_keyboard_sync_change=change_keyboard_sync" in source
    assert "group_choices=group_choices" in source
    assert "on_group_change=change_group" in source
    assert "group_entries_provider=group_entries" in source
    assert "workspace_state=workspace_state" in source
    assert "workspace_state_provider=(" in source
    assert "workspace_service.snapshot" in source
    assert "build_windows_card_overlay_selection_coordinator" in source
    assert "CardExpiryMonitor" in source
    assert "on_card_display_seconds_update=update_card_display_seconds" in source
    assert "CharacterDetailWindow" not in source
    assert "home_view.show_character_detail(" in source
    assert "entry_id=entry_id" in source
    assert "CharacterNoteService" in source
    assert "auto_click_service.configure_direct_left_sync(" in source
    assert "pointer_sync_controller.send_click(" in source
    assert "WindowsSystemTrayBackend" in source
    assert 'window.bind("<Unmap>"' in source
    assert "tray_controller.stop(" in source
    assert "on_stop_all=stop_all_automation_from_tray" in source
    assert "on_exit=close_window" in source
    assert "def hide_window_to_tray()" in source
    assert 'window.protocol("WM_DELETE_WINDOW", hide_window_to_tray)' in source
    assert 'window.protocol("WM_DELETE_WINDOW", close_window)' not in source
    assert "stop_complete_background_services" in source
    assert "沒有假裝已退出" in source
    assert "include_source=True" in source
    assert "execution_guard=direct_auto_click_execution_allowed" in source
    assert "BackgroundImageService" in source
    assert "background_image_service.current_background()" in source
    assert "on_choose_background_source=choose_background_source" in source
    assert "on_prepare_background_image=prepare_background_image" in source
    assert "on_clear_background_image=clear_background_image" in source
    assert '("所有檔案", "*.*")' in source
    assert "on_capture_sync_base_point=capture_sync_base_point" in source
    assert "on_capture_sync_target_point=capture_sync_target_point" in source
    assert "on_save_sync_target_settings=save_sync_target_settings" in source
    assert "on_calibrate_role_id=calibrate_role_id" in source
    assert "on_read_role_id=read_role_id" in source
    assert "game_time_offset_ms=(" in source
    assert "clamp_time_offset_ms(config.get(GAME_TIME_OFFSET_MS_KEY, 0))" in source
    assert "game_time_auto_update=(" in source
    assert "bool(config.get(GAME_TIME_AUTO_UPDATE_KEY, True))" in source
    assert "game_time_snapshot_provider=(" in source
    assert "game_time_timed_click_service.snapshot" in source
    assert "on_game_time_settings_change=change_game_time_settings" in source
    assert "FeatureCardLayoutService(config)" in source
    assert "feature_card_layout_service.preference" in source
    assert "feature_card_layout_service.order_for" in source
    assert "feature_card_layout_service.set_collapsed" in source
    assert "feature_card_layout_service.reorder" in source
    assert "feature_card_layout_service.set_title" in source
    assert "FeatureCardSettingsBatchService(" in source
    assert "feature_card_settings_batch_service.save(" in source
    assert (
        "on_save_feature_card_settings=save_feature_card_settings"
        in source
    )
    assert "current_card_background" in source
    assert "on_save_card_background=save_card_background" in source
    assert "on_clear_card_background=clear_card_background" in source
    assert 'GROUP_ROLE_DETAILS_EXPANDED_KEY = "group_role_details_expanded"' in source
    assert "group_role_details_expanded_provider=(" in source
    assert "on_group_role_details_expanded_change=(" in source


def test_ungrouped_window_provider_is_resolved_before_home_view_is_created():
    source = Path("main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    create_main_window = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "create_main_window"
    )
    assignment = next(
        node
        for node in create_main_window.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "ungrouped_window_service"
    )
    assert isinstance(assignment.value, ast.Call)
    assert isinstance(assignment.value.func, ast.Attribute)
    assert isinstance(assignment.value.func.value, ast.Name)
    assert assignment.value.func.value.id == "AppContext"
    assert assignment.value.func.attr == "get"
    assert len(assignment.value.args) == 1
    assert isinstance(assignment.value.args[0], ast.Name)
    assert assignment.value.args[0].id == "UngroupedWindowService"

    home_view = next(
        node
        for node in ast.walk(create_main_window)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "HomeView"
    )
    provider = next(
        keyword.value
        for keyword in home_view.keywords
        if keyword.arg == "ungrouped_windows_provider"
    )
    assert isinstance(provider, ast.Attribute)
    assert isinstance(provider.value, ast.Name)
    assert provider.value.id == "ungrouped_window_service"
    assert provider.attr == "snapshot"
    assert assignment.lineno < home_view.lineno


def test_main_window_start_message_is_player_facing():
    source = Path("main.py").read_text(encoding="utf-8")

    assert "輔｜目前狀態" in source
    assert "format_start_status(status, paths)" in source
    assert "同步按鍵不會自行送出" in source
    assert "已確認快捷鍵清單" in source
    assert "未知畫面不會點擊" in source
    assert "啟動入口已接入首頁" not in source
    assert "RC-01" not in source


def test_main_window_title_is_player_facing():
    source = Path("main.py").read_text(encoding="utf-8")

    assert "APP_TITLE = PRODUCT_NAME" in source
    assert 'APP_TITLE = "輔｜FLASH SP1"' not in source


def test_selected_group_plan_returns_scoped_registered_role_metadata():
    source = Path("main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    build_window = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "create_main_window"
    )
    selected_group_plan = next(
        node
        for node in build_window.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "selected_group_plan"
    )

    selected_source = ast.get_source_segment(source, selected_group_plan)
    assert selected_source is not None
    assert "member.character_id" in selected_source
    assert "profiles.get(character_id)" in selected_source
    assert "profiles.get(target.entry_id)" not in selected_source
    assert "registered_level" in selected_source
    assert "importance" in selected_source
    assert not any(
        isinstance(node, ast.Return)
        and isinstance(node.value, ast.Name)
        and node.value.id == "plan"
        for node in build_window.body
    )


def test_role_id_read_uses_the_entry_safe_window_not_groupwide_failure():
    source = Path("main.py").read_text(encoding="utf-8")
    function_source = source[
        source.index("    def unique_window_for_group_entry("):
        source.index("    def refresh_group_sync_identity(")
    ]

    assert "target_window_contract_service.snapshot(" in function_source
    assert "and item.safe" in function_source
    assert "if snapshot.failure_codes:" not in function_source


def test_role_id_calibration_uses_only_the_visible_game_text():
    source = Path("main.py").read_text(encoding="utf-8")
    function_source = source[
        source.index("    def calibrate_role_id("):
        source.index("    def read_role_id(")
    ]

    assert "entry.display_name" not in function_source
    assert "role_id_template_service.calibrate(" in function_source
    assert "entry_id=entry_id" in function_source


def test_role_id_is_automatically_read_only_for_connected_missing_roles():
    source = Path("main.py").read_text(encoding="utf-8")
    function_source = source[
        source.index("    def auto_read_missing_role_id("):
        source.index("    player_habit_reminder_service = (")
    ]

    assert "if not entry.role_id.strip()" in function_source
    assert "window_info is None or window_info.minimized" in function_source
    assert "screen_state is not ReconnectScreenState.CONNECTED" in function_source
    assert "role_id_template_service.read_if_missing(" in function_source
    assert "existing_role_id=entry.role_id" in function_source
    assert "group_configuration_service.set_role_id(" in function_source
    assert "entry.display_name" not in function_source
    assert "window.after(" in function_source
    assert "window.after_cancel(role_id_auto_read_id)" in source


def test_cancelled_bulk_shortcut_selection_has_no_side_effects():
    source = Path("main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    create_main_window = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "create_main_window"
    )
    add_group_shortcuts = next(
        node
        for node in ast.walk(create_main_window)
        if isinstance(node, ast.FunctionDef)
        and node.name == "add_group_shortcuts"
    )
    body = ast.get_source_segment(source, add_group_shortcuts)
    assert body is not None

    cancel_guard = next(
        node
        for node in add_group_shortcuts.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.UnaryOp)
        and isinstance(node.test.op, ast.Not)
        and isinstance(node.test.operand, ast.Name)
        and node.test.operand.id == "selected"
    )
    assert len(cancel_guard.body) == 1
    assert isinstance(cancel_guard.body[0], ast.Return)
    assert isinstance(cancel_guard.body[0].value, ast.Constant)
    assert cancel_guard.body[0].value.value is False

    cancel_end = cancel_guard.end_lineno
    assert cancel_end is not None
    assert cancel_end < body.count("\n") + add_group_shortcuts.lineno
    assert body.index("if not selected:") < body.index(
        "stop_group_automation_for_configuration_change()"
    ) < body.index("group_configuration_service.add_shortcuts(")
    assert body.count("group_configuration_service.add_shortcuts(") == 1
    assert "tuple(Path(path) for path in selected)" in body
    assert "set_role_id(" not in body


def test_startup_error_uses_product_name():
    source = Path("main.py").read_text(encoding="utf-8")

    assert "輔無法啟動" in source
    assert "FLASH 無法啟動" not in source
