"""每角色遊戲延伸資料的原子化 JSON 儲存。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Mapping

from domain.character_game_data import CharacterGameData


class CharacterGameDataStore:
    SCHEMA_VERSION = 1

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> tuple[CharacterGameData, ...]:
        if not self.path.exists():
            return ()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Character game data root must be an object.")
            if set(payload) != {"schema_version", "records"}:
                raise ValueError("Character game data root fields are invalid.")
            if payload["schema_version"] != self.SCHEMA_VERSION:
                raise ValueError("Unsupported character game data schema.")
            raw_records = payload["records"]
            if not isinstance(raw_records, list) or any(
                not isinstance(item, Mapping) for item in raw_records
            ):
                raise ValueError("records must be a list of objects.")
            records = tuple(
                CharacterGameData.from_dict(item) for item in raw_records
            )
            self._validate_unique(records)
            return records
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            self._preserve_corrupt_file()
            return ()

    def save(self, records: Iterable[CharacterGameData]) -> None:
        items = tuple(records)
        if any(not isinstance(item, CharacterGameData) for item in items):
            raise TypeError(
                "records must contain only CharacterGameData values."
            )
        self._validate_unique(items)
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

    @staticmethod
    def _validate_unique(records: tuple[CharacterGameData, ...]) -> None:
        identities = [record.character_id for record in records]
        if len(identities) != len(set(identities)):
            raise ValueError("Duplicate character game data identity.")

    def _preserve_corrupt_file(self) -> None:
        if not self.path.exists():
            return
        candidate = self.path.with_suffix(self.path.suffix + ".corrupt")
        index = 1
        while candidate.exists():
            candidate = self.path.with_suffix(
                self.path.suffix + f".corrupt.{index}"
            )
            index += 1
        try:
            self.path.replace(candidate)
        except OSError:
            pass
