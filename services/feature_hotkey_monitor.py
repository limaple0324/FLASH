"""Non-blocking hotkeys for player-controlled features and group launch."""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable, Mapping


FEATURE_HOTKEY_VIRTUAL_KEYS = {
    "LBUTTON": 0x01,
    "RBUTTON": 0x02,
    "MBUTTON": 0x04,
    "XBUTTON1": 0x05,
    "XBUTTON2": 0x06,
    "BACKSPACE": 0x08,
    "TAB": 0x09,
    "CLEAR": 0x0C,
    "ENTER": 0x0D,
    "SHIFT": 0x10,
    "CTRL": 0x11,
    "ALT": 0x12,
    "PAUSE": 0x13,
    "CAPSLOCK": 0x14,
    "ESC": 0x1B,
    "SPACE": 0x20,
    "PAGEUP": 0x21,
    "PAGEDOWN": 0x22,
    "END": 0x23,
    "HOME": 0x24,
    "LEFT": 0x25,
    "UP": 0x26,
    "RIGHT": 0x27,
    "DOWN": 0x28,
    "SELECT": 0x29,
    "PRINT": 0x2A,
    "EXECUTE": 0x2B,
    "PRINTSCREEN": 0x2C,
    "INSERT": 0x2D,
    "DELETE": 0x2E,
    "HELP": 0x2F,
    **{str(number): 0x30 + number for number in range(10)},
    **{letter: ord(letter) for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
    "LWIN": 0x5B,
    "RWIN": 0x5C,
    "APPS": 0x5D,
    "SLEEP": 0x5F,
    **{f"NUMPAD{number}": 0x60 + number for number in range(10)},
    "MULTIPLY": 0x6A,
    "ADD": 0x6B,
    "SEPARATOR": 0x6C,
    "SUBTRACT": 0x6D,
    "DECIMAL": 0x6E,
    "DIVIDE": 0x6F,
    **{f"F{number}": 0x6F + number for number in range(1, 25)},
    "NUMLOCK": 0x90,
    "SCROLLLOCK": 0x91,
    "LSHIFT": 0xA0,
    "RSHIFT": 0xA1,
    "LCTRL": 0xA2,
    "RCTRL": 0xA3,
    "LALT": 0xA4,
    "RALT": 0xA5,
    "BROWSER_BACK": 0xA6,
    "BROWSER_FORWARD": 0xA7,
    "BROWSER_REFRESH": 0xA8,
    "BROWSER_STOP": 0xA9,
    "BROWSER_SEARCH": 0xAA,
    "BROWSER_FAVORITES": 0xAB,
    "BROWSER_HOME": 0xAC,
    "VOLUME_MUTE": 0xAD,
    "VOLUME_DOWN": 0xAE,
    "VOLUME_UP": 0xAF,
    "MEDIA_NEXT": 0xB0,
    "MEDIA_PREVIOUS": 0xB1,
    "MEDIA_STOP": 0xB2,
    "MEDIA_PLAY_PAUSE": 0xB3,
    "LAUNCH_MAIL": 0xB4,
    "LAUNCH_MEDIA": 0xB5,
    "LAUNCH_APP1": 0xB6,
    "LAUNCH_APP2": 0xB7,
    "OEM_1": 0xBA,
    "OEM_PLUS": 0xBB,
    "OEM_COMMA": 0xBC,
    "OEM_MINUS": 0xBD,
    "OEM_PERIOD": 0xBE,
    "OEM_2": 0xBF,
    "OEM_3": 0xC0,
    "OEM_4": 0xDB,
    "OEM_5": 0xDC,
    "OEM_6": 0xDD,
    "OEM_7": 0xDE,
    "OEM_8": 0xDF,
    "OEM_102": 0xE2,
}

FEATURE_HOTKEYS = ("", *FEATURE_HOTKEY_VIRTUAL_KEYS)

_FEATURE_HOTKEY_ALIASES = {
    "MOUSELEFT": "LBUTTON",
    "MOUSERIGHT": "RBUTTON",
    "MOUSEMIDDLE": "MBUTTON",
    "MOUSE4": "XBUTTON1",
    "MOUSE5": "XBUTTON2",
    "ESCAPE": "ESC",
    "RETURN": "ENTER",
    "CONTROL": "CTRL",
    "MENU": "ALT",
    "SPACEBAR": "SPACE",
    "PGUP": "PAGEUP",
    "PGDN": "PAGEDOWN",
    "INS": "INSERT",
    "DEL": "DELETE",
    "ARROWLEFT": "LEFT",
    "ARROWUP": "UP",
    "ARROWRIGHT": "RIGHT",
    "ARROWDOWN": "DOWN",
}


def normalize_feature_hotkey(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip().upper().replace(" ", "")
    normalized = _FEATURE_HOTKEY_ALIASES.get(normalized, normalized)
    if normalized in FEATURE_HOTKEY_VIRTUAL_KEYS:
        return normalized
    if normalized.startswith("VK_") and len(normalized) == 5:
        try:
            virtual_key = int(normalized[3:], 16)
        except ValueError:
            return ""
        if 0x01 <= virtual_key <= 0xFE:
            return normalized
    return ""


def feature_hotkey_virtual_key(value: object) -> int | None:
    normalized = normalize_feature_hotkey(value)
    if not normalized:
        return None
    if normalized.startswith("VK_"):
        return int(normalized[3:], 16)
    return FEATURE_HOTKEY_VIRTUAL_KEYS[normalized]


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
                virtual_key = feature_hotkey_virtual_key(hotkey)
                is_down = bool(
                    virtual_key is not None
                    and self._state_backend.is_down(virtual_key)
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
                virtual_key = feature_hotkey_virtual_key(hotkey)
                is_down = bool(
                    virtual_key is not None
                    and self._state_backend.is_down(virtual_key)
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
