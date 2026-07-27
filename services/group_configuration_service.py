"""Editable group launcher configuration seeded from 輔V0.2 without modifying it."""

from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from services.feature_hotkey_monitor import normalize_feature_hotkey
from services.group_launch_service import SavedWindowPlacement


@dataclass(frozen=True, slots=True)
class GroupConfigurationEntry:
    entry_id: str
    display_name: str
    shortcut_path: Path
    role: str
    order: int
    placement: SavedWindowPlacement | None = None


@dataclass(frozen=True, slots=True)
class GroupConfiguration:
    name: str
    entries: tuple[GroupConfigurationEntry, ...]
    launch_hotkey: str = ""
    master_locked: bool = True


@dataclass(frozen=True, slots=True)
class GroupSyncMemberChoice:
    entry_id: str
    label: str


class SyncCycleError(ValueError):
    player_message = "無法加入：會形成重複控制"


class GroupHotkeyConflictError(ValueError):
    player_message = "無法設定：這個快捷鍵已被其他組別使用"


class GroupMasterLockedError(ValueError):
    player_message = "主窗上鎖中，請先解鎖後再調整組別角色。"


class GroupConfigurationService:
    """Own the new app's copy while treating the old config as read-only."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        path: Path,
        *,
        legacy_config_path: Path | None = None,
    ) -> None:
        self.path = Path(path)
        self._legacy_config_path = (
            Path(legacy_config_path)
            if legacy_config_path is not None
            else None
        )
        self._groups: list[dict[str, object]] = []
        self._sync_edges: dict[str, list[str]] = {}
        self._load_or_import()

    @staticmethod
    def _clean_name(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        if (
            not cleaned
            or len(cleaned) > 80
            or any(ord(character) < 32 for character in cleaned)
        ):
            return None
        return cleaned

    @staticmethod
    def _entry_id(path: Path) -> str:
        normalized = os.path.normcase(str(path.resolve(strict=False)))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _clean_placement(
        value: Mapping[str, object],
    ) -> SavedWindowPlacement | None:
        fields = tuple(
            value.get(name)
            for name in ("x", "y", "width", "height", "delay_ms")
        )
        if any(
            isinstance(item, bool) or not isinstance(item, int)
            for item in fields
        ):
            return None
        x, y, width, height, delay_ms = fields
        if (
            not (-100_000 <= x <= 100_000)
            or not (-100_000 <= y <= 100_000)
            or not (1 <= width <= 20_000)
            or not (1 <= height <= 20_000)
            or not (0 <= delay_ms <= 600_000)
        ):
            return None
        return SavedWindowPlacement(x, y, width, height, delay_ms)

    @classmethod
    def _clean_entry(cls, value: object) -> dict[str, object] | None:
        if not isinstance(value, Mapping):
            return None
        raw_path = value.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        path = Path(raw_path).resolve(strict=False)
        if path.suffix.casefold() != ".lnk" or not path.is_file():
            return None
        role = cls._clean_name(value.get("role")) or "同步窗口"
        entry = {
            "entry_id": cls._entry_id(path),
            "path": str(path),
            "role": role,
        }
        placement = cls._clean_placement(value)
        if placement is not None:
            entry.update(
                {
                    "x": placement.x,
                    "y": placement.y,
                    "width": placement.width,
                    "height": placement.height,
                    "delay_ms": placement.delay_ms,
                }
            )
        return entry

    @classmethod
    def _clean_groups(cls, payload: object) -> list[dict[str, object]]:
        if not isinstance(payload, Mapping):
            return []
        raw_groups = payload.get("groups")
        if not isinstance(raw_groups, list):
            return []
        groups: list[dict[str, object]] = []
        seen_names: set[str] = set()
        seen_launch_hotkeys: set[str] = set()
        for raw_group in raw_groups:
            if not isinstance(raw_group, Mapping):
                continue
            name = cls._clean_name(raw_group.get("name"))
            raw_entries = raw_group.get("launch_entries")
            if (
                name is None
                or name.casefold() in seen_names
                or not isinstance(raw_entries, list)
            ):
                continue
            entries: list[dict[str, object]] = []
            seen_entries: set[str] = set()
            for raw_entry in raw_entries:
                entry = cls._clean_entry(raw_entry)
                if entry is None or entry["entry_id"] in seen_entries:
                    continue
                seen_entries.add(str(entry["entry_id"]))
                entries.append(entry)
            if entries:
                entries[0]["role"] = "主窗口"
                for entry in entries[1:]:
                    entry["role"] = "同步窗口"
            launch_hotkey = normalize_feature_hotkey(
                raw_group.get("launch_hotkey")
                or raw_group.get("launch_hotkey_display")
            )
            if launch_hotkey in seen_launch_hotkeys:
                launch_hotkey = ""
            if launch_hotkey:
                seen_launch_hotkeys.add(launch_hotkey)
            seen_names.add(name.casefold())
            groups.append(
                {
                    "name": name,
                    "launch_entries": entries,
                    "launch_hotkey": launch_hotkey,
                    "master_locked": (
                        raw_group.get("master_locked")
                        if isinstance(
                            raw_group.get("master_locked"),
                            bool,
                        )
                        else True
                    ),
                }
            )
        return groups

    @staticmethod
    def _clear_duplicate_launch_hotkeys(
        groups: list[dict[str, object]],
    ) -> None:
        seen: set[str] = set()
        for group in groups:
            hotkey = normalize_feature_hotkey(
                group.get("launch_hotkey")
            )
            if hotkey in seen:
                group["launch_hotkey"] = ""
                continue
            group["launch_hotkey"] = hotkey
            if hotkey:
                seen.add(hotkey)

    @staticmethod
    def _read_json(path: Path | None) -> Mapping[str, object] | None:
        if path is None or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, Mapping) else None

    def _load_or_import(self) -> None:
        current = self._read_json(self.path)
        if (
            current is not None
            and current.get("schema_version") == self.SCHEMA_VERSION
        ):
            self._groups = self._clean_groups(current)
            self._sync_edges = self._clean_sync_edges(
                current.get("sync_edges")
            )
            if self._merge_legacy_placements():
                self._save()
            return
        legacy = self._read_json(self._legacy_config_path)
        self._groups = self._clean_groups(legacy or {})
        self._sync_edges = {}
        if self._groups:
            self._save()

    def _merge_legacy_placements(self) -> bool:
        legacy = self._read_json(self._legacy_config_path)
        legacy_groups = self._clean_groups(legacy or {})
        legacy_by_group: dict[str, dict[str, dict[str, object]]] = {}
        for group in legacy_groups:
            entries = group.get("launch_entries")
            if not isinstance(entries, list):
                continue
            legacy_by_group[str(group["name"]).casefold()] = {
                str(entry["entry_id"]): entry
                for entry in entries
                if self._clean_placement(entry) is not None
            }
        changed = False
        for group in self._groups:
            legacy_entries = legacy_by_group.get(
                str(group["name"]).casefold(),
                {},
            )
            entries = group.get("launch_entries")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if self._clean_placement(entry) is not None:
                    continue
                legacy_entry = legacy_entries.get(str(entry["entry_id"]))
                if legacy_entry is None:
                    continue
                placement = self._clean_placement(legacy_entry)
                if placement is None:
                    continue
                entry.update(
                    {
                        "x": placement.x,
                        "y": placement.y,
                        "width": placement.width,
                        "height": placement.height,
                        "delay_ms": placement.delay_ms,
                    }
                )
                changed = True
        return changed

    def _known_entry_ids(self) -> set[str]:
        return {
            str(entry["entry_id"])
            for group in self._groups
            for entry in group["launch_entries"]
        }

    def _implicit_sync_edges(
        self,
        groups: Iterable[Mapping[str, object]] | None = None,
    ) -> dict[str, list[str]]:
        edges: dict[str, list[str]] = {}
        for group in groups if groups is not None else self._groups:
            raw_entries = group.get("launch_entries")
            if not isinstance(raw_entries, list) or len(raw_entries) < 2:
                continue
            controller = str(raw_entries[0]["entry_id"])
            targets = edges.setdefault(controller, [])
            for entry in raw_entries[1:]:
                member = str(entry["entry_id"])
                if member != controller and member not in targets:
                    targets.append(member)
        return edges

    def _combined_sync_edges(
        self,
        *,
        explicit: Mapping[str, Iterable[str]] | None = None,
        groups: Iterable[Mapping[str, object]] | None = None,
    ) -> dict[str, list[str]]:
        combined = self._implicit_sync_edges(groups)
        for source, raw_targets in (
            explicit.items()
            if explicit is not None
            else self._sync_edges.items()
        ):
            targets = combined.setdefault(source, [])
            for target in raw_targets:
                if target != source and target not in targets:
                    targets.append(target)
        return combined

    def _clean_sync_edges(self, value: object) -> dict[str, list[str]]:
        if not isinstance(value, Mapping):
            return {}
        known = self._known_entry_ids()
        edges: dict[str, list[str]] = {}
        for raw_source, raw_targets in value.items():
            if (
                not isinstance(raw_source, str)
                or raw_source not in known
                or not isinstance(raw_targets, list)
            ):
                continue
            targets = tuple(
                dict.fromkeys(
                    target
                    for target in raw_targets
                    if isinstance(target, str)
                    and target in known
                    and target != raw_source
                )
            )
            if targets:
                edges[raw_source] = list(targets)
        if self._has_cycle(self._combined_sync_edges(explicit=edges)):
            return {}
        return edges

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "groups": self._groups,
            "sync_edges": self._sync_edges,
        }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temp_path.write_text(
                json.dumps(
                    self._payload(),
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(temp_path, self.path)
        finally:
            temp_path.unlink(missing_ok=True)

    def groups(self) -> tuple[GroupConfiguration, ...]:
        result: list[GroupConfiguration] = []
        for raw_group in self._groups:
            name = str(raw_group["name"])
            entries = tuple(
                GroupConfigurationEntry(
                    entry_id=str(raw_entry["entry_id"]),
                    display_name=Path(str(raw_entry["path"])).stem,
                    shortcut_path=Path(str(raw_entry["path"])),
                    role=str(raw_entry["role"]),
                    order=index,
                    placement=self._clean_placement(raw_entry),
                )
                for index, raw_entry in enumerate(
                    raw_group["launch_entries"],
                    start=1,
                )
            )
            result.append(
                GroupConfiguration(
                    name=name,
                    entries=entries,
                    launch_hotkey=normalize_feature_hotkey(
                        raw_group.get("launch_hotkey")
                    ),
                    master_locked=bool(
                        raw_group.get("master_locked", True)
                    ),
                )
            )
        return tuple(result)

    def group(self, name: object) -> GroupConfiguration | None:
        cleaned = self._clean_name(name)
        if cleaned is None:
            return None
        return next(
            (group for group in self.groups() if group.name == cleaned),
            None,
        )

    def create_group(self, name: object) -> bool:
        cleaned = self._clean_name(name)
        if cleaned is None:
            raise ValueError("group name is invalid.")
        if any(
            str(group["name"]).casefold() == cleaned.casefold()
            for group in self._groups
        ):
            return False
        self._groups.append(
            {
                "name": cleaned,
                "launch_entries": [],
                "launch_hotkey": "",
                "master_locked": True,
            }
        )
        self._save()
        return True

    def set_master_locked(
        self,
        group_name: object,
        locked: object,
    ) -> bool:
        cleaned = self._clean_name(group_name)
        if cleaned is None or not isinstance(locked, bool):
            return False
        raw_group = next(
            (
                group
                for group in self._groups
                if group["name"] == cleaned
            ),
            None,
        )
        if raw_group is None:
            return False
        if bool(raw_group.get("master_locked", True)) == locked:
            return False
        raw_group["master_locked"] = locked
        self._save()
        return True

    @staticmethod
    def _require_master_unlocked(
        raw_group: Mapping[str, object],
    ) -> None:
        if bool(raw_group.get("master_locked", True)):
            raise GroupMasterLockedError(
                GroupMasterLockedError.player_message
            )

    def set_launch_hotkey(
        self,
        group_name: object,
        hotkey: object,
    ) -> bool:
        cleaned = self._clean_name(group_name)
        if cleaned is None:
            return False
        normalized = normalize_feature_hotkey(hotkey)
        raw_group = next(
            (
                group
                for group in self._groups
                if group["name"] == cleaned
            ),
            None,
        )
        if raw_group is None:
            return False
        if normalized and any(
            group is not raw_group
            and normalize_feature_hotkey(
                group.get("launch_hotkey")
            )
            == normalized
            for group in self._groups
        ):
            raise GroupHotkeyConflictError(
                GroupHotkeyConflictError.player_message
            )
        if normalize_feature_hotkey(
            raw_group.get("launch_hotkey")
        ) == normalized:
            return False
        raw_group["launch_hotkey"] = normalized
        self._save()
        return True

    def launch_hotkeys(self) -> dict[str, str]:
        return {
            group.name: group.launch_hotkey
            for group in self.groups()
            if group.launch_hotkey
        }

    def rename_group(self, old_name: object, new_name: object) -> bool:
        old_cleaned = self._clean_name(old_name)
        new_cleaned = self._clean_name(new_name)
        if old_cleaned is None or new_cleaned is None:
            raise ValueError("group name is invalid.")
        target = next(
            (
                group
                for group in self._groups
                if group["name"] == old_cleaned
            ),
            None,
        )
        if target is None:
            return False
        if old_cleaned.casefold() != new_cleaned.casefold() and any(
            str(group["name"]).casefold() == new_cleaned.casefold()
            for group in self._groups
        ):
            return False
        if target["name"] == new_cleaned:
            return False
        target["name"] = new_cleaned
        self._save()
        return True

    def delete_group(self, name: object) -> bool:
        cleaned = self._clean_name(name)
        if cleaned is None:
            return False
        target = next(
            (
                group
                for group in self._groups
                if group["name"] == cleaned
            ),
            None,
        )
        if target is None:
            return False
        removed_ids = {
            str(entry["entry_id"])
            for entry in target["launch_entries"]
        }
        self._groups.remove(target)
        for entry_id in removed_ids:
            self._sync_edges.pop(entry_id, None)
        for source, targets in tuple(self._sync_edges.items()):
            remaining = [
                target_id
                for target_id in targets
                if target_id not in removed_ids
            ]
            if remaining:
                self._sync_edges[source] = remaining
            else:
                self._sync_edges.pop(source, None)
        self._save()
        return True

    def move_group(self, name: object, direction: int) -> bool:
        cleaned = self._clean_name(name)
        if cleaned is None or direction not in {-1, 1}:
            return False
        index = next(
            (
                index
                for index, group in enumerate(self._groups)
                if group["name"] == cleaned
            ),
            None,
        )
        if index is None:
            return False
        target_index = index + direction
        if target_index < 0 or target_index >= len(self._groups):
            return False
        self._groups[index], self._groups[target_index] = (
            self._groups[target_index],
            self._groups[index],
        )
        self._save()
        return True

    def update_saved_placements(
        self,
        group_name: object,
        placements: Mapping[Path, SavedWindowPlacement],
    ) -> bool:
        cleaned = self._clean_name(group_name)
        if cleaned is None:
            return False
        target = next(
            (
                group
                for group in self._groups
                if group["name"] == cleaned
            ),
            None,
        )
        if target is None:
            return False
        self._require_master_unlocked(target)
        entries = target.get("launch_entries")
        if not isinstance(entries, list) or not entries:
            return False
        normalized: dict[str, SavedWindowPlacement] = {}
        for raw_path, placement in placements.items():
            if not isinstance(placement, SavedWindowPlacement):
                raise TypeError(
                    "placements must contain SavedWindowPlacement values."
                )
            normalized[
                os.path.normcase(
                    str(Path(raw_path).resolve(strict=False))
                )
            ] = placement
        entry_paths = {
            os.path.normcase(
                str(Path(str(entry["path"])).resolve(strict=False))
            )
            for entry in entries
        }
        if set(normalized) != entry_paths:
            return False
        for entry in entries:
            path_key = os.path.normcase(
                str(Path(str(entry["path"])).resolve(strict=False))
            )
            placement = normalized[path_key]
            entry.update(
                {
                    "x": placement.x,
                    "y": placement.y,
                    "width": placement.width,
                    "height": placement.height,
                    "delay_ms": placement.delay_ms,
                }
            )
        self._save()
        return True

    def export_configuration(self, destination: Path) -> Path:
        path = Path(destination)
        if path.suffix.casefold() != ".json":
            raise ValueError("configuration export must be a JSON file.")
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            temp_path.write_text(
                json.dumps(
                    self._payload(),
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)
        return path

    def import_configuration(
        self,
        source: Path,
        *,
        reserved_hotkeys: Iterable[object] = (),
    ) -> tuple[str, ...]:
        path = Path(source)
        payload = self._read_json(path)
        if payload is None:
            raise ValueError("configuration import is invalid.")
        imported_groups = self._clean_groups(payload)
        if not imported_groups:
            raise ValueError("configuration import has no valid groups.")

        original_groups = self._groups
        original_edges = self._sync_edges
        proposed_groups = deepcopy(self._groups)
        replaced_source_ids: set[str] = set()
        obsolete_target_ids: set[str] = set()
        imported_names: list[str] = []
        for imported_group in imported_groups:
            imported_name = str(imported_group["name"])
            imported_names.append(imported_name)
            existing_index = next(
                (
                    index
                    for index, group in enumerate(proposed_groups)
                    if str(group["name"]).casefold()
                    == imported_name.casefold()
                ),
                None,
            )
            if existing_index is None:
                proposed_groups.append(imported_group)
                continue
            existing_entries = proposed_groups[existing_index].get(
                "launch_entries"
            )
            imported_entries = imported_group.get("launch_entries")
            existing_ids = {
                str(entry["entry_id"])
                for entry in existing_entries
            } if isinstance(existing_entries, list) else set()
            imported_ids = {
                str(entry["entry_id"])
                for entry in imported_entries
            } if isinstance(imported_entries, list) else set()
            replaced_source_ids.update(existing_ids)
            obsolete_target_ids.update(existing_ids - imported_ids)
            proposed_groups[existing_index] = imported_group
        self._clear_duplicate_launch_hotkeys(proposed_groups)
        reserved = {
            normalized
            for normalized in (
                normalize_feature_hotkey(value)
                for value in reserved_hotkeys
            )
            if normalized
        }
        if any(
            normalize_feature_hotkey(
                group.get("launch_hotkey")
            )
            in reserved
            for group in proposed_groups
        ):
            raise ValueError(
                "configuration import contains a reserved hotkey."
            )

        proposed_edges: dict[str, list[str]] = {}
        for source_id, targets in self._sync_edges.items():
            if source_id in replaced_source_ids:
                continue
            remaining = [
                target
                for target in targets
                if target not in obsolete_target_ids
            ]
            if remaining:
                proposed_edges[source_id] = remaining

        self._groups = proposed_groups
        self._sync_edges = proposed_edges
        imported_edges = self._clean_sync_edges(payload.get("sync_edges"))
        for source_id, targets in imported_edges.items():
            proposed_edges[source_id] = list(targets)
        if self._has_cycle(
            self._combined_sync_edges(explicit=proposed_edges)
        ):
            self._groups = original_groups
            self._sync_edges = original_edges
            raise SyncCycleError(SyncCycleError.player_message)
        self._sync_edges = proposed_edges
        try:
            self._save()
        except Exception:
            self._groups = original_groups
            self._sync_edges = original_edges
            raise
        return tuple(imported_names)

    def add_shortcuts(
        self,
        group_name: object,
        paths: Iterable[Path],
    ) -> tuple[GroupConfigurationEntry, ...]:
        cleaned = self._clean_name(group_name)
        if cleaned is None:
            raise ValueError("group_name is invalid.")
        raw_group = next(
            (
                group
                for group in self._groups
                if group["name"] == cleaned
            ),
            None,
        )
        if raw_group is None:
            raw_group = {
                "name": cleaned,
                "launch_entries": [],
                "launch_hotkey": "",
                "master_locked": False,
            }
            self._groups.append(raw_group)
        self._require_master_unlocked(raw_group)
        entries = raw_group["launch_entries"]
        if not isinstance(entries, list):
            raise RuntimeError("group launch entries are malformed.")
        existing = {str(entry["entry_id"]) for entry in entries}
        added_ids: list[str] = []
        for raw_path in paths:
            path = Path(raw_path).resolve(strict=False)
            if path.suffix.casefold() != ".lnk" or not path.is_file():
                raise ValueError("Only existing .lnk shortcuts can be added.")
            entry_id = self._entry_id(path)
            if entry_id in existing:
                continue
            entries.append(
                {
                    "entry_id": entry_id,
                    "path": str(path),
                    "role": "主窗口" if not entries else "同步窗口",
                }
            )
            existing.add(entry_id)
            added_ids.append(entry_id)
        if added_ids:
            if self._has_cycle(self._combined_sync_edges()):
                del entries[-len(added_ids):]
                raise SyncCycleError(SyncCycleError.player_message)
            self._save()
        group = self.group(cleaned)
        if group is None:
            return ()
        return tuple(
            entry for entry in group.entries if entry.entry_id in added_ids
        )

    def remove_shortcut(
        self,
        group_name: object,
        entry_id: object,
    ) -> bool:
        cleaned = self._clean_name(group_name)
        if (
            cleaned is None
            or not isinstance(entry_id, str)
            or not entry_id.strip()
        ):
            return False
        raw_group = next(
            (
                group
                for group in self._groups
                if group["name"] == cleaned
            ),
            None,
        )
        if raw_group is None:
            return False
        self._require_master_unlocked(raw_group)
        entries = raw_group["launch_entries"]
        if not isinstance(entries, list):
            return False
        remaining = [
            entry
            for entry in entries
            if entry.get("entry_id") != entry_id.strip()
        ]
        if len(remaining) == len(entries):
            return False
        if remaining:
            remaining[0]["role"] = "主窗口"
            for entry in remaining[1:]:
                entry["role"] = "同步窗口"
        raw_group["launch_entries"] = remaining
        self._sync_edges.pop(entry_id.strip(), None)
        for source, targets in tuple(self._sync_edges.items()):
            filtered = [
                target
                for target in targets
                if target != entry_id.strip()
            ]
            if filtered:
                self._sync_edges[source] = filtered
            else:
                self._sync_edges.pop(source, None)
        self._save()
        return True

    def set_main_entry(
        self,
        group_name: object,
        entry_id: object,
    ) -> bool:
        cleaned = self._clean_name(group_name)
        if (
            cleaned is None
            or not isinstance(entry_id, str)
            or not entry_id.strip()
        ):
            return False
        raw_group = next(
            (
                group
                for group in self._groups
                if group["name"] == cleaned
            ),
            None,
        )
        if raw_group is None:
            return False
        self._require_master_unlocked(raw_group)
        entries = raw_group.get("launch_entries")
        if not isinstance(entries, list):
            return False
        index = next(
            (
                index
                for index, entry in enumerate(entries)
                if entry.get("entry_id") == entry_id.strip()
            ),
            None,
        )
        if index is None or index == 0:
            return False
        original = list(entries)
        selected = entries.pop(index)
        entries.insert(0, selected)
        entries[0]["role"] = "主窗口"
        for entry in entries[1:]:
            entry["role"] = "同步窗口"
        if self._has_cycle(self._combined_sync_edges()):
            raw_group["launch_entries"] = original
            raise SyncCycleError(SyncCycleError.player_message)
        self._save()
        return True

    def clear_group(self, group_name: object) -> bool:
        cleaned = self._clean_name(group_name)
        if cleaned is None:
            return False
        raw_group = next(
            (
                group
                for group in self._groups
                if group["name"] == cleaned
            ),
            None,
        )
        if raw_group is None:
            return False
        self._require_master_unlocked(raw_group)
        entries = raw_group.get("launch_entries")
        if not isinstance(entries, list) or not entries:
            return False
        removed_ids = {
            str(entry["entry_id"])
            for entry in entries
        }
        raw_group["launch_entries"] = []
        for entry_id in removed_ids:
            self._sync_edges.pop(entry_id, None)
        for source, targets in tuple(self._sync_edges.items()):
            remaining = [
                target
                for target in targets
                if target not in removed_ids
            ]
            if remaining:
                self._sync_edges[source] = remaining
            else:
                self._sync_edges.pop(source, None)
        self._save()
        return True

    @staticmethod
    def _has_cycle(edges: Mapping[str, Iterable[str]]) -> bool:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> bool:
            if node in visiting:
                return True
            if node in visited:
                return False
            visiting.add(node)
            for target in edges.get(node, ()):
                if visit(target):
                    return True
            visiting.remove(node)
            visited.add(node)
            return False

        nodes = set(edges)
        for targets in edges.values():
            nodes.update(targets)
        return any(visit(node) for node in nodes)

    def add_sync_relation(
        self,
        controller_entry_id: object,
        member_entry_id: object,
    ) -> bool:
        if (
            not isinstance(controller_entry_id, str)
            or not isinstance(member_entry_id, str)
        ):
            raise ValueError("sync identities must be strings.")
        controller = controller_entry_id.strip()
        member = member_entry_id.strip()
        known = self._known_entry_ids()
        if (
            not controller
            or not member
            or controller not in known
            or member not in known
        ):
            raise ValueError("sync identities are not uniquely registered.")
        proposed = {
            source: list(targets)
            for source, targets in self._sync_edges.items()
        }
        targets = proposed.setdefault(controller, [])
        if member in targets:
            return False
        targets.append(member)
        if self._has_cycle(
            self._combined_sync_edges(explicit=proposed)
        ):
            raise SyncCycleError(SyncCycleError.player_message)
        self._sync_edges = proposed
        self._save()
        return True

    def remove_sync_relation(
        self,
        controller_entry_id: object,
        member_entry_id: object,
    ) -> bool:
        if (
            not isinstance(controller_entry_id, str)
            or not isinstance(member_entry_id, str)
        ):
            return False
        controller = controller_entry_id.strip()
        member = member_entry_id.strip()
        targets = self._sync_edges.get(controller)
        if not targets or member not in targets:
            return False
        remaining = [target for target in targets if target != member]
        if remaining:
            self._sync_edges[controller] = remaining
        else:
            self._sync_edges.pop(controller, None)
        self._save()
        return True

    def available_sync_members(
        self,
        group_name: object,
    ) -> tuple[GroupSyncMemberChoice, ...]:
        group = self.group(group_name)
        if group is None or not group.entries:
            return ()
        controller = group.entries[0].entry_id
        existing = set(self._sync_edges.get(controller, ()))
        choices: list[GroupSyncMemberChoice] = []
        seen: set[str] = set()
        for candidate_group in self.groups():
            for entry in candidate_group.entries:
                if (
                    entry.entry_id == controller
                    or entry.entry_id in existing
                    or entry.entry_id in seen
                ):
                    continue
                seen.add(entry.entry_id)
                choices.append(
                    GroupSyncMemberChoice(
                        entry.entry_id,
                        f"{candidate_group.name}｜{entry.display_name}",
                    )
                )
        return tuple(choices)

    def explicit_sync_members(
        self,
        group_name: object,
    ) -> tuple[GroupSyncMemberChoice, ...]:
        group = self.group(group_name)
        if group is None or not group.entries:
            return ()
        controller = group.entries[0].entry_id
        member_ids = tuple(self._sync_edges.get(controller, ()))
        labels: dict[str, str] = {}
        for candidate_group in self.groups():
            for entry in candidate_group.entries:
                labels.setdefault(
                    entry.entry_id,
                    f"{candidate_group.name}｜{entry.display_name}",
                )
        return tuple(
            GroupSyncMemberChoice(member_id, labels[member_id])
            for member_id in member_ids
            if member_id in labels
        )

    def expanded_sync_members(
        self,
        controller_entry_id: object,
    ) -> tuple[str, ...]:
        if not isinstance(controller_entry_id, str):
            return ()
        controller = controller_entry_id.strip()
        if controller not in self._known_entry_ids():
            return ()
        result: list[str] = []
        seen = {controller}
        edges = self._combined_sync_edges()
        stack = list(reversed(edges.get(controller, ())))
        while stack:
            member = stack.pop()
            if member in seen:
                continue
            seen.add(member)
            result.append(member)
            stack.extend(
                reversed(edges.get(member, ()))
            )
        return tuple(result)
