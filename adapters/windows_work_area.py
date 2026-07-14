"""Read the Windows desktop work area without choosing overlay styling."""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable

from ui.card_overlay import WorkArea


SPI_GETWORKAREA = 0x0030


class WorkAreaUnavailableError(RuntimeError):
    """Raised when Windows cannot provide a trustworthy work-area boundary."""


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_int32),
        ("top", ctypes.c_int32),
        ("right", ctypes.c_int32),
        ("bottom", ctypes.c_int32),
    ]


SystemParametersInfo = Callable[[int, int, object, int], int]


class WindowsWorkAreaReader:
    """Read the primary monitor area left after Windows reserves the taskbar."""

    def __init__(self, system_parameters_info: SystemParametersInfo | None = None):
        self._system_parameters_info = system_parameters_info

    def _resolve_api(self) -> SystemParametersInfo:
        if self._system_parameters_info is not None:
            return self._system_parameters_info
        if os.name != "nt":
            raise WorkAreaUnavailableError("Windows work area is unavailable on this platform.")

        try:
            return ctypes.windll.user32.SystemParametersInfoW
        except (AttributeError, OSError) as exc:
            raise WorkAreaUnavailableError("Windows work-area API is unavailable.") from exc

    def read(self) -> WorkArea:
        """Return the current primary work area, excluding the Windows taskbar."""
        rect = _Rect()
        try:
            succeeded = self._resolve_api()(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
        except WorkAreaUnavailableError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise WorkAreaUnavailableError("Windows work area could not be read.") from exc

        if not succeeded:
            raise WorkAreaUnavailableError("Windows did not return a work area.")

        try:
            return WorkArea(
                left=int(rect.left),
                top=int(rect.top),
                right=int(rect.right),
                bottom=int(rect.bottom),
            )
        except ValueError as exc:
            raise WorkAreaUnavailableError("Windows returned an invalid work area.") from exc
