from pathlib import Path

import main
from PIL import Image
from main import (
    APP_ICON_ICO,
    APP_ICON_PNG,
    ICON_BIG,
    ICON_SMALL,
    IMAGE_ICON,
    LR_DEFAULTSIZE,
    LR_LOADFROMFILE,
    WINDOWS_APP_USER_MODEL_ID,
    WM_SETICON,
    resource_path,
)


def test_app_icon_asset_exists():
    assert resource_path(APP_ICON_PNG).exists()
    assert resource_path(APP_ICON_ICO).exists()


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
        assert all(pixel == (0, 0, 0, 0) for pixel in corners), size


def test_windows_build_uses_the_confirmed_icon():
    spec = Path("FLASH.spec").read_text(encoding="utf-8")

    assert "assets/flash_icon.png" in spec
    assert "('assets/flash_icon.ico', 'assets')" in spec
    assert "('assets/reconnect_reference', 'assets/reconnect_reference')" in spec
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


def test_packaged_executable_uses_its_embedded_taskbar_identity(monkeypatch):
    calls = []

    class FakeShell32:
        @staticmethod
        def SetCurrentProcessExplicitAppUserModelID(value):
            calls.append(value)

    class FakeWindll:
        shell32 = FakeShell32()

    monkeypatch.setattr(main.sys, "platform", "win32")
    monkeypatch.setattr(main.sys, "frozen", True, raising=False)
    monkeypatch.setattr(main.ctypes, "windll", FakeWindll(), raising=False)

    main.apply_windows_app_identity()

    assert calls == []


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
        ("iconphoto", True, icon_object),
    ]
    assert window._flash_icon is icon_object


def test_native_window_icon_targets_the_real_windows_top_level(
    monkeypatch,
    tmp_path,
):
    ico = tmp_path / "flash_icon.ico"
    ico.write_bytes(b"ico")
    calls = []

    class FakeApi:
        def __init__(self, name, result):
            self.name = name
            self.result = result
            self.restype = None

        def __call__(self, *args):
            calls.append((self.name, args))
            return self.result

    class FakeUser32:
        GetParent = FakeApi("GetParent", 222)
        LoadImageW = FakeApi("LoadImageW", 333)
        SendMessageW = FakeApi("SendMessageW", 1)

    class FakeWindll:
        user32 = FakeUser32()

    class FakeWindow:
        def update_idletasks(self):
            calls.append(("update_idletasks", ()))

        @staticmethod
        def winfo_id():
            return 111

    monkeypatch.setattr(main.sys, "platform", "win32")
    monkeypatch.setattr(main.ctypes, "windll", FakeWindll(), raising=False)
    monkeypatch.setattr(main, "resource_path", lambda _path: ico)

    window = FakeWindow()
    main.apply_windows_native_window_icon(window)

    assert ("GetParent", (111,)) in calls
    assert (
        "LoadImageW",
        (
            None,
            str(ico),
            IMAGE_ICON,
            0,
            0,
            LR_LOADFROMFILE | LR_DEFAULTSIZE,
        ),
    ) in calls
    assert ("SendMessageW", (222, WM_SETICON, ICON_SMALL, 333)) in calls
    assert ("SendMessageW", (222, WM_SETICON, ICON_BIG, 333)) in calls
    assert window._flash_native_icon == 333
