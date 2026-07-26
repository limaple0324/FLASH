"""活動進度的原子化 JSON 儲存。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

from domain.progress import ActivityProgress


class ActivityProgressStore:
    SCHEMA_VERSION = 1

    def __init__(self, path: Path):
        self.path = Path(path)
        self.recovered_from_corruption = False
        self.corrupt_backup: Path | None = None

    def load(self) -> tuple[ActivityProgress, ...]:
        self.recovered_from_corruption = False
        self.corrupt_backup = None
        if not self.path.exists():
            return ()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Progress root must be an object.")
            if payload.get("schema_version") != self.SCHEMA_VERSION:
                raise ValueError("Unsupported progress schema version.")
            raw_items = payload.get("progress", [])
            if not isinstance(raw_items, list):
                raise ValueError("progress must be a list.")
            items = tuple(ActivityProgress.from_dict(item) for item in raw_items if isinstance(item, dict))
            if len(items) != len(raw_items):
                raise ValueError("Each progress item must be an object.")
            keys = [(item.activity_id, item.subject_id) for item in items]
            if len(keys) != len(set(keys)):
                raise ValueError("Duplicate activity progress identity.")
            return items
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            self.corrupt_backup = self._preserve_corrupt_file()
            self.recovered_from_corruption = True
            return ()

    def save(self, progress: Iterable[ActivityProgress]) -> None:
        items = tuple(progress)
        keys = [(item.activity_id, item.subject_id) for item in items]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate activity progress identity.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "progress": [item.to_dict() for item in items],
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
