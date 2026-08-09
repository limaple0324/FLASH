import os
from pathlib import Path

import pytest

from adapters.windows_battle_restart import (
    BattleRestartResult,
    WindowsShortcutOpenBackend,
)
from core.smart_reconnect_authorization import (
    ReconnectRevocationReason,
    ShortcutFileIdentity,
    ShortcutSeal,
)
from core.target_window_contract import WindowInstanceToken
from services.smart_reconnect_authorization_coordinator import (
    ReconnectAuthorizationMismatchError,
)
from services.group_launch_service import GroupLaunchTarget
from services.target_window_contract_service import ResolvedTargetWindows
from tests.test_windows_smart_reconnect import (
    FakeBattleRestarter,
    FakeMouseBackend,
    make_controller,
    make_window,
)


def _arm_login_action(fixture, window):
    fixture.controller._pending_reconnect_fingerprints.add(
        window.launch_fingerprint
    )


def test_source_disappears_before_first_frame_produces_zero_input():
    window = make_window(1)
    provider = {"value": ResolvedTargetWindows((window,))}
    fixture = make_controller(
        [3],
        windows=[window],
        expected_windows=1,
        target_windows_provider=lambda: provider["value"],
    )
    _arm_login_action(fixture, window)
    provider["value"] = ResolvedTargetWindows(
        (),
        ("target_window_provider_failed",),
    )

    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []
    assert fixture.authorization.current_authorization() is None


def test_source_changes_after_second_frame_produces_zero_input():
    window = make_window(1)
    provider = {"value": ResolvedTargetWindows((window,))}

    class SourceChangingMouse(FakeMouseBackend):
        def probe_responsive(self, handle, timeout_ms):
            provider["value"] = ResolvedTargetWindows(
                (),
                ("target_window_provider_failed",),
            )
            return super().probe_responsive(handle, timeout_ms)

    mouse = SourceChangingMouse()
    fixture = make_controller(
        [3],
        windows=[window],
        expected_windows=1,
        mouse=mouse,
        target_windows_provider=lambda: provider["value"],
    )
    _arm_login_action(fixture, window)

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert mouse.clicks == []


def test_identity_changes_before_final_delivery_produces_zero_input():
    window = make_window(1)
    coordinator_holder = {}

    class IdentityChangingMouse(FakeMouseBackend):
        def probe_responsive(self, handle, timeout_ms):
            coordinator_holder["value"].revoke(
                ReconnectRevocationReason.IDENTITY_WRITE
            )
            return super().probe_responsive(handle, timeout_ms)

    mouse = IdentityChangingMouse()
    fixture = make_controller(
        [3],
        windows=[window],
        expected_windows=1,
        mouse=mouse,
    )
    coordinator_holder["value"] = fixture.authorization
    _arm_login_action(fixture, window)

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert mouse.clicks == []
    assert fixture.authorization.current_authorization() is None


def test_bound_pending_reopen_provider_disappears_without_compatibility_fallback():
    window = make_window(1)
    provider = {"value": ResolvedTargetWindows((window,))}

    class ClosedButNotOpened(FakeBattleRestarter):
        def restart(self, source_window, target):
            self.calls.append((source_window, target))
            return BattleRestartResult(
                False,
                "battle_shortcut_open_failed",
                window_closed=True,
            )

    restarter = ClosedButNotOpened()
    fixture = make_controller(
        [2],
        windows=[window],
        expected_windows=1,
        battle_markers={2},
        battle_restarter=restarter,
        target_windows_provider=lambda: provider["value"],
    )

    fixture.controller.reconnect()
    fixture.controller.reconnect()
    provider["value"] = ResolvedTargetWindows(
        (),
        ("target_window_provider_failed",),
    )
    result = fixture.controller.reconnect()

    assert result.details["restarted_windows"] == 0
    assert restarter.reopen_calls == []
    assert fixture.mouse.clicks == []
    assert fixture.authorization.current_authorization() is None


