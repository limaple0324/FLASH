"""Player-enabled polling monitor for confirmed game shortcut synchronization."""

from __future__ import annotations

import ctypes
import os
import threading
from collections import deque
from dataclasses import replace
from ctypes import wintypes
from time import perf_counter_ns
from typing import Callable, Protocol

from adapters.windows_input_sync import (
    VIRTUAL_KEY_SEQUENCES,
    InputSyncResult,
    WindowsInputSyncController,
)
from domain.game_shortcuts import CONFIRMED_GAME_SHORTCUTS


class KeyboardStateBackend(Protocol):
    def foreground_game_handle(self) -> int | None:
        """Return the exact foreground game HWND captured with the key."""

    def foreground_is_game(self) -> bool:
        """Return whether the foreground window is a confirmed game window."""

    def is_down(self, virtual_key: int) -> bool:
        """Return the current high-bit state of a virtual key."""

    def conflicting_modifier_down(self) -> bool:
        """Return whether an unrelated Ctrl, Alt, or Windows modifier is held."""


class Win32KeyboardStateBackend:
    """Cheap Win32 polling matching the proven old-program approach."""

    _VK_CONTROL = 0x11
    _VK_MENU = 0x12
    _VK_LWIN = 0x5B
    _VK_RWIN = 0x5C

    def __init__(
        self,
        title_keywords: tuple[str, ...] = ("Adobe Flash Player",),
    ) -> None:
        self._keywords = tuple(
            keyword.strip().casefold()
            for keyword in title_keywords
            if keyword.strip()
        )

    @staticmethod
    def _user32():
        if os.name != "nt":
            return None
        return ctypes.windll.user32

    def foreground_game_handle(self) -> int | None:
        user32 = self._user32()
        if user32 is None:
            return None
        user32.GetForegroundWindow.argtypes = ()
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = (
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        )
        user32.GetWindowTextW.restype = ctypes.c_int

        handle = user32.GetForegroundWindow()
        if not handle:
            return None
        length = max(0, int(user32.GetWindowTextLengthW(handle)))
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, len(buffer))
        title = buffer.value.casefold()
        if not self._keywords or not all(
            keyword in title for keyword in self._keywords
        ):
            return None
        return int(handle)

    def foreground_is_game(self) -> bool:
        return self.foreground_game_handle() is not None

    def is_down(self, virtual_key: int) -> bool:
        user32 = self._user32()
        if user32 is None:
            return False
        user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
        user32.GetAsyncKeyState.restype = wintypes.SHORT
        return bool(user32.GetAsyncKeyState(int(virtual_key)) & 0x8000)

    def conflicting_modifier_down(self) -> bool:
        return any(
            self.is_down(key)
            for key in (
                self._VK_CONTROL,
                self._VK_MENU,
                self._VK_LWIN,
                self._VK_RWIN,
            )
        )


