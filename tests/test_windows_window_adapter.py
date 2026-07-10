from adapters.windows_window import WindowInfo, WindowsWindowAdapter


class FakeBackend:
    def __init__(self, windows):
        self._windows = windows

    def list_windows(self):
        return list(self._windows)


def make_window(title="LaTale", *, handle=100, minimized=False, rect=(10, 20, 810, 620)):
    return WindowInfo(
        handle=handle,
        title=title,
        visible=True,
        minimized=minimized,
        rect=rect,
    )


def test_adapter_requires_target_configuration():
    adapter = WindowsWindowAdapter([], backend=FakeBackend([make_window()]))

    result = adapter.health_check()

    assert result.success is False
    assert result.code == "window.not_configured"
    assert adapter.last_match is None


def test_adapter_reports_missing_target_without_sending_input():
    adapter = WindowsWindowAdapter(["latale"], backend=FakeBackend([make_window("Other Game")]))

    result = adapter.find_target()

    assert result.success is False
    assert result.code == "window.not_found"
    assert adapter.last_match is None


def test_adapter_rejects_ambiguous_matches():
    adapter = WindowsWindowAdapter(
        ["latale"],
        backend=FakeBackend([make_window("LaTale A", handle=1), make_window("LaTale B", handle=2)]),
    )

    result = adapter.find_target()

    assert result.success is False
    assert result.code == "window.ambiguous"
    assert result.details["count"] == 2
    assert adapter.last_match is None


def test_adapter_rejects_minimized_target():
    adapter = WindowsWindowAdapter(["latale"], backend=FakeBackend([make_window(minimized=True)]))

    result = adapter.find_target()

    assert result.success is False
    assert result.code == "window.minimized"
    assert adapter.last_match is None


def test_adapter_rejects_invalid_bounds():
    adapter = WindowsWindowAdapter(
        ["latale"],
        backend=FakeBackend([make_window(rect=(100, 100, 100, 500))]),
    )

    result = adapter.find_target()

    assert result.success is False
    assert result.code == "window.invalid_bounds"
    assert adapter.last_match is None


def test_adapter_accepts_exactly_one_safe_window():
    target = make_window("LaTale - 嘻", handle=321)
    adapter = WindowsWindowAdapter(["latale", "嘻"], backend=FakeBackend([target]))

    result = adapter.find_target()

    assert result.success is True
    assert result.code == "window.ready"
    assert adapter.last_match == target
    assert result.details["handle"] == 321


def test_shutdown_clears_cached_target():
    adapter = WindowsWindowAdapter(["latale"], backend=FakeBackend([make_window()]))
    assert adapter.find_target().success is True

    adapter.shutdown()

    assert adapter.last_match is None
