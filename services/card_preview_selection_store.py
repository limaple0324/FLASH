"""玩家明確選定的提醒卡預覽方案之原子化 JSON 儲存。"""

from __future__ import annotations

import json
import os
from pathlib import Path


class CardPreviewSelectionStore:
    SCHEMA_VERSION = 1

    def __init__(self, path: Path):
        self.path = Path(path)
        self.recovered_from_corruption = False
        self.corrupt_backup: Path | None = None

    def load(self) -> str | None:
        self.recovered_from_corruption = False
        self.corrupt_backup = None
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Selection root must be an object.")
            if payload.get("schema_version") != self.SCHEMA_VERSION:
                raise ValueError("Unsupported selection schema version.")
            profile_id = payload.get("selected_profile_id")
            if not isinstance(profile_id, str) or not profile_id.strip():
                raise ValueError("selected_profile_id must be a non-empty string.")
            return profile_id
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            self.corrupt_backup = self._preserve_corrupt_file()
            self.recovered_from_corruption = True
            return None

    def save(self, selected_profile_id: str | None) -> None:
        if selected_profile_id is None:
            self.path.unlink(missing_ok=True)
            return
        if not isinstance(selected_profile_id, str) or not selected_profile_id.strip():
            raise ValueError("selected_profile_id must be a non-empty string.")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "selected_profile_id": selected_profile_id,
        }
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
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
            candidate = self.path.with_suffix(
                self.path.suffix + f".corrupt.{index}"
            )
            index += 1
        try:
            self.path.replace(candidate)
            return candidate
        except OSError:
            return None
