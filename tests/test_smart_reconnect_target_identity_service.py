import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from core.window_registry import CharacterWindowRecord, WindowRegistry
from domain.character import Character, CharacterImportance
from core.smart_reconnect_authorization import (
    ReconnectAuthorizationTarget,
    ShortcutFileIdentity,
    ShortcutSeal,
)
from core.window_instance import WindowInstanceToken
from services.character_view_service import CharacterViewService
from services.group_configuration_service import (
    GroupConfiguration,
    GroupConfigurationEntry,
)
from services.smart_reconnect_target_identity_service import (
    SmartReconnectIdentityEvidence,
    SmartReconnectTargetIdentityService,
)
from services.identity_data_transaction_coordinator import (
    IdentityDataResource,
    IdentityDataTransactionCoordinator,
)


FINGERPRINT = "a" * 64
OTHER_FINGERPRINT = "b" * 64
CHARACTER_ID = "character-1"


class StaticShortcutResolver:
    def __init__(self, values):
        self.values = values
        self.calls = 0

    def resolve(self, paths):
        self.calls += 1
        return {
            resolved: self.values[resolved]
            for path in paths
            if (resolved := Path(path).resolve(strict=False)) in self.values
        }


class CoordinatorReadProbe:
    def __init__(self):
        self.coordinator = None
        self.calls = 0

    def assert_available(self):
        assert self.coordinator is not None
        completed = threading.Event()
        reader = threading.Thread(
            target=lambda: (
                self.coordinator.read_consistent(lambda: None),
                completed.set(),
            )
        )
        reader.start()
        assert completed.wait(0.5), "external evidence ran under identity lock"
        reader.join(1)
        assert reader.is_alive() is False
        self.calls += 1


class ProbedShortcutResolver(StaticShortcutResolver):
    def __init__(self, values, probe):
        super().__init__(values)
        self.probe = probe

    def resolve(self, paths):
        self.probe.assert_available()
        return super().resolve(paths)


class StaticGroupConfigurationService:
    def __init__(self, coordinator, groups):
        self.coordinator = coordinator
        self._groups = groups

    def groups(self):
        return tuple(self._groups)


class StaticRegistry(WindowRegistry):
    def __init__(self, records):
        super().__init__()
        self._records = {record.character_id: record for record in records}


def make_entry(
    path: Path,
    *,
    entry_id: str = CHARACTER_ID,
    role_id: str = "主號別名",
):
    return GroupConfigurationEntry(
        entry_id=entry_id,
        display_name=path.stem,
        shortcut_path=path,
        role="主視窗",
        order=1,
        role_id=role_id,
    )


def make_group(name: str, entries):
    return GroupConfiguration(
        group_id=f"group-{name}",
        name=name,
        entries=tuple(entries),
    )


def make_character(
    character_id: str = CHARACTER_ID,
    display_name: str = "登記主號",
):
    return Character(
        character_id,
        display_name,
        120,
        CharacterImportance.PRIMARY,
    )


def make_record(
    character_id: str = CHARACTER_ID,
    display_name: str = "登記主號",
    *,
    aliases=("舊別名",),
):
    return CharacterWindowRecord(
        character_id,
        display_name,
        aliases=aliases,
        role=CharacterImportance.PRIMARY.value,
    )


def make_service(
    tmp_path,
    groups,
    resolver,
    characters=None,
    records=None,
    ungrouped_shortcut_provider=None,
    ungrouped_shortcut_catalog_provider=None,
):
    coordinator = IdentityDataTransactionCoordinator()
    registry = StaticRegistry(records or (make_record(),))
    character_view = CharacterViewService(
        registry,
        tuple(characters or (make_character(),)),
        coordinator,
    )
    return SmartReconnectTargetIdentityService(
        coordinator,
        StaticGroupConfigurationService(coordinator, groups),
        character_view,
        registry,
        resolver,
        tmp_path / "smart_reconnect_targets.json",
        ungrouped_shortcut_provider=ungrouped_shortcut_provider,
        ungrouped_shortcut_catalog_provider=(
            ungrouped_shortcut_catalog_provider
        ),
    )


def test_public_target_reads_and_remember_resolve_outside_identity_lock(
    tmp_path,
):
    shortcut = tmp_path / "role.lnk"
    probe = CoordinatorReadProbe()
    resolver = ProbedShortcutResolver(
        {shortcut.resolve(): FINGERPRINT},
        probe,
    )
    service = make_service(
        tmp_path,
        (make_group("one", (make_entry(shortcut),)),),
        resolver,
    )
    probe.coordinator = service.coordinator

    assert service.target_for(FINGERPRINT) is not None
    assert len(service.targets_for_group("one")) == 1
    assert service.remember_verified_slot(
        FINGERPRINT,
        CHARACTER_ID,
        0,
    ) is True
    assert probe.calls >= 3


