"""Persist exact managed game-window identities and close only verified ones."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from adapters.windows_battle_restart import (
    WindowCloseBackend,
    Win32WindowCloseBackend,
)
from adapters.windows_launch_fingerprint import normalize_launch_fingerprint
from adapters.windows_window import WindowBackend, WindowInfo
from services.group_launch_service import GroupLaunchTarget


@dataclass(frozen=True, slots=True)
class ManagedGameWindow:
    group_name: str
    role_name: str
    process_id: int
    window_handle: int
    launch_fingerprint: str


@dataclass(frozen=True, slots=True)
class ManagedGameStopResult:
    success: bool
    stopped_count: int = 0
    failure_code: str | None = None


class ManagedGameProcessService:
    """Own one atomic runtime record and fail closed on identity drift."""

    VERSION = 1

    def __init__(
        self,
        state_path: Path,
        window_backend: WindowBackend,
        *,
        close_backend: WindowCloseBackend | None = None,
        title_keywords: Iterable[str] = ("Adobe Flash Player",),
        close_timeout_seconds: float = 10.0,
        poll_seconds: float = 0.1,
        monotonic_clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        record_callback: Callable[[str, str, str], object] | None = None,
    ) -> None:
        if close_timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("managed close timing values must be positive.")
        self.path = Path(state_path)
        self._window_backend = window_backend
        self._close_backend = close_backend or Win32WindowCloseBackend()
        self._keywords = tuple(
            keyword.strip().casefold()
            for keyword in title_keywords
            if isinstance(keyword, str) and keyword.strip()
        )
        self._close_timeout_seconds = float(close_timeout_seconds)
        self._poll_seconds = float(poll_seconds)
        self._monotonic_clock = monotonic_clock
        self._sleeper = sleeper
        self._record_callback = record_callback
        self._lock = threading.RLock()
        self._state_available = True
        self._records = self._load()

    @staticmethod
    def _clean_record(value: object) -> ManagedGameWindow | None:
        if not isinstance(value, Mapping):
            return None
        group_name = value.get("group_name")
        role_name = value.get("role_name")
        process_id = value.get("process_id")
        window_handle = value.get("window_handle")
        fingerprint = normalize_launch_fingerprint(
            value.get("launch_fingerprint")
        )
        if (
            not isinstance(group_name, str)
            or not group_name.strip()
            or not isinstance(role_name, str)
            or not role_name.strip()
            or isinstance(process_id, bool)
            or not isinstance(process_id, int)
            or process_id <= 0
            or isinstance(window_handle, bool)
            or not isinstance(window_handle, int)
            or window_handle <= 0
            or fingerprint is None
        ):
            return None
        return ManagedGameWindow(
            group_name.strip(),
            role_name.strip(),
            process_id,
            window_handle,
            fingerprint,
        )

    def _load(self) -> dict[str, ManagedGameWindow]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            self._state_available = False
            return {}
        if (
            not isinstance(payload, Mapping)
            or payload.get("version") != self.VERSION
            or not isinstance(payload.get("windows"), list)
        ):
            self._state_available = False
            return {}
        records: dict[str, ManagedGameWindow] = {}
        for value in payload["windows"]:
            record = self._clean_record(value)
            if (
                record is None
                or record.launch_fingerprint in records
                or any(
                    existing.window_handle == record.window_handle
                    or existing.process_id == record.process_id
                    for existing in records.values()
                )
            ):
                self._state_available = False
                return {}
            records[record.launch_fingerprint] = record
        return records

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.VERSION,
            "windows": [
                asdict(record)
                for record in sorted(
                    self._records.values(),
                    key=lambda item: (
                        item.group_name.casefold(),
                        item.role_name.casefold(),
                        item.launch_fingerprint,
                    ),
                )
            ],
        }
        temporary = self.path.with_name(f"{self.path.name}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def remember_group_windows(
        self,
        group_name: object,
        values: Iterable[tuple[GroupLaunchTarget, WindowInfo]],
    ) -> bool:
        if not isinstance(group_name, str) or not group_name.strip():
            return False
        if not self._state_available:
            return False
        cleaned_group = group_name.strip()
        proposed: dict[str, ManagedGameWindow] = {}
        for target, window in values:
            fingerprint = normalize_launch_fingerprint(
                window.launch_fingerprint
            )
            if (
                not isinstance(target, GroupLaunchTarget)
                or fingerprint is None
                or fingerprint != target.fingerprint
                or isinstance(window.process_id, bool)
                or not isinstance(window.process_id, int)
                or window.process_id <= 0
                or not isinstance(window.handle, int)
                or window.handle <= 0
                or fingerprint in proposed
            ):
                return False
            proposed[fingerprint] = ManagedGameWindow(
                cleaned_group,
                target.display_name,
                window.process_id,
                window.handle,
                fingerprint,
            )
        if (
            len({record.process_id for record in proposed.values()})
            != len(proposed)
            or len({record.window_handle for record in proposed.values()})
            != len(proposed)
        ):
            return False
        if not proposed:
            return False
        with self._lock:
            original = dict(self._records)
            proposed_process_ids = {
                record.process_id for record in proposed.values()
            }
            proposed_handles = {
                record.window_handle for record in proposed.values()
            }
            self._records = {
                fingerprint: record
                for fingerprint, record in self._records.items()
                if fingerprint not in proposed
                and record.process_id not in proposed_process_ids
                and record.window_handle not in proposed_handles
            }
            self._records.update(proposed)
            try:
                self._save()
            except OSError:
                self._records = original
                return False
        for record in proposed.values():
            self._record(
                record.role_name,
                "已保存受管程序身分",
            )
        return True

    def _record(self, role_name: str, detail: str) -> None:
        if self._record_callback is None:
            return
        try:
            self._record_callback("停止全部", role_name, detail)
        except Exception:
            pass

    def _candidate_windows(self) -> tuple[WindowInfo, ...]:
        return tuple(
            window
            for window in self._window_backend.list_windows()
            if self._keywords
            and all(
                keyword in window.title.casefold()
                for keyword in self._keywords
            )
        )

    @staticmethod
    def _exact_match(
        record: ManagedGameWindow,
        windows: Iterable[WindowInfo],
    ) -> tuple[WindowInfo, ...]:
        return tuple(
            window
            for window in windows
            if window.handle == record.window_handle
            and window.process_id == record.process_id
            and normalize_launch_fingerprint(
                window.launch_fingerprint
            )
            == record.launch_fingerprint
        )

    def _wait_closed(self, handle: int) -> bool:
        deadline = self._monotonic_clock() + self._close_timeout_seconds
        while self._close_backend.is_window(handle):
            if self._monotonic_clock() >= deadline:
                return False
            self._sleeper(self._poll_seconds)
        return True

    def stop_all(self) -> ManagedGameStopResult:
        if not self._state_available:
            return ManagedGameStopResult(
                False,
                failure_code="managed_game_state_unavailable",
            )
        with self._lock:
            records = tuple(self._records.values())
        if not records:
            return ManagedGameStopResult(True)
        try:
            windows = self._candidate_windows()
        except Exception:
            return ManagedGameStopResult(
                False,
                failure_code="managed_game_state_unavailable",
            )

        stopped: set[str] = set()
        stale: set[str] = set()
        failed = 0
        for record in records:
            same_handle = tuple(
                window
                for window in windows
                if window.handle == record.window_handle
            )
            if not same_handle:
                stale.add(record.launch_fingerprint)
                self._record(record.role_name, "受管視窗已不存在")
                continue
            matches = self._exact_match(record, same_handle)
            if len(matches) != 1:
                failed += 1
                stale.add(record.launch_fingerprint)
                self._record(record.role_name, "受管視窗身分不唯一，保持不動")
                continue
            window = matches[0]
            try:
                closed = (
                    self._close_backend.is_window(window.handle)
                    and self._close_backend.close_window(window.handle)
                    and self._wait_closed(window.handle)
                )
            except Exception:
                closed = False
            if not closed:
                failed += 1
                self._record(record.role_name, "受管視窗關閉失敗")
                continue
            stopped.add(record.launch_fingerprint)
            self._record(record.role_name, "受管視窗已停止")

        removable = stopped | stale
        if removable:
            with self._lock:
                original = dict(self._records)
                for fingerprint in removable:
                    self._records.pop(fingerprint, None)
                try:
                    self._save()
                except OSError:
                    self._records = original
                    failed += len(removable)
                    stopped.clear()
                    stale.clear()

        return ManagedGameStopResult(
            failed == 0,
            stopped_count=len(stopped),
            failure_code=(
                None if failed == 0 else "managed_game_stop_partial"
            ),
        )
