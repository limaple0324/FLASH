from pathlib import Path


def test_main_window_uses_home_view():
    source = Path("main.py").read_text(encoding="utf-8")

    assert "from ui.home import HomeView" in source
    assert "on_input_policy_change=change_input_policy" in source
    assert "on_keyboard_sync_change=change_keyboard_sync" in source
    assert "group_choices=group_choices" in source
    assert "on_group_change=change_group" in source
    assert "build_windows_card_overlay_runtime" in source
    assert "CardExpiryMonitor" in source
    assert "on_card_display_seconds_update=update_card_display_seconds" in source
    assert "CharacterDetailWindow" in source
    assert "CharacterNoteService" in source
    assert "auto_click_service.configure_direct_left_sync(" in source
    assert "pointer_sync_controller.send_click(" in source
    assert "include_source=True" in source
    assert "execution_guard=direct_auto_click_execution_allowed" in source
    assert "BackgroundImageService" in source
    assert "background_image_service.current_background()" in source
    assert "on_choose_background_source=choose_background_source" in source
    assert "on_prepare_background_image=prepare_background_image" in source
    assert "on_clear_background_image=clear_background_image" in source
    assert '("所有檔案", "*.*")' in source


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

    assert 'APP_TITLE = "輔"' in source
    assert 'APP_TITLE = "輔｜FLASH SP1"' not in source


def test_startup_error_uses_product_name():
    source = Path("main.py").read_text(encoding="utf-8")

    assert "輔無法啟動" in source
    assert "FLASH 無法啟動" not in source
