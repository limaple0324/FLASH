"""Player-enabled left-button and drag synchronization monitor."""

from __future__ import annotations

import ctypes
import os
import threading
from collections import deque
from dataclasses import dataclass, replace
from ctypes import wintypes
from time import perf_counter_ns
from typing import Callable, Iterable, Protocol

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

    def __init__(
        self,
        *,
        foreground_handle_provider: Callable[[], int | None] | None = None,
        target_handles_provider: Callable[[], Iterable[int]] | None = None,
    ) -> None:
        if foreground_handle_provider is not None and not callable(
            foreground_handle_provider
        ):
            raise TypeError("foreground_handle_provider must be callable.")
        if target_handles_provider is not None and not callable(
            target_handles_provider
        ):
            raise TypeError("target_handles_provider must be callable.")
        self._foreground_handle_provider = foreground_handle_provider
        self._target_handles_provider = target_handles_provider

    def sample(self) -> MouseSample | None:
        if os.name != "nt":
            return None
        user32 = ctypes.windll.user32
        if self._foreground_handle_provider is not None:
            try:
                hwnd = self._foreground_handle_provider()
            except Exception:
                return None
        else:
            user32.GetForegroundWindow.argtypes = ()
            user32.GetForegroundWindow.restype = wintypes.HWND
            hwnd = user32.GetForegroundWindow()
        if (
            not isinstance(hwnd, int)
            or isinstance(hwnd, bool)
            or hwnd <= 0
        ):
            return None
        if self._target_handles_provider is not None:
            try:
                target_handles = {
                    int(item) for item in self._target_handles_provider()
                }
            except Exception:
                return None
            if hwnd not in target_handles:
                return None
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
        interval_ms: int = 2,
    ) -> None:
        self._controller = controller
        self._policy_provider = policy_provider
        self._schedule = schedule
        self._cancel = cancel
        self._state_backend = state_backend or Win32MouseStateBackend()
        self._result_callback = result_callback
        self._interval_ms = max(1, int(interval_ms))
        self._enabled = False
        self._after_id: object | None = None
        self._previous: MouseSample | None = None
        self._queue: deque[
            tuple[int, MouseSample, str, object, int]
        ] = deque()
        self._worker_running = False
        self._queue_lock = threading.Lock()
        self._generation = 0
        self._release_pending_generation: int | None = None
        self._active_event: str | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self) -> bool:
        with self._queue_lock:
            if self._enabled:
                return True
            self._enabled = True
            self._generation += 1
            self._release_pending_generation = None
            self._active_event = None
        self._previous = None
        try:
            self._schedule_next()
        except Exception:
            with self._queue_lock:
                self._enabled = False
                self._generation += 1
            raise
        return True

    def stop(self) -> bool:
        with self._queue_lock:
            self._enabled = False
            self._generation += 1
            self._queue.clear()
            self._release_pending_generation = None
        released = True
        try:
            self._controller.release_pressed_targets()
        except Exception:
            released = False
        cancelled = True
        if self._after_id is not None:
            try:
                self._cancel(self._after_id)
            except Exception:
                cancelled = False
            self._after_id = None
        self._previous = None
        return released and cancelled

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
        try:
            policy = self._policy_provider()
        except Exception:
            return
        start_worker = False
        immediate_release = False
        item = None
        with self._queue_lock:
            if not self._enabled:
                return
            item = (
                self._generation,
                sample,
                event,
                policy,
                perf_counter_ns(),
            )
            if event == "move":
                if (
                    self._queue
                    and self._queue[-1][0] == self._generation
                    and self._queue[-1][2] == "move"
                ):
                    self._queue[-1] = item
                else:
                    self._queue.append(item)
            elif event == "left_up":
                self._release_pending_generation = self._generation
                self._queue = deque(
                    queued
                    for queued in self._queue
                    if not (
                        queued[0] == self._generation
                        and queued[2] == "move"
                    )
                )
                immediate_release = self._active_event == "move"
                if not immediate_release:
                    self._queue.append(item)
            else:
                self._queue.append(item)
            if self._queue and not self._worker_running:
                self._worker_running = True
                start_worker = True

        if immediate_release:
            try:
                self._controller.release_pressed_targets()
                still_pressed = bool(
                    self._controller.has_pressed_targets()
                )
            except Exception:
                still_pressed = True
            if still_pressed and item is not None:
                with self._queue_lock:
                    if (
                        self._enabled
                        and self._generation == item[0]
                    ):
                        self._queue.appendleft(item)
                        if not self._worker_running:
                            self._worker_running = True
                            start_worker = True

        if start_worker:
            threading.Thread(
                target=self._drain_queue,
                name="FLASH-MouseSync",
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
                    sample,
                    event,
                    policy,
                    queued_at_ns,
                ) = self._queue.popleft()
                self._active_event = event
            if not self._execution_allowed(generation):
                with self._queue_lock:
                    self._active_event = None
                continue
            try:
                queue_wait_ns = max(
                    0,
                    perf_counter_ns() - queued_at_ns,
                )
                result = self._controller.send(
                    source_handle=sample.source_handle,
                    x_ratio=sample.x_ratio,
                    y_ratio=sample.y_ratio,
                    event=event,
                    policy=policy,
                    execute=True,
                    execution_guard=(
                        lambda generation=generation, event=event:
                        self._event_execution_allowed(
                            generation,
                            event,
                        )
                    ),
                )
                if isinstance(result, PointerSyncResult):
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
            finally:
                with self._queue_lock:
                    self._active_event = None
                    if event == "left_up":
                        if (
                            self._release_pending_generation
                            == generation
                        ):
                            self._release_pending_generation = None
                    elif (
                        event == "move"
                        and self._release_pending_generation == generation
                        and not any(
                            item[0] == generation
                            and item[2] == "left_up"
                            for item in self._queue
                        )
                    ):
                        self._release_pending_generation = None

    def _execution_allowed(self, generation: int) -> bool:
        with self._queue_lock:
            return self._enabled and self._generation == generation

    def _event_execution_allowed(
        self,
        generation: int,
        event: str,
    ) -> bool:
        with self._queue_lock:
            return (
                self._enabled
                and self._generation == generation
                and not (
                    event == "move"
                    and self._release_pending_generation == generation
                )
            )
