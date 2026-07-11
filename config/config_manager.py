"""JSON configuration manager for FLASH."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class ConfigManager:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.data: dict[str, Any] = {}
        self.recovered_from_corruption = False
        self.corrupt_backup_path: Path | None = None
        self.load()

    def load(self) -> None:
        if not self.config_path.exists():
            self.data = {}
            self.save()
            return

        try:
            with self.config_path.open("r", encoding="utf-8") as file:
                loaded = json.load(file)
            if not isinstance(loaded, dict):
                raise ValueError("Configuration root must be a JSON object.")
            self.data = loaded
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._recover_corrupt_config()

    def _recover_corrupt_config(self) -> None:
        """Preserve an unreadable config and rebuild a clean settings file."""
        backup = self.config_path.with_suffix(self.config_path.suffix + ".corrupt")
        counter = 1
        while backup.exists():
            backup = self.config_path.with_suffix(self.config_path.suffix + f".corrupt.{counter}")
            counter += 1

        self.config_path.replace(backup)
        self.corrupt_backup_path = backup
        self.recovered_from_corruption = True
        self.data = {}
        self.save()

    def save(self) -> None:
        """Write configuration atomically to reduce partial-file corruption."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(self.data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(self.config_path)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()

    def update_values(self, values: dict[str, Any]) -> None:
        """Persist only values that actually changed."""
        changed = False
        for key, value in values.items():
            if self.data.get(key) != value:
                self.data[key] = value
                changed = True
        if changed:
            self.save()

    def ensure_defaults(self, defaults: dict[str, Any]) -> None:
        changed = False
        for key, value in defaults.items():
            if key not in self.data:
                self.data[key] = value
                changed = True
        if changed:
            self.save()
