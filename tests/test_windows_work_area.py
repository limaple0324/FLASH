import pytest

from adapters.windows_work_area import (
    SPI_GETWORKAREA,
    WindowsWorkAreaReader,
    WorkAreaUnavailableError,
)
from ui.card_overlay import WorkArea


class FakeSystemParametersInfo:
    def __init__(self, rect=(0, 0, 1920, 1040), *, succeeded=True):
        self.rect = rect
        self.succeeded = succeeded
        self.calls = []

    def __call__(self, action, parameter, rect_pointer, flags):
        self.calls.append((action, parameter, flags))
        if self.succeeded:
            target = rect_pointer._obj
            target.left, target.top, target.right, target.bottom = self.rect
        return int(self.succeeded)


def test_reader_returns_primary_area_above_the_taskbar():
    api = FakeSystemParametersInfo()

    area = WindowsWorkAreaReader(api).read()

    assert area == WorkArea(left=0, top=0, right=1920, bottom=1040)
    assert api.calls == [(SPI_GETWORKAREA, 0, 0)]


def test_reader_preserves_negative_monitor_coordinates():
    api = FakeSystemParametersInfo(rect=(-1600, -120, 0, 860))

    assert WindowsWorkAreaReader(api).read() == WorkArea(
        left=-1600,
        top=-120,
        right=0,
        bottom=860,
    )


def test_reader_reports_windows_api_failure():
    with pytest.raises(WorkAreaUnavailableError, match="did not return"):
        WindowsWorkAreaReader(FakeSystemParametersInfo(succeeded=False)).read()


def test_reader_rejects_invalid_bounds_from_windows():
    with pytest.raises(WorkAreaUnavailableError, match="invalid work area"):
        WindowsWorkAreaReader(FakeSystemParametersInfo(rect=(100, 0, 100, 900))).read()


def test_reader_without_injected_api_is_safe_off_windows(monkeypatch):
    monkeypatch.setattr("adapters.windows_work_area.os.name", "posix")

    with pytest.raises(WorkAreaUnavailableError, match="unavailable on this platform"):
        WindowsWorkAreaReader().read()
