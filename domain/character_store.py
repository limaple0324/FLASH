"""角色等級與重要度的原子化 JSON 儲存。"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Iterable, Mapping

from domain.character import Character, CharacterImportance


class CharacterStore:
    SCHEMA_VERSION = 1

    def __init__(self, path: Path):
        self.path = Path(path)
        self.backup_path = self.path.with_suffix(self.path.suffix + ".bak")
        self.recovered_from_corruption = False
        self.recovered_from_backup = False
        self.corrupt_backup: Path | None = None

    @staticmethod
    def _character_from_dict(payload: Mapping[str, object]) -> Character:
        character_id = payload.get("character_id")
        display_name = payload.get("display_name")
        level = payload.get("level")
        importance = payload.get("importance")
        if not isinstance(character_id, str) or not isinstance(display_name, str):
            raise ValueError("Character identity fields must be strings.")
        if isinstance(level, bool) or not isinstance(level, int):
            raise ValueError("Character level must be an integer.")
        if not isinstance(importance, str):
            raise ValueError("Character importance must be a string.")
        return Character(
            character_id=character_id,
            display_name=display_name,
            level=level,
            importance=CharacterImportance(importance),
        )

    @classmethod
    def _load_path(cls, path: Path) -> tuple[Character, ...]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Character root must be an object.")
        if payload.get("schema_version") != cls.SCHEMA_VERSION:
            raise ValueError("Unsupported character schema version.")
        raw_characters = payload.get("characters", [])
        if not isinstance(raw_characters, list):
            raise ValueError("characters must be a list.")
        if any(not isinstance(item, Mapping) for item in raw_characters):
            raise ValueError("Each character must be an object.")
        characters = tuple(
            cls._character_from_dict(item) for item in raw_characters
        )
        cls._validate_unique_identities(characters)
        return characters

    @staticmethod
    def _validate_unique_identities(characters: tuple[Character, ...]) -> None:
        identities = [character.character_id for character in characters]
        if len(identities) != len(set(identities)):
            raise ValueError("Duplicate stable character identity.")

    def load(self) -> tuple[Character, ...]:
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

    def _load_backup_or_empty(self) -> tuple[Character, ...]:
        if not self.backup_path.exists():
            return ()
        try:
            characters = self._load_path(self.backup_path)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            return ()
        self.recovered_from_backup = True
        return characters

    def save(self, characters: Iterable[Character]) -> None:
        items = tuple(characters)
        if any(not isinstance(item, Character) for item in items):
            raise TypeError("characters must contain only Character values.")
        self._validate_unique_identities(items)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "characters": [character.to_dict() for character in items],
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
            candidate = self.path.with_suffix(
                self.path.suffix + f".corrupt.{index}"
            )
            index += 1
        try:
            self.path.replace(candidate)
            return candidate
        except OSError:
            return None
