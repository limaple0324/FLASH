from adapters.windows_window import OperationArea, WindowInfo, WindowsWindowAdapter


class MutableBackend:
    def __init__(self, windows, *, foreground=None, covered_points=None):
        self.windows = list(windows)
        self.foreground = foreground
        self.covered_points = dict(covered_points or {})

    def list_windows(self):
        return list(self.windows)

    def foreground_handle(self):
        return self.foreground

    def top_window_at(self, x, y):
        return self.covered_points.get((x, y), self.foreground)


def make_window(*, handle=100, title="LaTale", rect=(0, 0, 800, 600)):
    return WindowInfo(
        handle=handle,
        title=title,
        visible=True,
        minimized=False,
        rect=rect,
    )


def test_adapter_replaces_cached_match_when_window_handle_changes():
    first = make_window(handle=100)
    second = make_window(handle=200)
    backend = MutableBackend([first], foreground=100)
    adapter = WindowsWindowAdapter(["latale"], backend=backend)

    assert adapter.find_target().success is True
    assert adapter.last_match == first

    backend.windows = [second]
    backend.foreground = 200
    result = adapter.find_target()

    assert result.success is True
    assert result.details["handle"] == 200
    assert adapter.last_match == second


def test_adapter_clears_cached_match_when_window_disappears():
    target = make_window(handle=100)
    backend = MutableBackend([target], foreground=100)
    adapter = WindowsWindowAdapter(["latale"], backend=backend)

    assert adapter.find_target().success is True
    backend.windows = []
    backend.foreground = None

    result = adapter.find_target()

    assert result.success is False
    assert result.code == "window.not_found"
    assert adapter.last_match is None


def test_adapter_clears_cached_match_when_focus_moves_away():
    target = make_window(handle=100)
    backend = MutableBackend([target], foreground=100)
    adapter = WindowsWindowAdapter(["latale"], backend=backend)

    assert adapter.find_target().success is True
    backend.foreground = 999

    result = adapter.find_target()

    assert result.success is False
    assert result.code == "window.not_foreground"
    assert adapter.last_match is None


def test_adapter_clears_cached_match_when_required_area_becomes_covered():
    target = make_window(handle=100, rect=(0, 0, 100, 100))
    area = OperationArea("action", (0.8, 0.8, 1.0, 1.0))
    backend = MutableBackend([target], foreground=100)
    adapter = WindowsWindowAdapter(["latale"], backend=backend)

    assert adapter.find_target([area]).success is True
    covered_point = WindowsWindowAdapter._area_sample_points(target.rect, area)[0]
    backend.covered_points[covered_point] = 999

    result = adapter.find_target([area])

    assert result.success is False
    assert result.code == "operation_area.overlapped"
    assert adapter.last_match is None