class KeyboardSyncMonitor:
    """Poll confirmed keys and queue every rising edge in arrival order."""

    def __init__(
        self,
        controller: WindowsInputSyncController,
        *,
        policy_provider: Callable[[], object],
        schedule: Callable[[int, Callable[[], None]], object],
        cancel: Callable[[object], None],
        state_backend: KeyboardStateBackend | None = None,
        result_callback: Callable[[InputSyncResult], None] | None = None,
        interval_ms: int = 5,
    ) -> None:
        self._controller = controller
        self._policy_provider = policy_provider
        self._schedule = schedule
        self._cancel = cancel
        self._state_backend = state_backend or Win32KeyboardStateBackend()
        self._result_callback = result_callback
        self._interval_ms = max(2, int(interval_ms))
        self._enabled = False
        self._after_id: object | None = None
        self._key_states = {
            shortcut.key: False for shortcut in CONFIRMED_GAME_SHORTCUTS
        }
        self._queue: deque[
            tuple[int, str, object, int, int]
        ] = deque()
        self._worker_running = False
        self._queue_lock = threading.Lock()
        self._generation = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self) -> None:
        with self._queue_lock:
            if self._enabled:
                return
            self._enabled = True
            self._generation += 1
        self._key_states = dict.fromkeys(self._key_states, False)
        self._schedule_next()

    def stop(self) -> None:
        with self._queue_lock:
            self._enabled = False
            self._generation += 1
            self._queue.clear()
        if self._after_id is not None:
            try:
                self._cancel(self._after_id)
            except Exception:
                pass
        self._after_id = None
        self._key_states = dict.fromkeys(self._key_states, False)

    def _schedule_next(self) -> None:
        if self._enabled:
            self._after_id = self._schedule(self._interval_ms, self.poll)

    def _shortcut_is_down(self, key: str) -> bool:
        virtual_keys = VIRTUAL_KEY_SEQUENCES[key]
        if len(virtual_keys) == 1 and self._state_backend.conflicting_modifier_down():
            return False
        return all(self._state_backend.is_down(value) for value in virtual_keys)

    def poll(self) -> None:
        self._after_id = None
        if not self._enabled:
            return
        try:
            source_handle_getter = getattr(
                self._state_backend,
                "foreground_game_handle",
                None,
            )
            source_handle = (
                source_handle_getter()
                if callable(source_handle_getter)
                else None
            )
            if source_handle is None:
                self._key_states = dict.fromkeys(self._key_states, False)
                return
            for shortcut in CONFIRMED_GAME_SHORTCUTS:
                is_down = self._shortcut_is_down(shortcut.key)
                was_down = self._key_states[shortcut.key]
                self._key_states[shortcut.key] = is_down
                if is_down and not was_down:
                    self._dispatch(
                        shortcut.key,
                        source_handle=source_handle,
                    )
        finally:
            self._schedule_next()

    def _dispatch(
        self,
        key: str,
        *,
        source_handle: int | None = None,
    ) -> None:
        try:
            policy = self._policy_provider()
        except Exception:
            return
        with self._queue_lock:
            if not self._enabled:
                return
            if source_handle is None:
                getter = getattr(
                    self._state_backend,
                    "foreground_game_handle",
                    None,
                )
                source_handle = (
                    getter() if callable(getter) else None
                )
            if (
                not isinstance(source_handle, int)
                or isinstance(source_handle, bool)
                or source_handle <= 0
            ):
                return
            self._queue.append(
                (
                    self._generation,
                    key,
                    policy,
                    source_handle,
                    perf_counter_ns(),
                )
            )
            if self._worker_running:
                return
            self._worker_running = True

        threading.Thread(
            target=self._drain_queue,
            name="FLASH-KeyboardSync",
            daemon=True,
        ).start()

    def _drain_queue(self) -> None:
        while True:
            with self._queue_lock:
                if not self._queue:
                    self._worker_running = False
                    return
                (
                    generation,
                    key,
                    policy,
                    source_handle,
                    queued_at_ns,
                ) = self._queue.popleft()
            if not self._execution_allowed(generation):
                continue
            try:
                queue_wait_ns = max(
                    0,
                    perf_counter_ns() - queued_at_ns,
                )
                result = self._controller.send_approved_key(
                    key,
                    policy=policy,
                    execute=True,
                    exclude_foreground=True,
                    source_handle=source_handle,
                    execution_guard=(
                        lambda generation=generation:
                        self._execution_allowed(generation)
                    ),
                )
                if isinstance(result, InputSyncResult):
                    result = replace(
                        result,
                        queue_wait_ns=queue_wait_ns,
                    )
                if (
                    self._result_callback is not None
                    and self._execution_allowed(generation)
                ):
                    self._schedule(
                        0,
                        lambda result=result: self._result_callback(result),
                    )
            except Exception:
                # One failed delivery must not discard later captured inputs.
                continue

    def _execution_allowed(self, generation: int) -> bool:
        with self._queue_lock:
            return self._enabled and self._generation == generation
