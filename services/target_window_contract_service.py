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
class TargetFailureEvidence:
    """One immutable, fully attributable target-local failure."""

    entry_id: str
    fingerprint: str
    failure_codes: tuple[str, ...]
    candidate_windows: tuple[WindowInfo, ...] = ()

    def __post_init__(self) -> None:
        entry_id = self.entry_id.strip() if isinstance(self.entry_id, str) else ""
        fingerprint = normalize_launch_fingerprint(self.fingerprint)
        failure_codes = tuple(
            dict.fromkeys(
                code.strip()
                for code in self.failure_codes
                if isinstance(code, str) and code.strip()
            )
        )
        try:
            candidates = tuple(self.candidate_windows)
        except TypeError as error:
            raise ValueError(
                "Target failure candidates must be an immutable collection."
            ) from error
        identities = []
        for candidate in candidates:
            identity = complete_window_instance_identity(candidate)
            if (
                identity is None
                or normalize_launch_fingerprint(candidate.launch_fingerprint)
                != fingerprint
            ):
                raise ValueError(
                    "Target failure candidates must have complete matching identities."
                )
            identities.append(identity)
        handles = [identity[1] for identity in identities]
        process_ids = [identity[2] for identity in identities]
        stable_tokens = [identity[:6] for identity in identities]
        if (
            not entry_id
            or fingerprint is None
            or not failure_codes
            or len(handles) != len(set(handles))
            or len(process_ids) != len(set(process_ids))
            or len(stable_tokens) != len(set(stable_tokens))
        ):
            raise ValueError("Target failure evidence must be fully attributable.")
        object.__setattr__(self, "entry_id", entry_id)
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(self, "failure_codes", failure_codes)
        object.__setattr__(self, "candidate_windows", candidates)


