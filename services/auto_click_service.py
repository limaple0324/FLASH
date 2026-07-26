"""Player-controlled continuous cursor clicking compatible with 輔V0.2."""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass(frozen=True, slots=True)
class AutoClickSettings:
    interval_ms: int = 20
    button: str = "left"
    repeat_forever: bool = True
    repeat_count: int = 1

    def __post_init__(self) -> None:
        if (
            isinstance(self.interval_ms, bool)
            or not isinstance(self.interval_ms, int)
            or not 1 <= self.interval_ms <= 600_000
        ):
            raise ValueError("interval_ms must be between 1 and 600000.")
        if self.button not in {"left", "right"}:
            raise ValueError("button must be left or right.")
        if (
            isinstance(self.repeat_count, bool)
            or not isinstance(self.repeat_count, int)
            or not 1 <= self.repeat_count <= 999_999
        ):
            raise ValueError("repeat_count must be between 1 and 999999.")


@dataclass(frozen=True, slots=True)
class AutoClickSnapshot:
    running: bool
    sent_count: int
    settings: AutoClickSettings


class CursorClickBackend(Protocol):
    def click(self, button: str) -> bool:
        """Click at the current real cursor position."""


class Win32CursorClickBackend:
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010

    def click(self, button: str) -> bool:
        if os.name != "nt" or button not in {"left", "right"}:
            return False
        user32 = ctypes.windll.user32
        user32.mouse_event.argtypes = (
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_size_t,
        )
        user32.mouse_event.restype = None
        down, up = (
            (self.MOUSEEVENTF_RIGHTDOWN, self.MOUSEEVENTF_RIGHTUP)
            if button == "right"
            else (self.MOUSEEVENTF_LEFTDOWN, self.MOUSEEVENTF_LEFTUP)
        )
        user32.mouse_event(down, 0, 0, 0, 0)
        user32.mouse_event(up, 0, 0, 0, 0)
        return True


class AutoClickService:
    """Schedule clicks without blocking Tk and stop on any delivery failure."""

    def __init__(
        self,
        backend: CursorClickBackend,
        *,
        schedule: Callable[[int, Callable[[], None]], object],
        cancel: Callable[[object], None],
    ) -> None:
        if not callable(getattr(backend, "click", None)):
            raise TypeError("backend must provide click(button).")
        self._backend = backend
        self._schedule = schedule
        self._cancel = cancel
        self._settings = AutoClickSettings()
        self._running = False
        self._sent_count = 0
        self._after_id: object | None = None
        self._subscribers: list[Callable[[AutoClickSnapshot], None]] = []

    @property
    def running(self) -> bool:
        return self._running

    def snapshot(self) -> AutoClickSnapshot:
        return AutoClickSnapshot(
            self._running,
            self._sent_count,
            self._settings,
        )

    def subscribe(
        self,
        callback: Callable[[AutoClickSnapshot], None],
    ) -> None:
        if callback not in self._subscribers:
            self._subscribers.append(callback)

    def unsubscribe(
        self,
        callback: Callable[[AutoClickSnapshot], None],
    ) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def _notify(self) -> None:
        snapshot = self.snapshot()
        for callback in tuple(self._subscribers):
            callback(snapshot)

    def start(self, settings: AutoClickSettings) -> bool:
        if not isinstance(settings, AutoClickSettings):
            raise TypeError("settings must be AutoClickSettings.")
        if self._running:
            return False
        self._settings = settings
        self._sent_count = 0
        self._running = True
        self._notify()
        self._tick()
        return True

    def stop(self) -> bool:
        was_running = self._running
        self._running = False
        if self._after_id is not None:
            try:
                self._cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        if was_running:
            self._notify()
        return was_running

    def toggle(self, settings: AutoClickSettings) -> bool:
        if self._running:
            self.stop()
            return False
        self.start(settings)
        return True

    def _tick(self) -> None:
        self._after_id = None
        if not self._running:
            return
        try:
            delivered = bool(self._backend.click(self._settings.button))
        except OSError:
            delivered = False
        if not delivered:
            self.stop()
            return
        self._sent_count += 1
        self._notify()
        if (
            not self._settings.repeat_forever
            and self._sent_count >= self._settings.repeat_count
        ):
            self.stop()
            return
        self._after_id = self._schedule(
            self._settings.interval_ms,
            self._tick,
        )


class FunctionKeyStateBackend(Protocol):
    def is_down(self, virtual_key: int) -> bool:
        """Return the high-bit key state."""


class Win32FunctionKeyStateBackend:
    def is_down(self, virtual_key: int) -> bool:
        if os.name != "nt":
            return False
        user32 = ctypes.windll.user32
        user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
        user32.GetAsyncKeyState.restype = ctypes.c_short
        return bool(user32.GetAsyncKeyState(int(virtual_key)) & 0x8000)


class AutoClickHotkeyMonitor:
    """Use the confirmed legacy F1 rising edge to toggle continuous clicking."""

    VK_F1 = 0x70

    def __init__(
        self,
        toggle: Callable[[], None],
        *,
        schedule: Callable[[int, Callable[[], None]], object],
        cancel: Callable[[object], None],
        state_backend: FunctionKeyStateBackend | None = None,
        interval_ms: int = 20,
    ) -> None:
        self._toggle = toggle
        self._schedule = schedule
        self._cancel = cancel
        self._state_backend = state_backend or Win32FunctionKeyStateBackend()
        self._interval_ms = max(10, int(interval_ms))
        self._running = False
        self._was_down = False
        self._after_id: object | None = None

    def start(self) -> bool:
        if self._running:
            return False
        self._running = True
        self._was_down = False
        self._schedule_next()
        return True

    def stop(self) -> bool:
        was_running = self._running
        self._running = False
        if self._after_id is not None:
            try:
                self._cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        self._was_down = False
        return was_running

    def _schedule_next(self) -> None:
        if self._running:
            self._after_id = self._schedule(
                self._interval_ms,
                self.poll,
            )

    def poll(self) -> None:
        self._after_id = None
        if not self._running:
            return
        is_down = self._state_backend.is_down(self.VK_F1)
        if is_down and not self._was_down:
            self._toggle()
        self._was_down = is_down
        self._schedule_next()
