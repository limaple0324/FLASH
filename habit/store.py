"""活動順序觀察與玩家確認習慣的原子化 JSON 儲存。"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Mapping

from habit.models import ActivityOrderHabitMemory


class ActivityOrderHabitStore:
    SCHEMA_VERSION = 1

    def __init__(self, path: Path):
        self.path = Path(path)
        self.backup_path = self.path.with_suffix(self.path.suffix + ".bak")
        self.recovered_from_corruption = False
        self.recovered_from_backup = False
        self.corrupt_backup: Path | None = None

    @classmethod
    def _load_path(cls, path: Path) -> ActivityOrderHabitMemory:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("Habit root must be an object.")
        if payload.get("schema_version") != cls.SCHEMA_VERSION:
            raise ValueError("Unsupported habit schema version.")
        memory = payload.get("activity_order")
        if not isinstance(memory, Mapping):
            raise ValueError("activity_order must be an object.")
        return ActivityOrderHabitMemory.from_dict(memory)

    def load(self) -> ActivityOrderHabitMemory:
        self.recovered_from_corruption = False
        self.recovered_from_backup = False
        self.corrupt_backup = None
        if not self.path.exists():
            return self._load_backup_or_empty()
        try:
            return self._load_path(self.path)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            self.corrupt_backup = self._preserve_corrupt_file()
            self.recovered_from_corruption = True
            return self._load_backup_or_empty()

    def _load_backup_or_empty(self) -> ActivityOrderHabitMemory:
        if not self.backup_path.exists():
            return ActivityOrderHabitMemory()
        try:
            memory = self._load_path(self.backup_path)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            return ActivityOrderHabitMemory()
        self.recovered_from_backup = True
        return memory

    def save(self, memory: ActivityOrderHabitMemory) -> None:
        if not isinstance(memory, ActivityOrderHabitMemory):
            raise TypeError("memory must be ActivityOrderHabitMemory.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "activity_order": memory.to_dict(),
        }
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            if self.path.exists():
                shutil.copy2(self.path, self.backup_path)
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
