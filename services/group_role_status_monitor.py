"""Background refresh for homepage role rows."""

from __future__ import annotations

import threading
from collections.abc import Callable

from services.group_role_status_service import GroupRoleStatusService


class GroupRoleStatusMonitor:
    def __init__(
        self,
        service: GroupRoleStatusService,
        group_name_provider: Callable[[], str | None],
        *,
        interval_seconds: float = 2.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive.")
        self._service = service
        self._group_name_provider = group_name_provider
        self._interval_seconds = float(interval_seconds)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._service.refresh(self._group_name_provider())
            except Exception:
                # Status observation must never interrupt the player.
                pass
            if self._stop_event.wait(self._interval_seconds):
                break

    def start(self) -> bool:
        with self._lock:
            if self.running:
                return False
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="FLASH-GroupRoleStatus",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self, timeout_seconds: float = 5.0) -> bool:
        with self._lock:
            thread = self._thread
            if thread is None:
                return True
            self._stop_event.set()
        if thread is not threading.current_thread():
            thread.join(max(0.0, timeout_seconds))
        stopped = not thread.is_alive()
        if stopped:
            with self._lock:
                if self._thread is thread:
                    self._thread = None
        return stopped
