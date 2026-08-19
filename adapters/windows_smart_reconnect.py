"""Step-scoped production overrides for Windows smart reconnect.

The frozen implementation remains in ``windows_smart_reconnect_base``.  This
module preserves the public import surface while narrowing only the six
production contradictions approved for the current smart-reconnect repair.
"""

from __future__ import annotations

from . import windows_smart_reconnect_base as _base

# Re-export the complete historical module surface, including private helpers
# used by the existing regression suite.  Dunder module metadata stays local.
for _name in dir(_base):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_base, _name)


_MANUAL_SCOPE_ONLY_FAILURE_CODES = frozenset(
    {
        "group_name_invalid",
        "group_entries_unavailable",
        "configured_entries_unavailable",
    }
)


class WindowsSmartReconnectController(_base.WindowsSmartReconnectController):
    """Add step-scoped live-instance authority without weakening identity gates."""

    def __init__(
        self,
        *args,
        step_scoped_live_reconnect: bool = False,
        minimized_refresh_capture_provider=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._step_scoped_live_reconnect = bool(step_scoped_live_reconnect)
        self._step_scoped_activation_ready = False
        self._minimized_refresh_capture_provider = (
            minimized_refresh_capture_provider
        )
        self._obscured_capture_access_ready: bool | None = None

    @classmethod
    def for_real_windows(cls, *args, **kwargs):
        controller = super().for_real_windows(*args, **kwargs)
        # The legacy factory remains byte-for-byte frozen in the base module.
        # Production enables the new authority only here; direct unit fixtures
        # retain their historical defaults unless they opt in explicitly.
        controller._step_scoped_live_reconnect = True
        controller._step_scoped_activation_ready = False
        controller._minimized_refresh_capture_provider = (
            Win32RecoveringPrintWindowProvider()
        )
        controller._obscured_capture_access_ready = None
        # Preserve the historical monkeypatch seam exposed by this module.
        controller._tcp_counts = globals()["_ipv4_established_counts_by_pid"]
        return controller

    def _manual_scope_fallback_allowed(
        self,
        global_failures: tuple[str, ...],
        target_failures: dict[str, tuple[str, ...]],
    ) -> bool:
        return bool(
            self._step_scoped_live_reconnect
            and global_failures
            and not target_failures
            and set(global_failures) <= _MANUAL_SCOPE_ONLY_FAILURE_CODES
        )

    def _candidate_window_set(self):
        windows, global_failures, target_failures = (
            super()._candidate_window_set()
        )
        if not self._manual_scope_fallback_allowed(
            global_failures,
            target_failures,
        ):
            return windows, global_failures, target_failures

        # No configured shortcut/role authority is not a live-instance safety
        # failure.  Enumerate only current FLASH windows and keep every normal
        # complete-instance check in the activation/action path.
        self._tcp_v = None
        try:
            direct_windows = tuple(
                window
                for window in self._window_backend.list_windows()
                if all(
                    keyword in window.title.casefold()
                    for keyword in self._keywords
                )
            )
        except Exception:
            return (), ("window_enumeration_failed",), {}
        bound, blocked = self._bind_activation_snapshot_window_set(
            direct_windows,
            frozenset(),
        )
        return (
            bound,
            (("window_identity_duplicate",) if blocked else ()),
            {},
        )

    def _current_activation_requires_obscured_access(self) -> bool:
        settings = self.capture_settings
        if (
            not settings.obscured
            or self._capture_access_preparer is None
        ):
            return False
        windows, global_failures, _target_failures = (
            self._candidate_window_set()
        )
        if global_failures:
            return False
        for window in windows:
            if window.minimized:
                continue
            if (
                self._window_is_fully_visible_without_capture(window)
                is False
            ):
                return True
        return False

    def _ensure_obscured_capture_access(self) -> bool:
        if self._capture_access_preparer is None:
            return True
        if self._obscured_capture_access_ready is not None:
            return self._obscured_capture_access_ready
        try:
            ready = self._capture_access_preparer() is True
        except Exception:
            ready = False
        self._obscured_capture_access_ready = ready
        return ready

    def prepare_execution_snapshot(self):
        if not self._step_scoped_live_reconnect:
            return super().prepare_execution_snapshot()

        self._step_scoped_activation_ready = False
        self._obscured_capture_access_ready = None
        original_preparer = self._capture_access_preparer

        # Borderless/WGC access is requested only when a currently obscured
        # route actually needs it.  Visible and minimized routes do not inherit
        # that unrelated permission requirement.
        needs_obscured_access = (
            self._current_activation_requires_obscured_access()
        )
        if needs_obscured_access and original_preparer is not None:
            if not self._ensure_obscured_capture_access():
                return self._snapshot_failure(
                    "reconnect.snapshot_capture_access_denied",
                    "Windows 無彩框背景擷取權限未允許，智慧重連未啟用。",
                    "borderless_capture_access_denied",
                )

        if not needs_obscured_access:
            self._capture_access_preparer = None
        try:
            prepared = super().prepare_execution_snapshot()
        finally:
            self._capture_access_preparer = original_preparer

        if not prepared.success:
            return prepared

        self._step_scoped_activation_ready = True
        snapshot = self._activation_snapshot_instances or {}
        _settings, capture_revision = self._capture_settings_snapshot()
        source_generation = self._source_state_generation_snapshot()
        expires_at = (
            self._monotonic_clock()
            + INITIAL_LOGIN_AUTHORIZATION_SECONDS
        )
        # "Detection-only" now means "no restart/role source", not "no live
        # action at all".  Every activation-proven live instance may advance
        # login/line steps; character and reopen still require their own proof.
        for fingerprint, instance in snapshot.items():
            self._initial_login_authorizations.setdefault(
                fingerprint,
                _InitialLoginAuthorization(
                    instance,
                    capture_revision,
                    source_generation,
                    expires_at,
                ),
            )
        return prepared

    def set_execution_enabled(self, enabled: bool) -> None:
        super().set_execution_enabled(enabled)
        if not enabled:
            self._step_scoped_activation_ready = False
            self._obscured_capture_access_ready = None

    def _capture_and_recognize_unobserved(
        self,
        window,
        fingerprint,
        *,
        execute: bool = False,
        expected_source_state_generation: int | None = None,
    ):
        if not (
            self._step_scoped_live_reconnect
            and self._step_scoped_activation_ready
        ):
            return super()._capture_and_recognize_unobserved(
                window,
                fingerprint,
                execute=execute,
                expected_source_state_generation=(
                    expected_source_state_generation
                ),
            )

        if WindowInstanceToken.from_window(window) is None:
            return self._unknown_capture_result()
        if expected_source_state_generation is None:
            expected_source_state_generation = (
                self._source_state_generation_snapshot()
            )
        if not self._source_authority_is_current(
            expected_source_state_generation,
        ):
            return self._unknown_capture_result()

        if window.minimized:
            route = CAPTURE_ROUTE_MINIMIZED
            if not self._remember_capture_route_if_source_current(
                fingerprint,
                route,
                expected_source_state_generation,
            ):
                return self._unknown_capture_result()
            settings = self.capture_settings
            if not settings.minimized:
                return self._disabled_capture_result(route)
            provider = self._minimized_refresh_capture_provider
            if (
                execute
                and self._execution_allowed()
                and provider is not None
            ):
                try:
                    sample = provider.capture(window.handle)
                except OSError:
                    sample = None
                if sample is not None and sample.api_succeeded:
                    return (
                        sample,
                        self._recognizer.recognize_capture(sample),
                        True,
                        route,
                    )
            return self._unknown_capture_result(route=route)

        if (
            execute
            and self.capture_settings.obscured
            and self._capture_access_preparer is not None
            and self._window_is_fully_visible_without_capture(window)
            is False
            and not self._ensure_obscured_capture_access()
        ):
            if self._remember_capture_route_if_source_current(
                fingerprint,
                CAPTURE_ROUTE_OBSCURED,
                expected_source_state_generation,
            ):
                return self._unknown_capture_result(
                    route=CAPTURE_ROUTE_OBSCURED
                )
            return self._unknown_capture_result()

        return super()._capture_and_recognize_unobserved(
            window,
            fingerprint,
            execute=execute,
            expected_source_state_generation=(
                expected_source_state_generation
            ),
        )

    def _manual_character_target_is_safe(
        self,
        fingerprint: str,
        item,
        *,
        initial_login_authorized: bool,
    ):
        candidates = tuple(item.character_candidates)
        pending_target = self._character_selection_targets.get(
            fingerprint
        )
        reconnect_session = self._has_reconnect_session(fingerprint)

        if pending_target is not None:
            selected_candidates = tuple(
                candidate
                for candidate in candidates
                if candidate.selected
            )
            if len(selected_candidates) == 1:
                selected = selected_candidates[0]
            elif (
                not candidates
                and item.character_slot_selected is True
            ):
                selected = self._candidate_from_recognition(item)
                if selected is None:
                    return None
            else:
                return None
            if (
                selected.slot_index != pending_target.slot_index
                or (
                    pending_target.level is not None
                    and selected.level != pending_target.level
                )
                or (
                    pending_target.digit_count is not None
                    and selected.digit_count
                    != pending_target.digit_count
                )
            ):
                return None
            pending_role = self._registered_role_for_candidate(
                pending_target
            )
            selected_role = self._registered_role_for_candidate(selected)
            expected_recent_role = self._recent_login_role_ids.get(
                fingerprint
            )
            if (
                pending_role is None
                or selected_role is None
                or pending_role.importance
                is not CharacterImportance.PRIMARY
                or selected_role.importance
                is not CharacterImportance.PRIMARY
                or pending_role.role_id.casefold()
                != selected_role.role_id.casefold()
                or (
                    expected_recent_role is not None
                    and selected_role.role_id.casefold()
                    != expected_recent_role
                )
            ):
                return None
            return self._candidate_result(
                item,
                selected,
                CharacterImportance.PRIMARY,
                selected_role.role_id.casefold(),
            )

        if initial_login_authorized and not reconnect_session:
            selection = self._global_character_candidate(
                fingerprint,
                item,
            )
        elif reconnect_session:
            selection = self._global_character_candidate(
                fingerprint,
                item,
            )
        else:
            selection = None
        if selection is None:
            return None
        selected, importance = selection
        registered = self._registered_role_for_candidate(selected)
        if registered is None:
            return None
        return self._candidate_result(
            item,
            selected,
            importance,
            registered.role_id.casefold(),
        )

    def _character_target_is_safe(
        self,
        fingerprint: str,
        item,
        *,
        initial_login_authorized: bool = False,
    ):
        if (
            self._step_scoped_live_reconnect
            and self._step_scoped_activation_ready
            and fingerprint in self._detection_only_fingerprints
            and self._target_for_fingerprint(fingerprint) is None
        ):
            return self._manual_character_target_is_safe(
                fingerprint,
                item,
                initial_login_authorized=initial_login_authorized,
            )
        return super()._character_target_is_safe(
            fingerprint,
            item,
            initial_login_authorized=initial_login_authorized,
        )

    def _current_action_window(self, expected, fingerprint: str):
        if not (
            self._step_scoped_live_reconnect
            and self._step_scoped_activation_ready
            and fingerprint in self._detection_only_fingerprints
        ):
            return super()._current_action_window(
                expected,
                fingerprint,
            )

        candidates, global_failures, target_failures = (
            self._candidate_window_set()
        )
        expected_instance = (
            expected
            if isinstance(expected, WindowInstanceToken)
            else WindowInstanceToken.from_window(expected)
        )
        allowed = self._allowed_fingerprints
        scoped_candidates = tuple(
            candidate
            for candidate in candidates
            if (
                allowed is None
                or normalize_launch_fingerprint(
                    candidate.launch_fingerprint
                )
                in allowed
            )
        )
        instances = self._unique_complete_candidate_instances(
            scoped_candidates
        )
        group_failures = tuple(
            self._group_failures(
                scoped_candidates,
                locally_isolated_fingerprints=frozenset(
                    target_failures
                ),
            )
        )
        if self._activation_snapshot_instances is not None:
            group_failures = tuple(
                code
                for code in group_failures
                if code != "group_identity_set_mismatch"
            )
        if (
            expected_instance is None
            or global_failures
            or fingerprint in target_failures
            or instances is None
            or group_failures
        ):
            with self._screen_state_lock:
                self._mark_fingerprints_unknown_locked(
                    (fingerprint,)
                )
            return None
        current = instances.get(fingerprint)
        if current is None:
            with self._screen_state_lock:
                self._mark_fingerprints_unknown_locked(
                    (fingerprint,)
                )
            return None
        window, instance = current
        plan = self._group_launch_plan
        if isinstance(self._tcp_v, ResolvedTargetWindows) and plan is not None:
            target = plan.target_for_fingerprint(fingerprint)
            if target is not None and (
                not target.entry_id
                or self._tcp_id(
                    self._tcp_v,
                    fingerprint,
                    instance,
                )
                != target.entry_id
            ):
                return None
        snapshot = self._activation_snapshot_instances
        if snapshot is not None and snapshot.get(fingerprint) != instance:
            with self._screen_state_lock:
                self._mark_fingerprints_unknown_locked(
                    (fingerprint,)
                )
            return None
        if instance != expected_instance:
            with self._screen_state_lock:
                self._mark_fingerprints_unknown_locked(
                    (fingerprint,)
                )
            return None
        return window

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
        plan = self._group_launch_plan
        for evidence in resolved.target_failure_evidence:
            fingerprint = normalize_launch_fingerprint(
                evidence.fingerprint
            )
            entry_id = evidence.entry_id.strip()
            failure_codes = tuple(evidence.failure_codes)
            if (
                fingerprint is None
                or not entry_id
                or not failure_codes
            ):
                global_failures.extend(failure_codes)
                global_failures.append(
                    "target_failure_unattributed"
                )
                continue
            malformed_candidates = False
            for candidate in evidence.candidate_windows:
                identity = complete_window_instance_identity(candidate)
                if identity is None or identity[0] != fingerprint:
                    malformed_candidates = True
                    break
                previous_entry = evidence_identities.setdefault(
                    identity,
                    entry_id,
                )
                previous_handle = evidence_handles.setdefault(
                    identity[1],
                    identity,
                )
                previous_process = evidence_processes.setdefault(
                    identity[2],
                    identity,
                )
                previous_stable = evidence_stable_tokens.setdefault(
                    identity[:6],
                    identity,
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
                global_failures.append(
                    "target_failure_unattributed"
                )
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
                    global_failures.append(
                        "target_failure_unattributed"
                    )
                    continue
            if fingerprint in local_failures:
                global_failures.extend(failure_codes)
                global_failures.append(
                    "target_failure_unattributed"
                )
                continue
            local_failures[fingerprint] = failure_codes
        return (
            tuple(dict.fromkeys(global_failures)),
            local_failures,
        )


# Preserve the public symbol after the broad re-export above.
globals()["WindowsSmartReconnectController"] = WindowsSmartReconnectController
