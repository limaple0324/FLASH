import threading
from dataclasses import FrozenInstanceError, replace

import pytest

from core.smart_reconnect_authorization import (
    ReconnectAuthorizationBatch,
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


def test_identity_bound_batch_is_non_empty_and_immutable(tmp_path):
    batch = make_batch(tmp_path)

    assert batch.targets[0].character_id == "character-1"
    with pytest.raises(FrozenInstanceError):
        batch.epoch = 2
    with pytest.raises(ValueError, match="non-empty"):
        ReconnectAuthorizationBatch(
            epoch=1,
            batch_id="empty",
            source=ReconnectSourceIdentity(0, 0, "g", "group", ("c",)),
            launch_mode=ReconnectLaunchMode.IDENTITY_BOUND,
            targets=(),
        )


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
        ("importance", None),
        ("original_slot_index", None),
        ("original_line_number", None),
        ("shortcut_seal", None),
    ),
)
def test_every_launch_mode_rejects_every_missing_identity_field(
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
def test_every_launch_mode_rejects_cross_batch_alias_ambiguity(
    tmp_path,
    launch_mode,
):
    first = make_target(tmp_path, 1)
    second = replace(make_target(tmp_path, 2), role_aliases=("角色1另一人",))

    with pytest.raises(ValueError, match="ambiguous"):
        make_batch(tmp_path, first, second, launch_mode=launch_mode)


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

    coordinator.begin_reprepare()

    assert coordinator.state is ReconnectAuthorizationState.REBINDING
    assert coordinator.current_authorization() is None
    second = coordinator.publish(
        make_source(target),
        ReconnectLaunchMode.IDENTITY_BOUND,
        (target,),
    )
    assert second.epoch == first.epoch + 1
    assert second.batch_id != first.batch_id


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
        assert current is target
        callback_entered.set()
        assert release_callback.wait(2)
        return "done"

    result = []
    worker = threading.Thread(
        target=lambda: result.append(
            coordinator.run_authorized(
                epoch=batch.epoch,
                batch_id=batch.batch_id,
                source_generation=batch.source.source_generation,
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
    request = dict(
        epoch=batch.epoch,
        batch_id=batch.batch_id,
        source_generation=batch.source.source_generation,
        fingerprint=target.fingerprint,
        character_id=target.character_id,
        instance=target.instance,
        callback=lambda current: current,
    )

    with pytest.raises(ReconnectAuthorizationMismatchError):
        coordinator.run_authorized(**{**request, "epoch": batch.epoch + 1})
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
