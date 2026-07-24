"""靈魂石角色紀錄的原子化 JSON 儲存。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Mapping

from domain.soul_stone import SoulStoneRecord


class SoulStoneStore:
    SCHEMA_VERSION = 1

    def __init__(self, path: Path):
        self.path = Path(path)
        self.recovered_from_corruption = False
        self.corrupt_backup: Path | None = None

    @staticmethod
    def _validate_unique_characters(records: tuple[SoulStoneRecord, ...]) -> None:
        character_ids = [record.character_id for record in records]
        if len(character_ids) != len(set(character_ids)):
            raise ValueError("Duplicate soul stone character identity.")

    def load(self) -> tuple[SoulStoneRecord, ...]:
        self.recovered_from_corruption = False
        self.corrupt_backup = None
        if not self.path.exists():
            return ()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Soul stone root must be an object.")
            if set(payload) != {"schema_version", "records"}:
                raise ValueError("Soul stone root fields are invalid.")
            if payload["schema_version"] != self.SCHEMA_VERSION:
                raise ValueError("Unsupported soul stone schema version.")
            raw_records = payload["records"]
            if not isinstance(raw_records, list):
                raise ValueError("records must be a list.")
            if any(not isinstance(item, Mapping) for item in raw_records):
                raise ValueError("Each soul stone record must be an object.")
            records = tuple(
                SoulStoneRecord.from_dict(item) for item in raw_records
            )
            self._validate_unique_characters(records)
            return records
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            self.corrupt_backup = self._preserve_corrupt_file()
            self.recovered_from_corruption = True
            return ()

    def save(self, records: Iterable[SoulStoneRecord]) -> None:
        items = tuple(records)
        if any(not isinstance(item, SoulStoneRecord) for item in items):
            raise TypeError("records must contain only SoulStoneRecord values.")
        self._validate_unique_characters(items)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "records": [record.to_dict() for record in items],
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
            candidate = self.path.with_suffix(
                self.path.suffix + f".corrupt.{index}"
            )
            index += 1
        try:
            self.path.replace(candidate)
            return candidate
        except OSError:
            return None
