import threading
from dataclasses import FrozenInstanceError, replace

import pytest

from core.smart_reconnect_authorization import (
    ReconnectAuthorizationBatch,
    ReconnectActionContext,
    ReconnectAuthorizationTarget,
    ReconnectLaunchMode,
    ReconnectRevocationReason,
    ReconnectSourceIdentity,
    ShortcutFileIdentity,
    ShortcutSeal,
    observed_alias_matches,
)
from core.window_instance import WindowInstanceToken
from domain.character import CharacterImportance
from services.smart_reconnect_authorization_coordinator import (
    ReconnectAuthorizationMismatchError,
    ReconnectAuthorizationState,
    ReconnectAuthorizationUnavailableError,
    ReconnectPreparationToken,
    SmartReconnectAuthorizationCoordinator,
)


def make_instance(number: int = 1) -> WindowInstanceToken:
    return WindowInstanceToken(
        handle=number,
        process_id=100 + number,
        thread_id=200 + number,
        window_class="FlashWindow",
        rect=(0, 0, 800, 600),
        minimized=False,
        process_lifecycle_token=300 + number,
    )


def make_seal(tmp_path, number: int = 1) -> ShortcutSeal:
    return ShortcutSeal(
        ShortcutFileIdentity(str(tmp_path / f"role-{number}.lnk"), 10, number),
        f"{number:x}" * 64,
        f"{number + 8:x}" * 64,
    )


def make_target(tmp_path, number: int = 1) -> ReconnectAuthorizationTarget:
    seal = make_seal(tmp_path, number)
    return ReconnectAuthorizationTarget(
        fingerprint=seal.launch_fingerprint,
        instance=make_instance(number),
        character_id=f"character-{number}",
        role_aliases=(f"角色{number}名稱",),
        importance=CharacterImportance.PRIMARY,
        original_slot_index=(number - 1) % 3,
        original_line_number=number,
        shortcut_seal=seal,
    )


def make_source(*targets: ReconnectAuthorizationTarget) -> ReconnectSourceIdentity:
    return ReconnectSourceIdentity(
        identity_generation=4,
        config_revision=2,
        group_id="group-1",
        group_name="第一組",
        character_ids=tuple(target.character_id for target in targets),
    )


def make_batch(
    tmp_path,
    *targets: ReconnectAuthorizationTarget,
    launch_mode: ReconnectLaunchMode = ReconnectLaunchMode.IDENTITY_BOUND,
):
    selected = targets or (make_target(tmp_path),)
    return ReconnectAuthorizationBatch(
        epoch=1,
        batch_id="batch-1",
        source=make_source(*selected),
        launch_mode=launch_mode,
        targets=selected,
    )


def test_window_instance_token_is_complete_immutable_and_adapter_compatible():
    from adapters.windows_smart_reconnect import WindowInstanceToken as Reexported

    token = make_instance()

    assert Reexported is WindowInstanceToken
    with pytest.raises(FrozenInstanceError):
        token.handle = 9
    with pytest.raises(ValueError):
        replace(token, process_lifecycle_token=0)


def test_identity_bound_batch_can_be_empty_and_is_immutable(tmp_path):
    batch = make_batch(tmp_path)

    assert batch.targets[0].character_id == "character-1"
    with pytest.raises(FrozenInstanceError):
        batch.epoch = 2
    empty = ReconnectAuthorizationBatch(
        epoch=1,
        batch_id="empty",
        source=ReconnectSourceIdentity(0, 0, "g", "group", ()),
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND,
        targets=(),
    )
    assert empty.targets == ()


def test_source_rejects_duplicate_character_identity():
    with pytest.raises(ValueError, match="duplicate"):
        ReconnectSourceIdentity(
            0,
            0,
            "group-1",
            "group",
            ("character-1", "character-1"),
        )


