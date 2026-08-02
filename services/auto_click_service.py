"""Player-controlled continuous cursor clicking compatible with 輔V0.2."""

from __future__ import annotations

import ctypes
import os
import queue
import threading
from dataclasses import dataclass
from ctypes import wintypes
from enum import Enum
from typing import Callable, Protocol

from services.game_operation_gate import GameOperationGate


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


@dataclass(frozen=True, slots=True)
class AutoClickPointerSource:
    """Exact foreground window and normalized cursor position for one click."""

    source_handle: int
    x_ratio: float
    y_ratio: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.source_handle, bool)
            or not isinstance(self.source_handle, int)
            or self.source_handle <= 0
        ):
            raise ValueError("source_handle must be positive.")
        if (
            isinstance(self.x_ratio, bool)
            or not isinstance(self.x_ratio, (int, float))
            or not 0.0 <= float(self.x_ratio) <= 1.0
        ):
            raise ValueError("x_ratio must be between 0 and 1.")
        if (
            isinstance(self.y_ratio, bool)
            or not isinstance(self.y_ratio, (int, float))
            or not 0.0 <= float(self.y_ratio) <= 1.0
        ):
            raise ValueError("y_ratio must be between 0 and 1.")


class _DirectLeftDisposition(Enum):
    PHYSICAL = "physical"
    QUEUED = "queued"
    BLOCKED = "blocked"


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


class Win32AutoClickPointerSourceBackend:
    """Capture one foreground client-relative cursor position."""

    def sample(self) -> AutoClickPointerSource | None:
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
        handle = user32.GetForegroundWindow()
        point = wintypes.POINT()
        rect = wintypes.RECT()
        if (
            not handle
            or not user32.GetCursorPos(ctypes.byref(point))
            or not user32.ScreenToClient(handle, ctypes.byref(point))
            or not user32.GetClientRect(handle, ctypes.byref(rect))
        ):
            return None
        width = max(1, rect.right - rect.left)
        height = max(1, rect.bottom - rect.top)
        return AutoClickPointerSource(
            int(handle),
            min(1.0, max(0.0, point.x / max(1, width - 1))),
            min(1.0, max(0.0, point.y / max(1, height - 1))),
        )


