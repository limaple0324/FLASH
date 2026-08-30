from __future__ import annotations

import json
import os
from pathlib import Path
import threading

from .models import AppConfig


APP_DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MagicManorAssistant"
CONFIG_PATH = APP_DATA_DIR / "config.json"
LOG_DIR = APP_DATA_DIR / "logs"


class ConfigStore:
    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()

    def load(self) -> AppConfig:
        with self._lock:
            if not self.path.exists():
                return AppConfig()
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                return AppConfig.from_dict(data)
            except (OSError, ValueError, TypeError):
                backup = self.path.with_suffix(".invalid.json")
                try:
                    self.path.replace(backup)
                except OSError:
                    pass
                return AppConfig()

    def save(self, config: AppConfig) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(config.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