@pytest.mark.parametrize(
    "launch_mode",
    (ReconnectLaunchMode.IDENTITY_BOUND, ReconnectLaunchMode.COMPATIBILITY),
)
@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("character_id", None),
        ("role_aliases", ()),
        ("shortcut_seal", None),
    ),
)
def test_every_launch_mode_rejects_every_missing_dedicated_identity_field(
    tmp_path,
    launch_mode,
    field,
    value,
):
    target = replace(make_target(tmp_path), **{field: value})
    source = ReconnectSourceIdentity(0, 0, "g", "group", ("character-1",))

    with pytest.raises(ValueError, match="incomplete"):
        ReconnectAuthorizationBatch(
            1,
            "batch",
            source,
            launch_mode,
            (target,),
        )


def test_only_identity_bound_target_may_omit_importance(tmp_path):
    target = replace(make_target(tmp_path), importance=None)
    source = ReconnectSourceIdentity(0, 0, "g", "group", ("character-1",))

    identity_bound = ReconnectAuthorizationBatch(
        1,
        "identity-bound",
        source,
        ReconnectLaunchMode.IDENTITY_BOUND,
        (target,),
    )

    assert identity_bound.targets == (target,)
    with pytest.raises(ValueError, match="incomplete"):
        ReconnectAuthorizationBatch(
            1,
            "compatibility",
            source,
            ReconnectLaunchMode.COMPATIBILITY,
            (target,),
        )


@pytest.mark.parametrize(
    "launch_mode",
    (ReconnectLaunchMode.IDENTITY_BOUND, ReconnectLaunchMode.COMPATIBILITY),
)
@pytest.mark.parametrize(
    "duplicate_kind",
    ("fingerprint", "character", "instance", "shortcut_path", "shortcut_file"),
)
def test_every_launch_mode_rejects_every_duplicate_identity(
    tmp_path,
    launch_mode,
    duplicate_kind,
):
    first = make_target(tmp_path, 1)
    second = make_target(tmp_path, 2)
    if duplicate_kind == "fingerprint":
        second = replace(
            second,
            fingerprint=first.fingerprint,
            shortcut_seal=replace(
                second.shortcut_seal,
                launch_fingerprint=first.fingerprint,
            ),
        )
    elif duplicate_kind == "character":
        second = replace(second, character_id=first.character_id)
    elif duplicate_kind == "instance":
        second = replace(second, instance=first.instance)
    elif duplicate_kind == "shortcut_path":
        second = replace(
            second,
            shortcut_seal=replace(
                second.shortcut_seal,
                file_identity=replace(
                    second.shortcut_seal.file_identity,
                    normalized_path=first.shortcut_seal.file_identity.normalized_path,
                ),
            ),
        )
    else:
        second = replace(
            second,
            shortcut_seal=replace(
                second.shortcut_seal,
                file_identity=replace(
                    second.shortcut_seal.file_identity,
                    volume_serial_number=(
                        first.shortcut_seal.file_identity.volume_serial_number
                    ),
                    file_index=first.shortcut_seal.file_identity.file_index,
                ),
            ),
        )

    with pytest.raises(ValueError, match="duplicate"):
        make_batch(tmp_path, first, second, launch_mode=launch_mode)


@pytest.mark.parametrize(
    "launch_mode",
    (ReconnectLaunchMode.IDENTITY_BOUND, ReconnectLaunchMode.COMPATIBILITY),
)
def test_full_shared_prefix_names_coexist_but_ambiguous_observation_is_blocked(
    tmp_path,
    launch_mode,
):
    first = replace(make_target(tmp_path, 1), role_aliases=("100古",))
    second = replace(make_target(tmp_path, 2), role_aliases=("100靈",))
    batch = make_batch(tmp_path, first, second, launch_mode=launch_mode)

    assert batch.unique_target_for_observed_identity("100古") == first
    assert batch.unique_target_for_observed_identity("100靈") == second
    assert batch.unique_target_for_observed_identity("100") is None
    assert batch.unique_target_for_observed_identity("100…") is None


