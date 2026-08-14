"""Resolve recursive cross-group synchronization into one deduplicated scope."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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
    shortcut_paths: tuple[Path, ...] = ()
    entry_fingerprints: tuple[str | None, ...] = ()
    isolated_entry_ids: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return (
            self.controller_entry_id is not None
            and bool(self.fingerprints)
            and bool(self.entry_ids)
            and self.entry_ids[0] == self.controller_entry_id
            and len(self.shortcut_paths) == len(self.entry_ids)
            and len(self.entry_fingerprints) == len(self.entry_ids)
            and self.entry_fingerprints[0] is not None
            and not self.failure_codes
        )


@dataclass(frozen=True, slots=True)
class SyncScopeInputs:
    group_name: str
    controller_entry_id: str | None = None
    entry_ids: tuple[str, ...] = ()
    shortcut_paths: tuple[Path, ...] = ()
    failure_codes: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return (
            self.controller_entry_id is not None
            and bool(self.entry_ids)
            and self.entry_ids[0] == self.controller_entry_id
            and len(self.shortcut_paths) == len(self.entry_ids)
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

    def inputs(self, group_name: object) -> SyncScopeInputs:
        if not isinstance(group_name, str) or not group_name.strip():
            return SyncScopeInputs(
                "",
                failure_codes=("group_name_invalid",),
            )
        name = group_name.strip()
        selected = self._configuration.group(name)
        if selected is None or not selected.entries:
            return SyncScopeInputs(
                name,
                failure_codes=("group_entries_unavailable",),
            )
        if selected.main_entry is None:
            return SyncScopeInputs(
                name,
                failure_codes=("group_controller_unavailable",),
            )
        controller = selected.main_entry.entry_id
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
                    return SyncScopeInputs(
                        name,
                        controller,
                        entry_ids=ordered_ids,
                        failure_codes=(
                            "sync_identity_path_conflict",
                        ),
                    )
                entry_by_id[entry.entry_id] = entry
        if any(entry_id not in entry_by_id for entry_id in ordered_ids):
            return SyncScopeInputs(
                name,
                controller,
                entry_ids=ordered_ids,
                failure_codes=("sync_identity_unresolved",),
            )
        paths = tuple(entry_by_id[entry_id].shortcut_path for entry_id in ordered_ids)
        return SyncScopeInputs(
            name,
            controller,
            ordered_ids,
            paths,
        )

    def configured_inputs(self) -> SyncScopeInputs:
        """Return every configured entry once, without resolving shortcuts."""

        entries = {}
        for group in self._configuration.groups():
            for entry in group.entries:
                existing = entries.get(entry.entry_id)
                if (
                    existing is not None
                    and existing.shortcut_path != entry.shortcut_path
                ):
                    return SyncScopeInputs(
                        "configured",
                        failure_codes=("configured_identity_path_conflict",),
                    )
                entries.setdefault(entry.entry_id, entry)
        entry_ids = tuple(entries)
        paths = tuple(entries[entry_id].shortcut_path for entry_id in entry_ids)
        if not entry_ids:
            return SyncScopeInputs(
                "configured",
                failure_codes=("configured_entries_unavailable",),
            )
        return SyncScopeInputs(
            "configured",
            entry_ids[0],
            entry_ids,
            paths,
        )

    def configured_scope(self) -> SyncScope:
        """Resolve all configured shortcut identities in one batch call."""

        inputs = self.configured_inputs()
        if not inputs.ready:
            return SyncScope(
                inputs.group_name,
                inputs.controller_entry_id,
                failure_codes=inputs.failure_codes,
                entry_ids=inputs.entry_ids,
                shortcut_paths=inputs.shortcut_paths,
            )
        resolved = self._fingerprint_resolver.resolve(inputs.shortcut_paths)
        entry_fingerprints = tuple(
            normalize_launch_fingerprint(resolved.get(path))
            for path in inputs.shortcut_paths
        )
        if any(fingerprint is None for fingerprint in entry_fingerprints):
            return SyncScope(
                inputs.group_name,
                inputs.controller_entry_id,
                tuple(
                    fingerprint
                    for fingerprint in entry_fingerprints
                    if fingerprint is not None
                ),
                failure_codes=("shortcut_identity_unresolved",),
                entry_ids=inputs.entry_ids,
                shortcut_paths=inputs.shortcut_paths,
                entry_fingerprints=entry_fingerprints,
                isolated_entry_ids=tuple(
                    entry_id
                    for entry_id, fingerprint in zip(
                        inputs.entry_ids,
                        entry_fingerprints,
                    )
                    if fingerprint is None
                ),
            )
        return SyncScope(
            inputs.group_name,
            inputs.controller_entry_id,
            tuple(entry_fingerprints),
            entry_ids=inputs.entry_ids,
            shortcut_paths=inputs.shortcut_paths,
            entry_fingerprints=entry_fingerprints,
        )

    def scope(self, group_name: object) -> SyncScope:
        inputs = self.inputs(group_name)
        if not inputs.ready:
            return SyncScope(
                inputs.group_name,
                inputs.controller_entry_id,
                failure_codes=inputs.failure_codes,
                entry_ids=inputs.entry_ids,
                shortcut_paths=inputs.shortcut_paths,
            )
        paths = inputs.shortcut_paths
        resolved = self._fingerprint_resolver.resolve(paths)
        entry_fingerprints = tuple(
            normalize_launch_fingerprint(resolved.get(path))
            for path in paths
        )
        fingerprints = tuple(
            fingerprint
            for fingerprint in entry_fingerprints
            if fingerprint is not None
        )
        isolated_entry_ids = tuple(
            entry_id
            for entry_id, fingerprint in zip(
                inputs.entry_ids,
                entry_fingerprints,
            )
            if fingerprint is None
        )
        if not entry_fingerprints or entry_fingerprints[0] is None:
            return SyncScope(
                inputs.group_name,
                inputs.controller_entry_id,
                fingerprints,
                failure_codes=("shortcut_identity_unresolved",),
                entry_ids=inputs.entry_ids,
                shortcut_paths=paths,
                entry_fingerprints=entry_fingerprints,
                isolated_entry_ids=isolated_entry_ids,
            )
        # A launch digest identifies the executable image, not one live
        # top-level game window.  Keep ordered duplicate digests here; the
        # target-window contract later binds each configured entry to exactly
        # one player-confirmed complete window instance before any controller
        # receives an allowed identity.
        return SyncScope(
            inputs.group_name,
            inputs.controller_entry_id,
            fingerprints,
            entry_ids=inputs.entry_ids,
            shortcut_paths=paths,
            entry_fingerprints=entry_fingerprints,
            isolated_entry_ids=isolated_entry_ids,
        )
