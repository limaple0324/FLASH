"""Character identity and window registry for FLASH SP1.

A character ID is a permanent identity. Display names may change without
changing that identity. Transient Windows handles are never trusted after load.
A game window may contain multiple characters, while each character record
tracks at most one current window at a time.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Mapping
from uuid import UUID, uuid4


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
    aliases: tuple[str, ...] = ()
    group: str | None = None
    role: str | None = None
    note: str | None = None
    locked: bool = False
    created_at_utc: str | None = None
    handle: int | None = None
    process_id: int | None = None
    window_class: str | None = None
    rect: tuple[int, int, int, int] | None = None
    health: WindowHealth = WindowHealth.UNKNOWN
    last_seen_utc: str | None = None
    confirmed: bool = False

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["aliases"] = list(self.aliases)
        data["health"] = self.health.value
        return data


class WindowRegistry:
    """Track stable character identities and their current window observations."""

    SCHEMA_VERSION = 2

    def __init__(self) -> None:
        self._records: dict[str, CharacterWindowRecord] = {}

    @staticmethod
    def _clean(value: str, field: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{field} must not be empty.")
        return cleaned

    @staticmethod
    def new_character_id() -> str:
        return str(uuid4())

    @staticmethod
    def _normalize_character_id(value: str) -> str:
        cleaned = WindowRegistry._clean(value, "character_id")
        try:
            return str(UUID(cleaned))
        except ValueError:
            # Legacy IDs remain valid during migration and can later be replaced
            # explicitly; never generate a new identity silently for existing data.
            return cleaned

    def register_character(
        self,
        character_id: str | None,
        display_name: str,
        *,
        group: str | None = None,
        role: str | None = None,
        note: str | None = None,
        locked: bool = False,
    ) -> CharacterWindowRecord:
        character_id = self._normalize_character_id(character_id or self.new_character_id())
        display_name = self._clean(display_name, "display_name")
        current = self._records.get(character_id)
        if current is not None:
            if current.display_name != display_name:
                raise ValueError("Character ID is already registered with another display name.")
            return current
        record = CharacterWindowRecord(
            character_id=character_id,
            display_name=display_name,
            group=group.strip() if isinstance(group, str) and group.strip() else None,
            role=role.strip() if isinstance(role, str) and role.strip() else None,
            note=note.strip() if isinstance(note, str) and note.strip() else None,
            locked=bool(locked),
            created_at_utc=datetime.now(timezone.utc).isoformat(),
        )
        self._records[character_id] = record
        return record

    def rename_character(self, character_id: str, new_display_name: str) -> CharacterWindowRecord:
        current = self.get(character_id)
        new_name = self._clean(new_display_name, "new_display_name")
        if current.locked:
            raise PermissionError("Character identity is locked; rename requires player unlock.")
        if new_name == current.display_name:
            return current
        aliases = tuple(dict.fromkeys((*current.aliases, current.display_name)))
        record = CharacterWindowRecord(
            **{
                **current.to_dict(),
                "display_name": new_name,
                "aliases": aliases,
                "health": current.health,
            }
        )
        self._records[character_id] = record
        return record

    def set_note(
        self,
        character_id: str,
        note: str | None,
    ) -> CharacterWindowRecord:
        """Set the player-confirmed note without changing stable identity."""
        current = self.get(character_id)
        if note is not None:
            if not isinstance(note, str):
                raise TypeError("note must be str or None.")
            note = note.strip()
            if not note:
                note = None
        record = replace(current, note=note)
        self._records[current.character_id] = record
        return record

    def set_group_role(
        self,
        character_id: str,
        *,
        group: str | None,
        role: str | None,
    ) -> CharacterWindowRecord:
        """Update player-facing membership without changing stable identity."""
        current = self.get(character_id)
        normalized_group = (
            group.strip()
            if isinstance(group, str) and group.strip()
            else None
        )
        normalized_role = (
            role.strip()
            if isinstance(role, str) and role.strip()
            else None
        )
        if (
            current.group == normalized_group
            and current.role == normalized_role
        ):
            return current
        record = replace(
            current,
            group=normalized_group,
            role=normalized_role,
        )
        self._records[current.character_id] = record
        return record

    def characters_for_handle(self, handle: int) -> tuple[CharacterWindowRecord, ...]:
        """Return all currently confirmed characters associated with a live window."""
        if handle <= 0:
            return ()
        return tuple(
            record
            for record in self.all()
            if record.confirmed and record.handle == handle
        )

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

        # A character record contains only one current handle, so confirming a new
        # handle replaces that character's previous window association. Multiple
        # different characters may legitimately share the same game window.
        record = CharacterWindowRecord(
            **{
                **current.to_dict(),
                "aliases": current.aliases,
                "handle": handle,
                "process_id": process_id,
                "window_class": window_class,
                "rect": rect,
                "health": health,
                "last_seen_utc": datetime.now(timezone.utc).isoformat(),
                "confirmed": True,
            }
        )
        self._records[character_id] = record
        return record

    def mark_offline(self, character_id: str) -> CharacterWindowRecord:
        current = self.get(character_id)
        record = CharacterWindowRecord(
            **{
                **current.to_dict(),
                "aliases": current.aliases,
                "handle": None,
                "health": WindowHealth.OFFLINE,
                "confirmed": False,
            }
        )
        self._records[character_id] = record
        return record

    def get(self, character_id: str) -> CharacterWindowRecord:
        key = self._normalize_character_id(character_id)
        try:
            return self._records[key]
        except KeyError as exc:
            raise KeyError(f"Unknown character ID: {key}") from exc

    def all(self) -> tuple[CharacterWindowRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    def clone_runtime(self) -> "WindowRegistry":
        """Clone the exact in-process registry, including trusted live fields."""
        clone = WindowRegistry()
        clone.replace_runtime(self)
        return clone

    def replace_runtime(self, source: "WindowRegistry") -> None:
        """Validate one complete runtime candidate and publish it exactly once."""
        if not isinstance(source, WindowRegistry):
            raise TypeError("source must be a WindowRegistry.")
        candidate: dict[str, CharacterWindowRecord] = {}
        for key, record in source._records.items():
            self._validate_runtime_record(key, record)
            if key in candidate:
                raise ValueError(f"Duplicate character ID in registry: {key}")
            candidate[key] = record
        self._records = candidate

    @classmethod
    def _validate_runtime_record(
        cls,
        key: object,
        record: object,
    ) -> None:
        if not isinstance(key, str) or not isinstance(record, CharacterWindowRecord):
            raise TypeError("Runtime registry entries must contain character records.")
        normalized_id = cls._normalize_character_id(key)
        if key != normalized_id or record.character_id != key:
            raise ValueError("Runtime registry key must match the character identity.")
        if record.display_name != cls._clean(record.display_name, "display_name"):
            raise ValueError("Runtime display name must already be normalized.")
        if (
            not isinstance(record.aliases, tuple)
            or any(not isinstance(alias, str) for alias in record.aliases)
        ):
            raise TypeError("Runtime aliases must be a tuple of strings.")
        if any(not alias.strip() for alias in record.aliases):
            raise ValueError("Runtime aliases must not be empty.")
        if len(record.aliases) != len(set(record.aliases)):
            raise ValueError("Runtime aliases must not contain duplicates.")
        for field_name in ("group", "role", "note", "created_at_utc", "window_class", "last_seen_utc"):
            value = getattr(record, field_name)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"Runtime {field_name} must be a string or None.")
        if not isinstance(record.locked, bool) or not isinstance(record.confirmed, bool):
            raise TypeError("Runtime boolean fields must be bool values.")
        for field_name in ("handle", "process_id"):
            value = getattr(record, field_name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(f"Runtime {field_name} must be a positive integer or None.")
        if record.rect is not None:
            if (
                not isinstance(record.rect, tuple)
                or len(record.rect) != 4
                or any(isinstance(value, bool) or not isinstance(value, int) for value in record.rect)
            ):
                raise TypeError("Runtime rect must be a four-integer tuple or None.")
            left, top, right, bottom = record.rect
            if right <= left or bottom <= top:
                raise ValueError("Runtime rect must have positive width and height.")
        if not isinstance(record.health, WindowHealth):
            raise TypeError("Runtime health must be WindowHealth.")
        if record.confirmed and record.handle is None:
            raise ValueError("A confirmed runtime record must have a live handle.")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "characters": [record.to_dict() for record in self.all()],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "WindowRegistry":
        """Restore v1/v2 identities without trusting stale window handles."""
        version = payload.get("schema_version", 1)
        if version not in (1, cls.SCHEMA_VERSION):
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

            aliases_value = raw.get("aliases", []) if version == 2 else []
            aliases = tuple(
                dict.fromkeys(
                    item.strip()
                    for item in aliases_value
                    if isinstance(item, str) and item.strip() and item.strip() != display_name.strip()
                )
            ) if isinstance(aliases_value, list) else ()

            rect_value = raw.get("rect")
            rect = None
            if isinstance(rect_value, (list, tuple)) and len(rect_value) == 4 and all(isinstance(v, int) for v in rect_value):
                left, top, right, bottom = rect_value
                if right > left and bottom > top:
                    rect = (left, top, right, bottom)

            process_id = raw.get("process_id")
            if not isinstance(process_id, int) or process_id <= 0:
                process_id = None
            window_class = raw.get("window_class")
            if not isinstance(window_class, str) or not window_class.strip():
                window_class = None
            last_seen = raw.get("last_seen_utc")
            if not isinstance(last_seen, str) or not last_seen.strip():
                last_seen = None
            created_at = raw.get("created_at_utc")
            if not isinstance(created_at, str) or not created_at.strip():
                created_at = last_seen or datetime.now(timezone.utc).isoformat()

            key = registry._normalize_character_id(character_id)
            if key in registry._records:
                raise ValueError(f"Duplicate character ID in registry: {key}")
            registry._records[key] = CharacterWindowRecord(
                character_id=key,
                display_name=registry._clean(display_name, "display_name"),
                aliases=aliases,
                group=raw.get("group") if isinstance(raw.get("group"), str) else None,
                role=raw.get("role") if isinstance(raw.get("role"), str) else None,
                note=raw.get("note") if isinstance(raw.get("note"), str) else None,
                locked=bool(raw.get("locked", False)),
                created_at_utc=created_at,
                handle=None,
                process_id=process_id,
                window_class=window_class,
                rect=rect,
                health=WindowHealth.UNKNOWN,
                last_seen_utc=last_seen,
                confirmed=False,
            )
        return registry
