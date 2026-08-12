"""Thread-safe player-visible status for smart reconnect failures."""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReconnectFailureStatus:
    key: str
    message: str
    revision: int


class ReconnectFailureStatusService:
    """Keep one mutable status row per role without exposing internal identity."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, ReconnectFailureStatus] = {}
        self._revision = 0

    @staticmethod
    def _clean(value: object, *, field: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{field} must be a string.")
        cleaned = value.strip()
        if not cleaned or len(cleaned) > 120:
            raise ValueError(f"{field} is invalid.")
        if any(ord(character) < 32 for character in cleaned):
            raise ValueError(f"{field} contains control characters.")
        return cleaned

    def report(self, key: object, subject_name: object) -> None:
        safe_key = self._clean(key, field="key")
        safe_subject = self._clean(subject_name, field="subject_name")
        message = f"{safe_subject}－重連失敗"
        with self._lock:
            self._revision += 1
            self._items[safe_key] = ReconnectFailureStatus(
                safe_key,
                message,
                self._revision,
            )

    def clear(self, key: object) -> bool:
        if not isinstance(key, str) or not key.strip():
            return False
        with self._lock:
            removed = self._items.pop(key.strip(), None)
            if removed is not None:
                self._revision += 1
                return True
            return False

    def snapshot(self) -> tuple[ReconnectFailureStatus, ...]:
        with self._lock:
            return tuple(
                sorted(
                    self._items.values(),
                    key=lambda item: (item.revision, item.message),
                )
            )

    def has(self, key: object) -> bool:
        if not isinstance(key, str) or not key.strip():
            return False
        with self._lock:
            return key.strip() in self._items
