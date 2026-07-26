"""Read-only group launch plans with stable order and anonymous identities."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from adapters.windows_launch_fingerprint import (
    ShortcutFingerprintResolver,
    normalize_launch_fingerprint,
)


CONFIRMED_GROUP_ORDERS: dict[str, tuple[str, ...]] = {
    "14支": (
        "100古",
        "100靈",
        "100福",
        "100獵",
        "120古",
        "120靈",
        "120射",
        "120福",
        "120獵",
        "亞洛",
        "餐廳",
        "大排",
        "160帥",
        "和尚",
    ),
}

# The old read-only registration still names this shortcut 160福. It is the
# only unmatched entry in the player's confirmed 14-character sequence, so
# the player-facing order uses the supplied name while preserving that exact
# registered shortcut identity.
CONFIRMED_ENTRY_ALIASES: dict[str, dict[str, str]] = {
    "14支": {"亞洛": "160福"},
}


@dataclass(frozen=True, slots=True)
class GroupLaunchTarget:
    order: int
    display_name: str
    shortcut_path: Path
    fingerprint: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.order, bool)
            or not isinstance(self.order, int)
            or self.order <= 0
        ):
            raise ValueError("order must be a positive integer.")
        display_name = self.display_name.strip()
        if not display_name:
            raise ValueError("display_name must not be empty.")
        shortcut_path = Path(self.shortcut_path)
        if shortcut_path.suffix.casefold() != ".lnk":
            raise ValueError("shortcut_path must be a .lnk file.")
        fingerprint = normalize_launch_fingerprint(self.fingerprint)
        if fingerprint is None:
            raise ValueError("fingerprint must be a complete SHA-256 digest.")
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "shortcut_path", shortcut_path)
        object.__setattr__(self, "fingerprint", fingerprint)


@dataclass(frozen=True, slots=True)
class GroupLaunchPlan:
    group_name: str
    targets: tuple[GroupLaunchTarget, ...] = ()
    failure_codes: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return bool(self.targets) and not self.failure_codes

    @property
    def fingerprints(self) -> frozenset[str]:
        return frozenset(target.fingerprint for target in self.targets)

    def target_for_fingerprint(
        self,
        fingerprint: object,
    ) -> GroupLaunchTarget | None:
        normalized = normalize_launch_fingerprint(fingerprint)
        if normalized is None:
            return None
        matches = tuple(
            target
            for target in self.targets
            if target.fingerprint == normalized
        )
        return matches[0] if len(matches) == 1 else None


class GroupLaunchService:
    """Build exact plans without modifying the old program or its config."""

    def __init__(
        self,
        legacy_config_path: Path | None,
        fingerprint_resolver: ShortcutFingerprintResolver,
    ) -> None:
        if not callable(getattr(fingerprint_resolver, "resolve", None)):
            raise TypeError("fingerprint_resolver must provide resolve(paths).")
        self._legacy_config_path = (
            Path(legacy_config_path)
            if legacy_config_path is not None
            else None
        )
        self._fingerprint_resolver = fingerprint_resolver
        self._cache_signature: tuple[int, int] | None = None
        self._cache: dict[str, GroupLaunchPlan] = {}

    @staticmethod
    def _clean_name(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        name = value.strip()
        if not name or len(name) > 80:
            return None
        if any(ord(character) < 32 for character in name):
            return None
        return name

    def _payload(self) -> Mapping[str, object] | None:
        path = self._legacy_config_path
        if path is None or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, Mapping) else None

    def _signature(self) -> tuple[int, int] | None:
        path = self._legacy_config_path
        if path is None:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def plan(self, group_name: object) -> GroupLaunchPlan:
        cleaned_group = self._clean_name(group_name)
        if cleaned_group is None:
            return GroupLaunchPlan("", failure_codes=("group_name_invalid",))
        signature = self._signature()
        if signature != self._cache_signature:
            self._cache.clear()
            self._cache_signature = signature
        cached = self._cache.get(cleaned_group)
        if cached is not None:
            return cached

        payload = self._payload()
        if payload is None:
            plan = GroupLaunchPlan(
                cleaned_group,
                failure_codes=("group_config_unavailable",),
            )
            self._cache[cleaned_group] = plan
            return plan
        raw_groups = payload.get("groups")
        if not isinstance(raw_groups, list):
            plan = GroupLaunchPlan(
                cleaned_group,
                failure_codes=("group_config_malformed",),
            )
            self._cache[cleaned_group] = plan
            return plan
        matching_groups = tuple(
            item
            for item in raw_groups
            if isinstance(item, Mapping)
            and self._clean_name(item.get("name")) == cleaned_group
        )
        if len(matching_groups) != 1:
            plan = GroupLaunchPlan(
                cleaned_group,
                failure_codes=("group_missing_or_duplicate",),
            )
            self._cache[cleaned_group] = plan
            return plan
        raw_entries = matching_groups[0].get("launch_entries")
        if not isinstance(raw_entries, list) or not raw_entries:
            plan = GroupLaunchPlan(
                cleaned_group,
                failure_codes=("group_launch_entries_unavailable",),
            )
            self._cache[cleaned_group] = plan
            return plan

        entries: list[tuple[str, Path]] = []
        failures: list[str] = []
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, Mapping):
                failures.append("group_launch_entry_invalid")
                continue
            raw_path = raw_entry.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                failures.append("group_launch_entry_invalid")
                continue
            path = Path(raw_path)
            name = self._clean_name(path.stem)
            if (
                name is None
                or path.suffix.casefold() != ".lnk"
                or not path.is_file()
            ):
                failures.append("group_launch_entry_invalid")
                continue
            entries.append((name, path))
        names = [name.casefold() for name, _path in entries]
        paths = [str(path).casefold() for _name, path in entries]
        if len(entries) != len(raw_entries):
            failures.append("group_launch_entry_count_mismatch")
        if len(names) != len(set(names)) or len(paths) != len(set(paths)):
            failures.append("group_launch_entry_duplicate")
        if failures:
            plan = GroupLaunchPlan(
                cleaned_group,
                failure_codes=tuple(dict.fromkeys(failures)),
            )
            self._cache[cleaned_group] = plan
            return plan

        by_name = {name.casefold(): path for name, path in entries}
        confirmed_order = CONFIRMED_GROUP_ORDERS.get(cleaned_group)
        aliases = CONFIRMED_ENTRY_ALIASES.get(cleaned_group, {})
        if confirmed_order is None:
            ordered_names = tuple(name for name, _path in entries)
        else:
            ordered_names = confirmed_order
            expected_actual = {
                aliases.get(display_name, display_name).casefold()
                for display_name in confirmed_order
            }
            if expected_actual != set(by_name):
                plan = GroupLaunchPlan(
                    cleaned_group,
                    failure_codes=("group_fixed_order_mismatch",),
                )
                self._cache[cleaned_group] = plan
                return plan

        ordered_paths = tuple(
            by_name[aliases.get(display_name, display_name).casefold()]
            for display_name in ordered_names
        )
        resolved = self._fingerprint_resolver.resolve(ordered_paths)
        fingerprints = tuple(
            normalize_launch_fingerprint(resolved.get(path))
            for path in ordered_paths
        )
        if any(fingerprint is None for fingerprint in fingerprints):
            plan = GroupLaunchPlan(
                cleaned_group,
                failure_codes=("shortcut_identity_unresolved",),
            )
            self._cache[cleaned_group] = plan
            return plan
        if len(set(fingerprints)) != len(fingerprints):
            plan = GroupLaunchPlan(
                cleaned_group,
                failure_codes=("shortcut_identity_duplicate",),
            )
            self._cache[cleaned_group] = plan
            return plan

        targets = tuple(
            GroupLaunchTarget(
                order=index,
                display_name=display_name,
                shortcut_path=path,
                fingerprint=fingerprint,
            )
            for index, (display_name, path, fingerprint) in enumerate(
                zip(ordered_names, ordered_paths, fingerprints),
                start=1,
            )
        )
        plan = GroupLaunchPlan(cleaned_group, targets=targets)
        self._cache[cleaned_group] = plan
        return plan

