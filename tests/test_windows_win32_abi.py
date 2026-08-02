import ctypes
import os
from ctypes import wintypes
from types import SimpleNamespace

import pytest

from adapters.windows_background_capture import (
    Win32PrintWindowProvider,
    _BITMAPINFO,
    _WINDOWPLACEMENT,
    _configure_win32_capture_api,
)
from adapters.windows_window import (
    Win32WindowBackend,
    _configure_user32_window_api,
)


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Win32 ABI checks require Windows ctypes")


class FakeWinFunction:
    """Small ctypes function double that preserves the default c_int narrowing."""

    def __init__(self, implementation=None, *, result=0):
        self.argtypes = None
        self.restype = ctypes.c_int
        self.calls = []
        self._implementation = implementation
        self._result = result

    def __call__(self, *args):
        self.calls.append(args)
        value = self._implementation(*args) if self._implementation is not None else self._result
        if self.restype is None or value is None:
            return None
        converted = self.restype(value)
        return getattr(converted, "value", converted)


def _fake_user32_for_windows(**overrides):
    names = (
        "IsWindowVisible",
        "GetWindowTextLengthW",
        "GetWindowTextW",
        "GetWindowRect",
        "IsIconic",
        "GetWindowThreadProcessId",
        "GetClassNameW",
        "EnumWindows",
        "GetForegroundWindow",
        "WindowFromPoint",
        "GetAncestor",
    )
    functions = {name: FakeWinFunction() for name in names}
    functions.update(overrides)
    return SimpleNamespace(**functions)


def _value(item):
    return getattr(item, "value", item)


