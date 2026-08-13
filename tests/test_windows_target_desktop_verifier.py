from dataclasses import replace
from types import SimpleNamespace

import pytest

from adapters.windows_target_desktop_verifier import TargetDesktopVerifier
from adapters.windows_window import WindowInfo


class FakeWindowBackend:
    def __init__(self, windows, *, foreground=None):
        self._windows = list(windows)
        self._foreground = foreground

    def list_windows(self):
        return list(self._windows)

    def foreground_handle(self):
        return self._foreground

    def top_window_at(self, _x, _y):
        return self._foreground


class FakeCaptureBackend:
    def __init__(self, captures):
        self._captures = dict(captures)
        self.last_sample = None
        self.handles = []

    def probe_background_capture(self, handle):
        self.handles.append(handle)
        capture = self._captures.get(handle)
        if capture is None:
            self.last_sample = None
            return False
        width, height = capture
        self.last_sample = SimpleNamespace(width=width, height=height)
        return True


def make_window(handle, fingerprint, *, minimized=False, process_id=None):
    return WindowInfo(
        handle=handle,
        title="Adobe Flash Player 11",
        visible=True,
        minimized=minimized,
        rect=(0, 0, 916, 629),
        process_id=process_id if process_id is not None else handle + 1000,
        window_class="ShockwaveFlash",
        launch_fingerprint=fingerprint,
    )


def make_verifier(windows, captures, *, expected=None, foreground=None):
    return TargetDesktopVerifier(
        expected_windows=expected if expected is not None else len(windows),
        title_keywords=["Adobe Flash Player"],
        window_backend=FakeWindowBackend(windows, foreground=foreground),
        capture_backend=FakeCaptureBackend(captures),
    )


def test_verifier_accepts_strict_one_to_one_identity_and_capture():
    windows = [
        replace(make_window(0, "0" * 64), title="Unrelated window"),
        make_window(1, "1" * 64),
        make_window(2, "2" * 64, minimized=True),
    ]
    verifier = make_verifier(
        windows,
        {1: (916, 629), 2: (911, 629)},
        expected=2,
        foreground=1,
    )

    result = verifier.verify()
    payload = result.to_dict()

    assert result.passed is True
    assert result.discovered_windows == 2
    assert result.individually_selected == 2
    assert result.unique_selected_windows == 2
    assert result.captures_passed == 2
    assert result.minimized_windows == 1
    assert result.nonforeground_windows == 1
    assert payload["capture_width"] == {"minimum": 911, "maximum": 916}
    assert payload["wrong_window_selections"] == 0
    assert payload["input_sent"] is False


def test_verifier_fails_closed_on_duplicate_fingerprint():
    fingerprint = "3" * 64
    windows = [
        make_window(1, fingerprint),
        make_window(2, fingerprint),
    ]

    result = make_verifier(
        windows,
        {1: (916, 629), 2: (916, 629)},
    ).verify()

    assert result.passed is False
    assert "fingerprint_duplicate" in result.failure_codes
    assert "identity_selection_failed" in result.failure_codes
    assert result.individually_selected == 0


def test_verifier_fails_closed_on_missing_identity_or_process_id():
    window = make_window(1, None, process_id=0)
    window = WindowInfo(
        handle=window.handle,
        title=window.title,
        visible=window.visible,
        minimized=window.minimized,
        rect=window.rect,
        process_id=None,
        window_class=window.window_class,
        launch_fingerprint=None,
    )

    result = make_verifier([window], {1: (916, 629)}).verify()

    assert result.passed is False
    assert "process_id_missing" in result.failure_codes
    assert "fingerprint_missing_or_invalid" in result.failure_codes
    assert "identity_selection_failed" in result.failure_codes


def test_verifier_reports_count_and_capture_failures_without_aborting():
    windows = [make_window(1, "4" * 64)]

    result = make_verifier(
        windows,
        {},
        expected=2,
    ).verify()

    assert result.passed is False
    assert result.failure_codes == (
        "window_count_mismatch",
        "background_capture_failed",
    )
    assert result.captures_passed == 0


def test_verifier_report_never_contains_identifiers_or_capture_bytes():
    fingerprint = "5" * 64
    window = make_window(987654, fingerprint, process_id=123456)

    payload = make_verifier(
        [window],
        {987654: (916, 629)},
    ).verify().to_dict()
    serialized = repr(payload)

    assert fingerprint not in serialized
    assert "987654" not in serialized
    assert "123456" not in serialized
    assert not any(isinstance(value, bytes) for value in payload.values())
    assert payload["raw_arguments_emitted"] is False
    assert payload["captured_pixels_persisted"] is False


def test_verifier_rejects_invalid_configuration():
    with pytest.raises(ValueError):
        TargetDesktopVerifier(
            expected_windows=0,
            title_keywords=["Adobe Flash Player"],
            window_backend=FakeWindowBackend([]),
            capture_backend=FakeCaptureBackend({}),
        )

    with pytest.raises(ValueError):
        TargetDesktopVerifier(
            expected_windows=14,
            title_keywords=[],
            window_backend=FakeWindowBackend([]),
            capture_backend=FakeCaptureBackend({}),
        )
