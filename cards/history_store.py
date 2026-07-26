"""斷線與恢復提醒歷史的原子化 JSON 儲存。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

from cards.history import CardHistoryRecord


class CardHistoryStore:
    SCHEMA_VERSION = 1

    def __init__(self, path: Path):
        self.path = Path(path)
        self.recovered_from_corruption = False
        self.corrupt_backup: Path | None = None

    def load(self) -> tuple[CardHistoryRecord, ...]:
        self.recovered_from_corruption = False
        self.corrupt_backup = None
        if not self.path.exists():
            return ()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("History root must be an object.")
            if payload.get("schema_version") != self.SCHEMA_VERSION:
                raise ValueError("Unsupported history schema version.")
            raw_records = payload.get("records", [])
            if not isinstance(raw_records, list):
                raise ValueError("records must be a list.")
            records = tuple(CardHistoryRecord.from_dict(item) for item in raw_records)
            return records
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            self.corrupt_backup = self._preserve_corrupt_file()
            self.recovered_from_corruption = True
            return ()

    def save(self, records: Iterable[CardHistoryRecord]) -> None:
        items = tuple(records)
        if any(not isinstance(item, CardHistoryRecord) for item in items):
            raise TypeError("records must contain only CardHistoryRecord values.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "records": [item.to_dict() for item in items],
        }
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _preserve_corrupt_file(self) -> Path | None:
        if not self.path.exists():
            return None
        candidate = self.path.with_suffix(self.path.suffix + ".corrupt")
        index = 1
        while candidate.exists():
            candidate = self.path.with_suffix(self.path.suffix + f".corrupt.{index}")
            index += 1
        try:
            self.path.replace(candidate)
            return candidate
        except OSError:
            return None