def test_slot_and_line_are_optional_monitoring_evidence(tmp_path):
    target = replace(
        make_target(tmp_path),
        original_slot_index=None,
        original_line_number=None,
    )

    batch = make_batch(tmp_path, target)

    assert batch.targets == (target,)


@pytest.mark.parametrize(
    "launch_mode",
    (ReconnectLaunchMode.IDENTITY_BOUND, ReconnectLaunchMode.COMPATIBILITY),
)
@pytest.mark.parametrize(
    "source_character_ids",
    (
        ("character-1",),
        ("character-2", "character-1"),
    ),
)
def test_every_launch_mode_requires_source_count_and_target_order(
    tmp_path,
    launch_mode,
    source_character_ids,
):
    targets = (make_target(tmp_path, 1), make_target(tmp_path, 2))
    source = ReconnectSourceIdentity(
        4,
        2,
        "group-1",
        "group",
        source_character_ids,
    )

    with pytest.raises(ValueError, match="do not match source identities"):
        ReconnectAuthorizationBatch(
            1,
            "batch",
            source,
            launch_mode,
            targets,
        )


def test_complete_compatibility_batch_can_be_explicitly_published(tmp_path):
    coordinator = SmartReconnectAuthorizationCoordinator()
    targets = (make_target(tmp_path, 1), make_target(tmp_path, 2))

    batch = coordinator.publish(
        make_source(*targets),
        ReconnectLaunchMode.COMPATIBILITY,
        targets,
    )

    assert batch.launch_mode is ReconnectLaunchMode.COMPATIBILITY
    assert coordinator.current_authorization() is batch


def test_abbreviated_observation_under_three_characters_never_matches():
    aliases = ("小明角色",)

    assert observed_alias_matches(aliases, "小明…") is False
    assert observed_alias_matches(aliases, "小明角…") is True
    assert observed_alias_matches(aliases, "小明角色") is True


def test_publish_failure_clears_old_batch_without_partial_targets(tmp_path):
    coordinator = SmartReconnectAuthorizationCoordinator()
    first = make_target(tmp_path, 1)
    published = coordinator.publish(
        make_source(first),
        ReconnectLaunchMode.IDENTITY_BOUND,
        (first,),
    )
    duplicate = replace(
        make_target(tmp_path, 2),
        fingerprint=first.fingerprint,
        shortcut_seal=replace(
            make_target(tmp_path, 2).shortcut_seal,
            launch_fingerprint=first.fingerprint,
        ),
    )

    with pytest.raises(ValueError):
        coordinator.publish(
            ReconnectSourceIdentity(
                4,
                2,
                "group-1",
                "第一組",
                ("character-1", "character-2"),
            ),
            ReconnectLaunchMode.IDENTITY_BOUND,
            (first, duplicate),
        )

    assert published.epoch == 1
    assert coordinator.current_authorization() is None
    assert coordinator.state is ReconnectAuthorizationState.EMPTY
    assert coordinator.last_revocation_reason is ReconnectRevocationReason.PREPARATION_FAILED


@pytest.mark.parametrize(
    "launch_mode",
    (ReconnectLaunchMode.IDENTITY_BOUND, ReconnectLaunchMode.COMPATIBILITY),
)
def test_direct_partial_publish_clears_old_authorization(tmp_path, launch_mode):
    coordinator = SmartReconnectAuthorizationCoordinator()
    first = make_target(tmp_path, 1)
    second = make_target(tmp_path, 2)
    coordinator.publish(
        make_source(first),
        ReconnectLaunchMode.IDENTITY_BOUND,
        (first,),
    )
    previous_epoch = coordinator.epoch

    with pytest.raises(ValueError, match="do not match source identities"):
        coordinator.publish(
            make_source(first, second),
            launch_mode,
            (first,),
        )

    assert coordinator.current_authorization() is None
    assert coordinator.state is ReconnectAuthorizationState.EMPTY
    assert coordinator.epoch == previous_epoch
    assert (
        coordinator.last_revocation_reason
        is ReconnectRevocationReason.PREPARATION_FAILED
    )


