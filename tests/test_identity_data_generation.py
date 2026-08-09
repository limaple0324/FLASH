from pathlib import Path

import pytest

from core.smart_reconnect_authorization import (
    ReconnectAuthorizationTarget,
    ReconnectLaunchMode,
    ReconnectRevocationReason,
    ReconnectSourceIdentity,
    ShortcutFileIdentity,
    ShortcutSeal,
)
from core.window_instance import WindowInstanceToken
from domain.character import CharacterImportance
from services.identity_data_transaction_coordinator import (
    IdentityDataResource,
    IdentityDataTransactionCoordinator,
    IdentityTransactionInvalidationError,
)
from services.smart_reconnect_authorization_coordinator import (
    SmartReconnectAuthorizationCoordinator,
)


def make_target():
    fingerprint = "a" * 64
    return ReconnectAuthorizationTarget(
        fingerprint=fingerprint,
        instance=WindowInstanceToken(
            1,
            2,
            3,
            "FlashWindow",
            (0, 0, 800, 600),
            False,
            4,
        ),
        character_id="diagnostic",
        role_aliases=("diagnostic-role",),
        importance=CharacterImportance.PRIMARY,
        original_slot_index=0,
        original_line_number=1,
        shortcut_seal=ShortcutSeal(
            ShortcutFileIdentity(
                str((Path.cwd() / "diagnostic-role.lnk").resolve()),
                1,
                1,
            ),
            "b" * 64,
            fingerprint,
        ),
    )


def test_read_only_snapshots_report_generation_without_incrementing():
    coordinator = IdentityDataTransactionCoordinator()

    captured = coordinator.capture_snapshot(lambda: "value")
    callback_generation = coordinator.snapshot_with_generation(
        lambda generation: generation
    )

    assert captured.generation == 0
    assert captured.value == "value"
    assert callback_generation == 0
    assert coordinator.generation == 0


def test_only_successful_actual_commit_increments_generation():
    coordinator = IdentityDataTransactionCoordinator()
    state = {"value": "before"}

    coordinator.execute(lambda transaction: None)
    assert coordinator.generation == 0

    def prepare(transaction):
        transaction.stage_memory(
            IdentityDataResource.RECONNECT_IDENTITY,
            lambda: dict(state),
            lambda: state.update(value="after"),
            lambda original: state.update(original),
        )

    coordinator.execute(prepare)

    assert state == {"value": "after"}
    assert coordinator.generation == 1


def test_before_write_listener_runs_before_prepare_and_failed_write_stays_revoked(
):
    identity = IdentityDataTransactionCoordinator()
    authorization = SmartReconnectAuthorizationCoordinator()
    target = make_target()
    authorization.publish(
        ReconnectSourceIdentity(0, 0, "g", "group", ("diagnostic",)),
        ReconnectLaunchMode.COMPATIBILITY,
        (target,),
    )
    events = []

    def invalidate(generation):
        events.append(("invalidate", generation))
        authorization.revoke(ReconnectRevocationReason.IDENTITY_WRITE)

    identity.register_before_write_listener(invalidate)

    def fail_prepare(transaction):
        events.append(("prepare", identity.generation))
        raise RuntimeError("write failed")

    with pytest.raises(RuntimeError, match="write failed"):
        identity.execute(fail_prepare)

    assert events == [("invalidate", 0), ("prepare", 0)]
    assert identity.generation == 0
    assert authorization.current_authorization() is None
    assert (
        authorization.last_revocation_reason
        is ReconnectRevocationReason.IDENTITY_WRITE
    )


def test_listener_failure_prevents_identity_prepare_from_starting():
    coordinator = IdentityDataTransactionCoordinator()
    prepare_started = False

    coordinator.register_before_write_listener(lambda generation: False)

    def prepare(transaction):
        nonlocal prepare_started
        prepare_started = True

    with pytest.raises(IdentityTransactionInvalidationError):
        coordinator.execute(prepare)

    assert prepare_started is False
    assert coordinator.generation == 0
