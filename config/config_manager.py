"""JSON configuration manager for FLASH."""

import json
from pathlib import Path
from typing import Any


class ConfigManager:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        if not self.config_path.exists():
            self.data = {}
            self.save()
            return

        with self.config_path.open("r", encoding="utf-8") as file:
            self.data = json.load(file)

    def save(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with self.config_path.open("w", encoding="utf-8") as file:
            json.dump(self.data, file, ensure_ascii=False, indent=2)

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
