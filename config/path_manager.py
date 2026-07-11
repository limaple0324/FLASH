"""Centralized filesystem paths for FLASH."""

from __future__ import annotations

import os
import sys
from pathlib import Path


class PathManager:
    """Resolve writable application paths in source and packaged builds."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else self._default_root()
        self.config_dir().mkdir(parents=True, exist_ok=True)
        self.logs_dir().mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _default_root() -> Path:
        if getattr(sys, "frozen", False):
            base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
            if base:
                return Path(base) / "FLASH"
            return Path.home() / ".flash"
        return Path(__file__).resolve().parent.parent

    def config_dir(self) -> Path:
        return self.root / "config"

    def logs_dir(self) -> Path:
        return self.root / "logs"

    def data_dir(self) -> Path:
        path = self.root / "data"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def config_file(self, filename: str) -> Path:
        return self.config_dir() / filename

    def log_file(self, filename: str) -> Path:
        return self.logs_dir() / filename
