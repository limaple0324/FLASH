"""Resolve one authoritative target set for detection, sync and reconnect."""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path

from adapters.windows_launch_fingerprint import normalize_launch_fingerprint
from adapters.windows_window import (
    WindowBackend,
    WindowInfo,
    complete_window_instance_identity,
    monitored_window_instance_fingerprint,
)
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
from services.sync_scope_service import SyncScope, SyncScopeService


@dataclass(frozen=True, slots=True)
class ResolvedTargetWindows:
    """Safe open windows plus aggregate isolation evidence."""

    windows: tuple[WindowInfo, ...]
    failure_codes: tuple[str, ...] = ()
    blocked_fingerprints: frozenset[str] = frozenset()
    sync_windows: tuple[WindowInfo, ...] = ()
    sync_entry_ids: tuple[str, ...] = ()
    sync_scope_entry_ids: tuple[str, ...] = ()
    sync_controller_entry_id: str | None = None


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
            tuple[tuple[object, ...], SyncScope],
        ] = {}

    @staticmethod
    def _file_evidence(path: Path) -> tuple[object, ...]:
        candidate = Path(path)
        try:
            with candidate.open("rb") as stream:
                before = os.fstat(stream.fileno())
                resolved_path = str(candidate.resolve(strict=True))
                digest = sha256()
                for chunk in iter(lambda: stream.read(65536), b""):
                    digest.update(chunk)
                after = os.fstat(stream.fileno())
        except OSError as error:
            return (
                str(candidate),
                False,
                type(error).__name__,
                getattr(error, "errno", None),
                getattr(error, "winerror", None),
            )
        return (
            str(candidate),
            resolved_path,
            True,
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            digest.hexdigest(),
        )

    def _scope_signature(
        self,
        shortcut_paths: tuple[Path, ...],
    ) -> tuple[object, ...]:
        return (
            GroupConfigurationService.SCHEMA_VERSION,
            self._file_evidence(self._configuration.path),
            tuple(
                self._file_evidence(path)
                for path in shortcut_paths
            ),
        )

    def _resolved_scope(
        self,
        group_name: str,
    ) -> tuple[tuple[object, ...], SyncScope]:
        inputs = self._scope_service.inputs(group_name)
        observed_signature = self._scope_signature(inputs.shortcut_paths)
        try:
            cached = self._scope_cache[group_name]
        except KeyError:
            cached = None
        if (
            cached is not None
            and cached[0] == observed_signature
            and cached[1].controller_entry_id == inputs.controller_entry_id
            and cached[1].entry_ids == inputs.entry_ids
            and cached[1].shortcut_paths == inputs.shortcut_paths
        ):
            return cached

        scope = self._scope_service.scope(group_name)
        resolved_signature = self._scope_signature(scope.shortcut_paths)
        if (
            observed_signature != resolved_signature
            or scope.controller_entry_id != inputs.controller_entry_id
            or scope.entry_ids != inputs.entry_ids
            or scope.shortcut_paths != inputs.shortcut_paths
        ):
            return (
                resolved_signature,
                replace(
                    scope,
                    failure_codes=tuple(
                        dict.fromkeys(
                            (
                                *scope.failure_codes,
                                "scope_evidence_changed_during_resolution",
                            )
                        )
                    ),
                ),
            )
        self._scope_cache[group_name] = (resolved_signature, scope)
        return self._scope_cache[group_name]

    def snapshot(
        self,
        group_name: object,
        *,
        expanded_sync_scope: bool = True,
        _resolved_scope: tuple[tuple[object, ...], SyncScope] | None = None,
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
        scope_signature, scope = (
            _resolved_scope
            if _resolved_scope is not None
            else self._resolved_scope(name)
        )
        if scope.ready:
            fingerprint_by_id = dict(
                zip(scope.entry_ids, scope.entry_fingerprints)
            )
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
        if self._scope_signature(scope.shortcut_paths) != scope_signature:
            return TargetWindowSnapshot(
                TargetWindowSnapshot.SCHEMA_VERSION,
                name,
                failure_codes=tuple(
                    dict.fromkeys(
                        (
                            *snapshot_failures,
                            "scope_evidence_changed_during_snapshot",
                        )
                    )
                ),
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
        normalized_group_name = (
            group_name.strip()
            if isinstance(group_name, str) and group_name.strip()
            else ""
        )
        resolved_scope = (
            self._resolved_scope(normalized_group_name)
            if normalized_group_name
            else (
                (),
                SyncScope(
                    "",
                    failure_codes=("group_name_invalid",),
                ),
            )
        )
        snapshot = self.snapshot(
            group_name,
            expanded_sync_scope=expanded_sync_scope,
            _resolved_scope=(resolved_scope if normalized_group_name else None),
        )
        failures = list(snapshot.failure_codes)
        for target in snapshot.targets:
            failures.extend(target.failure_codes)
        scope = resolved_scope[1]
        safe_pairs: list[tuple[str, WindowInfo, str]] = []
        scoped_resolution = (
            scope.ready
            and len(snapshot.targets) == len(scope.entry_ids)
        )
        if scoped_resolution:
            for entry_id, target in zip(scope.entry_ids, snapshot.targets):
                window = self._safe_window_for_target(target)
                if window is None:
                    continue
                monitor_fingerprint = monitored_window_instance_fingerprint(
                    window
                )
                if monitor_fingerprint is None:
                    continue
                safe_pairs.append((entry_id, window, monitor_fingerprint))

        if safe_pairs:
            handles = Counter(window.handle for _entry, window, _id in safe_pairs)
            stable_instances = Counter(
                complete_window_instance_identity(window)[:6]
                for _entry, window, _id in safe_pairs
                if complete_window_instance_identity(window) is not None
            )
            monitor_ids = Counter(
                monitor_fingerprint
                for _entry, _window, monitor_fingerprint in safe_pairs
            )
            conflicts = {
                entry_id
                for entry_id, window, monitor_fingerprint in safe_pairs
                if (
                    handles[window.handle] != 1
                    or complete_window_instance_identity(window) is None
                    or stable_instances[
                        complete_window_instance_identity(window)[:6]
                    ] != 1
                    or monitor_ids[monitor_fingerprint] != 1
                )
            }
            if conflicts:
                failures.append("window_identity_duplicate")
            safe_pairs = [
                item for item in safe_pairs if item[0] not in conflicts
            ]

        if scoped_resolution:
            windows = tuple(window for _entry, window, _id in safe_pairs)
            controller_is_safe = any(
                entry_id == scope.controller_entry_id
                for entry_id, _window, _identity in safe_pairs
            )
            if controller_is_safe:
                sync_windows = tuple(
                    replace(window, launch_fingerprint=monitor_fingerprint)
                    for _entry, window, monitor_fingerprint in safe_pairs
                )
                sync_entry_ids = tuple(
                    entry_id for entry_id, _window, _id in safe_pairs
                )
            else:
                sync_windows = ()
                sync_entry_ids = ()
        else:
            windows = self._safe_windows(snapshot)
            sync_windows = ()
            sync_entry_ids = ()

        safe_source_fingerprints = {
            fingerprint
            for window in windows
            if (
                fingerprint := normalize_launch_fingerprint(
                    window.launch_fingerprint
                )
            ) is not None
        }
        blocked_fingerprints = frozenset(
            target.fingerprint
            for target in snapshot.targets
            if target.fingerprint is not None
            and target.failure_codes
            and target.failure_codes != ("window_offline",)
            and target.fingerprint not in safe_source_fingerprints
        )
        return ResolvedTargetWindows(
            windows=windows,
            failure_codes=tuple(dict.fromkeys(failures)),
            blocked_fingerprints=blocked_fingerprints,
            sync_windows=sync_windows,
            sync_entry_ids=sync_entry_ids,
            sync_scope_entry_ids=(
                tuple(scope.entry_ids) if scope.ready else ()
            ),
            sync_controller_entry_id=(
                scope.controller_entry_id if scope.ready else None
            ),
        )

    @staticmethod
    def _has_complete_window_instance(window: WindowInfo) -> bool:
        return complete_window_instance_identity(window) is not None

    @staticmethod
    def _safe_window_for_target(target: TargetWindowContract) -> WindowInfo | None:
        if (
            not target.safe
            or target.handle is None
            or target.rect is None
            or target.fingerprint is None
        ):
            return None
        window = WindowInfo(
            handle=target.handle,
            title="",
            visible=target.visible,
            minimized=target.phase is TargetWindowPhase.MINIMIZED,
            rect=target.rect,
            process_id=target.process_id,
            launch_fingerprint=target.fingerprint,
            thread_id=target.thread_id,
            window_class=target.window_class,
            process_lifecycle_token=target.process_lifecycle_token,
        )
        return (
            window
            if TargetWindowContractService._has_complete_window_instance(window)
            else None
        )

    @staticmethod
    def _safe_windows(
        snapshot: TargetWindowSnapshot,
    ) -> tuple[WindowInfo, ...]:
        return tuple(
            window
            for target in snapshot.safe_targets
            if (
                window := TargetWindowContractService._safe_window_for_target(
                    target
                )
            ) is not None
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
            confirmed_matches = tuple(
                candidate
                for candidate in matches
                if (
                    record is not None
                    and record.confirmed is True
                    and record.group == group_name
                    and isinstance(record.handle, int)
                    and not isinstance(record.handle, bool)
                    and record.handle > 0
                    and isinstance(record.process_id, int)
                    and not isinstance(record.process_id, bool)
                    and record.process_id > 0
                    and isinstance(record.window_class, str)
                    and bool(record.window_class.strip())
                    and candidate.handle == record.handle
                    and candidate.process_id == record.process_id
                    and candidate.window_class == record.window_class
                )
            )
            if len(confirmed_matches) != 1:
                failures = ("window_identity_duplicate",)
                phase = TargetWindowPhase.UNKNOWN
            else:
                window = confirmed_matches[0]
                if not self._has_complete_window_instance(window):
                    failures = ("window_instance_incomplete",)
                    phase = TargetWindowPhase.UNKNOWN
                elif window.minimized:
                    phase = TargetWindowPhase.MINIMIZED
                elif window.handle == foreground_handle:
                    phase = TargetWindowPhase.FOREGROUND
                else:
                    phase = TargetWindowPhase.BACKGROUND
        else:
            window = matches[0]
            if not self._has_complete_window_instance(window):
                failures = ("window_instance_incomplete",)
                phase = TargetWindowPhase.UNKNOWN
            elif window.minimized:
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