def test_rebinding_has_zero_authorization_and_full_publish_gets_new_epoch(tmp_path):
    coordinator = SmartReconnectAuthorizationCoordinator()
    target = make_target(tmp_path)
    first = coordinator.publish(
        make_source(target),
        ReconnectLaunchMode.IDENTITY_BOUND,
        (target,),
    )

    preparation_token = coordinator.begin_reprepare()

    assert coordinator.state is ReconnectAuthorizationState.REBINDING
    assert coordinator.current_authorization() is None
    second = coordinator.publish(
        make_source(target),
        ReconnectLaunchMode.IDENTITY_BOUND,
        (target,),
        preparation_token=preparation_token,
    )
    assert second.epoch == first.epoch + 1
    assert second.batch_id != first.batch_id
    assert ReconnectActionContext.from_batch_target(
        second,
        second.targets[0],
    ) == ReconnectActionContext.from_batch_target(first, first.targets[0])


def test_explicit_revoke_invalidates_preparation_token_and_blocks_late_publish(
    tmp_path,
):
    coordinator = SmartReconnectAuthorizationCoordinator()
    target = make_target(tmp_path)
    first = coordinator.publish(
        make_source(target),
        ReconnectLaunchMode.IDENTITY_BOUND,
        (target,),
    )
    preparation_token = coordinator.begin_reprepare()

    coordinator.revoke(ReconnectRevocationReason.EXPLICIT)

    with pytest.raises(
        ReconnectAuthorizationMismatchError,
        match="token",
    ):
        coordinator.publish_if_current(
            preparation_token,
            make_source(target),
            ReconnectLaunchMode.IDENTITY_BOUND,
            (target,),
        )
    assert coordinator.current_authorization() is None
    assert coordinator.state is ReconnectAuthorizationState.EMPTY
    assert coordinator.epoch == first.epoch
    assert (
        coordinator.last_revocation_reason
        is ReconnectRevocationReason.EXPLICIT
    )


def test_new_preparation_token_rejects_old_and_missing_without_losing_new(
    tmp_path,
):
    coordinator = SmartReconnectAuthorizationCoordinator()
    target = make_target(tmp_path)
    old_token = coordinator.begin_reprepare()
    current_token = coordinator.begin_reprepare()

    with pytest.raises(ReconnectAuthorizationMismatchError, match="token"):
        coordinator.publish_if_current(
            old_token,
            make_source(target),
            ReconnectLaunchMode.IDENTITY_BOUND,
            (target,),
        )
    with pytest.raises(ReconnectAuthorizationMismatchError, match="token"):
        coordinator.publish(
            make_source(target),
            ReconnectLaunchMode.IDENTITY_BOUND,
            (target,),
        )

    published = coordinator.publish_if_current(
        current_token,
        make_source(target),
        ReconnectLaunchMode.IDENTITY_BOUND,
        (target,),
    )
    assert coordinator.current_authorization() is published


def test_preparation_token_rejects_equal_serial_from_other_or_caller(
    tmp_path,
):
    coordinator = SmartReconnectAuthorizationCoordinator()
    other_coordinator = SmartReconnectAuthorizationCoordinator()
    target = make_target(tmp_path)
    current_token = coordinator.begin_reprepare()
    foreign_token = other_coordinator.begin_reprepare()
    forged_token = ReconnectPreparationToken(current_token.serial)
    assert foreign_token.serial == current_token.serial
    assert forged_token.serial == current_token.serial

    for untrusted_token in (foreign_token, forged_token):
        with pytest.raises(ReconnectAuthorizationMismatchError, match="token"):
            coordinator.publish_if_current(
                untrusted_token,
                make_source(target),
                ReconnectLaunchMode.IDENTITY_BOUND,
                (target,),
            )
        assert coordinator.state is ReconnectAuthorizationState.REBINDING
        assert coordinator.current_authorization() is None

    published = coordinator.publish_if_current(
        current_token,
        make_source(target),
        ReconnectLaunchMode.IDENTITY_BOUND,
        (target,),
    )
    assert coordinator.current_authorization() is published


