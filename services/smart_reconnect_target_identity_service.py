"""Resolve one stable reconnect character for one launch fingerprint."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from adapters.windows_launch_fingerprint import (
    ShortcutFingerprintResolver,
    normalize_launch_fingerprint,
)
from core.window_registry import CharacterWindowRecord
from domain.character import Character, CharacterImportance
from services.group_configuration_service import GroupConfiguration


def normalize_reconnect_role_alias(value: object) -> str | None:
    """Return the comparison form used for saved role aliases and OCR text."""

    if not isinstance(value, str):
        return None
    normalized = "".join(
        character
        for character in value.strip().casefold()
        if not character.isspace() and ord(character) >= 32
    )
    return normalized or None


@dataclass(frozen=True, slots=True)
class SmartReconnectTargetIdentity:
    fingerprint: str
    character_id: str
    role_aliases: tuple[str, ...]
    importance: CharacterImportance
    original_slot_index: int | None = None
    original_line_number: int | None = None

    def __post_init__(self) -> None:
        fingerprint = normalize_launch_fingerprint(self.fingerprint)
        if fingerprint is None:
            raise ValueError("fingerprint must be a complete SHA-256 digest")
        character_id = self.character_id.strip()
        if not character_id:
            raise ValueError("character_id must not be empty")
        aliases = tuple(
            dict.fromkeys(
                normalized
                for normalized in (
                    normalize_reconnect_role_alias(value)
                    for value in self.role_aliases
                )
                if normalized is not None
            )
        )
        if not aliases:
            raise ValueError("role_aliases must contain a valid identity")
        if not isinstance(self.importance, CharacterImportance):
            raise TypeError("importance must be CharacterImportance")
        if (
            self.original_slot_index is not None
            and (
                isinstance(self.original_slot_index, bool)
                or not isinstance(self.original_slot_index, int)
                or self.original_slot_index not in (0, 1, 2)
            )
        ):
            raise ValueError("original_slot_index must be 0, 1, 2, or None")
        if self.original_line_number is not None and not (
            isinstance(self.original_line_number, int)
            and not isinstance(self.original_line_number, bool)
            and 1 <= self.original_line_number <= 8
        ):
            raise ValueError("original_line_number must be 1 through 8 or None")
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(self, "character_id", character_id)
        object.__setattr__(self, "role_aliases", aliases)

    def matches_observed_identity(self, value: object) -> bool:
        observed = normalize_reconnect_role_alias(value)
        if observed is None:
            return False
        abbreviated = observed.endswith(("…", "..."))
        observed = observed.rstrip(".…")
        if len(observed) < 3:
            return False
        if abbreviated:
            return sum(alias.startswith(observed) for alias in self.role_aliases) == 1
        return observed in self.role_aliases


@dataclass(frozen=True, slots=True)
class _SavedTargetState:
    character_id: str
    slot_index: int | None
    line_number: int | None


class SmartReconnectTargetIdentityService:
    """Bind launch identity to one registered character without group runtime state."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        groups_provider: Callable[[], Iterable[GroupConfiguration]],
        shortcut_fingerprint_resolver: ShortcutFingerprintResolver,
        characters_provider: Callable[[], Iterable[Character]],
        registry_provider: Callable[[], Iterable[CharacterWindowRecord]],
        state_path: Path,
    ) -> None:
        if not callable(groups_provider):
            raise TypeError("groups_provider must be callable")
        if not callable(getattr(shortcut_fingerprint_resolver, "resolve", None)):
            raise TypeError("shortcut_fingerprint_resolver must provide resolve")
        if not callable(characters_provider):
            raise TypeError("characters_provider must be callable")
        if not callable(registry_provider):
            raise TypeError("registry_provider must be callable")
        self._groups_provider = groups_provider
        self._resolver = shortcut_fingerprint_resolver
        self._characters_provider = characters_provider
        self._registry_provider = registry_provider
        self._state_path = Path(state_path)
        self._saved = self._load_state()
        self._source_signature: tuple[object, ...] | None = None
        self._targets: dict[str, SmartReconnectTargetIdentity] = {}

    @staticmethod
    def _items(provider: Callable[[], Iterable[object]]) -> tuple[object, ...]:
        try:
            return tuple(provider())
        except (OSError, RuntimeError, TypeError, ValueError):
            return ()

    @staticmethod
    def _signature(
        groups: tuple[GroupConfiguration, ...],
        characters: tuple[Character, ...],
        records: tuple[CharacterWindowRecord, ...],
        shortcut_sources: tuple[tuple[object, ...], ...],
    ) -> tuple[object, ...]:
        return (
            tuple(
                (
                    group.group_id,
                    tuple(
                        (
                            entry.entry_id,
                            str(entry.shortcut_path.resolve(strict=False)),
                            entry.role_id,
                        )
                        for entry in group.entries
                    ),
                )
                for group in groups
            ),
            tuple(
                (
                    item.character_id,
                    item.display_name,
                    item.importance.value,
                )
                for item in characters
            ),
            tuple(
                (
                    item.character_id,
                    item.display_name,
                    item.aliases,
                    item.role,
                )
                for item in records
            ),
            shortcut_sources,
        )

    @staticmethod
    def _shortcut_source_signature(
        paths: tuple[Path, ...],
    ) -> tuple[tuple[object, ...], ...]:
        """Return a cheap identity that changes when a shortcut is rewritten."""

        result: list[tuple[object, ...]] = []
        for path in paths:
            try:
                status = path.stat()
            except OSError:
                result.append((str(path), None))
                continue
            result.append(
                (
                    str(path),
                    status.st_size,
                    status.st_mtime_ns,
                    getattr(status, "st_ctime_ns", None),
                    getattr(status, "st_ino", None),
                )
            )
        return tuple(result)

    def _load_state(self) -> dict[str, _SavedTargetState]:
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, Mapping) or payload.get("version") != self.SCHEMA_VERSION:
            return {}
        raw_targets = payload.get("targets")
        if not isinstance(raw_targets, Mapping):
            return {}
        result: dict[str, _SavedTargetState] = {}
        for raw_fingerprint, raw_value in raw_targets.items():
            fingerprint = normalize_launch_fingerprint(raw_fingerprint)
            if fingerprint is None or not isinstance(raw_value, Mapping):
                return {}
            character_id = raw_value.get("character_id")
            slot_index = raw_value.get("slot_index")
            line_number = raw_value.get("line_number")
            if (
                not isinstance(character_id, str)
                or not character_id.strip()
                or (
                    slot_index is not None
                    and (
                        isinstance(slot_index, bool)
                        or not isinstance(slot_index, int)
                        or slot_index not in (0, 1, 2)
                    )
                )
                or (
                    line_number is not None
                    and (
                        isinstance(line_number, bool)
                        or not isinstance(line_number, int)
                        or not 1 <= line_number <= 8
                    )
                )
            ):
                return {}
            result[fingerprint] = _SavedTargetState(
                character_id.strip(),
                slot_index,
                line_number,
            )
        return result

    def _persist(self) -> bool:
        payload = {
            "version": self.SCHEMA_VERSION,
            "targets": {
                fingerprint: {
                    "character_id": value.character_id,
                    "slot_index": value.slot_index,
                    "line_number": value.line_number,
                }
                for fingerprint, value in sorted(self._saved.items())
            },
        }
        temporary = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8", newline="\n") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self._state_path)
            return True
        except (OSError, TypeError, ValueError):
            return False
        finally:
            temporary.unlink(missing_ok=True)

    def _refresh(self) -> None:
        groups = tuple(
            item
            for item in self._items(self._groups_provider)
            if isinstance(item, GroupConfiguration)
        )
        characters = tuple(
            item
            for item in self._items(self._characters_provider)
            if isinstance(item, Character)
        )
        records = tuple(
            item
            for item in self._items(self._registry_provider)
            if isinstance(item, CharacterWindowRecord)
        )
        paths = tuple(
            dict.fromkeys(
                entry.shortcut_path.resolve(strict=False)
                for group in groups
                for entry in group.entries
            )
        )
        signature = self._signature(
            groups,
            characters,
            records,
            self._shortcut_source_signature(paths),
        )
        if signature == self._source_signature:
            return
        try:
            resolved = self._resolver.resolve(paths)
        except (OSError, RuntimeError, TypeError, ValueError):
            self._source_signature = None
            self._targets = {}
            return
        if not isinstance(resolved, Mapping):
            self._source_signature = None
            self._targets = {}
            return
        normalized_by_path = {
            path.resolve(strict=False): normalize_launch_fingerprint(value)
            for path, value in resolved.items()
            if isinstance(path, Path)
        }
        resolution_complete = all(
            normalized_by_path.get(path) is not None for path in paths
        )
        characters_by_id: dict[str, list[Character]] = {}
        records_by_id: dict[str, list[CharacterWindowRecord]] = {}
        for character in characters:
            characters_by_id.setdefault(character.character_id, []).append(character)
        for record in records:
            records_by_id.setdefault(record.character_id, []).append(record)

        candidates: dict[str, dict[str, tuple[Character, CharacterWindowRecord, set[str]]]] = {}
        blocked_fingerprints: set[str] = set()
        for group in groups:
            for entry in group.entries:
                fingerprint = normalized_by_path.get(
                    entry.shortcut_path.resolve(strict=False)
                )
                if fingerprint is None:
                    continue
                character_matches = characters_by_id.get(entry.entry_id, [])
                record_matches = records_by_id.get(entry.entry_id, [])
                if len(character_matches) != 1 or len(record_matches) != 1:
                    blocked_fingerprints.add(fingerprint)
                    continue
                character = character_matches[0]
                record = record_matches[0]
                if record.display_name != character.display_name:
                    blocked_fingerprints.add(fingerprint)
                    continue
                aliases = {
                    alias
                    for alias in (
                        normalize_reconnect_role_alias(character.display_name),
                        normalize_reconnect_role_alias(record.display_name),
                        normalize_reconnect_role_alias(entry.role_id),
                        *(
                            normalize_reconnect_role_alias(value)
                            for value in record.aliases
                        ),
                    )
                    if alias is not None
                }
                by_character = candidates.setdefault(fingerprint, {})
                current = by_character.get(character.character_id)
                if current is None:
                    by_character[character.character_id] = (
                        character,
                        record,
                        aliases,
                    )
                else:
                    current[2].update(aliases)

        targets: dict[str, SmartReconnectTargetIdentity] = {}
        for fingerprint, by_character in candidates.items():
            if fingerprint in blocked_fingerprints or len(by_character) != 1:
                continue
            character_id, (character, _record, aliases) = next(
                iter(by_character.items())
            )
            saved = self._saved.get(fingerprint)
            slot_index = None
            line_number = None
            if saved is not None and saved.character_id == character_id:
                slot_index = saved.slot_index
                line_number = saved.line_number
            targets[fingerprint] = SmartReconnectTargetIdentity(
                fingerprint=fingerprint,
                character_id=character_id,
                role_aliases=tuple(sorted(aliases)),
                importance=character.importance,
                original_slot_index=slot_index,
                original_line_number=line_number,
            )
        self._targets = targets
        # The PowerShell resolver deliberately reports operational failures as
        # an empty or partial mapping.  Keep successful targets usable, but do
        # not make that incomplete result sticky: the next lookup must retry.
        self._source_signature = signature if resolution_complete else None

    def target_for(self, fingerprint: object) -> SmartReconnectTargetIdentity | None:
        normalized = normalize_launch_fingerprint(fingerprint)
        if normalized is None:
            return None
        self._refresh()
        return self._targets.get(normalized)

    def _remember(
        self,
        fingerprint: object,
        character_id: object,
        *,
        slot_index: int | None = None,
        line_number: int | None = None,
    ) -> bool:
        target = self.target_for(fingerprint)
        if (
            target is None
            or not isinstance(character_id, str)
            or character_id.strip() != target.character_id
        ):
            return False
        current = self._saved.get(
            target.fingerprint,
            _SavedTargetState(target.character_id, None, None),
        )
        updated = _SavedTargetState(
            target.character_id,
            current.slot_index if slot_index is None else slot_index,
            current.line_number if line_number is None else line_number,
        )
        if updated == current:
            return True
        self._saved[target.fingerprint] = updated
        if not self._persist():
            self._saved[target.fingerprint] = current
            return False
        self._source_signature = None
        return True

    def remember_verified_slot(
        self,
        fingerprint: object,
        character_id: object,
        slot_index: object,
    ) -> bool:
        if (
            isinstance(slot_index, bool)
            or not isinstance(slot_index, int)
            or slot_index not in (0, 1, 2)
        ):
            return False
        return self._remember(
            fingerprint,
            character_id,
            slot_index=slot_index,
        )

    def remember_verified_line(
        self,
        fingerprint: object,
        character_id: object,
        line_number: object,
    ) -> bool:
        if (
            isinstance(line_number, bool)
            or not isinstance(line_number, int)
            or not 1 <= line_number <= 8
        ):
            return False
        return self._remember(
            fingerprint,
            character_id,
            line_number=line_number,
        )
