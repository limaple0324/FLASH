"""Resolve recursive cross-group synchronization into one deduplicated scope."""

from __future__ import annotations

from dataclasses import dataclass

from adapters.windows_launch_fingerprint import (
    ShortcutFingerprintResolver,
    normalize_launch_fingerprint,
)
from services.group_configuration_service import GroupConfigurationService


@dataclass(frozen=True, slots=True)
class SyncScope:
    group_name: str
    controller_entry_id: str | None = None
    fingerprints: tuple[str, ...] = ()
    failure_codes: tuple[str, ...] = ()
    entry_ids: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return (
            self.controller_entry_id is not None
            and bool(self.fingerprints)
            and not self.failure_codes
        )


class SyncScopeService:
    """Use saved acyclic relations and unique shortcut identities."""

    def __init__(
        self,
        configuration: GroupConfigurationService,
        fingerprint_resolver: ShortcutFingerprintResolver,
    ) -> None:
        self._configuration = configuration
        self._fingerprint_resolver = fingerprint_resolver

    def scope(self, group_name: object) -> SyncScope:
        if not isinstance(group_name, str) or not group_name.strip():
            return SyncScope("", failure_codes=("group_name_invalid",))
        name = group_name.strip()
        selected = self._configuration.group(name)
        if selected is None or not selected.entries:
            return SyncScope(
                name,
                failure_codes=("group_entries_unavailable",),
            )
        controller = selected.entries[0].entry_id
        member_ids = self._configuration.expanded_sync_members(controller)
        ordered_ids = (controller, *member_ids)
        entry_by_id = {}
        for group in self._configuration.groups():
            for entry in group.entries:
                current = entry_by_id.get(entry.entry_id)
                if (
                    current is not None
                    and current.shortcut_path != entry.shortcut_path
                ):
                    return SyncScope(
                        name,
                        controller,
                        failure_codes=(
                            "sync_identity_path_conflict",
                        ),
                    )
                entry_by_id[entry.entry_id] = entry
        if any(entry_id not in entry_by_id for entry_id in ordered_ids):
            return SyncScope(
                name,
                controller,
                failure_codes=("sync_identity_unresolved",),
            )
        paths = tuple(entry_by_id[entry_id].shortcut_path for entry_id in ordered_ids)
        resolved = self._fingerprint_resolver.resolve(paths)
        fingerprints = tuple(
            normalize_launch_fingerprint(resolved.get(path))
            for path in paths
        )
        if any(fingerprint is None for fingerprint in fingerprints):
            return SyncScope(
                name,
                controller,
                failure_codes=("shortcut_identity_unresolved",),
            )
        if len(fingerprints) != len(set(fingerprints)):
            return SyncScope(
                name,
                controller,
                failure_codes=("shortcut_identity_duplicate",),
            )
        return SyncScope(
            name,
            controller,
            tuple(fingerprint for fingerprint in fingerprints if fingerprint),
            entry_ids=ordered_ids,
        )
