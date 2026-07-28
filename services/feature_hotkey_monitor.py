"""Non-blocking hotkeys for player-controlled features and group launch."""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable, Mapping


FEATURE_HOTKEYS = (
    "",
    "XBUTTON1",
    "XBUTTON2",
    "F1",
    "F2",
    "F3",
    "F4",
    "F5",
    "F6",
    "F7",
    "F8",
    "F9",
    "F10",
    "F11",
    "F12",
)

FEATURE_HOTKEY_VIRTUAL_KEYS = {
    "XBUTTON1": 0x05,
    "XBUTTON2": 0x06,
    **{f"F{number}": 0x6F + number for number in range(1, 13)},
}


def normalize_feature_hotkey(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip().upper()
    return normalized if normalized in FEATURE_HOTKEY_VIRTUAL_KEYS else ""


class Win32FeatureHotkeyStateBackend:
    def is_down(self, virtual_key: int) -> bool:
        if os.name != "nt":
            return False
        user32 = ctypes.windll.user32
        user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
        user32.GetAsyncKeyState.restype = ctypes.c_short
        return bool(user32.GetAsyncKeyState(int(virtual_key)) & 0x8000)


class FeatureHotkeyMonitor:
    """Poll configurable feature hotkeys without swallowing player input."""

    def __init__(
        self,
        callbacks: Mapping[str, Callable[[], None]],
        *,
        hotkeys_provider: Callable[[], Mapping[str, object]],
        schedule: Callable[[int, Callable[[], None]], object],
        cancel: Callable[[object], None],
        state_backend=None,
        interval_ms: int = 20,
    ) -> None:
        if not callbacks or any(
            not isinstance(name, str) or not callable(callback)
            for name, callback in callbacks.items()
        ):
            raise TypeError("callbacks must map feature names to callables.")
        if not callable(hotkeys_provider):
            raise TypeError("hotkeys_provider must be callable.")
        self._callbacks = dict(callbacks)
        self._hotkeys_provider = hotkeys_provider
        self._schedule = schedule
        self._cancel = cancel
        self._state_backend = (
            state_backend or Win32FeatureHotkeyStateBackend()
        )
        self._interval_ms = max(10, int(interval_ms))
        self._running = False
        self._after_id: object | None = None
        self._was_down = dict.fromkeys(self._callbacks, False)

    def start(self) -> bool:
        if self._running:
            return True
        self._running = True
        self._was_down = dict.fromkeys(self._callbacks, False)
        try:
            self._schedule_next()
        except Exception:
            self._running = False
            raise
        return True

    def stop(self) -> bool:
        self._running = False
        cancelled = True
        if self._after_id is not None:
            try:
                self._cancel(self._after_id)
            except Exception:
                cancelled = False
        self._after_id = None
        self._was_down = dict.fromkeys(self._callbacks, False)
        return cancelled

    def _schedule_next(self) -> None:
        if self._running:
            self._after_id = self._schedule(self._interval_ms, self.poll)

    def poll(self) -> None:
        self._after_id = None
        if not self._running:
            return
        try:
            try:
                raw_hotkeys = self._hotkeys_provider()
            except Exception:
                raw_hotkeys = {}
            for name, callback in self._callbacks.items():
                hotkey = normalize_feature_hotkey(raw_hotkeys.get(name))
                is_down = bool(
                    hotkey
                    and self._state_backend.is_down(
                        FEATURE_HOTKEY_VIRTUAL_KEYS[hotkey]
                    )
                )
                if is_down and not self._was_down[name]:
                    callback()
                self._was_down[name] = is_down
        finally:
            self._schedule_next()


class GroupLaunchHotkeyMonitor:
    """Launch the one group assigned to a rising-edge hotkey."""

    def __init__(
        self,
        callback: Callable[[str], None],
        *,
        hotkeys_provider: Callable[[], Mapping[str, object]],
        schedule: Callable[[int, Callable[[], None]], object],
        cancel: Callable[[object], None],
        state_backend=None,
        interval_ms: int = 20,
    ) -> None:
        if not callable(callback):
            raise TypeError("callback must be callable.")
        if not callable(hotkeys_provider):
            raise TypeError("hotkeys_provider must be callable.")
        self._callback = callback
        self._hotkeys_provider = hotkeys_provider
        self._schedule = schedule
        self._cancel = cancel
        self._state_backend = (
            state_backend or Win32FeatureHotkeyStateBackend()
        )
        self._interval_ms = max(10, int(interval_ms))
        self._running = False
        self._after_id: object | None = None
        self._was_down: dict[str, bool] = {}

    def start(self) -> bool:
        if self._running:
            return True
        self._running = True
        self._was_down = {}
        try:
            self._schedule_next()
        except Exception:
            self._running = False
            raise
        return True

    def stop(self) -> bool:
        self._running = False
        cancelled = True
        if self._after_id is not None:
            try:
                self._cancel(self._after_id)
            except Exception:
                cancelled = False
        self._after_id = None
        self._was_down = {}
        return cancelled

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
        try:
            try:
                raw_hotkeys = self._hotkeys_provider()
            except Exception:
                raw_hotkeys = {}
            next_was_down: dict[str, bool] = {}
            for raw_name, raw_hotkey in raw_hotkeys.items():
                if not isinstance(raw_name, str) or not raw_name.strip():
                    continue
                group_name = raw_name.strip()
                hotkey = normalize_feature_hotkey(raw_hotkey)
                is_down = bool(
                    hotkey
                    and self._state_backend.is_down(
                        FEATURE_HOTKEY_VIRTUAL_KEYS[hotkey]
                    )
                )
                if is_down and not self._was_down.get(
                    group_name,
                    False,
                ):
                    self._callback(group_name)
                next_was_down[group_name] = is_down
            self._was_down = next_was_down
        finally:
            self._schedule_next()
