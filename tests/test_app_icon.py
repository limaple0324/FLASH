from pathlib import Path

from main import APP_ICON_PNG, resource_path


def test_app_icon_asset_exists():
    assert resource_path(APP_ICON_PNG).exists()


def test_windows_build_uses_the_confirmed_icon():
    spec = Path("FLASH.spec").read_text(encoding="utf-8")

    assert "assets/flash_icon.png" in spec
    assert "icon='assets/flash_icon.ico'" in spec
