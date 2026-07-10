"""Character-oriented window registry for FLASH SP1.

The registry keeps player-facing character identities separate from transient
Windows handles. It never discovers or binds characters automatically; callers
must explicitly register a character identity and confirm window observations.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable


class WindowHealth(str, Enum):
    UNKNOWN = "unknown"
    READY = "ready"
    WARNING = "warning"
    UNSAFE = "unsafe"
    OFFLINE = "offline"


@dataclass(frozen=True, slots=True)
class CharacterWindowRecord:
    character_id: str
    display_name: str
    handle: int | None = None
    process_id: int | None = None
    window_class: str | None = None
    rect: tuple[int, int, int, int] | None = None
    health: WindowHealth = WindowHealth.UNKNOWN
    last_seen_utc: str | None = None
    confirmed: bool = False

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["health"] = self.health.value
        return data


class WindowRegistry:
    """Track stable character identities and their current window observations."""

    def __init__(self) -> None:
        self._records: dict[str, CharacterWindowRecord] = {}

    @staticmethod
    def _clean(value: str, field: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{field} must not be empty.")
        return cleaned

    def register_character(self, character_id: str, display_name: str) -> CharacterWindowRecord:
        character_id = self._clean(character_id, "character_id")
        display_name = self._clean(display_name, "display_name")
        current = self._records.get(character_id)
        if current is not None:
            if current.display_name != display_name:
                raise ValueError("Character ID is already registered with another display name.")
            return current
        record = CharacterWindowRecord(character_id=character_id, display_name=display_name)
        self._records[character_id] = record
        return record

    def confirm_window(
        self,
        character_id: str,
        *,
        handle: int,
        rect: tuple[int, int, int, int],
        health: WindowHealth,
        process_id: int | None = None,
        window_class: str | None = None,
    ) -> CharacterWindowRecord:
        current = self.get(character_id)
        if handle <= 0:
            raise ValueError("handle must be positive.")
        left, top, right, bottom = rect
        if right <= left or bottom <= top:
            raise ValueError("rect must have positive width and height.")
        record = CharacterWindowRecord(
            character_id=current.character_id,
            display_name=current.display_name,
            handle=handle,
            process_id=process_id,
            window_class=window_class,
            rect=rect,
            health=health,
            last_seen_utc=datetime.now(timezone.utc).isoformat(),
            confirmed=True,
        )
        self._records[character_id] = record
        return record

    def mark_offline(self, character_id: str) -> CharacterWindowRecord:
        current = self.get(character_id)
        record = CharacterWindowRecord(
            character_id=current.character_id,
            display_name=current.display_name,
            health=WindowHealth.OFFLINE,
            last_seen_utc=current.last_seen_utc,
            confirmed=False,
        )
        self._records[character_id] = record
        return record

    def get(self, character_id: str) -> CharacterWindowRecord:
        key = self._clean(character_id, "character_id")
        try:
            return self._records[key]
        except KeyError as exc:
            raise KeyError(f"Unknown character ID: {key}") from exc

    def all(self) -> tuple[CharacterWindowRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    def to_dict(self) -> dict[str, object]:
        return {"characters": [record.to_dict() for record in self.all()]}
