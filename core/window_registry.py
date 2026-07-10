"""Character-oriented window registry for FLASH SP1.

The registry keeps player-facing character identities separate from transient
Windows handles. It never discovers or binds characters automatically; callers
must explicitly register a character identity and confirm window observations.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Mapping


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

    SCHEMA_VERSION = 1

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
            process_id=current.process_id,
            window_class=current.window_class,
            rect=current.rect,
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
        return {
            "schema_version": self.SCHEMA_VERSION,
            "characters": [record.to_dict() for record in self.all()],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "WindowRegistry":
        """Restore persisted identities without trusting stale window handles."""
        version = payload.get("schema_version", cls.SCHEMA_VERSION)
        if version != cls.SCHEMA_VERSION:
            raise ValueError(f"Unsupported registry schema version: {version}")

        raw_characters = payload.get("characters", [])
        if not isinstance(raw_characters, list):
            raise ValueError("characters must be a list.")

        registry = cls()
        for raw in raw_characters:
            if not isinstance(raw, Mapping):
                raise ValueError("Each character record must be an object.")

            character_id = raw.get("character_id")
            display_name = raw.get("display_name")
            if not isinstance(character_id, str) or not isinstance(display_name, str):
                raise ValueError("Character identity fields must be strings.")

            registry.register_character(character_id, display_name)
            current = registry.get(character_id)

            rect_value = raw.get("rect")
            rect: tuple[int, int, int, int] | None = None
            if isinstance(rect_value, (list, tuple)) and len(rect_value) == 4 and all(
                isinstance(value, int) for value in rect_value
            ):
                left, top, right, bottom = rect_value
                if right > left and bottom > top:
                    rect = (left, top, right, bottom)

            process_id = raw.get("process_id")
            if not isinstance(process_id, int) or process_id <= 0:
                process_id = None

            window_class = raw.get("window_class")
            if not isinstance(window_class, str) or not window_class.strip():
                window_class = None

            last_seen_utc = raw.get("last_seen_utc")
            if not isinstance(last_seen_utc, str) or not last_seen_utc.strip():
                last_seen_utc = None

            registry._records[character_id] = CharacterWindowRecord(
                character_id=current.character_id,
                display_name=current.display_name,
                handle=None,
                process_id=process_id,
                window_class=window_class,
                rect=rect,
                health=WindowHealth.UNKNOWN,
                last_seen_utc=last_seen_utc,
                confirmed=False,
            )

        return registry
