from pathlib import Path


def test_main_window_uses_home_view():
    source = Path("main.py").read_text(encoding="utf-8")

    assert "from ui.home import HomeView" in source
    assert "HomeView(window, status, on_start=show_start_status).build()" in source
