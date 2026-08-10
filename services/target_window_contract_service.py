"""Resolve one authoritative target set for detection, sync and reconnect."""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from adapters.windows_launch_fingerprint import normalize_launch_fingerprint
from adapters.windows_smart_reconnect_observation_broker import (
    WindowsSmartReconnectObservationBroker,
)
from adapters.windows_window import WindowBackend, WindowInfo
from core.target_window_contract import (
    ActualWindowContract,
    ActualWindowSnapshot,
    TargetWindowContract,
    TargetWindowPhase,
    TargetWindowSnapshot,
)
from core.window_instance import WindowInstanceToken
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
    isolated_window_count: int = 0
    anonymous_isolated_window_count: int = 0
    actual_window_snapshot: bool = False
    observation_generation: int = 0


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
        observation_broker: (
            WindowsSmartReconnectObservationBroker | None
        ) = None,
    ) -> None:
        self._configuration = configuration
        self._scope_service = scope_service
        self._registry = registry
        self._window_backend = window_backend
        self._observation_broker = observation_broker
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
        broker = self._observation_broker
        observation = broker.current_snapshot() if broker is not None else None
        if broker is not None and observation is None:
            return TargetWindowSnapshot(
                TargetWindowSnapshot.SCHEMA_VERSION,
                name,
                failure_codes=("observation_unavailable",),
            )
        if observation is not None:
            (
                selected_ids,
                fingerprint_by_id,
                snapshot_failures,
            ) = self._scope_from_observation(
                group,
                configured_groups,
                observation,
                expanded_sync_scope=expanded_sync_scope,
            )
        else:
            scope = self._scope(name)
            if scope.ready:
                fingerprint_by_id = dict(
                    zip(scope.entry_ids, scope.fingerprints)
                )
                selected_ids = (
                    scope.entry_ids
                    if expanded_sync_scope
                    else tuple(entry.entry_id for entry in group.entries)
                )
                snapshot_failures = ()
            else:
                fingerprint_by_id = {}
                selected_ids = tuple(
                    entry.entry_id for entry in group.entries
                )
                snapshot_failures = scope.failure_codes

        if observation is not None:
            windows = tuple(item.window for item in observation.windows)
            foreground_handle = observation.foreground_handle
        else:
            try:
                windows = tuple(self._window_backend.list_windows())
                foreground_handle = self._window_backend.foreground_handle()
            except Exception:
                windows = ()
                foreground_handle = None
                snapshot_failures = tuple(
                    dict.fromkeys(
                        (*snapshot_failures, "window_enumeration_failed")
                    )
                )

        if observation is not None and observation.failure_codes:
            snapshot_failures = tuple(
                dict.fromkeys(
                    (*snapshot_failures, *observation.failure_codes)
                )
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
        if (
            broker is not None
            and broker.current_snapshot() is not observation
        ):
            return TargetWindowSnapshot(
                TargetWindowSnapshot.SCHEMA_VERSION,
                name,
                failure_codes=("observation_superseded",),
            )
        return TargetWindowSnapshot(
            TargetWindowSnapshot.SCHEMA_VERSION,
            name,
            targets,
            snapshot_failures,
        )

    @staticmethod
    def _path_key(path: object) -> str:
        return os.path.normcase(os.path.abspath(os.fspath(path)))

    def _scope_from_observation(
        self,
        group,
        configured_groups,
        observation,
        *,
        expanded_sync_scope: bool,
    ) -> tuple[tuple[str, ...], dict[str, str], tuple[str, ...]]:
        root_ids = tuple(entry.entry_id for entry in group.entries)
        failures: list[str] = []
        if observation.generation <= 0:
            failures.append("observation_unavailable")
        failures.extend(observation.failure_codes)

        entry_by_id: dict[str, GroupConfigurationEntry] = {}
        conflicting_ids: set[str] = set()
        for configured_group in configured_groups:
            for entry in configured_group.entries:
                current = entry_by_id.get(entry.entry_id)
                if (
                    current is not None
                    and self._path_key(current.shortcut_path)
                    != self._path_key(entry.shortcut_path)
                ):
                    conflicting_ids.add(entry.entry_id)
                else:
                    entry_by_id[entry.entry_id] = entry

        if expanded_sync_scope:
            controller = group.main_entry
            if controller is None:
                failures.append("group_controller_unavailable")
                selected_ids = root_ids
            else:
                try:
                    members = tuple(
                        self._configuration.expanded_sync_members(
                            controller.entry_id
                        )
                    )
                except Exception:
                    members = ()
                    failures.append("sync_identity_unresolved")
                selected_ids = (controller.entry_id, *members)
        else:
            selected_ids = root_ids

        if any(entry_id in conflicting_ids for entry_id in selected_ids):
            failures.append("sync_identity_path_conflict")
        if any(entry_id not in entry_by_id for entry_id in selected_ids):
            failures.append("sync_identity_unresolved")

        fingerprint_by_path: dict[str, str] = {}
        duplicate_paths: set[str] = set()
        for item in observation.shortcuts:
            fingerprint = normalize_launch_fingerprint(item.fingerprint)
            if fingerprint is None or item.seal is None or item.failure_codes:
                continue
            key = self._path_key(item.path)
            previous = fingerprint_by_path.get(key)
            if previous is not None and previous != fingerprint:
                duplicate_paths.add(key)
            else:
                fingerprint_by_path[key] = fingerprint

        resolved: list[str] = []
        for entry_id in selected_ids:
            entry = entry_by_id.get(entry_id)
            if entry is None:
                continue
            key = self._path_key(entry.shortcut_path)
            fingerprint = (
                None
                if key in duplicate_paths
                else fingerprint_by_path.get(key)
            )
            if fingerprint is None:
                failures.append("shortcut_identity_unresolved")
            else:
                resolved.append(fingerprint)
        if len(resolved) != len(set(resolved)):
            failures.append("shortcut_identity_duplicate")

        unique_failures = tuple(dict.fromkeys(failures))
        if unique_failures or len(resolved) != len(selected_ids):
            return selected_ids, {}, unique_failures
        return (
            selected_ids,
            dict(zip(selected_ids, resolved)),
            (),
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

    def actual_snapshot(self) -> ActualWindowSnapshot:
        """Enumerate every existing game window without consulting a group."""

        broker = self._observation_broker
        if broker is not None:
            observation = broker.refresh(self._configured_shortcut_paths())
            if (
                observation.generation <= 0
                or not broker.is_generation_current(observation.generation)
            ):
                return ActualWindowSnapshot(
                    ActualWindowSnapshot.SCHEMA_VERSION,
                    failure_codes=tuple(
                        dict.fromkeys(
                            (*observation.failure_codes, "observation_unavailable")
                        )
                    ),
                )
            targets = tuple(
                ActualWindowContract(
                    fingerprint=fingerprint,
                    instance=item.instance,
                    visible=bool(item.window.visible),
                )
                for item in observation.windows
                if item.instance is not None
                and (
                    fingerprint := normalize_launch_fingerprint(
                        item.window.launch_fingerprint
                    )
                ) is not None
                and fingerprint not in observation.blocked_fingerprints
            )
            return ActualWindowSnapshot(
                ActualWindowSnapshot.SCHEMA_VERSION,
                targets=targets,
                blocked_fingerprints=observation.blocked_fingerprints,
                isolated_window_count=observation.isolated_window_count,
                anonymous_isolated_window_count=(
                    observation.anonymous_isolated_window_count
                ),
                failure_codes=observation.failure_codes,
                observation_generation=observation.generation,
            )

        try:
            candidates = tuple(
                window
                for window in self._window_backend.list_windows()
                if all(
                    keyword in window.title.casefold()
                    for keyword in self._title_keywords
                )
            )
        except Exception:
            return ActualWindowSnapshot(
                ActualWindowSnapshot.SCHEMA_VERSION,
                failure_codes=("window_enumeration_failed",),
            )

        parsed: list[tuple[WindowInfo, str, WindowInstanceToken]] = []
        blocked: set[str] = set()
        anonymous_isolated = 0
        isolated_count = 0
        for window in candidates:
            fingerprint = normalize_launch_fingerprint(
                window.launch_fingerprint
            )
            if fingerprint is None:
                anonymous_isolated += 1
                isolated_count += 1
                continue
            instance = WindowInstanceToken.from_window(window)
            if instance is None:
                blocked.add(fingerprint)
                isolated_count += 1
                continue
            parsed.append((window, fingerprint, instance))

        handle_counts = Counter(
            window.handle
            for window in candidates
            if isinstance(window.handle, int)
            and not isinstance(window.handle, bool)
            and window.handle > 0
        )
        process_counts = Counter(
            window.process_id
            for window in candidates
            if isinstance(window.process_id, int)
            and not isinstance(window.process_id, bool)
            and window.process_id > 0
        )
        fingerprint_counts = Counter(
            fingerprint for _, fingerprint, _ in parsed
        )
        targets: list[ActualWindowContract] = []
        for window, fingerprint, instance in parsed:
            if (
                fingerprint in blocked
                or handle_counts[instance.handle] != 1
                or process_counts[instance.process_id] != 1
                or fingerprint_counts[fingerprint] != 1
            ):
                blocked.add(fingerprint)
                isolated_count += 1
                continue
            targets.append(
                ActualWindowContract(
                    fingerprint=fingerprint,
                    instance=instance,
                    visible=bool(window.visible),
                )
            )
        return ActualWindowSnapshot(
            ActualWindowSnapshot.SCHEMA_VERSION,
            targets=tuple(targets),
            blocked_fingerprints=frozenset(blocked),
            isolated_window_count=isolated_count,
            anonymous_isolated_window_count=anonymous_isolated,
        )

    def _configured_shortcut_paths(self) -> tuple[Path, ...]:
        """Capture only immutable path values before broker I/O begins."""

        try:
            groups = tuple(self._configuration.groups())
        except Exception:
            return ()
        return tuple(
            dict.fromkeys(
                Path(entry.shortcut_path)
                for group in groups
                for entry in group.entries
            )
        )

    def actual_reconnect_targets(self) -> ResolvedTargetWindows:
        """Return only safe actual windows plus per-window isolation evidence."""

        snapshot = self.actual_snapshot()
        windows = tuple(
            WindowInfo(
                handle=target.instance.handle,
                title="",
                visible=target.visible,
                minimized=target.instance.minimized,
                rect=target.instance.rect,
                process_id=target.instance.process_id,
                launch_fingerprint=target.fingerprint,
                thread_id=target.instance.thread_id,
                window_class=target.instance.window_class,
                process_lifecycle_token=(
                    target.instance.process_lifecycle_token
                ),
            )
            for target in snapshot.targets
        )
        return ResolvedTargetWindows(
            windows=windows,
            failure_codes=snapshot.failure_codes,
            blocked_fingerprints=snapshot.blocked_fingerprints,
            isolated_window_count=snapshot.isolated_window_count,
            anonymous_isolated_window_count=(
                snapshot.anonymous_isolated_window_count
            ),
            actual_window_snapshot=True,
            observation_generation=snapshot.observation_generation,
        )

    @staticmethod
    def _has_complete_window_instance(window: WindowInfo) -> bool:
        return (
            isinstance(window.handle, int)
            and not isinstance(window.handle, bool)
            and window.handle > 0
            and isinstance(window.process_id, int)
            and not isinstance(window.process_id, bool)
            and window.process_id > 0
            and isinstance(window.thread_id, int)
            and not isinstance(window.thread_id, bool)
            and window.thread_id > 0
            and isinstance(window.window_class, str)
            and bool(window.window_class.strip())
            and isinstance(window.process_lifecycle_token, int)
            and not isinstance(window.process_lifecycle_token, bool)
            and window.process_lifecycle_token > 0
            and isinstance(window.rect, tuple)
            and len(window.rect) == 4
            and all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in window.rect
            )
            and type(window.minimized) is bool
        )

    @staticmethod
    def _safe_windows(
        snapshot: TargetWindowSnapshot,
    ) -> tuple[WindowInfo, ...]:
        windows = tuple(
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
        return tuple(
            window
            for window in windows
            if TargetWindowContractService._has_complete_window_instance(
                window
            )
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
