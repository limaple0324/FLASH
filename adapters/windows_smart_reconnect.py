"""Step-scoped production overrides for Windows smart reconnect."""

from __future__ import annotations

import inspect
from dataclasses import replace
from pathlib import Path

from . import windows_smart_reconnect_base as _base

for _name in dir(_base):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_base, _name)

_MANUAL_SCOPE_ONLY_FAILURE_CODES = frozenset({
    "group_name_invalid",
    "group_entries_unavailable",
    "configured_entries_unavailable",
})
_OLD_SCOPE_PHRASES = (
    "目前組別的安全視窗身分尚未完成",
    "目前組別的安全視窗身分尚未完整",
    "安全視窗身分尚未完成",
    "安全視窗身分尚未完整",
)


class WindowsSmartReconnectController(_base.WindowsSmartReconnectController):
    """Grant each safely proved live instance only the authority its step needs."""

    def __init__(
        self,
        *args,
        step_scoped_live_reconnect: bool = False,
        minimized_refresh_capture_provider=None,
        manual_shortcut_resolver=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._step_scoped_live_reconnect = bool(step_scoped_live_reconnect)
        self._step_scoped_activation_ready = False
        self._minimized_refresh_capture_provider = minimized_refresh_capture_provider
        self._obscured_capture_access_ready: bool | None = None
        self._manual_shortcut_resolver = manual_shortcut_resolver
        self._manual_empty_plan_requested = False
        self._manual_runtime_plan_installed = False
        self._manual_live_fingerprints: frozenset[str] = frozenset()
        self._manual_reopen_targets: dict[str, GroupLaunchTarget] = {}

    @staticmethod
    def _normalize_product_scope_message(message: object) -> object:
        if not isinstance(message, str):
            return message
        normalized = message
        for phrase in _OLD_SCOPE_PHRASES:
            normalized = normalized.replace(
                phrase,
                "智慧重連安全操作尚未完成",
            )
        return normalized

    @classmethod
    def _install_product_scope_message_normalization(cls) -> None:
        try:
            from ui.home import HomeView, SmartReconnectToggleViewResult
        except Exception:
            return

        result_type = SmartReconnectToggleViewResult
        if not getattr(result_type, "_fu_reconnect_scope_normalized", False):
            original_init = result_type.__init__

            def normalized_init(instance, *args, **kwargs):
                values = list(args)
                if len(values) >= 3:
                    values[2] = cls._normalize_product_scope_message(values[2])
                elif "message" in kwargs:
                    kwargs["message"] = cls._normalize_product_scope_message(
                        kwargs["message"]
                    )
                return original_init(instance, *values, **kwargs)

            result_type.__init__ = normalized_init
            result_type._fu_reconnect_scope_normalized = True

        home_type = HomeView
        if not getattr(home_type, "_fu_reconnect_scope_normalized", False):
            def normalized_failure_display(instance) -> str:
                message = cls._normalize_product_scope_message(
                    getattr(instance, "_smart_reconnect_failure_message", "")
                )
                if not isinstance(message, str):
                    return ""
                if "智慧重連安全操作尚未完成" not in message:
                    return message
                return (
                    f"{message}\n"
                    "處理步驟：確認目前 FLASH 視窗皆可唯一辨識；"
                    "只有需要自動關閉／重開的視窗，才需要唯一可靠捷徑來源。"
                )

            home_type._smart_reconnect_failure_display = normalized_failure_display
            home_type._fu_reconnect_scope_normalized = True

    @staticmethod
    def _discover_manual_shortcut_resolver(provider):
        candidates = [provider, getattr(provider, "__self__", None)]
        closure = getattr(provider, "__closure__", None)
        if closure:
            for cell in closure:
                try:
                    candidates.append(cell.cell_contents)
                except ValueError:
                    continue
        seen: set[int] = set()
        for candidate in candidates:
            if candidate is None or id(candidate) in seen:
                continue
            seen.add(id(candidate))
            service = getattr(candidate, "_ungrouped_window_service", None)
            resolver = getattr(service, "shortcut_for", None)
            if callable(resolver):
                return resolver
            resolver = getattr(candidate, "shortcut_for", None)
            if (
                callable(resolver)
                and candidate.__class__.__name__ == "UngroupedWindowService"
            ):
                return resolver
        return None

    @classmethod
    def for_real_windows(cls, *args, **kwargs):
        manual_shortcut_resolver = kwargs.pop("manual_shortcut_resolver", None)
        controller = super().for_real_windows(*args, **kwargs)
        controller._step_scoped_live_reconnect = True
        controller._step_scoped_activation_ready = False
        controller._minimized_refresh_capture_provider = (
            Win32RecoveringPrintWindowProvider()
        )
        controller._obscured_capture_access_ready = None
        controller._manual_empty_plan_requested = False
        controller._manual_runtime_plan_installed = False
        controller._manual_live_fingerprints = frozenset()
        controller._manual_reopen_targets = {}
        controller._manual_shortcut_resolver = (
            manual_shortcut_resolver
            or cls._discover_manual_shortcut_resolver(
                getattr(controller, "_target_windows_provider", None)
            )
        )
        controller._tcp_counts = globals()["_ipv4_established_counts_by_pid"]
        cls._install_product_scope_message_normalization()
        return controller

    @staticmethod
    def _clean_empty_configured_plan(plan: object) -> bool:
        return bool(
            isinstance(plan, GroupLaunchPlan)
            and plan.group_name.strip().casefold() == "configured"
            and not plan.targets
            and not plan.failure_codes
        )

    def set_group_launch_plan(self, plan: GroupLaunchPlan | None) -> None:
        self._manual_reopen_targets.clear()
        self._manual_live_fingerprints = frozenset()
        self._manual_runtime_plan_installed = False
        if (
            self._step_scoped_live_reconnect
            and self._clean_empty_configured_plan(plan)
        ):
            super().set_group_launch_plan(None)
            self._manual_empty_plan_requested = True
            return
        self._manual_empty_plan_requested = False
        super().set_group_launch_plan(plan)

    def set_capture_settings(self, settings) -> None:
        previous = self.capture_settings
        super().set_capture_settings(settings)
        if (
            not self._step_scoped_live_reconnect
            or previous.obscured != settings.obscured
        ):
            self._obscured_capture_access_ready = None

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
        if not settings.obscured or self._capture_access_preparer is None:
            return False
        windows, global_failures, _target_failures = self._candidate_window_set()
        if global_failures:
            return False
        for window in windows:
            if window.minimized:
                continue
            if self._window_is_fully_visible_without_capture(window) is False:
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

    def _manual_shortcut_for_source(self, source_fingerprint: str) -> Path | None:
        resolver = self._manual_shortcut_resolver
        if not callable(resolver):
            return None
        try:
            candidate = resolver(source_fingerprint)
        except Exception:
            return None
        if candidate is None:
            return None
        path = Path(candidate)
        try:
            available = path.is_file()
        except OSError:
            available = False
        return path if available and path.suffix.casefold() == ".lnk" else None

    def _build_manual_reopen_authority(self) -> None:
        snapshot = self._activation_snapshot_instances or {}
        source_map = self._activation_snapshot_source_fingerprints or {}
        formal_plan = (
            None
            if self._manual_runtime_plan_installed
            else self._group_launch_plan
        )
        if formal_plan is None:
            manual_live = frozenset(snapshot)
        else:
            manual_live = frozenset(self._detection_only_fingerprints)
        self._manual_live_fingerprints = manual_live

        targets: dict[str, GroupLaunchTarget] = {}
        for order, monitor_fingerprint in enumerate(sorted(manual_live), start=1):
            source_fingerprint = source_map.get(
                monitor_fingerprint,
                monitor_fingerprint,
            )
            shortcut = self._manual_shortcut_for_source(source_fingerprint)
            if shortcut is None:
                continue
            try:
                target = GroupLaunchTarget(
                    order,
                    shortcut.stem or "manual-flash",
                    shortcut,
                    source_fingerprint,
                )
            except (TypeError, ValueError):
                continue
            targets[monitor_fingerprint] = target
        self._manual_reopen_targets = targets

        if formal_plan is None and targets:
            self._group_launch_plan = GroupLaunchPlan(
                "manual-live",
                tuple(targets.values()),
            )
            self._manual_runtime_plan_installed = True

    def prepare_execution_snapshot(self):
        if not self._step_scoped_live_reconnect:
            return super().prepare_execution_snapshot()

        if self._manual_runtime_plan_installed:
            self._group_launch_plan = None
            self._manual_runtime_plan_installed = False
        self._manual_reopen_targets.clear()
        self._manual_live_fingerprints = frozenset()
        self._step_scoped_activation_ready = False
        self._obscured_capture_access_ready = None
        original_preparer = self._capture_access_preparer

        needs_obscured_access = self._current_activation_requires_obscured_access()
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
            self._monotonic_clock() + INITIAL_LOGIN_AUTHORIZATION_SECONDS
        )
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
        self._build_manual_reopen_authority()
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
                expected_source_state_generation=expected_source_state_generation,
            )

        if WindowInstanceToken.from_window(window) is None:
            return self._unknown_capture_result()
        if expected_source_state_generation is None:
            expected_source_state_generation = self._source_state_generation_snapshot()
        if not self._source_authority_is_current(expected_source_state_generation):
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
            if execute and self._execution_allowed() and provider is not None:
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
            and self._window_is_fully_visible_without_capture(window) is False
            and not self._ensure_obscured_capture_access()
        ):
            if self._remember_capture_route_if_source_current(
                fingerprint,
                CAPTURE_ROUTE_OBSCURED,
                expected_source_state_generation,
            ):
                return self._unknown_capture_result(route=CAPTURE_ROUTE_OBSCURED)
            return self._unknown_capture_result()

        return super()._capture_and_recognize_unobserved(
            window,
            fingerprint,
            execute=execute,
            expected_source_state_generation=expected_source_state_generation,
        )

    def _target_for_fingerprint(self, fingerprint: object):
        formal = super()._target_for_fingerprint(fingerprint)
        if formal is not None:
            return formal
        normalized = normalize_launch_fingerprint(fingerprint)
        if normalized is None:
            return None
        return self._manual_reopen_targets.get(normalized)

    def _manual_character_target_is_safe(
        self,
        fingerprint: str,
        item,
        *,
        initial_login_authorized: bool,
    ):
        candidates = tuple(item.character_candidates)
        pending_target = self._character_selection_targets.get(fingerprint)
        reconnect_session = self._has_reconnect_session(fingerprint)

        if pending_target is not None:
            selected_candidates = tuple(
                candidate for candidate in candidates if candidate.selected
            )
            if len(selected_candidates) == 1:
                selected = selected_candidates[0]
            elif not candidates and item.character_slot_selected is True:
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
                    and selected.digit_count != pending_target.digit_count
                )
            ):
                return None
            pending_role = self._registered_role_for_candidate(pending_target)
            selected_role = self._registered_role_for_candidate(selected)
            expected_recent_role = self._recent_login_role_ids.get(fingerprint)
            if (
                pending_role is None
                or selected_role is None
                or pending_role.importance is not CharacterImportance.PRIMARY
                or selected_role.importance is not CharacterImportance.PRIMARY
                or pending_role.role_id.casefold() != selected_role.role_id.casefold()
                or (
                    expected_recent_role is not None
                    and selected_role.role_id.casefold() != expected_recent_role
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
            selection = self._global_character_candidate(fingerprint, item)
        elif reconnect_session:
            selection = self._global_character_candidate(fingerprint, item)
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
            and fingerprint in self._manual_live_fingerprints
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

    @staticmethod
    def _delivery_window_proxy(window):
        if window is None or not window.minimized:
            return window
        frame = inspect.currentframe()
        caller = frame.f_back if frame is not None else None
        caller = caller.f_back if caller is not None else None
        if (
            caller is not None
            and caller.f_code.co_name == "deliver_click"
            and caller.f_code.co_filename == _base.__file__
        ):
            return replace(window, minimized=False)
        return window

    def _current_action_window(self, expected, fingerprint: str):
        if not (
            self._step_scoped_live_reconnect
            and self._step_scoped_activation_ready
            and fingerprint in self._manual_live_fingerprints
        ):
            current = super()._current_action_window(expected, fingerprint)
            return self._delivery_window_proxy(current)

        candidates, global_failures, target_failures = self._candidate_window_set()
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
                or normalize_launch_fingerprint(candidate.launch_fingerprint)
                in allowed
            )
        )
        instances = self._unique_complete_candidate_instances(scoped_candidates)
        group_failures = tuple(
            self._group_failures(
                scoped_candidates,
                locally_isolated_fingerprints=frozenset(target_failures),
            )
        )
        if self._activation_snapshot_instances is not None:
            group_failures = tuple(
                code for code in group_failures
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
                self._mark_fingerprints_unknown_locked((fingerprint,))
            return None
        current = instances.get(fingerprint)
        if current is None:
            with self._screen_state_lock:
                self._mark_fingerprints_unknown_locked((fingerprint,))
            return None
        window, instance = current
        plan = self._group_launch_plan
        if isinstance(self._tcp_v, ResolvedTargetWindows) and plan is not None:
            target = plan.target_for_fingerprint(fingerprint)
            if target is not None and (
                not target.entry_id
                or self._tcp_id(self._tcp_v, fingerprint, instance)
                != target.entry_id
            ):
                return None
        snapshot = self._activation_snapshot_instances
        if snapshot is not None and snapshot.get(fingerprint) != instance:
            with self._screen_state_lock:
                self._mark_fingerprints_unknown_locked((fingerprint,))
            return None
        if instance != expected_instance:
            with self._screen_state_lock:
                self._mark_fingerprints_unknown_locked((fingerprint,))
            return None
        return self._delivery_window_proxy(window)

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
        formal_plan = (
            None if self._manual_runtime_plan_installed
            else self._group_launch_plan
        )
        attributed_codes: set[str] = set()

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
                global_failures.append("target_failure_unattributed")
                continue
            if formal_plan is not None:
                targets = tuple(
                    target
                    for target in formal_plan.targets
                    if (
                        target.entry_id == entry_id
                        and target.fingerprint == fingerprint
                    )
                )
                if len(targets) != 1:
                    global_failures.extend(failure_codes)
                    global_failures.append("target_failure_unattributed")
                    continue
            if fingerprint in local_failures:
                global_failures.extend(failure_codes)
                global_failures.append("target_failure_unattributed")
                continue
            local_failures[fingerprint] = failure_codes
            attributed_codes.update(failure_codes)

        global_failures = [
            code for code in global_failures if code not in attributed_codes
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
        failed_fingerprints: set[str] = set()
        for evidence in resolved.target_failure_evidence:
            target = targets_by_entry.get(evidence.entry_id)
            fingerprint = normalize_launch_fingerprint(evidence.fingerprint)
            if (
                target is None
                or evidence.entry_id in evidence_by_entry
                or fingerprint is None
                or fingerprint != target.fingerprint
                or local_failures.get(fingerprint)
                != tuple(evidence.failure_codes)
            ):
                return None
            evidence_by_entry[evidence.entry_id] = evidence
            failed_fingerprints.add(fingerprint)

        filtered_instances = {
            monitor_fingerprint: candidate
            for monitor_fingerprint, candidate in complete_instances.items()
            if source_fingerprints.get(monitor_fingerprint)
            not in failed_fingerprints
        }
        filtered_sources = {
            monitor_fingerprint: source
            for monitor_fingerprint, source in source_fingerprints.items()
            if monitor_fingerprint in filtered_instances
        }

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
            or len(filtered_instances)
            != len(required_targets) + len(resolved.detection_only_windows)
            or len(filtered_sources) != len(filtered_instances)
        ):
            return None

        verified_instances = {}
        verified_sources: dict[str, str] = {}
        detection_only: set[str] = set()

        for target in required_targets:
            matches = tuple(
                (monitor_fingerprint, candidate)
                for monitor_fingerprint, candidate
                in filtered_instances.items()
                if (
                    filtered_sources.get(monitor_fingerprint)
                    == target.fingerprint
                    and self._tcp_id(
                        resolved,
                        target.fingerprint,
                        candidate[1],
                    ) == target.entry_id
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
                for monitor_fingerprint, candidate
                in filtered_instances.items()
                if (
                    monitor_fingerprint not in verified_instances
                    and filtered_sources.get(monitor_fingerprint)
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

        if len(verified_instances) != len(filtered_instances):
            return None
        return (
            verified_instances,
            verified_sources,
            frozenset(detection_only),
            len(evidence_by_entry),
        )

    def _scan_locked(self, *, execute: bool):
        if not (
            self._step_scoped_live_reconnect
            and self._step_scoped_activation_ready
            and self._manual_live_fingerprints
            and self._tcp_counts is not None
        ):
            return super()._scan_locked(execute=execute)

        provider = self._tcp_counts
        self._tcp_counts = None
        try:
            return super()._scan_locked(execute=execute)
        finally:
            self._tcp_counts = provider


globals()["WindowsSmartReconnectController"] = WindowsSmartReconnectController
