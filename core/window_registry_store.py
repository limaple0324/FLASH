"""Persistent storage for the SP1 character window registry."""

from __future__ import annotations

import json
import os
from pathlib import Path

from core.window_registry import WindowRegistry


class WindowRegistryStore:
    """Load and save registry data using atomic replacement and safe recovery."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.recovered_from_corruption = False
        self.corrupt_backup: Path | None = None

    def load(self) -> WindowRegistry:
        self.recovered_from_corruption = False
        self.corrupt_backup = None
        if not self.path.exists():
            return WindowRegistry()

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Registry root must be an object.")
            return WindowRegistry.from_dict(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            self.corrupt_backup = self._preserve_corrupt_file()
            self.recovered_from_corruption = True
            return WindowRegistry()

    def save(self, registry: WindowRegistry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(registry.to_dict(), ensure_ascii=False, indent=2)
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as file:
                file.write(payload)
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
