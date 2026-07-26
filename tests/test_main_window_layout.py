from pathlib import Path


def test_main_window_uses_home_view():
    source = Path("main.py").read_text(encoding="utf-8")

    assert "from ui.home import HomeView" in source
    assert "on_input_policy_change=change_input_policy" in source
    assert "on_test_key=test_approved_key" in source
    assert "group_choices=group_choices" in source
    assert "on_group_change=change_group" in source


def test_main_window_start_message_is_player_facing():
    source = Path("main.py").read_text(encoding="utf-8")

    assert "輔｜目前狀態" in source
    assert "format_start_status(status, paths)" in source
    assert "同步按鍵不會自行送出" in source
    assert "目前只允許玩家明確執行 B／C 同步測試" in source
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
