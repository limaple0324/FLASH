import ctypes
from ctypes import wintypes

from adapters.windows_client_size import Win32WindowClientSizeBackend


class FakeWinFunction:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class FakeUser32:
    def __init__(self):
        self.set_window_pos_calls = []
        self.context_calls = []
        self.IsWindow = FakeWinFunction(lambda _hwnd: True)
        self.GetClientRect = FakeWinFunction(self._get_client_rect)
        self.GetWindowRect = FakeWinFunction(self._get_window_rect)
        self.SetWindowPos = FakeWinFunction(self._set_window_pos)
        self.SetThreadDpiAwarenessContext = FakeWinFunction(
            self._set_context
        )

    @staticmethod
    def _get_client_rect(_hwnd, pointer):
        rect = pointer._obj
        rect.left = 0
        rect.top = 0
        rect.right = 1000
        rect.bottom = 700
        return True

    @staticmethod
    def _get_window_rect(_hwnd, pointer):
        rect = pointer._obj
        rect.left = 10
        rect.top = 20
        rect.right = 1026
        rect.bottom = 759
        return True

    def _set_window_pos(
        self,
        hwnd,
        insert_after,
        x,
        y,
        width,
        height,
        flags,
    ):
        self.set_window_pos_calls.append(
            (
                int(getattr(hwnd, "value", hwnd)),
                int(getattr(insert_after, "value", insert_after) or 0),
                x,
                y,
                width,
                height,
                flags,
            )
        )
        return True

    def _set_context(self, value):
        self.context_calls.append(
            int(getattr(value, "value", value))
        )
        return 123 if len(self.context_calls) == 1 else 0


def test_reads_and_resizes_exact_client_area_without_moving_or_activating(
    monkeypatch,
):
    user32 = FakeUser32()
    monkeypatch.setattr(
        Win32WindowClientSizeBackend,
        "_user32",
        staticmethod(lambda: user32),
    )
    backend = Win32WindowClientSizeBackend()

    assert backend.read(500) == (1000, 700)
    assert backend.resize(500, 1200, 800) is True

    assert user32.set_window_pos_calls == [
        (
            500,
            0,
            10,
            20,
            1216,
            839,
            (
                Win32WindowClientSizeBackend.SWP_NOZORDER
                | Win32WindowClientSizeBackend.SWP_NOACTIVATE
            ),
        )
    ]
    assert all(
        argument is not None
        for argument in user32.GetClientRect.argtypes
    )
    assert user32.GetClientRect.restype is wintypes.BOOL
    assert user32.context_calls
