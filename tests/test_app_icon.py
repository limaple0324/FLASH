from pathlib import Path

import main
from main import APP_ICON_ICO, APP_ICON_PNG, WINDOWS_APP_USER_MODEL_ID, resource_path


def test_app_icon_asset_exists():
    assert resource_path(APP_ICON_PNG).exists()
    assert resource_path(APP_ICON_ICO).exists()


def test_windows_build_uses_the_confirmed_icon():
    spec = Path("FLASH.spec").read_text(encoding="utf-8")

    assert "assets/flash_icon.png" in spec
    assert "('assets/flash_icon.ico', 'assets')" in spec
    assert "icon='assets/flash_icon.ico'" in spec


def test_windows_app_identity_is_set_before_window_creation(monkeypatch):
    calls = []

    class FakeShell32:
        @staticmethod
        def SetCurrentProcessExplicitAppUserModelID(value):
            calls.append(value)

    class FakeWindll:
        shell32 = FakeShell32()

    monkeypatch.setattr(main.sys, "platform", "win32")
    monkeypatch.setattr(main.ctypes, "windll", FakeWindll(), raising=False)

    main.apply_windows_app_identity()

    assert calls == [WINDOWS_APP_USER_MODEL_ID]
