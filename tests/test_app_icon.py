import hashlib
from pathlib import Path

import main
from adapters import windows_app_identity
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
    assert (
        "('assets/game_data_reference/obsidian', "
        "'assets/game_data_reference/obsidian')"
    ) in spec
    assert "icon='assets/flash_icon.ico'" in spec


def test_taskbar_icon_uses_packaged_executable_resource(monkeypatch, tmp_path):
    executable = tmp_path / "FLASH.exe"
    monkeypatch.setattr(main.sys, "frozen", True, raising=False)
    monkeypatch.setattr(main.sys, "executable", str(executable))

    assert main.taskbar_icon_resource() == f"{executable.resolve()},0"


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


def test_process_identity_is_explicit_and_stable(monkeypatch):
    calls = []

    class FakeBackend:
        def set_process_identity(self, app_id):
            calls.append(app_id)

    monkeypatch.setattr(windows_app_identity.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_app_identity,
        "_windows_backend",
        lambda: FakeBackend(),
    )

    assert windows_app_identity.configure_process_app_identity() is True
    assert calls == [windows_app_identity.APP_USER_MODEL_ID]


def test_process_identity_failure_does_not_block_program_start(monkeypatch):
    class FailingBackend:
        def set_process_identity(self, _app_id):
            raise OSError("unsupported shell")

    monkeypatch.setattr(windows_app_identity.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_app_identity,
        "_windows_backend",
        lambda: FailingBackend(),
    )

    assert windows_app_identity.configure_process_app_identity() is False


def test_tk_window_identity_uses_root_handle_and_confirmed_icon(monkeypatch):
    calls = []

    class FakeBackend:
        def root_window_handle(self, window_handle):
            calls.append(("root", window_handle))
            return 456

        def set_window_identity(self, window_handle, app_id, icon_resource):
            calls.append(("set", window_handle, app_id, icon_resource))

    class FakeWindow:
        def winfo_id(self):
            return 123

    backend = FakeBackend()
    monkeypatch.setattr(windows_app_identity.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_app_identity,
        "_windows_backend",
        lambda: backend,
    )

    identity = windows_app_identity.configure_tk_window_app_identity(
        FakeWindow(),
        r"C:\Program Files\輔\FLASH.exe,0",
    )

    assert identity is not None
    assert calls == [
        ("root", 123),
        (
            "set",
            456,
            windows_app_identity.APP_USER_MODEL_ID,
            r"C:\Program Files\輔\FLASH.exe,0",
        ),
    ]

    identity.clear()
    identity.clear()
    assert calls[-1] == ("set", 456, None, None)
    assert calls.count(("set", 456, None, None)) == 1


def test_tk_window_identity_failure_keeps_window_launchable(monkeypatch):
    class FailingBackend:
        def root_window_handle(self, window_handle):
            return window_handle

        def set_window_identity(self, _window_handle, _app_id, _icon_resource):
            raise OSError("property store unavailable")

    class FakeWindow:
        def winfo_id(self):
            return 123

    monkeypatch.setattr(windows_app_identity.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_app_identity,
        "_windows_backend",
        lambda: FailingBackend(),
    )

    assert (
        windows_app_identity.configure_tk_window_app_identity(
            FakeWindow(),
            r"C:\Program Files\輔\FLASH.exe,0",
        )
        is None
    )
