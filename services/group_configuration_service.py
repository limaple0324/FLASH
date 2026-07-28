"""Editable group launcher configuration seeded from 輔V0.2 without modifying it."""

from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from domain.sync_target_settings import (
    MAX_SYNC_DELAY_MS,
    SyncTargetSettings,
    clamp_sync_delay_ms,
    clamp_sync_offset_px,
)
from services.feature_hotkey_monitor import normalize_feature_hotkey
from services.group_launch_service import (
    CONFIRMED_ENTRY_ALIASES,
    CONFIRMED_GROUP_ORDERS,
    SavedWindowPlacement,
)


@dataclass(frozen=True, slots=True)
class GroupConfigurationEntry:
    entry_id: str
    display_name: str
    shortcut_path: Path
    role: str
    order: int
    placement: SavedWindowPlacement | None = None
    sync_settings: SyncTargetSettings = SyncTargetSettings()
    role_id: str = ""


@dataclass(frozen=True, slots=True)
class GroupConfiguration:
    group_id: str
    name: str
    entries: tuple[GroupConfigurationEntry, ...]
    launch_hotkey: str = ""
    master_locked: bool = True
    sync_base_point: tuple[int, int] | None = None
    entry_order_customized: bool = False

    @property
    def main_entry(self) -> GroupConfigurationEntry | None:
        matches = tuple(
            entry for entry in self.entries if entry.role == "主窗口"
        )
        return matches[0] if len(matches) == 1 else None


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

    SCHEMA_VERSION = 2
    _SUPPORTED_SCHEMA_VERSIONS = frozenset({1, SCHEMA_VERSION})
    _ROOT_FIELDS = frozenset({"schema_version", "groups", "sync_edges"})
    _GROUP_FIELDS = frozenset(
        {
            "name",
            "group_id",
            "launch_entries",
            "launch_hotkey",
            "launch_hotkey_display",
            "master_locked",
            "entry_order_customized",
            "sync_base_x",
            "sync_base_y",
        }
    )
    _ENTRY_FIELDS = frozenset(
        {
            "entry_id",
            "path",
            "role",
            "x",
            "y",
            "width",
            "height",
            "delay_ms",
            "sync_offset_enabled",
            "sync_offset_x",
            "sync_offset_y",
            "sync_delay_ms",
            "role_id",
        }
    )
    _SENSITIVE_FIELDS = frozenset(
        {
            "arguments",
            "command_line",
            "credential",
            "launch_arguments",
            "password",
            "token",
        }
    )

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
        self._root_extras: dict[str, object] = {}
        self.migration_backup_path: Path | None = None
        self.corrupt_backup_path: Path | None = None
        self.recovered_from_backup = False
        self._load_or_import()

    @classmethod
    def _safe_extra_value(cls, value: object) -> object:
        if isinstance(value, Mapping):
            return {
                str(key): cls._safe_extra_value(item)
                for key, item in value.items()
                if (
                    isinstance(key, str)
                    and key.casefold() not in cls._SENSITIVE_FIELDS
                )
            }
        if isinstance(value, list):
            return [cls._safe_extra_value(item) for item in value]
        return deepcopy(value)

    @classmethod
    def _safe_extras(
        cls,
        value: Mapping[str, object],
        excluded: Iterable[str],
    ) -> dict[str, object]:
        excluded_names = {str(name).casefold() for name in excluded}
        return {
            str(key): cls._safe_extra_value(item)
            for key, item in value.items()
            if (
                isinstance(key, str)
                and key.casefold() not in excluded_names
                and key.casefold() not in cls._SENSITIVE_FIELDS
            )
        }

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
    def group_id_for_name(name: str) -> str:
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
        return f"group-{digest}"

    @classmethod
    def _clean_group_id(cls, value: object, name: str) -> str:
        cleaned = cls._clean_name(value)
        if cleaned is not None and cleaned.startswith("group-"):
            return cleaned
        return cls.group_id_for_name(name)

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

    @staticmethod
    def _clean_role_id(value: object) -> str:
        if not isinstance(value, str):
            return ""
        cleaned = "".join(
            character
            for character in value.strip()
            if not character.isspace() and ord(character) >= 32
        )
        return cleaned[:24]

    @staticmethod
    def _clean_sync_base_point(
        value: Mapping[str, object],
    ) -> tuple[int, int] | None:
        raw_x = value.get("sync_base_x")
        raw_y = value.get("sync_base_y")
        if (
            isinstance(raw_x, bool)
            or not isinstance(raw_x, int)
            or isinstance(raw_y, bool)
            or not isinstance(raw_y, int)
            or not (-20_000 <= raw_x <= 20_000)
            or not (-20_000 <= raw_y <= 20_000)
        ):
            return None
        return raw_x, raw_y

    @classmethod
    def _clean_sync_settings(
        cls,
        value: Mapping[str, object],
        *,
        placement: SavedWindowPlacement | None = None,
    ) -> SyncTargetSettings:
        raw_delay = value.get("sync_delay_ms")
        if raw_delay is None and placement is not None:
            raw_delay = min(placement.delay_ms, MAX_SYNC_DELAY_MS)
        return SyncTargetSettings.normalized(
            offset_enabled=value.get("sync_offset_enabled", False),
            offset_x=value.get("sync_offset_x", 0),
            offset_y=value.get("sync_offset_y", 0),
            delay_ms=raw_delay if raw_delay is not None else 0,
        )

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
        entry = cls._safe_extras(value, cls._ENTRY_FIELDS)
        entry.update(
            {
                "entry_id": cls._entry_id(path),
                "path": str(path),
                "role": role,
            }
        )
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
        sync_settings = cls._clean_sync_settings(
            value,
            placement=placement,
        )
        entry.update(
            {
                "sync_offset_enabled": sync_settings.offset_enabled,
                "sync_offset_x": sync_settings.offset_x,
                "sync_offset_y": sync_settings.offset_y,
                "sync_delay_ms": sync_settings.delay_ms,
                "role_id": cls._clean_role_id(value.get("role_id")),
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
        seen_group_ids: set[str] = set()
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
                main_indices = tuple(
                    index
                    for index, entry in enumerate(entries)
                    if entry.get("role") == "主窗口"
                )
                main_index = (
                    main_indices[0]
                    if len(main_indices) == 1
                    else 0
                )
                for index, entry in enumerate(entries):
                    entry["role"] = (
                        "主窗口"
                        if index == main_index
                        else "同步窗口"
                    )
            launch_hotkey = normalize_feature_hotkey(
                raw_group.get("launch_hotkey")
                or raw_group.get("launch_hotkey_display")
            )
            if launch_hotkey in seen_launch_hotkeys:
                launch_hotkey = ""
            if launch_hotkey:
                seen_launch_hotkeys.add(launch_hotkey)
            seen_names.add(name.casefold())
            group_id = cls._clean_group_id(
                raw_group.get("group_id"),
                name,
            )
            if group_id in seen_group_ids:
                group_id = cls.group_id_for_name(name)
            seen_group_ids.add(group_id)
            group = cls._safe_extras(raw_group, cls._GROUP_FIELDS)
            group.update(
                {
                    "group_id": group_id,
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
                    "entry_order_customized": (
                        raw_group.get("entry_order_customized")
                        if isinstance(
                            raw_group.get("entry_order_customized"),
                            bool,
                        )
                        else False
                    ),
                    **(
                        {
                            "sync_base_x": sync_base_point[0],
                            "sync_base_y": sync_base_point[1],
                        }
                        if (
                            sync_base_point
                            := cls._clean_sync_base_point(raw_group)
                        )
                        is not None
                        else {}
                    ),
                }
            )
            groups.append(group)
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

    @staticmethod
    def _next_sidecar_path(path: Path, label: str) -> Path:
        candidate = path.with_name(path.name + label)
        index = 1
        while candidate.exists():
            candidate = path.with_name(path.name + f"{label}.{index}")
            index += 1
        return candidate

    @staticmethod
    def _write_bytes_atomic(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        try:
            with temporary.open("wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def _write_json_atomic(
        cls,
        path: Path,
        payload: Mapping[str, object],
    ) -> None:
        data = (
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        cls._write_bytes_atomic(path, data)

    def _preserve_owned_file(self, label: str) -> Path | None:
        if not self.path.is_file():
            return None
        backup = self._next_sidecar_path(self.path, label)
        self._write_bytes_atomic(backup, self.path.read_bytes())
        return backup

    @classmethod
    def _payload_version(
        cls,
        payload: Mapping[str, object],
        *,
        allow_legacy: bool,
    ) -> int | None:
        version = payload.get("schema_version")
        if version is None and allow_legacy:
            return 0
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version <= 0
        ):
            return None
        if version > cls.SCHEMA_VERSION:
            raise ValueError("group configuration is newer than this app.")
        return version if version in cls._SUPPORTED_SCHEMA_VERSIONS else None

    def _load_payload(self, payload: Mapping[str, object]) -> None:
        self._root_extras = self._safe_extras(
            payload,
            self._ROOT_FIELDS,
        )
        self._groups = self._clean_groups(payload)
        self._sync_edges = self._clean_sync_edges(
            payload.get("sync_edges")
        )

    @classmethod
    def _merge_missing_fields(
        cls,
        destination: dict[str, object],
        source: Mapping[str, object],
    ) -> bool:
        changed = False
        for key, value in source.items():
            if key not in destination:
                destination[key] = cls._safe_extra_value(value)
                changed = True
                continue
            current = destination[key]
            if isinstance(current, dict) and isinstance(value, Mapping):
                changed = (
                    cls._merge_missing_fields(current, value)
                    or changed
                )
        return changed

    def _load_or_import(self) -> None:
        current_exists = self.path.is_file()
        current = self._read_json(self.path)
        if current is not None:
            version = self._payload_version(current, allow_legacy=False)
            if version is not None:
                self._load_payload(current)
                changed = self._merge_legacy_data()
                if version < self.SCHEMA_VERSION:
                    self.migration_backup_path = self._preserve_owned_file(
                        ".pre-migration"
                    )
                    changed = True
                if changed:
                    self._save()
                return

        if current_exists:
            self.corrupt_backup_path = self._next_sidecar_path(
                self.path,
                ".corrupt",
            )
            os.replace(self.path, self.corrupt_backup_path)

        backup = self._read_json(self.backup_path)
        if backup is not None:
            backup_version = self._payload_version(
                backup,
                allow_legacy=False,
            )
            if backup_version is not None:
                self._load_payload(backup)
                self.recovered_from_backup = True
                self._save()
                return

        legacy = self._read_json(self._legacy_config_path)
        if legacy is not None:
            self._load_payload(legacy)
        if self._groups:
            self.migration_backup_path = self._next_sidecar_path(
                self.path,
                ".pre-migration",
            )
            self._write_json_atomic(
                self.migration_backup_path,
                self._payload(),
            )
            self._save()

    def _merge_legacy_data(self) -> bool:
        legacy = self._read_json(self._legacy_config_path)
        if legacy is None:
            return False
        changed = self._merge_missing_fields(
            self._root_extras,
            self._safe_extras(legacy, self._ROOT_FIELDS),
        )
        legacy_groups = self._clean_groups(legacy or {})
        legacy_by_group: dict[str, dict[str, object]] = {}
        for group in legacy_groups:
            entries = group.get("launch_entries")
            if not isinstance(entries, list):
                continue
            legacy_by_group[str(group["name"]).casefold()] = group
        for group in self._groups:
            legacy_group = legacy_by_group.get(
                str(group["name"]).casefold(),
            )
            if legacy_group is None:
                continue
            changed = (
                self._merge_missing_fields(
                    group,
                    self._safe_extras(
                        legacy_group,
                        self._GROUP_FIELDS,
                    ),
                )
                or changed
            )
            raw_legacy_entries = legacy_group.get("launch_entries")
            legacy_entries = {
                str(entry["entry_id"]): entry
                for entry in raw_legacy_entries
                if isinstance(entry, Mapping)
            } if isinstance(raw_legacy_entries, list) else {}
            entries = group.get("launch_entries")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                legacy_entry = legacy_entries.get(str(entry["entry_id"]))
                if legacy_entry is None:
                    continue
                changed = (
                    self._merge_missing_fields(
                        entry,
                        self._safe_extras(
                            legacy_entry,
                            self._ENTRY_FIELDS,
                        ),
                    )
                    or changed
                )
                if self._clean_placement(entry) is not None:
                    continue
                placement = self._clean_placement(legacy_entry)
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
            controllers = tuple(
                entry
                for entry in raw_entries
                if entry.get("role") == "主窗口"
            )
            if len(controllers) != 1:
                continue
            controller = str(controllers[0]["entry_id"])
            targets = edges.setdefault(controller, [])
            for entry in raw_entries:
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
            **deepcopy(self._root_extras),
            "schema_version": self.SCHEMA_VERSION,
            "groups": self._groups,
            "sync_edges": self._sync_edges,
        }

    @property
    def backup_path(self) -> Path:
        return self.path.with_name(self.path.name + ".bak")

    def _save(self) -> None:
        current = self._read_json(self.path)
        if current is not None:
            self._write_bytes_atomic(
                self.backup_path,
                self.path.read_bytes(),
            )
        self._write_json_atomic(self.path, self._payload())

    def groups(self) -> tuple[GroupConfiguration, ...]:
        result: list[GroupConfiguration] = []
        for raw_group in self._groups:
            name = str(raw_group["name"])
            raw_entries = list(raw_group["launch_entries"])
            if (
                not bool(raw_group.get("entry_order_customized", False))
                and name in CONFIRMED_GROUP_ORDERS
            ):
                aliases = CONFIRMED_ENTRY_ALIASES.get(name, {})
                by_name = {
                    Path(str(entry["path"])).stem.casefold(): entry
                    for entry in raw_entries
                }
                expected_names = tuple(
                    aliases.get(display_name, display_name).casefold()
                    for display_name in CONFIRMED_GROUP_ORDERS[name]
                )
                if (
                    len(by_name) == len(raw_entries)
                    and set(expected_names) == set(by_name)
                ):
                    raw_entries = [
                        by_name[entry_name]
                        for entry_name in expected_names
                    ]
            entries = tuple(
                GroupConfigurationEntry(
                    entry_id=str(raw_entry["entry_id"]),
                    display_name=Path(str(raw_entry["path"])).stem,
                    shortcut_path=Path(str(raw_entry["path"])),
                    role=str(raw_entry["role"]),
                    order=index,
                    placement=self._clean_placement(raw_entry),
                    sync_settings=self._clean_sync_settings(
                        raw_entry,
                        placement=self._clean_placement(raw_entry),
                    ),
                    role_id=self._clean_role_id(
                        raw_entry.get("role_id")
                    ),
                )
                for index, raw_entry in enumerate(
                    raw_entries,
                    start=1,
                )
            )
            result.append(
                GroupConfiguration(
                    group_id=str(raw_group["group_id"]),
                    name=name,
                    entries=entries,
                    launch_hotkey=normalize_feature_hotkey(
                        raw_group.get("launch_hotkey")
                    ),
                    master_locked=bool(
                        raw_group.get("master_locked", True)
                    ),
                    sync_base_point=self._clean_sync_base_point(
                        raw_group
                    ),
                    entry_order_customized=bool(
                        raw_group.get("entry_order_customized", False)
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
                "group_id": self.group_id_for_name(cleaned),
                "name": cleaned,
                "launch_entries": [],
                "launch_hotkey": "",
                "master_locked": True,
                "entry_order_customized": False,
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

    def reorder_group_entries(
        self,
        group_name: object,
        entry_ids: object,
    ) -> bool:
        cleaned = self._clean_name(group_name)
        if cleaned is None or not isinstance(entry_ids, tuple):
            return False
        if (
            any(
                not isinstance(entry_id, str) or not entry_id.strip()
                for entry_id in entry_ids
            )
            or len(entry_ids) != len(set(entry_ids))
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
        current_ids = tuple(str(entry["entry_id"]) for entry in entries)
        normalized_ids = tuple(entry_id.strip() for entry_id in entry_ids)
        if (
            len(normalized_ids) != len(current_ids)
            or set(normalized_ids) != set(current_ids)
            or normalized_ids == current_ids
        ):
            return False
        entry_by_id = {
            str(entry["entry_id"]): entry
            for entry in entries
        }
        raw_group["launch_entries"] = [
            entry_by_id[entry_id]
            for entry_id in normalized_ids
        ]
        raw_group["entry_order_customized"] = True
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

    def set_sync_target_settings(
        self,
        group_name: object,
        entry_id: object,
        *,
        offset_enabled: object,
        offset_x: object,
        offset_y: object,
        delay_ms: object,
    ) -> bool:
        cleaned = self._clean_name(group_name)
        if (
            cleaned is None
            or not isinstance(entry_id, str)
            or not entry_id.strip()
            or not isinstance(offset_enabled, bool)
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
        entries = raw_group.get("launch_entries")
        if not isinstance(entries, list):
            return False
        raw_entry = next(
            (
                entry
                for entry in entries
                if entry.get("entry_id") == entry_id.strip()
            ),
            None,
        )
        if raw_entry is None:
            return False
        settings = SyncTargetSettings.normalized(
            offset_enabled=offset_enabled,
            offset_x=clamp_sync_offset_px(offset_x),
            offset_y=clamp_sync_offset_px(offset_y),
            delay_ms=clamp_sync_delay_ms(delay_ms),
        )
        current = self._clean_sync_settings(raw_entry)
        if current == settings:
            return False
        raw_entry.update(
            {
                "sync_offset_enabled": settings.offset_enabled,
                "sync_offset_x": settings.offset_x,
                "sync_offset_y": settings.offset_y,
                "sync_delay_ms": settings.delay_ms,
            }
        )
        self._save()
        return True

    def clear_sync_target_settings(
        self,
        group_name: object,
        entry_id: object,
    ) -> bool:
        return self.set_sync_target_settings(
            group_name,
            entry_id,
            offset_enabled=False,
            offset_x=0,
            offset_y=0,
            delay_ms=0,
        )

    def set_sync_base_point(
        self,
        group_name: object,
        point: tuple[int, int] | None,
    ) -> bool:
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
        if point is None:
            if self._clean_sync_base_point(raw_group) is None:
                return False
            raw_group.pop("sync_base_x", None)
            raw_group.pop("sync_base_y", None)
            self._save()
            return True
        if (
            not isinstance(point, tuple)
            or len(point) != 2
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in point
            )
            or any(not (-20_000 <= value <= 20_000) for value in point)
        ):
            return False
        normalized = (int(point[0]), int(point[1]))
        if self._clean_sync_base_point(raw_group) == normalized:
            return False
        raw_group["sync_base_x"], raw_group["sync_base_y"] = normalized
        self._save()
        return True

    def set_role_id(
        self,
        group_name: object,
        entry_id: object,
        role_id: object,
    ) -> bool:
        cleaned = self._clean_name(group_name)
        normalized_role_id = self._clean_role_id(role_id)
        if (
            cleaned is None
            or not isinstance(entry_id, str)
            or not entry_id.strip()
            or (
                isinstance(role_id, str)
                and role_id.strip()
                and not normalized_role_id
            )
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
        entries = raw_group.get("launch_entries")
        if not isinstance(entries, list):
            return False
        raw_entry = next(
            (
                entry
                for entry in entries
                if entry.get("entry_id") == entry_id.strip()
            ),
            None,
        )
        if raw_entry is None:
            return False
        if self._clean_role_id(raw_entry.get("role_id")) == normalized_role_id:
            return False
        raw_entry["role_id"] = normalized_role_id
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
        original_root_extras = self._root_extras
        proposed_groups = deepcopy(self._groups)
        proposed_root_extras = deepcopy(self._root_extras)
        self._merge_missing_fields(
            proposed_root_extras,
            self._safe_extras(payload, self._ROOT_FIELDS),
        )
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
        self._root_extras = proposed_root_extras
        imported_edges = self._clean_sync_edges(payload.get("sync_edges"))
        for source_id, targets in imported_edges.items():
            proposed_edges[source_id] = list(targets)
        if self._has_cycle(
            self._combined_sync_edges(explicit=proposed_edges)
        ):
            self._groups = original_groups
            self._sync_edges = original_edges
            self._root_extras = original_root_extras
            raise SyncCycleError(SyncCycleError.player_message)
        self._sync_edges = proposed_edges
        try:
            self._save()
        except Exception:
            self._groups = original_groups
            self._sync_edges = original_edges
            self._root_extras = original_root_extras
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
                "group_id": self.group_id_for_name(cleaned),
                "name": cleaned,
                "launch_entries": [],
                "launch_hotkey": "",
                "master_locked": False,
                "entry_order_customized": False,
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
        removed_entry = next(
            (
                entry
                for entry in entries
                if entry.get("entry_id") == entry_id.strip()
            ),
            None,
        )
        remaining = [
            entry
            for entry in entries
            if entry.get("entry_id") != entry_id.strip()
        ]
        if len(remaining) == len(entries):
            return False
        if (
            remaining
            and removed_entry is not None
            and removed_entry.get("role") == "主窗口"
        ):
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
        if (
            index is None
            or entries[index].get("role") == "主窗口"
        ):
            return False
        original_roles = tuple(
            str(entry.get("role", "")) for entry in entries
        )
        for entry_index, entry in enumerate(entries):
            entry["role"] = (
                "主窗口"
                if entry_index == index
                else "同步窗口"
            )
        if self._has_cycle(self._combined_sync_edges()):
            for entry, role in zip(entries, original_roles):
                entry["role"] = role
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
        raw_group["entry_order_customized"] = False
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
        if group is None or group.main_entry is None:
            return ()
        controller = group.main_entry.entry_id
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
        if group is None or group.main_entry is None:
            return ()
        controller = group.main_entry.entry_id
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
