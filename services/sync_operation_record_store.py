"""One-month in-app role records with permanent daily text archives."""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping


RETENTION_DAYS = 30
DEFERRED_FLUSH_SECONDS = 0.05
PERSISTENCE_RETRY_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class SyncOperationRecord:
    record_id: str
    occurred_at: datetime
    category: str
    role_name: str
    detail: str

    @property
    def player_line(self) -> str:
        local = self.occurred_at.astimezone()
        return (
            f"{local:%Y-%m-%d %H:%M:%S}｜{self.category}｜"
            f"{self.role_name}｜{self.detail}"
        )


@dataclass(frozen=True, slots=True)
class OperationRecordSearchResult:
    daily_file: Path
    date_text: str
    role_name: str
    preview: str


class SyncOperationRecordStore:
    SCHEMA_VERSION = 1

    def __init__(
        self,
        active_path: Path,
        archive_dir: Path,
        *,
        now_provider: Callable[[], datetime] | None = None,
        role_names_provider: Callable[[], tuple[str, ...]] | None = None,
    ) -> None:
        self.active_path = Path(active_path)
        self.archive_dir = Path(archive_dir)
        self.pending_path = self.active_path.with_name(
            f"{self.active_path.name}.pending"
        )
        self._now_provider = now_provider or (
            lambda: datetime.now(timezone.utc)
        )
        self._role_names_provider = role_names_provider or (lambda: ())
        self._lock = threading.RLock()
        self._flush_io_lock = threading.Lock()
        active_records = list(self._load())
        active_ids = {item.record_id for item in active_records}
        pending_records = [
            item
            for item in self._load_pending()
            if item.record_id not in active_ids
        ]
        self._pending_records: list[SyncOperationRecord] = pending_records
        self._flush_timer: threading.Timer | None = None
        self._closed = False
        self._persistence_failure: str | None = None
        self._records = active_records + pending_records
        if not pending_records:
            self.pending_path.unlink(missing_ok=True)
        self.archive_expired()
        self.ensure_daily_file()

    @staticmethod
    def _clean(value: object, *, maximum: int = 240) -> str:
        if not isinstance(value, str):
            raise TypeError("record text must be a string.")
        cleaned = value.strip()
        if (
            not cleaned
            or len(cleaned) > maximum
            or any(ord(character) < 32 for character in cleaned)
        ):
            raise ValueError("record text is invalid.")
        return cleaned

    @staticmethod
    def _parse_record(value: object) -> SyncOperationRecord | None:
        if not isinstance(value, Mapping):
            return None
        try:
            occurred_at = datetime.fromisoformat(str(value["occurred_at"]))
            if occurred_at.tzinfo is None:
                occurred_at = occurred_at.replace(tzinfo=timezone.utc)
            return SyncOperationRecord(
                str(value["record_id"]),
                occurred_at.astimezone(timezone.utc),
                str(value["category"]),
                str(value["role_name"]),
                str(value["detail"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _load(self) -> tuple[SyncOperationRecord, ...]:
        if not self.active_path.is_file():
            return ()
        try:
            payload = json.loads(
                self.active_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            return ()
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema_version") != self.SCHEMA_VERSION
            or not isinstance(payload.get("records"), list)
        ):
            return ()
        return tuple(
            record
            for value in payload["records"]
            if (record := self._parse_record(value)) is not None
        )

    def _load_pending(self) -> tuple[SyncOperationRecord, ...]:
        if not self.pending_path.is_file():
            return ()
        try:
            lines = self.pending_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return ()
        records: list[SyncOperationRecord] = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            record = self._parse_record(value)
            if record is not None:
                records.append(record)
        return tuple(records)

    def _save_records(
        self,
        records: tuple[SyncOperationRecord, ...],
    ) -> None:
        self.active_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "records": [
                {
                    "record_id": item.record_id,
                    "occurred_at": item.occurred_at.isoformat(),
                    "category": item.category,
                    "role_name": item.role_name,
                    "detail": item.detail,
                }
                for item in records
            ],
        }
        temporary = self.active_path.with_name(
            f".{self.active_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.active_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _new_record(
        self,
        category: object,
        role_name: object,
        detail: object,
    ) -> SyncOperationRecord:
        return SyncOperationRecord(
            uuid.uuid4().hex,
            self._now_provider().astimezone(timezone.utc),
            self._clean(category, maximum=40),
            self._clean(role_name, maximum=80),
            self._clean(detail),
        )

    def _cancel_flush_timer_locked(self) -> None:
        timer = self._flush_timer
        self._flush_timer = None
        if timer is not None:
            timer.cancel()

    def _flush_pending_once(self) -> bool:
        """Persist one snapshot without holding the hot-path state lock."""
        with self._flush_io_lock:
            with self._lock:
                pending = tuple(self._pending_records)
                records = tuple(self._records)
            if not pending:
                return True
            try:
                self._rewrite_pending_journal(pending)
                self._append_daily_records(pending)
                self._save_records(records)
            except (OSError, UnicodeError):
                with self._lock:
                    self._persistence_failure = "record_batch_write_failed"
                return False
            processed_ids = {item.record_id for item in pending}
            while True:
                with self._lock:
                    state_snapshot = tuple(self._pending_records)
                    remaining_snapshot = tuple(
                        item
                        for item in state_snapshot
                        if item.record_id not in processed_ids
                    )
                try:
                    self._rewrite_pending_journal(remaining_snapshot)
                except (OSError, UnicodeError):
                    with self._lock:
                        self._persistence_failure = (
                            "record_pending_journal_write_failed"
                        )
                    return False
                with self._lock:
                    if tuple(self._pending_records) != state_snapshot:
                        continue
                    self._pending_records = list(remaining_snapshot)
                    self._persistence_failure = None
                    return True

    def _flush_all_pending(self) -> bool:
        while True:
            with self._lock:
                if not self._pending_records:
                    return True
            if not self._flush_pending_once():
                return False

    def _flush_deferred(self) -> None:
        with self._lock:
            self._flush_timer = None
            if self._closed:
                return
        # Disk work is deliberately outside _lock so input delivery can queue
        # its next role record while the previous batch is persisted.
        succeeded = self._flush_pending_once()
        with self._lock:
            if self._pending_records and self._flush_timer is None:
                self._schedule_flush_locked(
                    DEFERRED_FLUSH_SECONDS
                    if succeeded
                    else PERSISTENCE_RETRY_SECONDS
                )

    def _schedule_flush_locked(
        self,
        delay_seconds: float = DEFERRED_FLUSH_SECONDS,
    ) -> None:
        if self._flush_timer is not None or self._closed:
            return
        timer = threading.Timer(
            max(0.01, float(delay_seconds)),
            self._flush_deferred,
        )
        timer.daemon = True
        self._flush_timer = timer
        timer.start()

    def append(
        self,
        category: object,
        role_name: object,
        detail: object,
    ) -> SyncOperationRecord:
        record = self._new_record(category, role_name, detail)
        with self._lock:
            if self._closed:
                raise RuntimeError("operation record store is closed.")
            self._records.append(record)
            self._pending_records.append(record)
            try:
                self._append_pending_journal(record)
            except (OSError, UnicodeError):
                self._persistence_failure = (
                    "record_pending_journal_write_failed"
                )
            self._cancel_flush_timer_locked()
        if not self.flush():
            raise OSError("operation record could not be persisted.")
        self.archive_expired()
        return record

    def append_deferred(
        self,
        category: object,
        role_name: object,
        detail: object,
    ) -> SyncOperationRecord:
        """Record hot-path events without blocking synchronized input."""
        record = self._new_record(category, role_name, detail)
        with self._lock:
            if self._closed:
                raise RuntimeError("operation record store is closed.")
            self._records.append(record)
            self._pending_records.append(record)
            self._schedule_flush_locked()
        return record

    def flush(self) -> bool:
        with self._lock:
            self._cancel_flush_timer_locked()
        return self._flush_all_pending()

    def close(self) -> bool:
        with self._lock:
            if self._closed:
                return True
            self._cancel_flush_timer_locked()
            self._closed = True
        if self._flush_all_pending():
            return True
        with self._lock:
            self._closed = False
        return False

    def records(self) -> tuple[SyncOperationRecord, ...]:
        with self._lock:
            return tuple(
                sorted(
                    self._records,
                    key=lambda item: (item.occurred_at, item.record_id),
                    reverse=True,
                )
            )

    @property
    def persistence_failure(self) -> str | None:
        with self._lock:
            return self._persistence_failure

    def player_lines(self) -> tuple[str, ...]:
        return tuple(item.player_line for item in self.records())

    def _daily_archive_path(self, local_day: str) -> Path:
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        return self.archive_dir / f"輔_角色紀錄_{local_day}.txt"

    @staticmethod
    def _record_lines(path: Path) -> list[str]:
        if not path.is_file():
            return []
        try:
            return [
                line
                for line in path.read_text(encoding="utf-8").splitlines()
                if len(line.split("｜", 4)) == 5
                and len(line.split("｜", 1)[0]) == 32
            ]
        except (OSError, UnicodeError):
            return []

    def _write_daily_lines(
        self,
        path: Path,
        day: str,
        record_lines: list[str],
    ) -> None:
        by_role: dict[str, list[str]] = {
            role.strip(): []
            for role in self._role_names_provider()
            if isinstance(role, str) and role.strip()
        }
        for line in record_lines:
            parts = line.split("｜", 4)
            by_role.setdefault(parts[3].strip(), []).append(line)
        rendered = [f"輔｜角色每日永久紀錄｜{day}", ""]
        configured_order = [
            role.strip()
            for role in self._role_names_provider()
            if isinstance(role, str) and role.strip()
        ]
        role_order = tuple(
            dict.fromkeys(
                configured_order
                + sorted(
                    (
                        role
                        for role in by_role
                        if role not in configured_order
                    ),
                    key=str.casefold,
                )
            )
        )
        for role_name in role_order:
            rendered.append(f"【角色：{role_name}】")
            rendered.extend(
                sorted(
                    by_role[role_name],
                    key=lambda value: value.split("｜", 4)[1],
                )
            )
            rendered.append("")
        temporary = path.with_name(
            f".{path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                "\n".join(rendered),
                encoding="utf-8",
                newline="\n",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _record_payload(item: SyncOperationRecord) -> dict[str, str]:
        return {
            "record_id": item.record_id,
            "occurred_at": item.occurred_at.isoformat(),
            "category": item.category,
            "role_name": item.role_name,
            "detail": item.detail,
        }

    def _append_pending_journal(self, item: SyncOperationRecord) -> None:
        self.pending_path.parent.mkdir(parents=True, exist_ok=True)
        with self.pending_path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(
                json.dumps(
                    self._record_payload(item),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
            file.flush()

    def _rewrite_pending_journal(
        self,
        items: tuple[SyncOperationRecord, ...],
    ) -> None:
        if not items:
            self.pending_path.unlink(missing_ok=True)
            return
        self.pending_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.pending_path.with_name(
            f".{self.pending_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                "".join(
                    json.dumps(
                        self._record_payload(item),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                    for item in items
                ),
                encoding="utf-8",
                newline="\n",
            )
            os.replace(temporary, self.pending_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _append_daily_records(
        self,
        items: tuple[SyncOperationRecord, ...],
    ) -> tuple[Path, ...]:
        by_day: dict[str, list[SyncOperationRecord]] = {}
        for item in items:
            day = item.occurred_at.astimezone().strftime("%Y-%m-%d")
            by_day.setdefault(day, []).append(item)
        written: list[Path] = []
        for day, day_items in by_day.items():
            path = self._daily_archive_path(day)
            record_lines = self._record_lines(path)
            existing_ids = {
                line.split("｜", 1)[0] for line in record_lines
            }
            for item in day_items:
                if item.record_id in existing_ids:
                    continue
                record_lines.append(f"{item.record_id}｜{item.player_line}")
                existing_ids.add(item.record_id)
            self._write_daily_lines(path, day, record_lines)
            written.append(path)
        return tuple(written)

    def _append_daily_record(self, item: SyncOperationRecord) -> Path:
        paths = self._append_daily_records((item,))
        if not paths:
            raise OSError("daily operation record was not written.")
        return paths[0]

    def ensure_daily_file(self) -> Path:
        self.flush()
        now = self._now_provider().astimezone()
        day = now.strftime("%Y-%m-%d")
        path = self._daily_archive_path(day)
        self._write_daily_lines(path, day, self._record_lines(path))
        return path

    def daily_files(self) -> tuple[Path, ...]:
        self.flush()
        if not self.archive_dir.is_dir():
            return ()
        return tuple(
            sorted(
                self.archive_dir.glob("輔_角色紀錄_????-??-??.txt"),
                key=lambda path: path.name,
                reverse=True,
            )
        )

    def open_daily_file(self, path: object) -> bool:
        if not isinstance(path, (str, Path)):
            return False
        candidate = Path(path).resolve(strict=False)
        archive_root = self.archive_dir.resolve(strict=False)
        try:
            candidate.relative_to(archive_root)
        except ValueError:
            return False
        if candidate.suffix.casefold() != ".txt" or not candidate.is_file():
            return False
        if os.name != "nt":
            return False
        try:
            os.startfile(str(candidate))  # type: ignore[attr-defined]
            return True
        except OSError:
            return False

    def search(
        self,
        date_text: object = "",
        role_name: object = "",
        *,
        maximum_results: int = 200,
    ) -> tuple[OperationRecordSearchResult, ...]:
        if (
            not isinstance(date_text, str)
            or not isinstance(role_name, str)
            or maximum_results <= 0
        ):
            return ()
        wanted_date = date_text.strip()
        wanted_role = role_name.strip().casefold()
        results: list[OperationRecordSearchResult] = []
        for path in self.daily_files():
            file_date = path.stem.removeprefix("輔_角色紀錄_")
            if wanted_date and wanted_date != file_date:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            for line in lines:
                parts = line.split("｜", 4)
                if len(parts) != 5 or len(parts[0]) != 32:
                    continue
                role = parts[3].strip()
                if wanted_role and wanted_role not in role.casefold():
                    continue
                results.append(
                    OperationRecordSearchResult(
                        path,
                        file_date,
                        role,
                        "｜".join(parts[1:]),
                    )
                )
                if len(results) >= maximum_results:
                    return tuple(results)
        return tuple(results)

    def archive_expired(self) -> Path | None:
        now = self._now_provider().astimezone(timezone.utc)
        cutoff = now - timedelta(days=RETENTION_DAYS)
        if not self.flush():
            return None
        with self._flush_io_lock:
            with self._lock:
                records = tuple(self._records)
            expired = [
                item for item in records if item.occurred_at < cutoff
            ]
            if not expired:
                return None
            archived_paths = self._append_daily_records(
                tuple(
                    sorted(
                        expired,
                        key=lambda value: (
                            value.occurred_at,
                            value.record_id,
                        ),
                    )
                )
            )
            expired_ids = {item.record_id for item in expired}
            retained = tuple(
                item for item in records if item.record_id not in expired_ids
            )
            self._save_records(retained)
            with self._lock:
                self._records = [
                    item
                    for item in self._records
                    if item.record_id not in expired_ids
                ]
            return archived_paths[0] if archived_paths else None
