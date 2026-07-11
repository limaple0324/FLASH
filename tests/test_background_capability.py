from adapters.background_capability import (
    BackgroundCapabilityProbe,
    CapabilityState,
)


class FakeBackend:
    def __init__(self, *, capture=None, background_input=None, minimized_input=None, fail=None):
        self.capture = capture
        self.background_input = background_input
        self.minimized_input = minimized_input
        self.fail = fail
        self.calls = []

    def _value(self, name, value, handle):
        self.calls.append((name, handle))
        if self.fail == name:
            raise RuntimeError(f"{name} probe failed")
        return value

    def probe_background_capture(self, window_handle):
        return self._value("capture", self.capture, window_handle)

    def probe_background_input(self, window_handle):
        return self._value("background_input", self.background_input, window_handle)

    def probe_minimized_input(self, window_handle):
        return self._value("minimized_input", self.minimized_input, window_handle)


def test_probe_does_not_run_without_selected_window():
    backend = FakeBackend(capture=True, background_input=True, minimized_input=True)

    report = BackgroundCapabilityProbe(backend).run(None)

    assert report.fully_supported is False
    assert backend.calls == []
    assert report.background_capture.state is CapabilityState.UNTESTED
    assert report.background_input.state is CapabilityState.UNTESTED
    assert report.minimized_input.state is CapabilityState.UNTESTED


def test_probe_reports_full_background_support():
    backend = FakeBackend(capture=True, background_input=True, minimized_input=True)

    report = BackgroundCapabilityProbe(backend).run(321)

    assert report.fully_supported is True
    assert all(item[1] == 321 for item in backend.calls)
    assert report.background_capture.state is CapabilityState.SUPPORTED
    assert report.background_input.state is CapabilityState.SUPPORTED
    assert report.minimized_input.state is CapabilityState.SUPPORTED


def test_probe_keeps_partial_support_explicit():
    backend = FakeBackend(capture=True, background_input=False, minimized_input=None)

    report = BackgroundCapabilityProbe(backend).run(321)

    assert report.fully_supported is False
    assert report.background_capture.state is CapabilityState.SUPPORTED
    assert report.background_input.state is CapabilityState.UNSUPPORTED
    assert report.minimized_input.state is CapabilityState.UNKNOWN


def test_probe_converts_backend_exception_to_report():
    backend = FakeBackend(capture=True, background_input=True, minimized_input=True, fail="background_input")

    report = BackgroundCapabilityProbe(backend).run(321)

    assert report.fully_supported is False
    assert report.background_input.state is CapabilityState.ERROR
    assert "probe failed" in report.background_input.message


def test_report_is_machine_readable():
    report = BackgroundCapabilityProbe(
        FakeBackend(capture=True, background_input=False, minimized_input=None)
    ).run(88)

    payload = report.to_dict()

    assert payload["fully_supported"] is False
    assert payload["capabilities"]["background_capture"]["state"] == CapabilityState.SUPPORTED
    assert payload["capabilities"]["background_input"]["state"] == CapabilityState.UNSUPPORTED
