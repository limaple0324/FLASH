import json
from dataclasses import replace
from pathlib import Path

from core.window_registry import CharacterWindowRecord
from domain.character import Character, CharacterImportance
from services.group_configuration_service import (
    GroupConfiguration,
    GroupConfigurationEntry,
)
from services.smart_reconnect_target_identity_service import (
    SmartReconnectTargetIdentityService,
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
            Path(path).resolve(strict=False): self.values.get(
                Path(path).resolve(strict=False)
            )
            for path in paths
        }


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
):
    return CharacterWindowRecord(
        character_id,
        display_name,
        aliases=("舊別名",),
        role=CharacterImportance.PRIMARY.value,
    )


def make_service(tmp_path, groups, resolver, characters=None, records=None):
    return SmartReconnectTargetIdentityService(
        lambda: tuple(groups),
        resolver,
        lambda: tuple(characters or (make_character(),)),
        lambda: tuple(records or (make_record(),)),
        tmp_path / "smart_reconnect_targets.json",
    )


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
    assert resolver.calls == 1


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
    assert payload["targets"][FINGERPRINT] == {
        "character_id": CHARACTER_ID,
        "slot_index": 2,
        "line_number": 8,
    }
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

    assert target is not None
    assert target.original_slot_index is None
    assert target.original_line_number is None
    assert state_path.read_text(encoding="utf-8") == "{broken"
    assert tuple(tmp_path.glob("*.corrupt*")) == ()


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
    assert resolver.calls == 2


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
    assert resolver.calls == 2


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
