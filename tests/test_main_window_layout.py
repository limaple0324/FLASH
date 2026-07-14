from pathlib import Path


def test_main_window_uses_home_view():
    source = Path("main.py").read_text(encoding="utf-8")

    assert "from ui.home import HomeView" in source
    assert "card_view_state_service.snapshot" in source
    assert "card_view_state_provider=" in source
    assert "window._home_view = home_view" in source
    assert "card_service.subscribe" in source
    assert "window.after_idle(home_view.refresh_cards)" in source


def test_main_window_start_message_is_player_facing():
    source = Path("main.py").read_text(encoding="utf-8")

    assert "輔｜目前狀態" in source
    assert "遊戲操作尚未啟用" in source
    assert "啟動入口已接入首頁" not in source
    assert "RC-01" not in source


def test_main_window_title_is_player_facing():
    source = Path("main.py").read_text(encoding="utf-8")
    from main import APP_TITLE

    assert APP_TITLE == "輔"
    assert 'APP_TITLE = "輔｜FLASH SP1"' not in source


def test_startup_error_uses_product_name():
    source = Path("main.py").read_text(encoding="utf-8")

    assert "輔無法啟動" in source
    assert "FLASH 無法啟動" not in source
