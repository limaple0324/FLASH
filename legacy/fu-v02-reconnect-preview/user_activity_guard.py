from __future__ import annotations

import ctypes
from ctypes import wintypes
import threading
import time


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class UserActivityGuard:
    """Pause one game window for three minutes after real local user input.

    GetLastInputInfo is updated by physical mouse/keyboard input, but not by the
    WM_* background messages used by this application.  The foreground root at
    the moment of that input is the only window whose deadline is extended.
    """

    def __init__(self, quiet_seconds: float = 180.0) -> None:
        self.quiet_seconds = max(1.0, float(quiet_seconds))
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._lock = threading.RLock()
        self._busy_until: dict[int, float] = {}
        self._last_input_tick = self._read_last_input_tick()

    def root(self, hwnd: int) -> int:
        value = int(hwnd or 0)
        if value <= 0:
            return 0
        try:
            return int(self.user32.GetAncestor(wintypes.HWND(value), 2) or value)
        except Exception:
            return value

    def _read_last_input_tick(self) -> int:
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        try:
            if self.user32.GetLastInputInfo(ctypes.byref(info)):
                return int(info.dwTime)
        except Exception:
            pass
        return 0

    def _snapshot(self) -> tuple[int, int, int]:
        tick = self._read_last_input_tick()
        try:
            foreground = self.root(int(self.user32.GetForegroundWindow() or 0))
        except Exception:
            foreground = 0
        try:
            current_tick = int(self.user32.GetTickCount()) & 0xFFFFFFFF
        except Exception:
            current_tick = tick
        idle_ms = (current_tick - tick) & 0xFFFFFFFF
        return tick, foreground, idle_ms

    def observe(self) -> None:
        tick, foreground, idle_ms = self._snapshot()
        now = time.monotonic()
        with self._lock:
            changed = bool(tick and tick != self._last_input_tick)
            self._last_input_tick = tick or self._last_input_tick
            # The idle fallback covers input made immediately before the first
            # worker poll. Background PostMessage clicks do not reset this timer.
            if foreground and (changed or idle_ms <= 1200):
                self._busy_until[foreground] = now + self.quiet_seconds
            for root, deadline in list(self._busy_until.items()):
                if deadline <= now:
                    self._busy_until.pop(root, None)

    def remaining(self, hwnd: int) -> float:
        self.observe()
        root = self.root(hwnd)
        with self._lock:
            return max(0.0, float(self._busy_until.get(root, 0.0)) - time.monotonic())

    def blocked(self, hwnd: int) -> bool:
        return self.remaining(hwnd) > 0.0

    def wait_until_allowed(self, hwnd: int, cancelled=None) -> bool:
        while True:
            remaining = self.remaining(hwnd)
            if remaining <= 0.0:
                return True
            if cancelled is not None and cancelled.is_set():
                return False
            time.sleep(min(0.5, max(0.05, remaining)))


USER_ACTIVITY_GUARD = UserActivityGuard(180.0)
