from main import _attach_card_overlay_runtime


class FakeWindow:
    def __init__(self) -> None:
        self.protocols = {}
        self.destroy_calls = 0

    def protocol(self, name, callback) -> None:
        self.protocols[name] = callback

    def destroy(self) -> None:
        self.destroy_calls += 1


class FakeRuntime:
    def __init__(self, *, stop_error: Exception | None = None) -> None:
        self.start_calls = 0
        self.stop_calls = 0
        self.stop_error = stop_error

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1
        if self.stop_error is not None:
            raise self.stop_error


def test_missing_runtime_keeps_default_window_close_behavior() -> None:
    window = FakeWindow()

    _attach_card_overlay_runtime(window, None)

    assert window.protocols == {}
    assert window.destroy_calls == 0


def test_runtime_starts_and_stops_with_main_window() -> None:
    window = FakeWindow()
    runtime = FakeRuntime()

    _attach_card_overlay_runtime(window, runtime)
    window.protocols["WM_DELETE_WINDOW"]()

    assert runtime.start_calls == 1
    assert runtime.stop_calls == 1
    assert window.destroy_calls == 1
    assert window._card_overlay_runtime is runtime


def test_repeated_close_does_not_stop_or_destroy_twice() -> None:
    window = FakeWindow()
    runtime = FakeRuntime()

    _attach_card_overlay_runtime(window, runtime)
    close = window.protocols["WM_DELETE_WINDOW"]
    close()
    close()

    assert runtime.stop_calls == 1
    assert window.destroy_calls == 1


def test_runtime_stop_failure_does_not_block_window_close() -> None:
    window = FakeWindow()
    error = RuntimeError("overlay stop failed")
    runtime = FakeRuntime(stop_error=error)

    _attach_card_overlay_runtime(window, runtime)
    window.protocols["WM_DELETE_WINDOW"]()

    assert window.destroy_calls == 1
    assert window._card_overlay_stop_error is error