def test_full_window_instance_change_produces_zero_input():
    original = make_window(1)
    sibling = make_window(2)
    fixture = make_controller(
        [3, 1],
        windows=[original, sibling],
        expected_windows=2,
    )
    _arm_login_action(fixture, original)
    old_batch = fixture.authorization.current_authorization()
    assert old_batch is not None
    old_grant = old_batch.target_for(original.launch_fingerprint)
    sibling_grant = old_batch.target_for(sibling.launch_fingerprint)
    assert old_grant is not None
    assert sibling_grant is not None
    old_instance = WindowInstanceToken.from_window(original)
    assert old_instance is not None

    fixture.controller.reconnect()
    replacement = make_window(
        1,
        process_id=original.process_id,
        fingerprint=original.launch_fingerprint,
        thread_id=original.thread_id,
        process_lifecycle_token=original.process_lifecycle_token + 1,
    )
    replacement_instance = WindowInstanceToken.from_window(replacement)
    assert replacement_instance is not None
    fixture.controller._window_backend.windows = [replacement, sibling]
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []
    current = fixture.authorization.current_authorization()
    assert current is not None
    replacement_grant = current.target_for(replacement.launch_fingerprint)
    current_sibling_grant = current.target_for(sibling.launch_fingerprint)
    assert replacement_grant is not None
    assert current_sibling_grant is not None
    assert replacement_grant.instance == replacement_instance
    assert replacement_grant.instance != old_instance
    assert replacement_grant.authorization_id != old_grant.authorization_id
    assert replacement_grant.authorization_epoch != old_grant.authorization_epoch
    assert replacement_grant.source_generation > old_grant.source_generation
    assert current_sibling_grant == sibling_grant
    with pytest.raises(ReconnectAuthorizationMismatchError):
        fixture.authorization.validate(
            epoch=old_grant.authorization_epoch,
            batch_id=old_grant.authorization_id,
            source_generation=old_grant.source_generation,
            fingerprint=old_grant.fingerprint,
            character_id=old_grant.character_id,
            instance=old_instance,
            callback=lambda target: target,
        )
    with pytest.raises(ReconnectAuthorizationMismatchError):
        fixture.authorization.validate(
            epoch=replacement_grant.authorization_epoch,
            batch_id=replacement_grant.authorization_id,
            source_generation=replacement_grant.source_generation,
            fingerprint=replacement_grant.fingerprint,
            character_id=replacement_grant.character_id,
            instance=old_instance,
            callback=lambda target: target,
        )


def test_shortcut_file_signature_change_blocks_real_reopen(
    tmp_path,
    monkeypatch,
):
    shortcut_path = tmp_path / "role.lnk"
    fingerprint = "a" * 64
    seal = ShortcutSeal(
        file_identity=ShortcutFileIdentity(
            normalized_path=str(shortcut_path),
            volume_serial_number=7,
            file_index=11,
        ),
        content_sha256="b" * 64,
        launch_fingerprint=fingerprint,
    )
    target = GroupLaunchTarget(
        1,
        "role",
        shortcut_path,
        fingerprint,
    )

    class ChangedSealResolver:
        def revalidate(self, expected):
            assert expected == seal
            return False

    opened = []
    monkeypatch.setattr(os, "startfile", lambda path: opened.append(path), raising=False)
    backend = WindowsShortcutOpenBackend(
        shortcut_seal_resolver=ChangedSealResolver()
    )

    success, failure = backend.open_shortcut_if_target_absent(
        target,
        lambda: None,
        seal,
    )

    assert success is False
    assert failure == "battle_shortcut_identity_changed"
    assert opened == []


def test_batch_missing_one_role_leaves_no_authorization_and_zero_input():
    windows = [make_window(1), make_window(2)]
    fixture = make_controller(
        [3, 3],
        windows=windows,
        expected_windows=2,
        authorization_missing_last_target=True,
    )

    prepared = fixture.controller.prepare_execution_snapshot()
    fixture.controller.set_execution_enabled(True)
    result = fixture.controller.reconnect()

    assert prepared.success is False
    assert fixture.authorization.current_authorization() is None
    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []


def test_revoked_batch_cannot_open_the_monitor_execution_gate():
    window = make_window(1)
    fixture = make_controller(
        [3],
        windows=[window],
        expected_windows=1,
    )

    assert fixture.controller.set_execution_enabled(False) is True
    assert fixture.controller.prepare_execution_snapshot().success is True
    fixture.authorization.revoke(ReconnectRevocationReason.IDENTITY_WRITE)

    assert fixture.controller.set_execution_enabled(True) is False
    assert fixture.authorization.current_authorization() is None
    result = fixture.controller.reconnect()
    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []
