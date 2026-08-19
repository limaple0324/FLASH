"""Corrective smart-reconnect layer for entry/instance scoped recovery."""

from __future__ import annotations

import hashlib

from . import windows_smart_reconnect_step_scoped_base as _step

for _name in dir(_step):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_step, _name)

_LOCAL_FAILURE_NAMESPACE = b"fu-smart-reconnect-local-failure-v1\0"
_NEVER_DEMOTE_GLOBAL_FAILURE_CODES = frozenset(
    {
        "window_offline",
        "unattributed_candidate_window",
        "target_failure_unattributed",
        "target_window_provider_failed",
        "window_enumeration_failed",
        "configured_identity_path_conflict",
        "scope_evidence_changed_during_snapshot",
        "snapshot_identity_collision",
    }
)


class WindowsSmartReconnectController(_step.WindowsSmartReconnectController):
    """Keep global failures global and isolate only one proven entry/instance."""

    @staticmethod
    def _formal_plan(controller):
        return (
            None
            if getattr(controller, "_manual_runtime_plan_installed", False)
            else controller._group_launch_plan
        )

    @classmethod
    def _local_failure_key(
        cls,
        plan,
        entry_id: str,
        fingerprint: str,
    ) -> str:
        if plan is None:
            return fingerprint
        same_source = tuple(
            target.entry_id
            for target in plan.targets
            if target.fingerprint == fingerprint
        )
        if len(same_source) <= 1:
            return fingerprint
        payload = (
            _LOCAL_FAILURE_NAMESPACE
            + entry_id.encode("utf-8", errors="strict")
            + b"\0"
            + fingerprint.encode("ascii", errors="strict")
        )
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _resolved_global_identity_collision(resolved) -> bool:
        """Re-prove cross-window collisions instead of trusting a mirrored code."""
        identities = []
        for window in (
            *tuple(resolved.windows),
            *tuple(resolved.detection_only_windows),
        ):
            identity = complete_window_instance_identity(window)
            if identity is None:
                return True
            identities.append(identity)
        if not identities:
            return False
        handles = [identity[1] for identity in identities]
        process_ids = [identity[2] for identity in identities]
        stable_tokens = [identity[:6] for identity in identities]
        return bool(
            len(handles) != len(set(handles))
            or len(process_ids) != len(set(process_ids))
            or len(stable_tokens) != len(set(stable_tokens))
        )

    def _contract_failure_evidence(self, resolved):
        if not self._step_scoped_live_reconnect:
            return super()._contract_failure_evidence(resolved)

        global_failures = list(resolved.global_failure_codes)
        local_failures: dict[str, tuple[str, ...]] = {}
        evidence_identities: dict[tuple[object, ...], str] = {}
        evidence_handles: dict[int, tuple[object, ...]] = {}
        evidence_processes: dict[int, tuple[object, ...]] = {}
        evidence_stable_tokens: dict[
            tuple[object, ...],
            tuple[object, ...],
        ] = {}
        plan = self._formal_plan(self)
        demotable_mirrors: set[str] = set()

        for evidence in resolved.target_failure_evidence:
            fingerprint = normalize_launch_fingerprint(evidence.fingerprint)
            entry_id = evidence.entry_id.strip()
            failure_codes = tuple(evidence.failure_codes)
            if fingerprint is None or not entry_id or not failure_codes:
                global_failures.extend(failure_codes)
                global_failures.append("target_failure_unattributed")
                continue

            malformed_candidates = False
            for candidate in evidence.candidate_windows:
                identity = complete_window_instance_identity(candidate)
                if identity is None or identity[0] != fingerprint:
                    malformed_candidates = True
                    break
                previous_entry = evidence_identities.setdefault(identity, entry_id)
                previous_handle = evidence_handles.setdefault(identity[1], identity)
                previous_process = evidence_processes.setdefault(identity[2], identity)
                previous_stable = evidence_stable_tokens.setdefault(
                    identity[:6], identity
                )
                if (
                    previous_entry != entry_id
                    or previous_handle != identity
                    or previous_process != identity
                    or previous_stable != identity
                ):
                    malformed_candidates = True
                    break
            if malformed_candidates:
                global_failures.extend(failure_codes)
                global_failures.append("target_failure_unattributed")
                continue

            if plan is not None:
                targets = tuple(
                    target
                    for target in plan.targets
                    if (
                        target.entry_id == entry_id
                        and target.fingerprint == fingerprint
                    )
                )
                if len(targets) != 1:
                    global_failures.extend(failure_codes)
                    global_failures.append("target_failure_unattributed")
                    continue

            key = self._local_failure_key(plan, entry_id, fingerprint)
            if key in local_failures:
                global_failures.extend(failure_codes)
                global_failures.append("target_failure_unattributed")
                continue
            local_failures[key] = failure_codes
            demotable_mirrors.update(
                code
                for code in failure_codes
                if code not in _NEVER_DEMOTE_GLOBAL_FAILURE_CODES
            )

        # TargetWindowContractService historically mirrors attributable target
        # failures into its aggregate global code list. Remove only mirrors
        # that can be independently re-proven as non-global. Never remove an
        # unattributed error or a real cross-window collision.
        if (
            "window_identity_duplicate" in demotable_mirrors
            and self._resolved_global_identity_collision(resolved)
        ):
            demotable_mirrors.discard("window_identity_duplicate")
        if "target_failure_unattributed" not in global_failures:
            global_failures = [
                code
                for code in global_failures
                if code not in demotable_mirrors
            ]
        return tuple(dict.fromkeys(global_failures)), local_failures

    def _verified_group_activation_snapshot(
        self,
        resolved,
        complete_instances,
        source_fingerprints,
    ):
        if not self._step_scoped_live_reconnect:
            return super()._verified_group_activation_snapshot(
                resolved,
                complete_instances,
                source_fingerprints,
            )

        plan = self._group_launch_plan
        if (
            not isinstance(resolved, ResolvedTargetWindows)
            or plan is None
            or not plan.targets
        ):
            return None

        global_failures, local_failures = self._contract_failure_evidence(resolved)
        if global_failures:
            return None

        targets = tuple(plan.targets)
        targets_by_entry = {}
        for target in targets:
            if not target.entry_id or target.entry_id in targets_by_entry:
                return None
            targets_by_entry[target.entry_id] = target

        evidence_by_entry = {}
        for evidence in resolved.target_failure_evidence:
            target = targets_by_entry.get(evidence.entry_id)
            fingerprint = normalize_launch_fingerprint(evidence.fingerprint)
            if target is None or evidence.entry_id in evidence_by_entry:
                return None
            if fingerprint is None or fingerprint != target.fingerprint:
                return None
            key = self._local_failure_key(
                plan,
                evidence.entry_id,
                fingerprint,
            )
            if local_failures.get(key) != tuple(evidence.failure_codes):
                return None
            evidence_by_entry[evidence.entry_id] = evidence

        plan_entry_ids = tuple(target.entry_id for target in targets)
        required_targets = tuple(
            target
            for target in targets
            if target.entry_id not in evidence_by_entry
        )
        required_entry_ids = tuple(target.entry_id for target in required_targets)
        if (
            tuple(resolved.sync_scope_entry_ids) != plan_entry_ids
            or tuple(resolved.sync_entry_ids) != required_entry_ids
            or len(resolved.windows) != len(required_targets)
            or len(complete_instances)
            != len(required_targets) + len(resolved.detection_only_windows)
            or len(source_fingerprints) != len(complete_instances)
        ):
            return None

        safe_by_entry = dict(zip(resolved.sync_entry_ids, resolved.windows))
        verified_instances = {}
        verified_sources: dict[str, str] = {}
        detection_only: set[str] = set()

        for target in required_targets:
            safe_window = safe_by_entry.get(target.entry_id)
            safe_instance = (
                WindowInstanceToken.from_window(safe_window)
                if safe_window is not None
                else None
            )
            if (
                safe_instance is None
                or normalize_launch_fingerprint(safe_window.launch_fingerprint)
                != target.fingerprint
            ):
                return None
            matches = tuple(
                (monitor_fingerprint, candidate)
                for monitor_fingerprint, candidate in complete_instances.items()
                if (
                    source_fingerprints.get(monitor_fingerprint)
                    == target.fingerprint
                    and candidate[1] == safe_instance
                )
            )
            if len(matches) != 1 or matches[0][0] in verified_instances:
                return None
            monitor_fingerprint, candidate = matches[0]
            verified_instances[monitor_fingerprint] = candidate
            verified_sources[monitor_fingerprint] = target.fingerprint
            if not target.role_id:
                detection_only.add(monitor_fingerprint)

        for window in resolved.detection_only_windows:
            source_fingerprint = normalize_launch_fingerprint(
                window.launch_fingerprint
            )
            instance = WindowInstanceToken.from_window(window)
            if source_fingerprint is None or instance is None:
                return None
            matches = tuple(
                (monitor_fingerprint, candidate)
                for monitor_fingerprint, candidate in complete_instances.items()
                if (
                    monitor_fingerprint not in verified_instances
                    and source_fingerprints.get(monitor_fingerprint)
                    == source_fingerprint
                    and candidate[1] == instance
                )
            )
            if len(matches) != 1:
                return None
            monitor_fingerprint, candidate = matches[0]
            detection_only.add(monitor_fingerprint)
            verified_instances[monitor_fingerprint] = candidate
            verified_sources[monitor_fingerprint] = source_fingerprint

        if len(verified_instances) != len(complete_instances):
            return None
        return (
            verified_instances,
            verified_sources,
            frozenset(detection_only),
            len(evidence_by_entry),
        )

    def _tcp_id(
        self,
        resolved: object,
        fingerprint: str,
        token: WindowInstanceToken,
    ) -> str | None:
        if not self._step_scoped_live_reconnect:
            return super()._tcp_id(resolved, fingerprint, token)
        plan = self._group_launch_plan
        if not isinstance(resolved, ResolvedTargetWindows) or plan is None:
            return None
        global_failures, local_failures = self._contract_failure_evidence(resolved)
        if global_failures or not plan.targets:
            return None
        entry_ids = tuple(target.entry_id for target in plan.targets)
        if (
            any(not entry_id for entry_id in entry_ids)
            or len(set(entry_ids)) != len(entry_ids)
            or tuple(resolved.sync_scope_entry_ids) != entry_ids
        ):
            return None

        source = (
            (self._activation_snapshot_source_fingerprints or {}).get(fingerprint)
            or normalize_launch_fingerprint(fingerprint)
        )
        if source is None:
            return None
        matches = tuple(
            (entry_id, window)
            for entry_id, window in zip(
                resolved.sync_entry_ids,
                resolved.windows,
            )
            if (
                normalize_launch_fingerprint(window.launch_fingerprint) == source
                and WindowInstanceToken.from_window(window) == token
            )
        )
        if len(matches) != 1:
            return None
        entry_id, _window = matches[0]
        target = self._target_for_entry(entry_id)
        if target is None or target.fingerprint != source:
            return None
        failure_key = self._local_failure_key(plan, entry_id, source)
        if failure_key in local_failures:
            return None
        return entry_id

    def _target_for_fingerprint(self, fingerprint: object):
        formal = _step._base.WindowsSmartReconnectController._target_for_fingerprint(
            self,
            fingerprint,
        )
        if formal is not None:
            return formal
        normalized = normalize_launch_fingerprint(fingerprint)
        if normalized is None:
            return None

        snapshot = self._activation_snapshot_instances or {}
        instance = snapshot.get(normalized)
        if instance is not None:
            entry_id = self._tcp_id(self._tcp_v, normalized, instance)
            if entry_id is not None:
                target = self._target_for_entry(entry_id)
                if target is not None:
                    return target

        return self._manual_reopen_targets.get(normalized)

    def _ordered_tcp_owner(self, confirmed):
        if not self._step_scoped_live_reconnect:
            return super()._ordered_tcp_owner(confirmed)

        plan = self._group_launch_plan
        if plan is None:
            return None, None, None

        sessions = []
        snapshot = self._activation_snapshot_instances or {}
        for monitor_fingerprint in tuple(self._login_only_recovery_fingerprints):
            if not self._has_reconnect_session(monitor_fingerprint):
                continue
            instance = snapshot.get(monitor_fingerprint)
            entry_id = (
                self._tcp_id(self._tcp_v, monitor_fingerprint, instance)
                if instance is not None
                else None
            )
            if entry_id is not None:
                target = self._target_for_entry(entry_id)
                if target is not None and target.role_id:
                    sessions.append(monitor_fingerprint)
            elif monitor_fingerprint in self._manual_live_fingerprints:
                sessions.append(monitor_fingerprint)
        sessions = tuple(dict.fromkeys(sessions))
        if len(sessions) > 1:
            return None, None, "reconnect_owner_ambiguous"
        if len(sessions) == 1:
            return sessions[0], None, None

        confirmed_by_entry = {}
        manual_confirmed = []
        for monitor_fingerprint, state in confirmed:
            if monitor_fingerprint in self._tcp_timeout_isolated:
                continue
            if state.entry_id:
                confirmed_by_entry.setdefault(state.entry_id, []).append(
                    (monitor_fingerprint, state)
                )
            elif monitor_fingerprint in self._manual_live_fingerprints:
                manual_confirmed.append((monitor_fingerprint, state))

        for target in plan.targets:
            if not target.role_id:
                continue
            matches = tuple(confirmed_by_entry.get(target.entry_id, ()))
            if len(matches) > 1:
                return None, None, "reconnect_owner_ambiguous"
            if not matches:
                continue
            monitor_fingerprint, state = matches[0]
            event = _BattleRestartEvent.from_instance(state.instance)
            if (
                self._battle_restart_attempts.get((monitor_fingerprint, True))
                == event
            ):
                continue
            return monitor_fingerprint, state, None

        for monitor_fingerprint, state in sorted(
            manual_confirmed,
            key=lambda item: item[0],
        ):
            event = _BattleRestartEvent.from_instance(state.instance)
            if (
                self._battle_restart_attempts.get((monitor_fingerprint, False))
                == event
            ):
                continue
            return monitor_fingerprint, state, None
        return None, None, None

    def _scan_locked(self, *, execute: bool):
        formal_plan = self._formal_plan(self)
        if (
            self._step_scoped_live_reconnect
            and self._step_scoped_activation_ready
            and self._manual_live_fingerprints
            and formal_plan is not None
            and formal_plan.targets
            and self._tcp_counts is not None
        ):
            # In a mixed configured+manual set, retain configured TCP evidence.
            # Manual windows remain anonymous (entry_id=None) and therefore do
            # not inherit configured close/reopen authority.
            return _step._base.WindowsSmartReconnectController._scan_locked(
                self,
                execute=execute,
            )
        return super()._scan_locked(execute=execute)


globals()["WindowsSmartReconnectController"] = WindowsSmartReconnectController
