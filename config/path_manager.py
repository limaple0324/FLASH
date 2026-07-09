"""Centralized filesystem paths for FLASH."""

from pathlib import Path


class PathManager:
    def __init__(self, root: Path | None = None):
        self.root = root or Path(__file__).resolve().parent.parent
        self.config_dir().mkdir(parents=True, exist_ok=True)
        self.logs_dir().mkdir(parents=True, exist_ok=True)

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