class AutoClickService:
    """Schedule clicks without blocking Tk and stop on delivery failure.

    When synchronized left-click delivery is configured and the foreground
    source is eligible, the physical mouse backend is deliberately bypassed.
    The whole click is queued to one ordered worker so a short synthetic
    ``mouse_event`` pulse cannot be missed (or duplicated) by the polling
    mouse monitor.
    """

    _DIRECT_STOP = object()

    def __init__(
        self,
        backend: CursorClickBackend,
        *,
        schedule: Callable[[int, Callable[[], None]], object],
        cancel: Callable[[object], None],
        operation_gate: GameOperationGate | None = None,
    ) -> None:
        if not callable(getattr(backend, "click", None)):
            raise TypeError("backend must provide click(button).")
        self._backend = backend
        self._schedule = schedule
        self._cancel = cancel
        self._operation_gate = operation_gate
        self._settings = AutoClickSettings()
        self._running = False
        self._sent_count = 0
        self._after_id: object | None = None
        self._subscribers: list[Callable[[AutoClickSnapshot], None]] = []
        self._direct_source_provider: (
            Callable[[], AutoClickPointerSource | None] | None
        ) = None
        self._direct_eligible: (
            Callable[[AutoClickPointerSource], bool] | None
        ) = None
        self._direct_deliver: (
            Callable[[AutoClickPointerSource], bool] | None
        ) = None
        self._direct_enabled: Callable[[], bool] | None = None
        self._direct_block_physical_fallback: (
            Callable[[AutoClickPointerSource], bool] | None
        ) = None
        self._direct_queue: queue.Queue[
            tuple[int, AutoClickPointerSource] | object
        ] = queue.Queue()
        self._direct_lock = threading.RLock()
        self._direct_generation = 0
        self._direct_failed_generation: int | None = None
        self._direct_worker: threading.Thread | None = None
        self._direct_context = threading.local()
        self._closed = False

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
        if self._running or self._closed:
            return False
        with self._direct_lock:
            self._direct_failed_generation = None
        self._settings = settings
        self._sent_count = 0
        self._running = True
        self._notify()
        self._tick()
        return True

    def stop(self) -> bool:
        was_running = self._running
        self._running = False
        self.invalidate_direct_sync()
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

    def configure_direct_left_sync(
        self,
        *,
        source_provider: Callable[[], AutoClickPointerSource | None],
        eligible: Callable[[AutoClickPointerSource], bool],
        deliver: Callable[[AutoClickPointerSource], bool],
        enabled: Callable[[], bool],
        block_physical_fallback: (
            Callable[[AutoClickPointerSource], bool] | None
        ) = None,
    ) -> None:
        """Configure one ordered, non-physical synchronized-click path."""
        if not all(
            callable(value)
            for value in (source_provider, eligible, deliver, enabled)
        ):
            raise TypeError("direct left sync callbacks must be callable.")
        if (
            block_physical_fallback is not None
            and not callable(block_physical_fallback)
        ):
            raise TypeError("block_physical_fallback must be callable.")
        self.invalidate_direct_sync()
        with self._direct_lock:
            self._direct_source_provider = source_provider
            self._direct_eligible = eligible
            self._direct_deliver = deliver
            self._direct_enabled = enabled
            self._direct_block_physical_fallback = (
                block_physical_fallback
            )

    def invalidate_direct_sync(self) -> None:
        """Cancel queued clicks that belong to an older sync session."""
        with self._direct_lock:
            self._direct_generation += 1
            self._direct_failed_generation = None
        # Discard pending work immediately instead of letting an old generation
        # occupy the worker. Preserve a close sentinel and Queue accounting.
        with self._direct_queue.mutex:
            retained = [
                item
                for item in self._direct_queue.queue
                if item is self._DIRECT_STOP
            ]
            removed = len(self._direct_queue.queue) - len(retained)
            if removed:
                self._direct_queue.queue.clear()
                self._direct_queue.queue.extend(retained)
                self._direct_queue.unfinished_tasks = max(
                    0,
                    self._direct_queue.unfinished_tasks - removed,
                )
                self._direct_queue.all_tasks_done.notify_all()
                self._direct_queue.not_full.notify_all()

    def direct_sync_execution_allowed(self) -> bool:
        """Return whether the worker's current click still belongs to this session."""
        generation = getattr(self._direct_context, "generation", None)
        with self._direct_lock:
            return (
                not self._closed
                and generation is not None
                and generation == self._direct_generation
            )

    def close(self, timeout_seconds: float = 1.0) -> bool:
        self.stop()
        with self._direct_lock:
            self._closed = True
            worker = self._direct_worker
        if worker is not None and worker.is_alive():
            self._direct_queue.put(self._DIRECT_STOP)
            if (
                worker is not threading.current_thread()
            ):
                worker.join(max(0.0, float(timeout_seconds)))
        stopped = worker is None or not worker.is_alive()
        if stopped:
            with self._direct_lock:
                if self._direct_worker is worker:
                    self._direct_worker = None
        return stopped

    def _ensure_direct_worker(self) -> bool:
        with self._direct_lock:
            if self._closed:
                return False
            worker = self._direct_worker
            if worker is not None and worker.is_alive():
                return True
            worker = threading.Thread(
                target=self._direct_worker_loop,
                name="FLASH-AutoClickSync",
                daemon=True,
            )
            self._direct_worker = worker
            worker.start()
            return True

    def _queue_direct_left_click(self) -> _DirectLeftDisposition:
        if self._settings.button != "left":
            return _DirectLeftDisposition.PHYSICAL
        with self._direct_lock:
            source_provider = self._direct_source_provider
            eligible = self._direct_eligible
            deliver = self._direct_deliver
            enabled = self._direct_enabled
            block_physical_fallback = (
                self._direct_block_physical_fallback
            )
            generation = self._direct_generation
        if (
            source_provider is None
            or eligible is None
            or deliver is None
            or enabled is None
        ):
            return _DirectLeftDisposition.PHYSICAL
        try:
            if not enabled():
                return _DirectLeftDisposition.PHYSICAL
            source = source_provider()
            if source is None:
                return _DirectLeftDisposition.BLOCKED
            if not eligible(source):
                should_block = (
                    block_physical_fallback is not None
                    and block_physical_fallback(source)
                )
                return (
                    _DirectLeftDisposition.BLOCKED
                    if should_block
                    else _DirectLeftDisposition.PHYSICAL
                )
        except Exception:
            return _DirectLeftDisposition.BLOCKED
        if not self._ensure_direct_worker():
            return _DirectLeftDisposition.BLOCKED
        self._direct_queue.put((generation, source))
        return _DirectLeftDisposition.QUEUED

    def _direct_worker_loop(self) -> None:
        while True:
            work = self._direct_queue.get()
            try:
                if work is self._DIRECT_STOP:
                    return
                generation, source = work
                with self._direct_lock:
                    current_generation = self._direct_generation
                    failed_generation = self._direct_failed_generation
                    enabled = self._direct_enabled
                    eligible = self._direct_eligible
                    deliver = self._direct_deliver
                    closed = self._closed
                if (
                    closed
                    or generation != current_generation
                    or failed_generation == generation
                    or enabled is None
                    or eligible is None
                    or deliver is None
                ):
                    continue
                try:
                    still_valid = enabled() and eligible(source)
                except Exception:
                    still_valid = False
                if not still_valid:
                    with self._direct_lock:
                        if generation == self._direct_generation:
                            self._direct_failed_generation = generation
                    continue
                try:
                    self._direct_context.generation = generation
                    delivered = bool(deliver(source))
                except Exception:
                    delivered = False
                finally:
                    self._direct_context.generation = None
                if not delivered:
                    with self._direct_lock:
                        if generation == self._direct_generation:
                            self._direct_failed_generation = generation
            finally:
                self._direct_queue.task_done()

    def _tick(self) -> None:
        self._after_id = None
        if not self._running:
            return
        with self._direct_lock:
            direct_failed = (
                self._direct_failed_generation == self._direct_generation
            )
        if direct_failed:
            self.stop()
            return
        try:
            disposition = self._queue_direct_left_click()
            if disposition is _DirectLeftDisposition.QUEUED:
                delivered = True
            elif disposition is _DirectLeftDisposition.BLOCKED:
                delivered = False
            else:
                lease = (
                    self._operation_gate.acquire(
                        "auto-click",
                        timeout_seconds=0,
                    )
                    if self._operation_gate is not None
                    else None
                )
                if self._operation_gate is not None and lease is None:
                    delivered = False
                else:
                    try:
                        delivered = bool(
                            self._backend.click(self._settings.button)
                        )
                    finally:
                        if lease is not None:
                            lease.release()
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
            self._running = False
            self._notify()
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
