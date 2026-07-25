from adapters.windows_background_capture import (
    CaptureSample,
    Win32PrintWindowProvider,
    Win32RecoveringPrintWindowProvider,
    WindowsBackgroundCaptureBackend,
)


class FakeProvider:
    def __init__(self, sample):
        self.sample = sample
        self.handles = []

    def capture(self, window_handle):
        self.handles.append(window_handle)
        return self.sample


def sample(pixels, *, width=2, height=2, api_succeeded=True):
    return CaptureSample(
        width=width,
        height=height,
        pixels=bytes(pixels),
        api_succeeded=api_succeeded,
    )


def test_capture_probe_is_unknown_when_provider_cannot_capture():
    provider = FakeProvider(None)
    backend = WindowsBackgroundCaptureBackend(provider=provider)

    assert backend.probe_background_capture(123) is None
    assert provider.handles == [123]
    assert backend.last_sample is None


def test_capture_probe_rejects_failed_printwindow_call():
    provider = FakeProvider(sample([0, 20, 80, 255] * 4, api_succeeded=False))
    backend = WindowsBackgroundCaptureBackend(provider=provider)

    assert backend.probe_background_capture(123) is False


def test_capture_probe_rejects_blank_frame():
    provider = FakeProvider(sample([0, 0, 0, 255] * 4))
    backend = WindowsBackgroundCaptureBackend(provider=provider)

    assert backend.probe_background_capture(123) is False


def test_capture_probe_accepts_non_blank_frame():
    provider = FakeProvider(
        sample(
            [
                0, 0, 0, 255,
                30, 80, 120, 255,
                200, 100, 20, 255,
                255, 255, 255, 255,
            ]
        )
    )
    backend = WindowsBackgroundCaptureBackend(provider=provider)

    assert backend.probe_background_capture(321) is True
    assert backend.last_sample is provider.sample


def test_input_capabilities_remain_unknown_without_user_approved_probe():
    backend = WindowsBackgroundCaptureBackend(provider=FakeProvider(None))

    assert backend.probe_background_input(1) is None
    assert backend.probe_minimized_input(1) is None


class FakeWindowStateApi:
    def __init__(self, *, minimized):
        self.minimized = minimized
        self.show_commands = []
        self.IsIconic = FakeCallable(lambda _handle: self.minimized)
        self.ShowWindow = FakeCallable(self._show_window)

    def _show_window(self, _handle, command):
        self.show_commands.append(command)
        if command == Win32RecoveringPrintWindowProvider.SW_SHOWNOACTIVATE:
            self.minimized = False
        elif command == Win32RecoveringPrintWindowProvider.SW_SHOWMINNOACTIVE:
            self.minimized = True
        return True


class FakeCallable:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


def test_recovering_provider_refreshes_and_restores_minimized_window(monkeypatch):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeWindowStateApi(minimized=True)
    provider = Win32RecoveringPrintWindowProvider(paint_settle_seconds=0)
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, object()))
    monkeypatch.setattr(
        Win32PrintWindowProvider,
        "capture",
        lambda _self, _handle: expected,
    )

    assert provider.capture(123) is expected
    assert user32.minimized is True
    assert user32.show_commands == [
        provider.SW_SHOWNOACTIVATE,
        provider.SW_SHOWMINNOACTIVE,
    ]


def test_recovering_provider_does_not_touch_normal_window(monkeypatch):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeWindowStateApi(minimized=False)
    provider = Win32RecoveringPrintWindowProvider(paint_settle_seconds=0)
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, object()))
    monkeypatch.setattr(
        Win32PrintWindowProvider,
        "capture",
        lambda _self, _handle: expected,
    )

    assert provider.capture(123) is expected
    assert user32.show_commands == []