def test_run_authorized_holds_lock_until_callback_returns(tmp_path):
    coordinator = SmartReconnectAuthorizationCoordinator()
    target = make_target(tmp_path)
    batch = coordinator.publish(
        make_source(target),
        ReconnectLaunchMode.IDENTITY_BOUND,
        (target,),
    )
    callback_entered = threading.Event()
    release_callback = threading.Event()
    revoked = threading.Event()

    def callback(current):
        assert current == batch.targets[0]
        callback_entered.set()
        assert release_callback.wait(2)
        return "done"

    result = []
    context = ReconnectActionContext.from_batch_target(
        batch,
        batch.targets[0],
    )
    worker = threading.Thread(
        target=lambda: result.append(
            coordinator.run_authorized(
                    epoch=context.authorization_epoch,
                    batch_id=context.batch_id,
                    source_generation=context.source_generation,
                fingerprint=target.fingerprint,
                character_id=target.character_id,
                instance=target.instance,
                callback=callback,
            )
        )
    )
    worker.start()
    assert callback_entered.wait(2)
    revoker = threading.Thread(
        target=lambda: (
            coordinator.revoke(ReconnectRevocationReason.SOURCE_CHANGED),
            revoked.set(),
        )
    )
    revoker.start()

    assert revoked.wait(0.05) is False
    release_callback.set()
    worker.join(2)
    revoker.join(2)

    assert result == ["done"]
    assert revoked.is_set()
    assert coordinator.current_authorization() is None


def test_run_authorized_rejects_stale_epoch_instance_and_stopped_state(tmp_path):
    coordinator = SmartReconnectAuthorizationCoordinator()
    target = make_target(tmp_path)
    batch = coordinator.publish(
        make_source(target),
        ReconnectLaunchMode.IDENTITY_BOUND,
        (target,),
    )
    context = ReconnectActionContext.from_batch_target(
        batch,
        batch.targets[0],
    )
    request = dict(
        epoch=context.authorization_epoch,
        batch_id=context.batch_id,
        source_generation=context.source_generation,
        fingerprint=target.fingerprint,
        character_id=target.character_id,
        instance=target.instance,
        callback=lambda current: current,
    )

    with pytest.raises(ReconnectAuthorizationMismatchError):
        coordinator.run_authorized(
            **{**request, "epoch": context.authorization_epoch + 1}
        )
    with pytest.raises(ReconnectAuthorizationMismatchError):
        coordinator.run_authorized(**{**request, "instance": make_instance(9)})

    coordinator.stop()

    with pytest.raises(ReconnectAuthorizationUnavailableError):
        coordinator.run_authorized(**request)
    with pytest.raises(ReconnectAuthorizationUnavailableError):
        coordinator.publish(
            make_source(target),
            ReconnectLaunchMode.IDENTITY_BOUND,
            (target,),
        )