def test_window_api_declares_every_used_win32_signature():
    user32 = _fake_user32_for_windows()

    enum_proc_type = _configure_user32_window_api(user32)

    assert user32.IsWindowVisible.argtypes == (wintypes.HWND,)
    assert user32.IsWindowVisible.restype is wintypes.BOOL
    assert user32.GetWindowTextLengthW.argtypes == (wintypes.HWND,)
    assert user32.GetWindowTextLengthW.restype is ctypes.c_int
    assert user32.GetWindowTextW.argtypes == (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    assert user32.GetWindowTextW.restype is ctypes.c_int
    assert user32.GetWindowRect.argtypes == (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    assert user32.GetWindowRect.restype is wintypes.BOOL
    assert user32.IsIconic.argtypes == (wintypes.HWND,)
    assert user32.IsIconic.restype is wintypes.BOOL
    assert user32.GetWindowThreadProcessId.argtypes == (
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    )
    assert user32.GetWindowThreadProcessId.restype is wintypes.DWORD
    assert user32.GetClassNameW.argtypes == (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    assert user32.GetClassNameW.restype is ctypes.c_int
    assert user32.EnumWindows.argtypes == (enum_proc_type, wintypes.LPARAM)
    assert user32.EnumWindows.restype is wintypes.BOOL
    assert user32.GetForegroundWindow.argtypes == ()
    assert user32.GetForegroundWindow.restype is wintypes.HWND
    assert user32.WindowFromPoint.argtypes == (wintypes.POINT,)
    assert user32.WindowFromPoint.restype is wintypes.HWND
    assert user32.GetAncestor.argtypes == (wintypes.HWND, wintypes.UINT)
    assert user32.GetAncestor.restype is wintypes.HWND


def test_window_backend_preserves_high_bit_hwnds():
    foreground_hwnd = 0x1234567887654321
    point_hwnd = 0x2345678998765432
    root_hwnd = 0x3456789AA9876543
    user32 = _fake_user32_for_windows(
        GetForegroundWindow=FakeWinFunction(result=foreground_hwnd),
        WindowFromPoint=FakeWinFunction(result=point_hwnd),
        GetAncestor=FakeWinFunction(result=root_hwnd),
    )
    backend = Win32WindowBackend()
    backend._user32 = lambda: user32

    assert backend.foreground_handle() == foreground_hwnd
    assert backend.top_window_at(10, 20) == root_hwnd
    assert _value(user32.GetAncestor.calls[0][0]) == point_hwnd


def test_top_window_lookup_rejects_failed_root_ancestor_resolution():
    user32 = _fake_user32_for_windows(
        WindowFromPoint=FakeWinFunction(result=321),
        GetAncestor=FakeWinFunction(result=0),
    )
    backend = Win32WindowBackend()
    backend._user32 = lambda: user32

    assert backend.top_window_at(10, 20) is None


def _enumerating_user32(*, hwnd, process_id=None, window_class=None):
    title = "Observed window"

    def enumerate_windows(callback, _lparam):
        assert callback(hwnd, 0)
        return 1

    def write_title(_hwnd, buffer, _maximum):
        buffer.value = title
        return len(title)

    def set_window_rect(_hwnd, rect_pointer):
        rect = rect_pointer._obj
        rect.left = 10
        rect.top = 20
        rect.right = 810
        rect.bottom = 620
        return 1

    def write_process_id(_hwnd, process_id_pointer):
        if process_id is None:
            return 0
        process_id_pointer._obj.value = process_id
        return 123

    def write_window_class(_hwnd, buffer, _maximum):
        if window_class is None:
            return 0
        buffer.value = window_class
        return len(window_class)

    return _fake_user32_for_windows(
        IsWindowVisible=FakeWinFunction(result=1),
        GetWindowTextLengthW=FakeWinFunction(result=len(title)),
        GetWindowTextW=FakeWinFunction(write_title),
        GetWindowRect=FakeWinFunction(set_window_rect),
        IsIconic=FakeWinFunction(result=0),
        GetWindowThreadProcessId=FakeWinFunction(write_process_id),
        GetClassNameW=FakeWinFunction(write_window_class),
        EnumWindows=FakeWinFunction(enumerate_windows),
    )


def test_window_enumeration_preserves_high_hwnd_and_read_only_identity():
    hwnd = 0x1234567887654321
    user32 = _enumerating_user32(
        hwnd=hwnd,
        process_id=9876,
        window_class="ObservedClass",
    )
    backend = Win32WindowBackend()
    backend._user32 = lambda: user32

    windows = backend.list_windows()

    assert len(windows) == 1
    assert windows[0].handle == hwnd
    assert windows[0].title == "Observed window"
    assert windows[0].rect == (10, 20, 810, 620)
    assert windows[0].process_id == 9876
    assert windows[0].window_class == "ObservedClass"


def test_window_enumeration_attaches_only_resolved_anonymous_fingerprint():
    class FakeResolver:
        def __init__(self):
            self.process_ids = None

        def resolve(self, process_ids):
            self.process_ids = list(process_ids)
            return {9876: "a" * 64}

    resolver = FakeResolver()
    user32 = _enumerating_user32(
        hwnd=321,
        process_id=9876,
        window_class="ObservedClass",
    )
    backend = Win32WindowBackend(
        fingerprint_resolver=resolver,
        process_lifecycle_provider=lambda _process_id: 1,
    )
    backend._user32 = lambda: user32

    (window,) = backend.list_windows()

    assert resolver.process_ids == [9876]
    assert window.launch_fingerprint == "a" * 64


def test_window_identity_is_resolved_once_per_process_lifecycle():
    class FakeResolver:
        def __init__(self):
            self.calls = []

        def resolve(self, process_ids):
            self.calls.append(tuple(process_ids))
            return {9876: "a" * 64}

    resolver = FakeResolver()
    lifecycle = {"value": 111}
    backend = Win32WindowBackend(
        fingerprint_resolver=resolver,
        process_lifecycle_provider=lambda _process_id: lifecycle["value"],
    )
    backend._user32 = lambda: _enumerating_user32(
        hwnd=321,
        process_id=9876,
        window_class="ObservedClass",
    )

    assert backend.list_windows()[0].launch_fingerprint == "a" * 64
    assert backend.list_windows()[0].launch_fingerprint == "a" * 64
    assert resolver.calls == [(9876,)]

    lifecycle["value"] = 222
    assert backend.list_windows()[0].launch_fingerprint == "a" * 64
    assert resolver.calls == [(9876,), (9876,)]


def test_failed_identity_resolution_is_cached_fail_closed_for_same_lifecycle():
    class FailingResolver:
        def __init__(self):
            self.calls = 0

        def resolve(self, process_ids):
            self.calls += 1
            tuple(process_ids)
            return {}

    resolver = FailingResolver()
    backend = Win32WindowBackend(
        fingerprint_resolver=resolver,
        process_lifecycle_provider=lambda _process_id: 111,
    )
    backend._user32 = lambda: _enumerating_user32(
        hwnd=321,
        process_id=9876,
        window_class="ObservedClass",
    )

    assert backend.list_windows()[0].launch_fingerprint is None
    assert backend.list_windows()[0].launch_fingerprint is None
    assert resolver.calls == 1


def test_window_enumeration_keeps_failed_optional_identity_unknown():
    user32 = _enumerating_user32(
        hwnd=321,
        process_id=None,
        window_class=None,
    )
    backend = Win32WindowBackend()
    backend._user32 = lambda: user32

    (window,) = backend.list_windows()

    assert window.process_id is None
    assert window.window_class is None


def test_window_enumeration_survives_optional_identity_api_errors():
    def fail_optional_identity(*_args):
        raise OSError("optional identity API unavailable")

    user32 = _enumerating_user32(
        hwnd=321,
        process_id=9876,
        window_class="ObservedClass",
    )
    user32.GetWindowThreadProcessId = FakeWinFunction(fail_optional_identity)
    user32.GetClassNameW = FakeWinFunction(fail_optional_identity)
    backend = Win32WindowBackend()
    backend._user32 = lambda: user32

    (window,) = backend.list_windows()

    assert window.process_id is None
    assert window.window_class is None


def test_window_enumeration_discards_partial_results_when_enumwindows_fails():
    user32 = _enumerating_user32(
        hwnd=321,
        process_id=9876,
        window_class="ObservedClass",
    )

    def enumerate_then_fail(callback, _lparam):
        assert callback(321, 0)
        return 0

    user32.EnumWindows = FakeWinFunction(enumerate_then_fail)
    backend = Win32WindowBackend()
    backend._user32 = lambda: user32

    assert backend.list_windows() == []


def _fake_capture_libraries(handles):
    def set_window_rect(_hwnd, rect_pointer):
        rect = rect_pointer._obj
        rect.left = 0
        rect.top = 0
        rect.right = 2
        rect.bottom = 2
        return 1

    def copied_scan_lines(_dc, _bitmap, _start, scan_lines, _bits, _info, _usage):
        return scan_lines

    user32 = SimpleNamespace(
        GetWindowRect=FakeWinFunction(set_window_rect),
        IsIconic=FakeWinFunction(result=0),
        GetWindowPlacement=FakeWinFunction(result=0),
        GetWindowDC=FakeWinFunction(result=handles["window_dc"]),
        PrintWindow=FakeWinFunction(result=1),
        ReleaseDC=FakeWinFunction(result=1),
    )
    gdi32 = SimpleNamespace(
        CreateCompatibleDC=FakeWinFunction(result=handles["memory_dc"]),
        CreateCompatibleBitmap=FakeWinFunction(result=handles["bitmap"]),
        SelectObject=FakeWinFunction(result=handles["old_object"]),
        GetDIBits=FakeWinFunction(copied_scan_lines),
        DeleteObject=FakeWinFunction(result=1),
        DeleteDC=FakeWinFunction(result=1),
    )
    return user32, gdi32


def test_capture_api_declares_every_used_win32_signature():
    handles = {
        "window_dc": 1,
        "memory_dc": 2,
        "bitmap": 3,
        "old_object": 4,
    }
    user32, gdi32 = _fake_capture_libraries(handles)

    _configure_win32_capture_api(user32, gdi32)

    assert user32.GetWindowRect.argtypes == (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    assert user32.GetWindowRect.restype is wintypes.BOOL
    assert user32.IsIconic.argtypes == (wintypes.HWND,)
    assert user32.IsIconic.restype is wintypes.BOOL
    assert user32.GetWindowPlacement.argtypes == (
        wintypes.HWND,
        ctypes.POINTER(_WINDOWPLACEMENT),
    )
    assert user32.GetWindowPlacement.restype is wintypes.BOOL
    assert user32.GetWindowDC.argtypes == (wintypes.HWND,)
    assert user32.GetWindowDC.restype is wintypes.HDC
    assert user32.PrintWindow.argtypes == (wintypes.HWND, wintypes.HDC, wintypes.UINT)
    assert user32.PrintWindow.restype is wintypes.BOOL
    assert user32.ReleaseDC.argtypes == (wintypes.HWND, wintypes.HDC)
    assert user32.ReleaseDC.restype is ctypes.c_int

    assert gdi32.CreateCompatibleDC.argtypes == (wintypes.HDC,)
    assert gdi32.CreateCompatibleDC.restype is wintypes.HDC
    assert gdi32.CreateCompatibleBitmap.argtypes == (wintypes.HDC, ctypes.c_int, ctypes.c_int)
    assert gdi32.CreateCompatibleBitmap.restype is wintypes.HBITMAP
    assert gdi32.SelectObject.argtypes == (wintypes.HDC, wintypes.HGDIOBJ)
    assert gdi32.SelectObject.restype is wintypes.HGDIOBJ
    assert gdi32.GetDIBits.argtypes == (
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        wintypes.LPVOID,
        ctypes.POINTER(_BITMAPINFO),
        wintypes.UINT,
    )
    assert gdi32.GetDIBits.restype is ctypes.c_int
    assert gdi32.DeleteObject.argtypes == (wintypes.HGDIOBJ,)
    assert gdi32.DeleteObject.restype is wintypes.BOOL
    assert gdi32.DeleteDC.argtypes == (wintypes.HDC,)
    assert gdi32.DeleteDC.restype is wintypes.BOOL


def test_capture_provider_preserves_high_bit_gdi_handles_during_use_and_cleanup():
    hwnd = 0x1234567887654321
    handles = {
        "window_dc": 0x2345678998765432,
        "memory_dc": 0x3456789AA9876543,
        "bitmap": 0x456789ABBA987654,
        "old_object": 0x56789ABCCBA98765,
    }
    user32, gdi32 = _fake_capture_libraries(handles)
    provider = Win32PrintWindowProvider()
    provider._libraries = lambda: (user32, gdi32)

    captured = provider.capture(hwnd)

    assert captured is not None
    assert (captured.width, captured.height) == (2, 2)
    assert _value(user32.GetWindowRect.calls[0][0]) == hwnd
    assert gdi32.CreateCompatibleDC.calls[0] == (handles["window_dc"],)
    assert gdi32.CreateCompatibleBitmap.calls[0] == (handles["window_dc"], 2, 2)
    assert gdi32.SelectObject.calls[0] == (handles["memory_dc"], handles["bitmap"])
    assert gdi32.SelectObject.calls[1] == (
        handles["memory_dc"],
        handles["old_object"],
    )
    assert _value(user32.PrintWindow.calls[0][0]) == hwnd
    assert user32.PrintWindow.calls[0][1] == handles["memory_dc"]
    assert gdi32.GetDIBits.calls[0][0] == handles["memory_dc"]
    assert gdi32.GetDIBits.calls[0][1] == handles["bitmap"]
    assert gdi32.SelectObject.calls[-1] == (handles["memory_dc"], handles["old_object"])
    assert gdi32.DeleteObject.calls == [(handles["bitmap"],)]
    assert gdi32.DeleteDC.calls == [(handles["memory_dc"],)]
    assert _value(user32.ReleaseDC.calls[0][0]) == hwnd
    assert user32.ReleaseDC.calls[0][1] == handles["window_dc"]


def test_capture_provider_uses_normal_window_size_while_minimized():
    handles = {
        "window_dc": 11,
        "memory_dc": 22,
        "bitmap": 33,
        "old_object": 44,
    }
    observed_lengths = []
    user32, gdi32 = _fake_capture_libraries(handles)
    user32.IsIconic = FakeWinFunction(result=1)

    def set_normal_placement(_hwnd, placement_pointer):
        placement = placement_pointer._obj
        observed_lengths.append(placement.length)
        placement.rcNormalPosition.left = 10
        placement.rcNormalPosition.top = 20
        placement.rcNormalPosition.right = 18
        placement.rcNormalPosition.bottom = 26
        return 1

    user32.GetWindowPlacement = FakeWinFunction(set_normal_placement)
    provider = Win32PrintWindowProvider()
    provider._libraries = lambda: (user32, gdi32)

    captured = provider.capture(123)

    assert captured is not None
    assert (captured.width, captured.height) == (8, 6)
    assert observed_lengths == [ctypes.sizeof(_WINDOWPLACEMENT)]
    assert user32.GetWindowRect.calls == []
    assert gdi32.CreateCompatibleBitmap.calls[0] == (handles["window_dc"], 8, 6)


def test_capture_provider_rejects_minimized_window_without_normal_placement():
    handles = {
        "window_dc": 11,
        "memory_dc": 22,
        "bitmap": 33,
        "old_object": 44,
    }
    user32, gdi32 = _fake_capture_libraries(handles)
    user32.IsIconic = FakeWinFunction(result=1)
    user32.GetWindowPlacement = FakeWinFunction(result=0)
    provider = Win32PrintWindowProvider()
    provider._libraries = lambda: (user32, gdi32)

    assert provider.capture(123) is None
    assert user32.GetWindowRect.calls == []
    assert user32.GetWindowDC.calls == []
    assert gdi32.CreateCompatibleBitmap.calls == []


def test_capture_provider_rejects_selectobject_failure_before_capture():
    handles = {
        "window_dc": 11,
        "memory_dc": 22,
        "bitmap": 33,
        "old_object": 44,
    }
    user32, gdi32 = _fake_capture_libraries(handles)
    gdi32.SelectObject = FakeWinFunction(result=0)
    provider = Win32PrintWindowProvider()
    provider._libraries = lambda: (user32, gdi32)

    assert provider.capture(123) is None
    assert user32.PrintWindow.calls == []
    assert gdi32.GetDIBits.calls == []
    assert gdi32.DeleteObject.calls == [(handles["bitmap"],)]
    assert gdi32.DeleteDC.calls == [(handles["memory_dc"],)]
    assert user32.ReleaseDC.calls


def test_capture_provider_restores_bitmap_before_getdibits():
    handles = {
        "window_dc": 11,
        "memory_dc": 22,
        "bitmap": 33,
        "old_object": 44,
    }
    events = []
    user32, gdi32 = _fake_capture_libraries(handles)
    select_results = iter((handles["old_object"], handles["bitmap"]))

    def select_object(_dc, selected_object):
        events.append(("select", selected_object))
        return next(select_results)

    def print_window(*_args):
        events.append(("print", None))
        return 1

    def get_dibits(_dc, _bitmap, _start, scan_lines, _bits, _info, _usage):
        events.append(("getdibits", None))
        return scan_lines

    gdi32.SelectObject = FakeWinFunction(select_object)
    gdi32.GetDIBits = FakeWinFunction(get_dibits)
    user32.PrintWindow = FakeWinFunction(print_window)
    provider = Win32PrintWindowProvider()
    provider._libraries = lambda: (user32, gdi32)

    assert provider.capture(123) is not None
    assert events == [
        ("select", handles["bitmap"]),
        ("print", None),
        ("select", handles["old_object"]),
        ("getdibits", None),
    ]


def test_capture_provider_releases_dc_before_bitmap_after_restore_failure():
    handles = {
        "window_dc": 11,
        "memory_dc": 22,
        "bitmap": 33,
        "old_object": 44,
    }
    events = []
    state = {"select_calls": 0, "memory_dc_alive": True}
    user32, gdi32 = _fake_capture_libraries(handles)

    def select_object(_dc, selected_object):
        state["select_calls"] += 1
        events.append(("select", selected_object))
        if state["select_calls"] == 1:
            return handles["old_object"]
        return 0

    def delete_dc(_dc):
        events.append(("delete_dc", None))
        state["memory_dc_alive"] = False
        return 1

    def delete_object(_bitmap):
        events.append(("delete_bitmap", None))
        assert state["memory_dc_alive"] is False
        return 1

    gdi32.SelectObject = FakeWinFunction(select_object)
    gdi32.DeleteDC = FakeWinFunction(delete_dc)
    gdi32.DeleteObject = FakeWinFunction(delete_object)
    provider = Win32PrintWindowProvider()
    provider._libraries = lambda: (user32, gdi32)

    assert provider.capture(123) is None
    assert gdi32.GetDIBits.calls == []
    assert events == [
        ("select", handles["bitmap"]),
        ("select", handles["old_object"]),
        ("select", handles["old_object"]),
        ("delete_dc", None),
        ("delete_bitmap", None),
    ]
