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
        self._now_provider = now_provider or (
            lambda: datetime.now(timezone.utc)
        )
        self._role_names_provider = role_names_provider or (lambda: ())
        self._lock = threading.RLock()
        self._records = list(self._load())
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

    def _save(self) -> None:
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
                for item in self._records
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

    def append(
        self,
        category: object,
        role_name: object,
        detail: object,
    ) -> SyncOperationRecord:
        record = SyncOperationRecord(
            uuid.uuid4().hex,
            self._now_provider().astimezone(timezone.utc),
            self._clean(category, maximum=40),
            self._clean(role_name, maximum=80),
            self._clean(detail),
        )
        with self._lock:
            self._append_daily_record(record)
            self._records.append(record)
            self._save()
        self.archive_expired()
        return record

    def records(self) -> tuple[SyncOperationRecord, ...]:
        with self._lock:
            return tuple(
                sorted(
                    self._records,
                    key=lambda item: (item.occurred_at, item.record_id),
                    reverse=True,
                )
            )

    def player_lines(self) -> tuple[str, ...]:
        return tuple(item.player_line for item in self.records())

    def _daily_archive_path(self, local_day: str) -> Path:
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        return self.archive_dir / f"輔_角色紀錄_{local_day}.txt"

    def _append_daily_record(self, item: SyncOperationRecord) -> Path:
        day = item.occurred_at.astimezone().strftime("%Y-%m-%d")
        path = self._daily_archive_path(day)
        existing_lines: list[str] = []
        if path.is_file():
            try:
                existing_lines = path.read_text(
                    encoding="utf-8"
                ).splitlines()
            except (OSError, UnicodeError):
                existing_lines = []
        record_lines = [
            line
            for line in existing_lines
            if len(line.split("｜", 4)) == 5
            and len(line.split("｜", 1)[0]) == 32
        ]
        existing_ids = {
            line.split("｜", 1)[0] for line in record_lines
        }
        if item.record_id in existing_ids:
            return path
        record_lines.append(f"{item.record_id}｜{item.player_line}")
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
        return path

    def ensure_daily_file(self) -> Path:
        now = self._now_provider().astimezone()
        day = now.strftime("%Y-%m-%d")
        path = self._daily_archive_path(day)
        existing_lines: list[str] = []
        if path.is_file():
            try:
                existing_lines = [
                    line
                    for line in path.read_text(
                        encoding="utf-8"
                    ).splitlines()
                    if len(line.split("｜", 4)) == 5
                    and len(line.split("｜", 1)[0]) == 32
                ]
            except (OSError, UnicodeError):
                existing_lines = []
        by_role: dict[str, list[str]] = {
            role.strip(): []
            for role in self._role_names_provider()
            if isinstance(role, str) and role.strip()
        }
        for line in existing_lines:
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
        return path

    def daily_files(self) -> tuple[Path, ...]:
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
        with self._lock:
            expired = [
                item for item in self._records if item.occurred_at < cutoff
            ]
            if not expired:
                return None
            archived_paths = [
                self._append_daily_record(item)
                for item in sorted(
                    expired,
                    key=lambda value: (
                        value.occurred_at,
                        value.record_id,
                    ),
                )
            ]
            expired_ids = {item.record_id for item in expired}
            self._records = [
                item
                for item in self._records
                if item.record_id not in expired_ids
            ]
            self._save()
            return archived_paths[0] if archived_paths else None
