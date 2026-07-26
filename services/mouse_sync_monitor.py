"""Player-enabled left-button and drag synchronization monitor."""

from __future__ import annotations

import ctypes
import os
import threading
from dataclasses import dataclass
from ctypes import wintypes
from typing import Callable, Protocol

from adapters.windows_pointer_sync import (
    PointerSyncResult,
    WindowsPointerSyncController,
)


@dataclass(frozen=True, slots=True)
class MouseSample:
    source_handle: int
    x_ratio: float
    y_ratio: float
    left_down: bool


class MouseStateBackend(Protocol):
    def sample(self) -> MouseSample | None: ...


class Win32MouseStateBackend:
    VK_LBUTTON = 0x01

    def sample(self) -> MouseSample | None:
        if os.name != "nt":
            return None
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.argtypes = ()
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
        user32.GetCursorPos.restype = wintypes.BOOL
        user32.ScreenToClient.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.POINT),
        )
        user32.ScreenToClient.restype = wintypes.BOOL
        user32.GetClientRect.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        )
        user32.GetClientRect.restype = wintypes.BOOL
        user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
        user32.GetAsyncKeyState.restype = wintypes.SHORT
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        point = wintypes.POINT()
        rect = wintypes.RECT()
        if (
            not user32.GetCursorPos(ctypes.byref(point))
            or not user32.ScreenToClient(hwnd, ctypes.byref(point))
            or not user32.GetClientRect(hwnd, ctypes.byref(rect))
        ):
            return None
        width = max(1, rect.right - rect.left)
        height = max(1, rect.bottom - rect.top)
        return MouseSample(
            int(hwnd),
            min(1.0, max(0.0, point.x / max(1, width - 1))),
            min(1.0, max(0.0, point.y / max(1, height - 1))),
            bool(user32.GetAsyncKeyState(self.VK_LBUTTON) & 0x8000),
        )


class MouseSyncMonitor:
    def __init__(
        self,
        controller: WindowsPointerSyncController,
        *,
        policy_provider: Callable[[], object],
        schedule: Callable[[int, Callable[[], None]], object],
        cancel: Callable[[object], None],
        state_backend: MouseStateBackend | None = None,
        result_callback: Callable[[PointerSyncResult], None] | None = None,
        interval_ms: int = 10,
    ) -> None:
        self._controller = controller
        self._policy_provider = policy_provider
        self._schedule = schedule
        self._cancel = cancel
        self._state_backend = state_backend or Win32MouseStateBackend()
        self._result_callback = result_callback
        self._interval_ms = max(5, int(interval_ms))
        self._enabled = False
        self._after_id: object | None = None
        self._previous: MouseSample | None = None
        self._busy = False
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self) -> None:
        if self._enabled:
            return
        self._enabled = True
        self._previous = None
        self._schedule_next()

    def stop(self) -> None:
        self._enabled = False
        if self._after_id is not None:
            try:
                self._cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        self._previous = None

    def _schedule_next(self) -> None:
        if self._enabled:
            self._after_id = self._schedule(
                self._interval_ms,
                self.poll,
            )

    def poll(self) -> None:
        self._after_id = None
        if not self._enabled:
            return
        try:
            sample = self._state_backend.sample()
            previous = self._previous
            if sample is None:
                return
            self._previous = sample
            if sample.left_down and (
                previous is None or not previous.left_down
            ):
                self._dispatch(sample, "left_down")
            elif (
                not sample.left_down
                and previous is not None
                and previous.left_down
            ):
                self._dispatch(sample, "left_up")
            elif (
                sample.left_down
                and previous is not None
                and previous.left_down
                and (
                    sample.source_handle != previous.source_handle
                    or abs(sample.x_ratio - previous.x_ratio) >= 0.001
                    or abs(sample.y_ratio - previous.y_ratio) >= 0.001
                )
            ):
                self._dispatch(sample, "move")
        finally:
            self._schedule_next()

    def _dispatch(self, sample: MouseSample, event: str) -> None:
        with self._lock:
            if self._busy:
                return
            self._busy = True

        def worker() -> None:
            try:
                result = self._controller.send(
                    source_handle=sample.source_handle,
                    x_ratio=sample.x_ratio,
                    y_ratio=sample.y_ratio,
                    event=event,
                    policy=self._policy_provider(),
                    execute=True,
                )
                if self._result_callback is not None:
                    self._schedule(
                        0,
                        lambda result=result: self._result_callback(result),
                    )
            finally:
                with self._lock:
                    self._busy = False

        threading.Thread(
            target=worker,
            name="FLASH-MouseSync",
            daemon=True,
        ).start()