def test_rebind_preserves_unchanged_grants_and_changes_only_changed_target(
    tmp_path,
):
    coordinator = SmartReconnectAuthorizationCoordinator()
    first = make_target(tmp_path, 1)
    second = make_target(tmp_path, 2)
    initial = coordinator.publish(
        make_source(first, second),
        ReconnectLaunchMode.IDENTITY_BOUND,
        (first, second),
    )
    initial_contexts = {
        target.fingerprint: ReconnectActionContext.from_batch_target(
            initial,
            target,
        )
        for target in initial.targets
    }
    changed_second = replace(second, instance=make_instance(9))
    preparation_token = coordinator.begin_reprepare()
    rebound = coordinator.publish(
        make_source(first, changed_second),
        ReconnectLaunchMode.IDENTITY_BOUND,
        (first, changed_second),
        preparation_token=preparation_token,
    )

    unchanged = rebound.target_for(first.fingerprint)
    changed = rebound.target_for(second.fingerprint)
    assert unchanged == initial.target_for(first.fingerprint)
    assert ReconnectActionContext.from_batch_target(
        rebound,
        unchanged,
    ) == initial_contexts[first.fingerprint]
    changed_context = ReconnectActionContext.from_batch_target(
        rebound,
        changed,
    )
    assert changed_context != initial_contexts[second.fingerprint]
    assert (
        changed_context.source_generation
        > initial_contexts[second.fingerprint].source_generation
    )
    assert rebound.source.source_generation == initial.source.source_generation

    with pytest.raises(ReconnectAuthorizationMismatchError):
        coordinator.run_authorized(
            epoch=initial_contexts[second.fingerprint].authorization_epoch,
            batch_id=initial_contexts[second.fingerprint].batch_id,
            source_generation=(
                initial_contexts[second.fingerprint].source_generation
            ),
            fingerprint=second.fingerprint,
            character_id=second.character_id,
            instance=second.instance,
            callback=lambda current: current,
        )


def test_explicit_reenable_advances_target_generation_and_rejects_old_grant(
    tmp_path,
):
    coordinator = SmartReconnectAuthorizationCoordinator()
    target = make_target(tmp_path)
    first = coordinator.publish(
        make_source(target),
        ReconnectLaunchMode.IDENTITY_BOUND,
        (target,),
    )
    old_context = ReconnectActionContext.from_batch_target(
        first,
        first.targets[0],
    )

    coordinator.revoke(ReconnectRevocationReason.EXPLICIT)
    second = coordinator.publish(
        make_source(target),
        ReconnectLaunchMode.IDENTITY_BOUND,
        (target,),
    )
    new_context = ReconnectActionContext.from_batch_target(
        second,
        second.targets[0],
    )

    assert new_context.authorization_epoch > old_context.authorization_epoch
    assert new_context.batch_id != old_context.batch_id
    assert new_context.source_generation > old_context.source_generation
    with pytest.raises(ReconnectAuthorizationMismatchError):
        coordinator.run_authorized(
            epoch=old_context.authorization_epoch,
            batch_id=old_context.batch_id,
            source_generation=old_context.source_generation,
            fingerprint=old_context.fingerprint,
            character_id=old_context.character_id,
            instance=old_context.instance,
            callback=lambda current: current,
        )


def test_continuous_session_never_adopts_changed_shortcut_seal(tmp_path):
    coordinator = SmartReconnectAuthorizationCoordinator()
    target = make_target(tmp_path)
    coordinator.publish(
        make_source(target),
        ReconnectLaunchMode.IDENTITY_BOUND,
        (target,),
    )
    changed = replace(
        target,
        shortcut_seal=replace(
            target.shortcut_seal,
            content_sha256="f" * 64,
        ),
    )

    preparation_token = coordinator.begin_reprepare()
    isolated = coordinator.publish(
        make_source(changed),
        ReconnectLaunchMode.IDENTITY_BOUND,
        (changed,),
        preparation_token=preparation_token,
    )

    assert isolated.targets == ()
    assert isolated.isolated_fingerprints == frozenset({target.fingerprint})

    coordinator.revoke(ReconnectRevocationReason.EXPLICIT)
    accepted = coordinator.publish(
        make_source(changed),
        ReconnectLaunchMode.IDENTITY_BOUND,
        (changed,),
    )
    assert accepted.target_for(target.fingerprint) is not None


def test_global_alias_owner_catalog_blocks_shared_complete_alias(tmp_path):
    target = replace(
        make_target(tmp_path),
        role_aliases=("SharedCompleteAlias",),
    )
    batch = make_batch(tmp_path, target)

    assert batch.unique_target_for_observed_identity(
        "SharedCompleteAlias",
        (
            ("SharedCompleteAlias", target.character_id),
            ("SharedCompleteAlias", "closed-character-owner"),
        ),
    ) is None