def test_ungrouped_catalog_provider_and_resolver_run_outside_identity_lock(
    tmp_path,
):
    shortcut = tmp_path / "登記主號.lnk"
    probe = CoordinatorReadProbe()
    resolver = ProbedShortcutResolver(
        {shortcut.resolve(): FINGERPRINT},
        probe,
    )

    def catalog():
        probe.assert_available()
        return (shortcut,)

    def shortcut_for(_fingerprint):
        probe.assert_available()
        return shortcut

    service = make_service(
        tmp_path,
        (),
        resolver,
        ungrouped_shortcut_provider=shortcut_for,
        ungrouped_shortcut_catalog_provider=catalog,
    )
    probe.coordinator = service.coordinator

    assert service.target_for(FINGERPRINT) is not None
    assert probe.calls >= 4


def test_duplicate_group_membership_collapses_to_one_stable_target(tmp_path):
    shortcut = tmp_path / "role.lnk"
    resolver = StaticShortcutResolver({shortcut.resolve(): FINGERPRINT})
    service = make_service(
        tmp_path,
        (
            make_group("one", (make_entry(shortcut),)),
            make_group("two", (make_entry(shortcut),)),
        ),
        resolver,
    )

    target = service.target_for(FINGERPRINT)

    assert target is not None
    assert target.character_id == CHARACTER_ID
    assert target.importance is CharacterImportance.PRIMARY
    assert target.matches_observed_identity("登記主號") is True
    assert target.matches_observed_identity("主號別名") is True
    assert target.matches_observed_identity("舊別名") is True
    assert resolver.calls == 1
    assert service.target_for(FINGERPRINT) == target
    assert resolver.calls == 2


def test_same_fingerprint_bound_to_different_characters_fails_closed(tmp_path):
    first = tmp_path / "one.lnk"
    second = tmp_path / "two.lnk"
    resolver = StaticShortcutResolver(
        {first.resolve(): FINGERPRINT, second.resolve(): FINGERPRINT}
    )
    service = make_service(
        tmp_path,
        (
            make_group(
                "one",
                (
                    make_entry(first),
                    make_entry(second, entry_id="character-2"),
                ),
            ),
        ),
        resolver,
        characters=(make_character(), make_character("character-2", "次要角色")),
        records=(make_record(), make_record("character-2", "次要角色")),
    )

    assert service.target_for(FINGERPRINT) is None


def test_unregistered_entry_sharing_a_fingerprint_blocks_the_valid_entry(tmp_path):
    first = tmp_path / "one.lnk"
    second = tmp_path / "two.lnk"
    resolver = StaticShortcutResolver(
        {first.resolve(): FINGERPRINT, second.resolve(): FINGERPRINT}
    )
    service = make_service(
        tmp_path,
        (
            make_group(
                "one",
                (
                    make_entry(first),
                    make_entry(second, entry_id="missing-character"),
                ),
            ),
        ),
        resolver,
    )

    assert service.target_for(FINGERPRINT) is None


def test_character_and_registry_identity_must_agree(tmp_path):
    shortcut = tmp_path / "role.lnk"
    resolver = StaticShortcutResolver({shortcut.resolve(): FINGERPRINT})
    service = make_service(
        tmp_path,
        (make_group("one", (make_entry(shortcut),)),),
        resolver,
        records=(make_record(display_name="另一個角色"),),
    )

    assert service.target_for(FINGERPRINT) is None


def test_verified_slot_and_line_persist_without_role_names(tmp_path):
    shortcut = tmp_path / "role.lnk"
    resolver = StaticShortcutResolver({shortcut.resolve(): FINGERPRINT})
    service = make_service(
        tmp_path,
        (make_group("one", (make_entry(shortcut),)),),
        resolver,
    )

    assert service.remember_verified_slot(FINGERPRINT, CHARACTER_ID, 2) is True
    assert service.remember_verified_line(FINGERPRINT, CHARACTER_ID, 8) is True

    state_path = tmp_path / "smart_reconnect_targets.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    serialized = state_path.read_text(encoding="utf-8")
    saved = payload["targets"][FINGERPRINT]
    assert saved["character_id"] == CHARACTER_ID
    assert saved["slot_index"] == 2
    assert saved["line_number"] == 8
    assert saved["status"] == "confirmed"
    assert saved["verified_aliases"] == []
    assert saved["evidence_revision"] == 0
    assert "登記主號" not in serialized
    assert "主號別名" not in serialized
    assert "舊別名" not in serialized

    reloaded = make_service(
        tmp_path,
        (make_group("one", (make_entry(shortcut),)),),
        resolver,
    )
    target = reloaded.target_for(FINGERPRINT)
    assert target is not None
    assert target.original_slot_index == 2
    assert target.original_line_number == 8


