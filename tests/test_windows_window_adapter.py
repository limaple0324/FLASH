from adapters.windows_window import OperationArea, WindowInfo, WindowsWindowAdapter


class FakeBackend:
    def __init__(self, windows, *, foreground=None, covered_points=None):
        self._windows = windows
        self._foreground = foreground
        self._covered_points = covered_points or {}

    def list_windows(self):
        return list(self._windows)

    def foreground_handle(self):
        if self._foreground is not None:
            return self._foreground
        if len(self._windows) == 1:
            return self._windows[0].handle
        return None

    def top_window_at(self, x, y):
        if (x, y) in self._covered_points:
            return self._covered_points[(x, y)]
        return self.foreground_handle()


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


def test_adapter_rejects_target_that_is_not_foreground():
    target = make_window(handle=321)
    adapter = WindowsWindowAdapter(["latale"], backend=FakeBackend([target], foreground=999))
    result = adapter.find_target()
    assert result.success is False
    assert result.code == "window.not_foreground"
    assert result.details["foreground_handle"] == 999
    assert adapter.last_match is None


def test_window_overlap_is_ignored_when_no_operation_area_is_requested():
    target = make_window(handle=321, rect=(0, 0, 100, 100))
    backend = FakeBackend([target], foreground=321, covered_points={(50, 50): 999})
    adapter = WindowsWindowAdapter(["latale"], backend=backend)

    result = adapter.find_target()

    assert result.success is True
    assert result.code == "window.ready"
    assert result.details["checked_areas"] == ()
    assert result.details["input_enabled"] is False


def test_adapter_rejects_covered_required_operation_area():
    target = make_window(handle=321, rect=(0, 0, 100, 100))
    button_area = OperationArea("auto_fishing_button", (0.8, 0.8, 1.0, 1.0))
    sample_points = WindowsWindowAdapter._area_sample_points(target.rect, button_area)
    backend = FakeBackend([target], foreground=321, covered_points={sample_points[0]: 999})
    adapter = WindowsWindowAdapter(["latale"], backend=backend)

    result = adapter.find_target([button_area])

    assert result.success is False
    assert result.code == "operation_area.overlapped"
    assert result.details["covered"][0]["area"] == "auto_fishing_button"
    assert adapter.last_match is None


def test_adapter_allows_overlap_outside_required_operation_area():
    target = make_window(handle=321, rect=(0, 0, 100, 100))
    button_area = OperationArea("auto_fishing_button", (0.8, 0.8, 1.0, 1.0))
    backend = FakeBackend([target], foreground=321, covered_points={(10, 10): 999})
    adapter = WindowsWindowAdapter(["latale"], backend=backend)

    result = adapter.find_target([button_area])

    assert result.success is True
    assert result.code == "window.ready"
    assert result.details["checked_areas"] == ("auto_fishing_button",)
    assert result.details["input_enabled"] is False


def test_invalid_operation_area_is_rejected():
    target = make_window(handle=321)
    adapter = WindowsWindowAdapter(["latale"], backend=FakeBackend([target], foreground=321))

    try:
        adapter.find_target([OperationArea("bad", (0.9, 0.9, 0.2, 0.2))])
    except ValueError as exc:
        assert "Invalid relative operation area" in str(exc)
    else:
        raise AssertionError("Invalid operation area should raise ValueError")


def test_adapter_accepts_exactly_one_foreground_window():
    target = make_window("LaTale - 嘻", handle=321)
    adapter = WindowsWindowAdapter(
        ["latale", "嘻"],
        backend=FakeBackend([target], foreground=321),
    )
    result = adapter.find_target()
    assert result.success is True
    assert result.code == "window.ready"
    assert adapter.last_match == target
    assert result.details["handle"] == 321
    assert result.details["input_enabled"] is False


def test_shutdown_clears_cached_target():
    target = make_window(handle=100)
    adapter = WindowsWindowAdapter(["latale"], backend=FakeBackend([target], foreground=100))
    assert adapter.find_target().success is True
    adapter.shutdown()
    assert adapter.last_match is None
