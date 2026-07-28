"""Resolve one authoritative target set for detection, sync and reconnect."""

from __future__ import annotations

from adapters.windows_launch_fingerprint import normalize_launch_fingerprint
from adapters.windows_window import WindowBackend, WindowInfo
from core.target_window_contract import (
    TargetWindowContract,
    TargetWindowPhase,
    TargetWindowSnapshot,
)
from core.window_registry import WindowRegistry
from services.group_configuration_service import (
    GroupConfigurationEntry,
    GroupConfigurationService,
)
from services.sync_scope_service import SyncScopeService


class TargetWindowContractService:
    """Build immutable snapshots from one backend enumeration."""

    def __init__(
        self,
        configuration: GroupConfigurationService,
        scope_service: SyncScopeService,
        registry: WindowRegistry,
        window_backend: WindowBackend,
    ) -> None:
        self._configuration = configuration
        self._scope_service = scope_service
        self._registry = registry
        self._window_backend = window_backend
        self._scope_cache: dict[
            str,
            tuple[tuple[int, int] | None, object],
        ] = {}

    def _scope(self, group_name: str):
        try:
            stat = self._configuration.path.stat()
            signature: tuple[int, int] | None = (
                stat.st_mtime_ns,
                stat.st_size,
            )
        except OSError:
            signature = None
        cached = self._scope_cache.get(group_name)
        if cached is not None and cached[0] == signature:
            return cached[1]
        scope = self._scope_service.scope(group_name)
        self._scope_cache[group_name] = (signature, scope)
        return scope

    def snapshot(
        self,
        group_name: object,
        *,
        expanded_sync_scope: bool = True,
    ) -> TargetWindowSnapshot:
        if not isinstance(group_name, str) or not group_name.strip():
            return TargetWindowSnapshot(
                TargetWindowSnapshot.SCHEMA_VERSION,
                "",
                failure_codes=("group_name_invalid",),
            )
        name = group_name.strip()
        group = self._configuration.group(name)
        if group is None or not group.entries:
            return TargetWindowSnapshot(
                TargetWindowSnapshot.SCHEMA_VERSION,
                name,
                failure_codes=("group_entries_unavailable",),
            )

        entry_by_id = {
            entry.entry_id: entry
            for configured_group in self._configuration.groups()
            for entry in configured_group.entries
        }
        entry_group = {
            entry.entry_id: configured_group.name
            for configured_group in self._configuration.groups()
            for entry in configured_group.entries
        }
        scope = self._scope(name)
        if scope.ready:
            fingerprint_by_id = dict(zip(scope.entry_ids, scope.fingerprints))
            selected_ids = (
                scope.entry_ids
                if expanded_sync_scope
                else tuple(entry.entry_id for entry in group.entries)
            )
            snapshot_failures: tuple[str, ...] = ()
        else:
            fingerprint_by_id = {}
            selected_ids = tuple(entry.entry_id for entry in group.entries)
            snapshot_failures = scope.failure_codes

        try:
            windows = tuple(self._window_backend.list_windows())
            foreground_handle = self._window_backend.foreground_handle()
        except Exception:
            windows = ()
            foreground_handle = None
            snapshot_failures = tuple(
                dict.fromkeys((*snapshot_failures, "window_enumeration_failed"))
            )

        windows_by_fingerprint: dict[str, list[WindowInfo]] = {}
        for window in windows:
            fingerprint = normalize_launch_fingerprint(
                window.launch_fingerprint
            )
            if fingerprint is not None:
                windows_by_fingerprint.setdefault(fingerprint, []).append(window)

        targets = tuple(
            self._resolve_entry(
                entry_by_id[entry_id],
                entry_group[entry_id],
                fingerprint_by_id.get(entry_id),
                windows_by_fingerprint,
                foreground_handle,
            )
            for entry_id in selected_ids
            if entry_id in entry_by_id
        )
        if len(targets) != len(selected_ids):
            snapshot_failures = tuple(
                dict.fromkeys((*snapshot_failures, "target_entry_unresolved"))
            )
        return TargetWindowSnapshot(
            TargetWindowSnapshot.SCHEMA_VERSION,
            name,
            targets,
            snapshot_failures,
        )

    def windows(
        self,
        group_name: object,
        *,
        expanded_sync_scope: bool = True,
    ) -> tuple[WindowInfo, ...]:
        """Return only uniquely resolved windows from the versioned snapshot."""
        snapshot = self.snapshot(
            group_name,
            expanded_sync_scope=expanded_sync_scope,
        )
        return tuple(
            WindowInfo(
                handle=target.handle,
                title="",
                visible=target.visible,
                minimized=target.phase is TargetWindowPhase.MINIMIZED,
                rect=target.rect,
                process_id=target.process_id,
                launch_fingerprint=target.fingerprint,
            )
            for target in snapshot.safe_targets
            if target.handle is not None
            and target.rect is not None
            and target.fingerprint is not None
        )

    def _resolve_entry(
        self,
        entry: GroupConfigurationEntry,
        group_name: str,
        fingerprint: str | None,
        windows_by_fingerprint: dict[str, list[WindowInfo]],
        foreground_handle: int | None,
    ) -> TargetWindowContract:
        character_id = None
        try:
            record = self._registry.get(entry.entry_id)
        except KeyError:
            record = None
        if record is not None and record.group == group_name:
            character_id = record.character_id

        matches = (
            tuple(windows_by_fingerprint.get(fingerprint, ()))
            if fingerprint is not None
            else ()
        )
        failures: tuple[str, ...] = ()
        window = None
        if fingerprint is None:
            failures = ("shortcut_identity_unresolved",)
            phase = TargetWindowPhase.UNKNOWN
        elif not matches:
            failures = ("window_offline",)
            phase = TargetWindowPhase.OFFLINE
        elif len(matches) != 1:
            failures = ("window_identity_duplicate",)
            phase = TargetWindowPhase.UNKNOWN
        else:
            window = matches[0]
            if window.minimized:
                phase = TargetWindowPhase.MINIMIZED
            elif window.handle == foreground_handle:
                phase = TargetWindowPhase.FOREGROUND
            else:
                phase = TargetWindowPhase.BACKGROUND

        return TargetWindowContract(
            TargetWindowContract.SCHEMA_VERSION,
            group_name,
            entry.entry_id,
            entry.display_name,
            entry.role,
            entry.role_id or None,
            character_id,
            window.process_id if window is not None else None,
            fingerprint,
            phase,
            safe=window is not None and not failures,
            failure_codes=failures,
            handle=window.handle if window is not None else None,
            rect=window.rect if window is not None else None,
            visible=bool(window.visible) if window is not None else False,
        )
