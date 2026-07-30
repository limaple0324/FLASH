"""JSON configuration manager for FLASH."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator


class ConfigManager:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.data: dict[str, Any] = {}
        self.recovered_from_corruption = False
        self.corrupt_backup_path: Path | None = None
        self._transaction_depth = 0
        self._transaction_snapshot: dict[str, Any] | None = None
        self._transaction_dirty = False
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
        if self._transaction_depth > 0:
            self._transaction_dirty = True
            return
        self._save_now()

    def _save_now(self) -> None:
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

    @contextmanager
    def transaction(self) -> Iterator["ConfigManager"]:
        """Publish related setting changes once, or keep the prior file intact."""
        outermost = self._transaction_depth == 0
        if outermost:
            self._transaction_snapshot = deepcopy(self.data)
            self._transaction_dirty = False
        self._transaction_depth += 1
        try:
            yield self
        except BaseException:
            self._transaction_depth -= 1
            if outermost:
                snapshot = self._transaction_snapshot or {}
                self.data.clear()
                self.data.update(snapshot)
                self._transaction_snapshot = None
                self._transaction_dirty = False
            raise
        else:
            self._transaction_depth -= 1
            if not outermost:
                return
            snapshot = self._transaction_snapshot or {}
            dirty = self._transaction_dirty
            self._transaction_snapshot = None
            self._transaction_dirty = False
            if not dirty:
                return
            try:
                self._save_now()
            except BaseException:
                self.data.clear()
                self.data.update(snapshot)
                raise

    def replace_all(self, values: dict[str, Any]) -> None:
        """Atomically replace all settings, used only for a failed batch rollback."""
        replacement = deepcopy(values)
        previous = deepcopy(self.data)
        self.data.clear()
        self.data.update(replacement)
        try:
            self.save()
        except BaseException:
            self.data.clear()
            self.data.update(previous)
            raise