@dataclass(frozen=True, slots=True)
class ResolvedTargetWindows:
    """Safe open windows with immutable local and global failure evidence."""

    windows: tuple[WindowInfo, ...]
    sync_windows: tuple[WindowInfo, ...] = ()
    sync_entry_ids: tuple[str, ...] = ()
    sync_scope_entry_ids: tuple[str, ...] = ()
    sync_controller_entry_id: str | None = None
    target_failure_evidence: tuple[TargetFailureEvidence, ...] = ()
    global_failure_codes: tuple[str, ...] = ()


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
        self._configured_scope_cache: tuple[
            tuple[object, ...], SyncScope
        ] | None = None

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

    def _resolved_configured_scope(
        self,
    ) -> tuple[tuple[object, ...], SyncScope]:
        inputs = self._scope_service.configured_inputs()
        observed_signature = self._scope_signature(inputs.shortcut_paths)
        cached = self._configured_scope_cache
        if (
            cached is not None
            and cached[0] == observed_signature
            and cached[1].entry_ids == inputs.entry_ids
            and cached[1].shortcut_paths == inputs.shortcut_paths
        ):
            return cached
        scope = self._scope_service.configured_scope()
        resolved_signature = self._scope_signature(scope.shortcut_paths)
        if (
            observed_signature != resolved_signature
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
        self._configured_scope_cache = (resolved_signature, scope)
        return self._configured_scope_cache

    def configured_scope(self) -> SyncScope:
        """Expose the cached configured identity authority without windows."""

        return self._resolved_configured_scope()[1]

    def snapshot(
        self,
        group_name: object,
        *,
        expanded_sync_scope: bool = True,
        _resolved_scope: tuple[tuple[object, ...], SyncScope] | None = None,
        _window_observation: tuple[
            tuple[WindowInfo, ...],
            int | None,
            bool,
        ] | None = None,
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

        if _window_observation is None:
            try:
                windows = tuple(self._window_backend.list_windows())
                foreground_handle = self._window_backend.foreground_handle()
                observation_failed = False
            except Exception:
                windows = ()
                foreground_handle = None
                observation_failed = True
        else:
            windows, foreground_handle, observation_failed = _window_observation
        if observation_failed:
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
        _configured_scope: bool = False,
    ) -> ResolvedTargetWindows:
        """Keep uniquely resolved open roles while isolating unsafe siblings."""
        groups = self._configuration.groups()
        normalized_group_name = (
            groups[0].name
            if _configured_scope and groups
            else (
                group_name.strip()
                if isinstance(group_name, str) and group_name.strip()
                else ""
            )
        )
        resolved_scope = (
            self._resolved_configured_scope()
            if _configured_scope
            else (
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
        )
        try:
            window_observation = (
                tuple(self._window_backend.list_windows()),
                self._window_backend.foreground_handle(),
                False,
            )
        except Exception:
            window_observation = ((), None, True)
        snapshot = self.snapshot(
            normalized_group_name,
            expanded_sync_scope=expanded_sync_scope,
            _resolved_scope=(resolved_scope if normalized_group_name else None),
            _window_observation=window_observation,
        )
        scope = resolved_scope[1]
        windows_by_fingerprint: dict[str, list[WindowInfo]] = {}
        for window in window_observation[0]:
            if not all(
                keyword in window.title.casefold()
                for keyword in self._title_keywords
            ):
                continue
            fingerprint = normalize_launch_fingerprint(window.launch_fingerprint)
            if fingerprint is not None:
                windows_by_fingerprint.setdefault(fingerprint, []).append(window)
        global_failures = list(snapshot.failure_codes)
        target_failures: list[TargetFailureEvidence] = []
        safe_pairs: list[tuple[str, WindowInfo, str]] = []
        scoped_resolution = (
            scope.ready
            and len(snapshot.targets) == len(scope.entry_ids)
        )
        if scoped_resolution:
            for entry_id, target in zip(scope.entry_ids, snapshot.targets):
                failure_codes = tuple(target.failure_codes)
                fingerprint = normalize_launch_fingerprint(target.fingerprint)
                if failure_codes:
                    if fingerprint is None:
                        # Without a complete normalized fingerprint, this
                        # cannot be attributed to one target safely.
                        global_failures.extend(failure_codes)
                    else:
                        try:
                            target_failures.append(
                                TargetFailureEvidence(
                                    entry_id,
                                    fingerprint,
                                    failure_codes,
                                    tuple(
                                        windows_by_fingerprint.get(
                                            fingerprint,
                                            (),
                                        )
                                    ),
                                )
                            )
                        except ValueError:
                            global_failures.extend(failure_codes)
                window = self._safe_window_for_target(target)
                if window is None:
                    continue
                monitor_fingerprint = monitored_window_instance_fingerprint(
                    window
                )
                if monitor_fingerprint is None:
                    continue
                safe_pairs.append((entry_id, window, monitor_fingerprint))
        else:
            # A target failure is only local when the same scope proves its
            # entry binding.  A partial or changed scope is a global denial.
            for target in snapshot.targets:
                global_failures.extend(target.failure_codes)

        if scoped_resolution:
            # A shared launcher is safe only when every raw candidate can be
            # attributed to exactly one entry.  A confirmed registry mapping
            # may choose one sibling, but it may not silently discard another.
            safe_candidate_identities = {
                identity
                for _entry_id, window, _monitor_fingerprint in safe_pairs
                if (
                    identity := complete_window_instance_identity(window)
                ) is not None
            }
            filtered_failures: list[TargetFailureEvidence] = []
            for evidence in target_failures:
                candidates = tuple(
                    candidate
                    for candidate in evidence.candidate_windows
                    if complete_window_instance_identity(candidate)
                    not in safe_candidate_identities
                )
                if evidence.candidate_windows and not candidates:
                    global_failures.extend(evidence.failure_codes)
                    global_failures.append("target_failure_unattributed")
                    continue
                if candidates == evidence.candidate_windows:
                    filtered_failures.append(evidence)
                    continue
                try:
                    filtered_failures.append(
                        TargetFailureEvidence(
                            evidence.entry_id,
                            evidence.fingerprint,
                            evidence.failure_codes,
                            candidates,
                        )
                    )
                except ValueError:
                    global_failures.extend(evidence.failure_codes)
                    global_failures.append("target_failure_unattributed")
            target_failures = filtered_failures
            attributed_candidates: dict[
                tuple[object, ...],
                set[str],
            ] = {}
            for entry_id, window, _monitor_fingerprint in safe_pairs:
                identity = complete_window_instance_identity(window)
                if identity is not None:
                    attributed_candidates.setdefault(identity, set()).add(entry_id)
            for evidence in target_failures:
                for window in evidence.candidate_windows:
                    identity = complete_window_instance_identity(window)
                    if identity is not None:
                        attributed_candidates.setdefault(identity, set()).add(
                            evidence.entry_id
                        )
            scoped_fingerprints = {
                fingerprint
                for target in snapshot.targets
                if (
                    fingerprint := normalize_launch_fingerprint(
                        target.fingerprint
                    )
                ) is not None
            }
            configured_scope = (
                scope
                if _configured_scope
                else self._resolved_configured_scope()[1]
            )
            if not configured_scope.ready:
                global_failures.extend(configured_scope.failure_codes)
            configured_owners: dict[str, set[str]] = {}
            if configured_scope.ready:
                for entry_id, fingerprint in zip(
                    configured_scope.entry_ids,
                    configured_scope.entry_fingerprints,
                ):
                    if fingerprint is not None:
                        configured_owners.setdefault(fingerprint, set()).add(
                            entry_id
                        )
            for fingerprint, candidates in windows_by_fingerprint.items():
                if fingerprint not in scoped_fingerprints:
                    owners = configured_owners.get(fingerprint, set())
                    identities = tuple(
                        complete_window_instance_identity(candidate)
                        for candidate in candidates
                    )
                    if (
                        len(owners) != 1
                        or len(candidates) != 1
                        or identities[0] is None
                    ):
                        global_failures.append(
                            "unattributed_candidate_window"
                        )
                    continue
                for candidate in candidates:
                    identity = complete_window_instance_identity(candidate)
                    owners = (
                        attributed_candidates.get(identity, set())
                        if identity is not None
                        else set()
                    )
                    if len(owners) != 1:
                        global_failures.append("unattributed_candidate_window")

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
                # A collision crosses entry boundaries, so no controller may
                # infer which target owns either identity.
                global_failures.append("window_identity_duplicate")
            safe_pairs = [
                item for item in safe_pairs if item[0] not in conflicts
            ]

        if scoped_resolution:
            windows = tuple(window for _entry, window, _id in safe_pairs)
            # Preserve every independently safe entry/window pair for
            # reconnect.  Basic input sync still rejects this partial list in
            # resolve_complete_sync_instance_windows when its controller entry
            # is absent, but one unsafe controller must not erase a safe
            # follower's TCP identity proof.
            sync_windows = tuple(
                replace(window, launch_fingerprint=monitor_fingerprint)
                for _entry, window, monitor_fingerprint in safe_pairs
            )
            sync_entry_ids = tuple(
                entry_id for entry_id, _window, _id in safe_pairs
            )
        else:
            windows = self._safe_windows(snapshot)
            sync_windows = ()
            sync_entry_ids = ()

        return ResolvedTargetWindows(
            windows=windows,
            sync_windows=sync_windows,
            sync_entry_ids=sync_entry_ids,
            sync_scope_entry_ids=(
                tuple(scope.entry_ids) if scope.ready else ()
            ),
            sync_controller_entry_id=(
                scope.controller_entry_id if scope.ready else None
            ),
            target_failure_evidence=tuple(target_failures),
            global_failure_codes=tuple(dict.fromkeys(global_failures)),
        )

    def configured_reconnect_targets(self) -> ResolvedTargetWindows:
        """Resolve every configured group entry as one reconnect authority."""

        return self.reconnect_targets("", _configured_scope=True)

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
