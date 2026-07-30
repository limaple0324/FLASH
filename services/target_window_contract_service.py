"""Resolve one authoritative target set for detection, sync and reconnect."""

from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True, slots=True)
class ResolvedTargetWindows:
    """Safe open windows plus aggregate isolation evidence."""

    windows: tuple[WindowInfo, ...]
    failure_codes: tuple[str, ...] = ()
    blocked_fingerprints: frozenset[str] = frozenset()


class TargetWindowContractService:
    """Build immutable snapshots from one backend enumeration."""

    def __init__(
        self,
        configuration: GroupConfigurationService,
        scope_service: SyncScopeService,
        registry: WindowRegistry,
        window_backend: WindowBackend,
        *,
        title_keywords: tuple[str, ...] = ("Adobe Flash Player",),
    ) -> None:
        self._configuration = configuration
        self._scope_service = scope_service
        self._registry = registry
        self._window_backend = window_backend
        self._title_keywords = tuple(
            keyword.strip().casefold()
            for keyword in title_keywords
            if isinstance(keyword, str) and keyword.strip()
        )
        if not self._title_keywords:
            raise ValueError("title_keywords must not be empty.")
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

        configured_groups = self._configuration.groups()
        root_entries = {
            entry.entry_id: entry for entry in group.entries
        }
        entry_candidates: dict[
            str,
            list[tuple[str, GroupConfigurationEntry]],
        ] = {}
        for configured_group in configured_groups:
            for entry in configured_group.entries:
                entry_candidates.setdefault(entry.entry_id, []).append(
                    (configured_group.name, entry)
                )
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

        candidate_windows = tuple(
            window
            for window in windows
            if all(
                keyword in window.title.casefold()
                for keyword in self._title_keywords
            )
        )
        if any(
            normalize_launch_fingerprint(window.launch_fingerprint) is None
            for window in candidate_windows
        ):
            snapshot_failures = tuple(
                dict.fromkeys(
                    (
                        *snapshot_failures,
                        "unidentified_candidate_window",
                    )
                )
            )

        windows_by_fingerprint: dict[str, list[WindowInfo]] = {}
        for window in candidate_windows:
            fingerprint = normalize_launch_fingerprint(
                window.launch_fingerprint
            )
            if fingerprint is not None:
                windows_by_fingerprint.setdefault(fingerprint, []).append(window)

        resolved_entries: list[
            tuple[GroupConfigurationEntry, str, str | None, bool]
        ] = []
        for entry_id in selected_ids:
            root_entry = root_entries.get(entry_id)
            if root_entry is not None:
                resolved_entries.append(
                    (
                        root_entry,
                        name,
                        fingerprint_by_id.get(entry_id),
                        len(entry_candidates.get(entry_id, ())) > 1,
                    )
                )
                continue
            candidates = entry_candidates.get(entry_id, ())
            if len(candidates) == 1:
                candidate_group, candidate_entry = candidates[0]
                resolved_entries.append(
                    (
                        candidate_entry,
                        candidate_group,
                        fingerprint_by_id.get(entry_id),
                        False,
                    )
                )
                continue
            snapshot_failures = tuple(
                dict.fromkeys(
                    (
                        *snapshot_failures,
                        (
                            "target_entry_group_ambiguous"
                            if candidates
                            else "target_entry_unresolved"
                        ),
                    )
                )
            )

        targets = tuple(
            self._resolve_entry(
                entry,
                entry_group,
                fingerprint,
                character_identity_ambiguous,
                windows_by_fingerprint,
                foreground_handle,
            )
            for (
                entry,
                entry_group,
                fingerprint,
                character_identity_ambiguous,
            ) in resolved_entries
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
        if snapshot.failure_codes:
            return ()
        return self._safe_windows(snapshot)

    def reconnect_targets(
        self,
        group_name: object,
        *,
        expanded_sync_scope: bool = True,
    ) -> ResolvedTargetWindows:
        """Keep uniquely resolved open roles while isolating unsafe siblings."""
        snapshot = self.snapshot(
            group_name,
            expanded_sync_scope=expanded_sync_scope,
        )
        failures = list(snapshot.failure_codes)
        for target in snapshot.targets:
            failures.extend(target.failure_codes)
        blocked_fingerprints = frozenset(
            target.fingerprint
            for target in snapshot.targets
            if target.fingerprint is not None
            and target.failure_codes
            and target.failure_codes != ("window_offline",)
        )
        return ResolvedTargetWindows(
            self._safe_windows(snapshot),
            tuple(dict.fromkeys(failures)),
            blocked_fingerprints,
        )

    @staticmethod
    def _safe_windows(
        snapshot: TargetWindowSnapshot,
    ) -> tuple[WindowInfo, ...]:
        return tuple(
            WindowInfo(
                handle=target.handle,
                title="",
                visible=target.visible,
                minimized=target.phase is TargetWindowPhase.MINIMIZED,
                rect=target.rect,
                process_id=target.process_id,
                launch_fingerprint=target.fingerprint,
                thread_id=target.thread_id,
                window_class=target.window_class,
                process_lifecycle_token=(
                    target.process_lifecycle_token
                ),
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
        character_identity_ambiguous: bool,
        windows_by_fingerprint: dict[str, list[WindowInfo]],
        foreground_handle: int | None,
    ) -> TargetWindowContract:
        character_id = None
        try:
            record = self._registry.get(entry.entry_id)
        except KeyError:
            record = None
        if (
            not character_identity_ambiguous
            and record is not None
            and record.group == group_name
        ):
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
            (
                f"{GroupConfigurationService.group_id_for_name(group_name)}:"
                f"{entry.entry_id}"
            ),
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
            thread_id=window.thread_id if window is not None else None,
            window_class=(
                window.window_class if window is not None else None
            ),
            process_lifecycle_token=(
                window.process_lifecycle_token
                if window is not None
                else None
            ),
        )