def test_stale_saved_character_never_authorizes_new_target(tmp_path):
    shortcut = tmp_path / "role.lnk"
    state_path = tmp_path / "smart_reconnect_targets.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "targets": {
                    FINGERPRINT: {
                        "character_id": "old-character",
                        "slot_index": 2,
                        "line_number": 8,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    resolver = StaticShortcutResolver({shortcut.resolve(): FINGERPRINT})
    service = make_service(
        tmp_path,
        (make_group("one", (make_entry(shortcut),)),),
        resolver,
    )

    target = service.target_for(FINGERPRINT)

    assert target is not None
    assert target.character_id == CHARACTER_ID
    assert target.original_slot_index is None
    assert target.original_line_number is None


def test_corrupt_state_fails_closed_without_renaming_user_file(tmp_path):
    shortcut = tmp_path / "role.lnk"
    state_path = tmp_path / "smart_reconnect_targets.json"
    state_path.write_text("{broken", encoding="utf-8")
    resolver = StaticShortcutResolver({shortcut.resolve(): FINGERPRINT})
    service = make_service(
        tmp_path,
        (make_group("one", (make_entry(shortcut),)),),
        resolver,
    )

    target = service.target_for(FINGERPRINT)

    assert target is None
    source = service.coordinator.capture_snapshot(
        service.capture_source_snapshot_in_current
    )
    resolution = service.resolve_for_fingerprints_from_source_snapshot(
        (FINGERPRINT,),
        source.value,
    )
    assert resolution.targets == ()
    assert resolution.pending_candidates == ()
    assert resolution.verification_candidates == ()
    assert service.remember_verified_slot(FINGERPRINT, CHARACTER_ID, 1) is False
    assert state_path.read_text(encoding="utf-8") == "{broken"
    assert tuple(tmp_path.glob("*.corrupt*")) == ()


@pytest.mark.parametrize(
    "invalid_shape",
    (
        "alias_without_evidence",
        "path_without_evidence",
        "complete_evidence_without_alias",
    ),
)
def test_v2_impossible_confirmed_evidence_is_corrupt(
    tmp_path,
    invalid_shape,
):
    fingerprint = "a" * 64
    shortcut = tmp_path / "unregistered-0.lnk"
    state_path = tmp_path / "smart_reconnect_targets.json"
    target_state = {
        "character_id": "smart-reconnect-fixed",
        "slot_index": None,
        "line_number": None,
        "verified_aliases": (
            ["100古"] if invalid_shape == "alias_without_evidence" else []
        ),
        "status": "confirmed",
        "shortcut_path": str(shortcut.resolve()),
        "instance": None,
        "shortcut_seal": None,
        "evidence_alias": None,
        "identity_generation": None,
        "config_revision": None,
        "evidence_revision": 0,
    }
    if invalid_shape == "complete_evidence_without_alias":
        target_state.update(
            instance={
                "handle": 100,
                "process_id": 200,
                "thread_id": 300,
                "window_class": "ShockwaveFlash",
                "rect": [0, 0, 800, 600],
                "minimized": False,
                "process_lifecycle_token": 400,
            },
            shortcut_seal={
                "file_identity": {
                    "normalized_path": str(shortcut.resolve()),
                    "volume_serial_number": 50,
                    "file_index": 60,
                },
                "content_sha256": "1" * 64,
                "launch_fingerprint": fingerprint,
            },
            evidence_alias="100古",
            identity_generation=1,
            config_revision=1,
            evidence_revision=1,
        )
    original = (
        json.dumps(
            {
                "version": 2,
                "targets": {fingerprint: target_state},
            },
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    state_path.write_bytes(original)

    service, fingerprints, _paths, _instances, _seals, _aliases = (
        _pending_identity_fixture(tmp_path, ("100古",))
    )
    source = service.coordinator.capture_snapshot(
        service.capture_source_snapshot_in_current
    )
    resolution = service.resolve_for_fingerprints_from_source_snapshot(
        fingerprints,
        source.value,
    )

    assert source.value.state_writable is False
    assert resolution.targets == ()
    assert service.target_for(fingerprint) is None
    assert ("100古", "smart-reconnect-fixed") not in (
        service.observed_identity_alias_catalog()
    )
    assert service.remember_verified_slot(
        fingerprint,
        "smart-reconnect-fixed",
        1,
    ) is False
    assert state_path.read_bytes() == original


def test_short_or_conflicting_observed_alias_is_never_accepted(tmp_path):
    shortcut = tmp_path / "role.lnk"
    resolver = StaticShortcutResolver({shortcut.resolve(): FINGERPRINT})
    service = make_service(
        tmp_path,
        (make_group("one", (make_entry(shortcut),)),),
        resolver,
    )
    target = service.target_for(FINGERPRINT)

    assert target is not None
    assert target.matches_observed_identity("登") is False
    assert target.matches_observed_identity("登記主…") is True
    assert target.matches_observed_identity("不同角色") is False
    assert service.remember_verified_slot(FINGERPRINT, CHARACTER_ID, True) is False


def test_changed_group_sources_refresh_the_cached_fingerprint_index(tmp_path):
    first = tmp_path / "one.lnk"
    second = tmp_path / "two.lnk"
    groups = [make_group("one", (make_entry(first),))]
    resolver = StaticShortcutResolver(
        {first.resolve(): FINGERPRINT, second.resolve(): OTHER_FINGERPRINT}
    )
    service = make_service(tmp_path, groups, resolver)

    assert service.target_for(FINGERPRINT) is not None
    groups[:] = [make_group("one", (make_entry(second),))]

    assert service.target_for(FINGERPRINT) is None
    assert service.target_for(OTHER_FINGERPRINT) is not None
    assert resolver.calls == 3


def test_rewritten_shortcut_refreshes_the_cached_fingerprint_index(tmp_path):
    shortcut = tmp_path / "role.lnk"
    shortcut.write_bytes(b"first-shortcut-content")
    resolver = StaticShortcutResolver({shortcut.resolve(): FINGERPRINT})
    service = make_service(
        tmp_path,
        (make_group("one", (make_entry(shortcut),)),),
        resolver,
    )

    assert service.target_for(FINGERPRINT) is not None
    shortcut.write_bytes(b"rewritten-shortcut-content-with-new-identity")
    resolver.values[shortcut.resolve()] = OTHER_FINGERPRINT

    assert service.target_for(FINGERPRINT) is None
    assert service.target_for(OTHER_FINGERPRINT) is not None
    assert resolver.calls == 3


def test_incomplete_resolver_result_is_retried_instead_of_cached(tmp_path):
    shortcut = tmp_path / "role.lnk"
    resolver = StaticShortcutResolver({})
    service = make_service(
        tmp_path,
        (make_group("one", (make_entry(shortcut),)),),
        resolver,
    )

    assert service.target_for(FINGERPRINT) is None
    resolver.values[shortcut.resolve()] = FINGERPRINT

    assert service.target_for(FINGERPRINT) is not None
    assert resolver.calls == 2


def test_same_complete_alias_keeps_targets_but_remains_in_ambiguity_catalog(
    tmp_path,
):
    first = tmp_path / "one.lnk"
    second = tmp_path / "two.lnk"
    resolver = StaticShortcutResolver(
        {first.resolve(): FINGERPRINT, second.resolve(): OTHER_FINGERPRINT}
    )
    service = make_service(
        tmp_path,
        (
            make_group(
                "one",
                (
                    make_entry(first, role_id="甲主號"),
                    make_entry(
                        second,
                        entry_id="character-2",
                        role_id="乙主號",
                    ),
                ),
            ),
        ),
        resolver,
        characters=(make_character(), make_character("character-2", "次要角色")),
        records=(
            make_record(aliases=("完整共用別名",)),
            make_record(
                "character-2",
                "次要角色",
                aliases=("完整共用別名",),
            ),
        ),
    )

    assert service.target_for(FINGERPRINT) is not None
    assert service.target_for(OTHER_FINGERPRINT) is not None
    assert (
        "完整共用別名",
        "character-1",
    ) in service.observed_identity_alias_catalog()


def test_shared_three_character_alias_prefix_keeps_both_full_targets(tmp_path):
    first = tmp_path / "one.lnk"
    second = tmp_path / "two.lnk"
    resolver = StaticShortcutResolver(
        {first.resolve(): FINGERPRINT, second.resolve(): OTHER_FINGERPRINT}
    )
    service = make_service(
        tmp_path,
        (
            make_group(
                "one",
                (
                    make_entry(first, role_id="甲主號"),
                    make_entry(
                        second,
                        entry_id="character-2",
                        role_id="乙主號",
                    ),
                ),
            ),
        ),
        resolver,
        characters=(make_character(), make_character("character-2", "次要角色")),
        records=(
            make_record(aliases=("共同角甲",)),
            make_record(
                "character-2",
                "次要角色",
                aliases=("共同角乙",),
            ),
        ),
    )

    assert service.target_for(FINGERPRINT) is not None
    assert service.target_for(OTHER_FINGERPRINT) is not None


def test_same_character_conflict_isolated_only_when_both_windows_are_requested(
    tmp_path,
):
    first = tmp_path / "one.lnk"
    second = tmp_path / "two.lnk"
    resolver = StaticShortcutResolver(
        {first.resolve(): FINGERPRINT, second.resolve(): OTHER_FINGERPRINT}
    )
    service = make_service(
        tmp_path,
        (
            make_group(
                "one",
                (
                    make_entry(first),
                    make_entry(second),
                ),
            ),
        ),
        resolver,
    )

    assert service.target_for(FINGERPRINT) is not None
    assert service.target_for(OTHER_FINGERPRINT) is not None
    resolved = service.targets_for_fingerprints(
        (FINGERPRINT, OTHER_FINGERPRINT)
    )
    assert resolved == ()


def test_group_label_never_decides_actual_window_eligibility(tmp_path):
    shortcut = tmp_path / "role.lnk"
    resolver = StaticShortcutResolver({shortcut.resolve(): FINGERPRINT})
    service = make_service(
        tmp_path,
        (make_group("one", (make_entry(shortcut),)),),
        resolver,
        records=(replace(make_record(), group="另一組"),),
    )

    assert service.target_for(FINGERPRINT) is not None


def test_first_ungrouped_window_uses_only_unique_exact_shortcut_name(tmp_path):
    shortcut = tmp_path / "登記主號.lnk"
    resolver = StaticShortcutResolver({shortcut.resolve(): FINGERPRINT})
    service = make_service(
        tmp_path,
        (),
        resolver,
        ungrouped_shortcut_provider=lambda fingerprint: (
            shortcut if fingerprint == FINGERPRINT else None
        ),
    )

    target = service.target_for(FINGERPRINT)

    assert target is not None
    assert target.character_id == CHARACTER_ID
    assert target.shortcut_path == shortcut.resolve()
    assert target.original_slot_index is None
    assert target.original_line_number is None


def test_ungrouped_shortcut_name_never_guesses_between_exact_aliases(tmp_path):
    shortcut = tmp_path / "完整共用別名.lnk"
    resolver = StaticShortcutResolver({shortcut.resolve(): FINGERPRINT})
    service = make_service(
        tmp_path,
        (),
        resolver,
        characters=(make_character(), make_character("character-2", "次要角色")),
        records=(
            make_record(aliases=("完整共用別名",)),
            make_record(
                "character-2",
                "次要角色",
                aliases=("完整共用別名",),
            ),
        ),
        ungrouped_shortcut_provider=lambda _fingerprint: shortcut,
    )

    assert service.target_for(FINGERPRINT) is None


def test_distinct_aliases_keep_both_character_targets_available(tmp_path):
    first = tmp_path / "one.lnk"
    second = tmp_path / "two.lnk"
    resolver = StaticShortcutResolver(
        {first.resolve(): FINGERPRINT, second.resolve(): OTHER_FINGERPRINT}
    )
    service = make_service(
        tmp_path,
        (
            make_group(
                "one",
                (
                    make_entry(first, role_id="甲主號"),
                    make_entry(
                        second,
                        entry_id="character-2",
                        role_id="乙主號",
                    ),
                ),
            ),
        ),
        resolver,
        characters=(make_character(), make_character("character-2", "次要角色")),
        records=(
            make_record(aliases=("北方甲",)),
            make_record(
                "character-2",
                "次要角色",
                aliases=("南方乙",),
            ),
        ),
    )

    assert service.target_for(FINGERPRINT) is not None
    assert service.target_for(OTHER_FINGERPRINT) is not None


def test_state_file_and_memory_commit_together(tmp_path):
    shortcut = tmp_path / "role.lnk"
    service = make_service(
        tmp_path,
        (make_group("one", (make_entry(shortcut),)),),
        StaticShortcutResolver({shortcut.resolve(): FINGERPRINT}),
    )

    assert service.remember_verified_slot(FINGERPRINT, CHARACTER_ID, 2) is True
    assert service.remember_verified_line(FINGERPRINT, CHARACTER_ID, 7) is True

    payload = json.loads(service.state_path.read_text(encoding="utf-8"))
    target = service.target_for(FINGERPRINT)
    assert payload["version"] == 2
    assert payload["targets"][FINGERPRINT]["character_id"] == CHARACTER_ID
    assert payload["targets"][FINGERPRINT]["slot_index"] == 2
    assert payload["targets"][FINGERPRINT]["line_number"] == 7
    assert payload["targets"][FINGERPRINT]["status"] == "confirmed"
    assert target.original_slot_index == 2
    assert target.original_line_number == 7


def test_state_write_exception_restores_file_and_memory(monkeypatch, tmp_path):
    shortcut = tmp_path / "role.lnk"
    service = make_service(
        tmp_path,
        (make_group("one", (make_entry(shortcut),)),),
        StaticShortcutResolver({shortcut.resolve(): FINGERPRINT}),
    )
    assert service.remember_verified_slot(FINGERPRINT, CHARACTER_ID, 1) is True
    before = service.state_path.read_bytes()
    import services.identity_data_transaction_coordinator as transaction_module

    original_replace = transaction_module.os.replace
    failed = False

    def replace_then_fail_once(source, destination):
        nonlocal failed
        original_replace(source, destination)
        if not failed:
            failed = True
            raise OSError("injected write failure")

    monkeypatch.setattr(transaction_module.os, "replace", replace_then_fail_once)

    assert service.remember_verified_line(FINGERPRINT, CHARACTER_ID, 6) is False

    assert service.state_path.read_bytes() == before
    target = service.target_for(FINGERPRINT)
    assert target.original_slot_index == 1
    assert target.original_line_number is None


def test_state_memory_publication_exception_restores_file_and_memory(
    monkeypatch,
    tmp_path,
):
    shortcut = tmp_path / "role.lnk"
    service = make_service(
        tmp_path,
        (make_group("one", (make_entry(shortcut),)),),
        StaticShortcutResolver({shortcut.resolve(): FINGERPRINT}),
    )
    assert service.remember_verified_slot(FINGERPRINT, CHARACTER_ID, 1) is True
    before = service.state_path.read_bytes()

    def fail_memory_publication(candidate):
        raise RuntimeError("injected memory failure")

    monkeypatch.setattr(service, "_install_saved", fail_memory_publication)

    with pytest.raises(RuntimeError, match="memory failure"):
        service.remember_verified_line(FINGERPRINT, CHARACTER_ID, 6)

    assert service.state_path.read_bytes() == before
    target = service.target_for(FINGERPRINT)
    assert target.original_slot_index == 1
    assert target.original_line_number is None


def test_concurrent_slot_and_line_updates_are_both_preserved(tmp_path):
    shortcut = tmp_path / "role.lnk"
    service = make_service(
        tmp_path,
        (make_group("one", (make_entry(shortcut),)),),
        StaticShortcutResolver({shortcut.resolve(): FINGERPRINT}),
    )
    start = threading.Barrier(3)
    results = []

    def remember_slot():
        start.wait()
        results.append(
            service.remember_verified_slot(FINGERPRINT, CHARACTER_ID, 0)
        )

    def remember_line():
        start.wait()
        results.append(
            service.remember_verified_line(FINGERPRINT, CHARACTER_ID, 8)
        )

    first = threading.Thread(target=remember_slot)
    second = threading.Thread(target=remember_line)
    first.start()
    second.start()
    start.wait()
    first.join(2)
    second.join(2)

    target = service.target_for(FINGERPRINT)
    assert results == [True, True]
    assert target.original_slot_index == 0
    assert target.original_line_number == 8


def test_stopped_identity_coordinator_rejects_state_write_without_changes(tmp_path):
    shortcut = tmp_path / "role.lnk"
    service = make_service(
        tmp_path,
        (make_group("one", (make_entry(shortcut),)),),
        StaticShortcutResolver({shortcut.resolve(): FINGERPRINT}),
    )
    assert service.coordinator.close_and_wait() is True

    assert service.remember_verified_slot(FINGERPRINT, CHARACTER_ID, 1) is False
    assert service.state_path.exists() is False


def test_target_state_has_no_direct_persist_or_replace_path():
    source = Path(
        "services/smart_reconnect_target_identity_service.py"
    ).read_text(encoding="utf-8")

    assert "def _persist" not in source
    assert "os.replace" not in source
    assert "transaction.stage_file(" in source
    assert "transaction.stage_memory(" in source


def test_group_and_desktop_paths_with_same_fingerprint_isolate_only_target(
    tmp_path,
):
    conflicted = tmp_path / "group-role.lnk"
    desktop_duplicate = tmp_path / "desktop-role.lnk"
    sibling = tmp_path / "sibling-role.lnk"
    resolver = StaticShortcutResolver(
        {
            conflicted.resolve(): FINGERPRINT,
            desktop_duplicate.resolve(): FINGERPRINT,
            sibling.resolve(): OTHER_FINGERPRINT,
        }
    )
    service = make_service(
        tmp_path,
        (
            make_group(
                "all",
                (
                    make_entry(conflicted),
                    make_entry(
                        sibling,
                        entry_id="character-2",
                        role_id="SiblingRole",
                    ),
                ),
            ),
        ),
        resolver,
        characters=(
            make_character(),
            make_character("character-2", "SiblingRole"),
        ),
        records=(
            make_record(),
            make_record(
                "character-2",
                "SiblingRole",
                aliases=("SiblingRole",),
            ),
        ),
        ungrouped_shortcut_catalog_provider=lambda: (
            desktop_duplicate,
        ),
    )

    assert service.target_for(FINGERPRINT) is None
    assert service.target_for(OTHER_FINGERPRINT) is not None


def test_fourteen_group_entries_with_one_unopened_bad_shortcut_keep_five_targets(
    tmp_path,
):
    paths = tuple(tmp_path / f"role-{index}.lnk" for index in range(14))
    fingerprints = tuple(f"{index + 1:064x}" for index in range(14))
    resolver_values = {
        path.resolve(): fingerprint
        for path, fingerprint in zip(paths, fingerprints)
    }
    resolver_values.pop(paths[9].resolve())
    entries = tuple(
        make_entry(
            path,
            entry_id=f"character-{index}",
            role_id=f"Role{index}",
        )
        for index, path in enumerate(paths)
    )
    characters = tuple(
        make_character(f"character-{index}", f"Role{index}")
        for index in range(14)
    )
    records = tuple(
        make_record(
            f"character-{index}",
            f"Role{index}",
            aliases=(f"Role{index}",),
        )
        for index in range(14)
    )
    service = make_service(
        tmp_path,
        (make_group("fourteen", entries),),
        StaticShortcutResolver(resolver_values),
        characters=characters,
        records=records,
    )

    targets = service.targets_for_fingerprints(fingerprints[:5])

    assert len(targets) == 5
    assert {target.fingerprint for target in targets} == set(
        fingerprints[:5]
    )


def test_absent_ungrouped_target_ignores_ordinary_desktop_shortcut(
    tmp_path,
):
    shortcut = tmp_path / "主要角色.lnk"
    ordinary = tmp_path / "ordinary-tool.lnk"
    resolver = StaticShortcutResolver(
        {shortcut.resolve(): FINGERPRINT}
    )
    service = make_service(
        tmp_path,
        (),
        resolver,
        characters=(make_character(display_name="主要角色"),),
        records=(
            make_record(
                display_name="主要角色",
                aliases=("主要角色",),
            ),
        ),
        ungrouped_shortcut_provider=lambda _fingerprint: shortcut,
        ungrouped_shortcut_catalog_provider=lambda: (
            shortcut,
            ordinary,
        ),
    )
    identity = service.target_for(FINGERPRINT)
    assert identity is not None
    authorization_target = ReconnectAuthorizationTarget(
        fingerprint=FINGERPRINT,
        instance=WindowInstanceToken(
            1,
            2,
            3,
            "ShockwaveFlash",
            (0, 0, 100, 100),
            False,
            4,
        ),
        character_id=identity.character_id,
        role_aliases=identity.role_aliases,
        importance=identity.importance,
        original_slot_index=identity.original_slot_index,
        original_line_number=identity.original_line_number,
        shortcut_seal=ShortcutSeal(
            ShortcutFileIdentity(shortcut, 1, 2),
            "c" * 64,
            FINGERPRINT,
        ),
    )

    assert service.retained_target_is_current(authorization_target) is True


def _pending_identity_fixture(tmp_path, aliases=("100古",)):
    fingerprints = tuple(
        chr(ord("a") + index) * 64 for index in range(len(aliases))
    )
    shortcuts = tuple(
        tmp_path / f"unregistered-{index}.lnk"
        for index in range(len(aliases))
    )
    paths_by_fingerprint = dict(zip(fingerprints, shortcuts))
    service = make_service(
        tmp_path,
        (),
        StaticShortcutResolver(
            {
                shortcut.resolve(): fingerprint
                for shortcut, fingerprint in zip(shortcuts, fingerprints)
            }
        ),
        ungrouped_shortcut_provider=paths_by_fingerprint.get,
        ungrouped_shortcut_catalog_provider=lambda: shortcuts,
    )
    instances = tuple(
        WindowInstanceToken(
            100 + index,
            200 + index,
            300 + index,
            "ShockwaveFlash",
            (0, 0, 800, 600),
            False,
            400 + index,
        )
        for index in range(len(aliases))
    )
    seals = tuple(
        ShortcutSeal(
            ShortcutFileIdentity(shortcut, 50, 60 + index),
            f"{index + 1}" * 64,
            fingerprint,
        )
        for index, (shortcut, fingerprint) in enumerate(
            zip(shortcuts, fingerprints)
        )
    )
    return service, fingerprints, shortcuts, instances, seals, tuple(aliases)


def _source_and_candidates(service, fingerprints):
    source = service.coordinator.capture_snapshot(
        service.capture_source_snapshot_in_current
    )
    resolution = service.resolve_for_fingerprints_from_source_snapshot(
        fingerprints,
        source.value,
    )
    candidates = tuple(
        dict.fromkeys(
            (
                *resolution.pending_candidates,
                *resolution.verification_candidates,
            )
        )
    )
    return source, resolution, candidates


def _record_complete_cycle(
    service,
    fingerprints,
    instances,
    seals,
    aliases,
    *,
    config_revision=7,
):
    source, resolution, candidates = _source_and_candidates(
        service,
        fingerprints,
    )
    evidence = tuple(
        SmartReconnectIdentityEvidence(candidate, instance, seal, alias)
        for candidate, instance, seal, alias in zip(
            candidates,
            instances,
            seals,
            aliases,
        )
    )
    result = service.record_identity_evidence(
        candidates,
        evidence,
        expected_generation=source.generation,
        expected_config_revision=config_revision,
        expected_source=source.value,
    )
    return source, resolution, candidates, result


def test_pending_identity_requires_two_cycles_and_identical_third_is_noop(
    tmp_path,
):
    fixture = _pending_identity_fixture(tmp_path)
    service, fingerprints, _paths, instances, seals, aliases = fixture

    first = _record_complete_cycle(
        service,
        fingerprints,
        instances,
        seals,
        aliases,
    )[-1]
    first_generation = service.coordinator.generation
    first_payload = json.loads(service.state_path.read_text(encoding="utf-8"))
    first_saved = first_payload["targets"][fingerprints[0]]

    assert first.source_current is True
    assert first.confirmed_fingerprints == frozenset()
    assert first_saved["status"] == "pending"
    assert first_saved["verified_aliases"] == []
    assert service.target_for(fingerprints[0]) is None
    assert all(
        owner != first_saved["character_id"]
        for _alias, owner in service.observed_identity_alias_catalog()
    )

    second = _record_complete_cycle(
        service,
        fingerprints,
        instances,
        seals,
        aliases,
    )[-1]
    second_generation = service.coordinator.generation
    second_bytes = service.state_path.read_bytes()
    second_saved = json.loads(second_bytes)["targets"][fingerprints[0]]

    assert second.confirmed_fingerprints == frozenset(fingerprints)
    assert second_saved["status"] == "confirmed"
    assert second_saved["character_id"] == first_saved["character_id"]
    assert second_saved["verified_aliases"] == [aliases[0]]
    assert second_generation == first_generation + 1
    assert service.target_for(fingerprints[0]) is not None

    third = _record_complete_cycle(
        service,
        fingerprints,
        instances,
        seals,
        aliases,
    )[-1]

    assert third.confirmed_fingerprints == frozenset(fingerprints)
    assert third.state_changed is False
    assert service.coordinator.generation == second_generation
    assert service.state_path.read_bytes() == second_bytes


@pytest.mark.parametrize(
    "changed_field",
    ("instance", "seal", "alias", "config", "generation"),
)
def test_confirmed_identity_change_resets_to_pending(tmp_path, changed_field):
    fixture = _pending_identity_fixture(tmp_path)
    service, fingerprints, _paths, instances, seals, aliases = fixture
    _record_complete_cycle(service, fingerprints, instances, seals, aliases)
    _record_complete_cycle(service, fingerprints, instances, seals, aliases)
    before = json.loads(service.state_path.read_text(encoding="utf-8"))[
        "targets"
    ][fingerprints[0]]

    changed_instances = instances
    changed_seals = seals
    changed_aliases = aliases
    config_revision = 7
    if changed_field == "instance":
        changed_instances = (replace(instances[0], handle=999),)
    elif changed_field == "seal":
        changed_seals = (
            replace(seals[0], content_sha256="f" * 64),
        )
    elif changed_field == "alias":
        changed_aliases = ("100靈",)
    elif changed_field == "config":
        config_revision = 8
    else:
        def stage_unrelated_identity_write(transaction):
            transaction.stage_memory(
                IdentityDataResource.CURRENT_GROUP,
                lambda: None,
                lambda: None,
                lambda _snapshot: None,
            )
            return True

        service.coordinator.execute(stage_unrelated_identity_write)

    result = _record_complete_cycle(
        service,
        fingerprints,
        changed_instances,
        changed_seals,
        changed_aliases,
        config_revision=config_revision,
    )[-1]
    after = json.loads(service.state_path.read_text(encoding="utf-8"))[
        "targets"
    ][fingerprints[0]]

    assert result.confirmed_fingerprints == frozenset()
    assert after["status"] == "pending"
    assert after["character_id"] == before["character_id"]
    assert after["verified_aliases"] == []
    assert after["evidence_revision"] > before["evidence_revision"]
    assert service.target_for(fingerprints[0]) is None


def test_persisted_full_shared_prefix_identities_coexist(tmp_path):
    fixture = _pending_identity_fixture(tmp_path, ("100古", "100靈"))
    service, fingerprints, _paths, instances, seals, aliases = fixture
    _record_complete_cycle(service, fingerprints, instances, seals, aliases)
    promoted = _record_complete_cycle(
        service,
        fingerprints,
        instances,
        seals,
        aliases,
    )[-1]

    assert promoted.confirmed_fingerprints == frozenset(fingerprints)
    targets = service.targets_for_fingerprints(fingerprints)
    assert {target.role_aliases for target in targets} == {
        ("100古",),
        ("100靈",),
    }
    catalog = service.observed_identity_alias_catalog()
    assert {alias for alias, _owner in catalog if alias.startswith("100")} == {
        "100古",
        "100靈",
    }


def test_pending_identity_survives_service_restart_with_same_opaque_id(
    tmp_path,
):
    fixture = _pending_identity_fixture(tmp_path)
    service, fingerprints, paths, instances, seals, aliases = fixture
    _record_complete_cycle(service, fingerprints, instances, seals, aliases)
    before = json.loads(service.state_path.read_text(encoding="utf-8"))[
        "targets"
    ][fingerprints[0]]

    reloaded = make_service(
        tmp_path,
        (),
        StaticShortcutResolver({paths[0].resolve(): fingerprints[0]}),
        ungrouped_shortcut_provider=lambda fingerprint: (
            paths[0] if fingerprint == fingerprints[0] else None
        ),
        ungrouped_shortcut_catalog_provider=lambda: paths,
    )
    _source, resolution, candidates = _source_and_candidates(
        reloaded,
        fingerprints,
    )
    after = json.loads(reloaded.state_path.read_text(encoding="utf-8"))[
        "targets"
    ][fingerprints[0]]

    assert before["status"] == after["status"] == "pending"
    assert before["character_id"] == after["character_id"]
    assert len(candidates) == 1
    assert resolution.targets == ()
    assert reloaded.target_for(fingerprints[0]) is None


def test_remembered_slot_and_line_preserve_confirmed_verified_aliases(tmp_path):
    fixture = _pending_identity_fixture(tmp_path)
    service, fingerprints, _paths, instances, seals, aliases = fixture
    _record_complete_cycle(service, fingerprints, instances, seals, aliases)
    _record_complete_cycle(service, fingerprints, instances, seals, aliases)
    target = service.target_for(fingerprints[0])
    assert target is not None

    assert service.remember_verified_slot(
        fingerprints[0],
        target.character_id,
        2,
    ) is True
    assert service.remember_verified_line(
        fingerprints[0],
        target.character_id,
        7,
    ) is True
    saved = json.loads(service.state_path.read_text(encoding="utf-8"))[
        "targets"
    ][fingerprints[0]]

    assert saved["verified_aliases"] == [aliases[0]]
    assert saved["slot_index"] == 2
    assert saved["line_number"] == 7


def test_version_one_state_loads_and_migrates_on_next_verified_write(tmp_path):
    shortcut = tmp_path / "role.lnk"
    state_path = tmp_path / "smart_reconnect_targets.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "targets": {
                    FINGERPRINT: {
                        "character_id": CHARACTER_ID,
                        "slot_index": 1,
                        "line_number": None,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    service = make_service(
        tmp_path,
        (make_group("one", (make_entry(shortcut),)),),
        StaticShortcutResolver({shortcut.resolve(): FINGERPRINT}),
    )

    assert service.target_for(FINGERPRINT).original_slot_index == 1
    assert service.remember_verified_line(FINGERPRINT, CHARACTER_ID, 6) is True
    migrated = json.loads(state_path.read_text(encoding="utf-8"))

    assert migrated["version"] == 2
    assert migrated["targets"][FINGERPRINT]["status"] == "confirmed"
    assert migrated["targets"][FINGERPRINT]["slot_index"] == 1
    assert migrated["targets"][FINGERPRINT]["line_number"] == 6
