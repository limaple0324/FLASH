"""玩家習慣觀察與確認結果的原子化 JSON 儲存。"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Mapping

from habit.preference_models import PlayerHabitMemory


class PlayerHabitStore:
    SCHEMA_VERSION = 1

    def __init__(self, path: Path):
        self.path = Path(path)
        self.backup_path = self.path.with_suffix(self.path.suffix + ".bak")
        self.recovered_from_corruption = False
        self.recovered_from_backup = False

    @classmethod
    def _load_path(cls, path: Path) -> PlayerHabitMemory:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("Habit root must be an object.")
        if payload.get("schema_version") != cls.SCHEMA_VERSION:
            raise ValueError("Unsupported habit schema version.")
        memory = payload.get("player_habits")
        if not isinstance(memory, Mapping):
            raise ValueError("player_habits must be an object.")
        return PlayerHabitMemory.from_dict(memory)

    def load(self) -> PlayerHabitMemory:
        self.recovered_from_corruption = False
        self.recovered_from_backup = False
        if self.path.exists():
            try:
                return self._load_path(self.path)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
                self._preserve_corrupt_file()
                self.recovered_from_corruption = True
        if self.backup_path.exists():
            try:
                memory = self._load_path(self.backup_path)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
                return PlayerHabitMemory()
            self.recovered_from_backup = True
            return memory
        return PlayerHabitMemory()

    def save(self, memory: PlayerHabitMemory) -> None:
        if not isinstance(memory, PlayerHabitMemory):
            raise TypeError("memory must be PlayerHabitMemory.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "player_habits": memory.to_dict(),
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

    def _preserve_corrupt_file(self) -> None:
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
            return
