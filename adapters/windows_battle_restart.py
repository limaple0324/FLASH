"""Close exactly one verified battle-disconnected window and reopen its shortcut."""

from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass
from ctypes import wintypes
from typing import Callable, Iterable, Protocol

from adapters.windows_launch_fingerprint import normalize_launch_fingerprint
from adapters.windows_window import WindowBackend, WindowInfo
from services.group_launch_service import GroupLaunchTarget


class WindowCloseBackend(Protocol):
    def is_window(self, handle: int) -> bool:
        """Return whether the exact top-level HWND still exists."""

    def close_window(self, handle: int) -> bool:
        """Request a normal close for one exact top-level HWND."""


class ShortcutOpenBackend(Protocol):
    def open_shortcut(self, target: GroupLaunchTarget) -> bool:
        """Open only the already-validated shortcut target."""


@dataclass(frozen=True, slots=True)
class BattleRestartResult:
    success: bool
    failure_code: str | None = None
    window_closed: bool = False
    shortcut_open_requested: bool = False


class Win32WindowCloseBackend:
    WM_CLOSE = 0x0010

    @staticmethod
    def _user32():
        if os.name != "nt":
            return None
        return ctypes.windll.user32

    @staticmethod
    def _configure(user32) -> None:
        user32.IsWindow.argtypes = (wintypes.HWND,)
        user32.IsWindow.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.PostMessageW.restype = wintypes.BOOL

    def is_window(self, handle: int) -> bool:
        user32 = self._user32()
        if user32 is None:
            return False
        self._configure(user32)
        return bool(user32.IsWindow(wintypes.HWND(handle)))

    def close_window(self, handle: int) -> bool:
        user32 = self._user32()
        if user32 is None:
            return False
        self._configure(user32)
        hwnd = wintypes.HWND(handle)
        if not user32.IsWindow(hwnd):
            return False
        return bool(user32.PostMessageW(hwnd, self.WM_CLOSE, 0, 0))


class WindowsShortcutOpenBackend:
    def open_shortcut(self, target: GroupLaunchTarget) -> bool:
        if os.name != "nt" or not target.shortcut_path.is_file():
            return False
        try:
            os.startfile(str(target.shortcut_path))  # type: ignore[attr-defined]
            return True
        except OSError:
            return False


class WindowsBattleWindowRestarter:
    """Fail closed unless HWND, PID, fingerprint, and shortcut all agree."""

    def __init__(
        self,
        window_backend: WindowBackend,
        close_backend: WindowCloseBackend,
        open_backend: ShortcutOpenBackend,
        *,
        close_timeout_seconds: float = 10.0,
        poll_seconds: float = 0.1,
        monotonic_clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if close_timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("timeouts must be positive.")
        self._window_backend = window_backend
        self._close_backend = close_backend
        self._open_backend = open_backend
        self._close_timeout_seconds = float(close_timeout_seconds)
        self._poll_seconds = float(poll_seconds)
        self._monotonic_clock = monotonic_clock
        self._sleeper = sleeper

    def restart(
        self,
        window: WindowInfo,
        target: GroupLaunchTarget,
    ) -> BattleRestartResult:
        fingerprint = normalize_launch_fingerprint(window.launch_fingerprint)
        if (
            not window.handle
            or not isinstance(window.process_id, int)
            or window.process_id <= 0
            or fingerprint is None
            or fingerprint != target.fingerprint
        ):
            return BattleRestartResult(
                False,
                "battle_window_identity_invalid",
            )

        exact = tuple(
            candidate
            for candidate in self._window_backend.list_windows()
            if candidate.handle == window.handle
            and candidate.process_id == window.process_id
            and normalize_launch_fingerprint(
                candidate.launch_fingerprint
            )
            == fingerprint
        )
        if len(exact) != 1:
            return BattleRestartResult(
                False,
                "battle_window_identity_changed",
            )
        if not self._close_backend.is_window(window.handle):
            return BattleRestartResult(
                False,
                "battle_window_missing",
            )
        if not self._close_backend.close_window(window.handle):
            return BattleRestartResult(
                False,
                "battle_window_close_failed",
            )

        deadline = self._monotonic_clock() + self._close_timeout_seconds
        while self._close_backend.is_window(window.handle):
            if self._monotonic_clock() >= deadline:
                return BattleRestartResult(
                    False,
                    "battle_window_close_timeout",
                )
            self._sleeper(self._poll_seconds)

        if not self._open_backend.open_shortcut(target):
            return BattleRestartResult(
                False,
                "battle_shortcut_open_failed",
                window_closed=True,
            )
        return BattleRestartResult(
            True,
            window_closed=True,
            shortcut_open_requested=True,
        )

    def reopen_missing(
        self,
        target: GroupLaunchTarget,
        candidate_windows: Iterable[WindowInfo],
    ) -> BattleRestartResult:
        """Retry one shortcut only when no possibly-duplicate window exists."""
        candidates = tuple(candidate_windows)
        fingerprints = tuple(
            normalize_launch_fingerprint(window.launch_fingerprint)
            for window in candidates
        )
        if any(fingerprint is None for fingerprint in fingerprints):
            return BattleRestartResult(
                False,
                "battle_window_existing_state_unknown",
            )
        if len(fingerprints) != len(set(fingerprints)):
            return BattleRestartResult(
                False,
                "battle_window_identity_duplicate",
            )
        if target.fingerprint in fingerprints:
            return BattleRestartResult(
                False,
                "battle_window_already_exists",
            )
        if not self._open_backend.open_shortcut(target):
            return BattleRestartResult(
                False,
                "battle_shortcut_open_failed",
            )
        return BattleRestartResult(
            True,
            shortcut_open_requested=True,
        )
