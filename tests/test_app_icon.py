import hashlib
from pathlib import Path

import main
from PIL import Image
from main import APP_ICON_ICO, APP_ICON_PNG, resource_path


KNOWN_GOOD_V02_ICON_SHA256 = (
    "bee0b407e622eb87e98aa3517aacecb0f51744b8f19a2565eb3c6c7a6d91c290"
)


def test_app_icon_asset_exists():
    assert resource_path(APP_ICON_PNG).exists()
    assert resource_path(APP_ICON_ICO).exists()


def test_windows_icon_is_the_known_good_read_only_v02_asset():
    digest = hashlib.sha256(resource_path(APP_ICON_ICO).read_bytes()).hexdigest()

    assert digest == KNOWN_GOOD_V02_ICON_SHA256


def test_app_icon_has_transparent_corners():
    png = Image.open(resource_path(APP_ICON_PNG)).convert("RGBA")
    png_corners = [
        png.getpixel((0, 0)),
        png.getpixel((png.width - 1, 0)),
        png.getpixel((0, png.height - 1)),
        png.getpixel((png.width - 1, png.height - 1)),
    ]
    assert all(pixel == (0, 0, 0, 0) for pixel in png_corners)

    ico = Image.open(resource_path(APP_ICON_ICO))
    for size in ico.ico.sizes():
        frame = ico.ico.getimage(size).convert("RGBA")
        corners = [
            frame.getpixel((0, 0)),
            frame.getpixel((frame.width - 1, 0)),
            frame.getpixel((0, frame.height - 1)),
            frame.getpixel((frame.width - 1, frame.height - 1)),
        ]
        # The proven 輔V0.2 16px frame uses alpha=1 at its anti-aliased
        # corners; Windows renders it transparently. Larger frames use 0.
        assert all(pixel[3] <= 1 for pixel in corners), size


def test_windows_build_uses_the_confirmed_icon():
    spec = Path("FLASH.spec").read_text(encoding="utf-8")

    assert "assets/flash_icon.png" in spec
    assert "('assets/flash_icon.ico', 'assets')" in spec
    assert "('assets/reconnect_reference', 'assets/reconnect_reference')" in spec
    assert "icon='assets/flash_icon.ico'" in spec


def test_window_icon_sets_the_current_window_icon(monkeypatch, tmp_path):
    ico = tmp_path / "flash_icon.ico"
    png = tmp_path / "flash_icon.png"
    ico.write_bytes(b"ico")
    png.write_bytes(b"png")
    calls = []
    icon_object = object()

    class FakeWindow:
        def iconbitmap(self, path):
            calls.append(("iconbitmap", path))

        def iconphoto(self, default, icon):
            calls.append(("iconphoto", default, icon))

    def fake_resource_path(path):
        return ico if path == APP_ICON_ICO else png

    monkeypatch.setattr(main.sys, "platform", "win32")
    monkeypatch.setattr(main, "resource_path", fake_resource_path)
    monkeypatch.setattr(main, "PhotoImage", lambda **_kwargs: icon_object)

    window = FakeWindow()
    main.apply_window_icon(window)

    assert calls == [
        ("iconbitmap", str(ico)),
    ]
