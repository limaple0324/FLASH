from dataclasses import FrozenInstanceError

import pytest

from core.window_registry import WindowRegistry
from domain.character import Character, CharacterImportance
from services.character_detail_view_service import (
    CharacterDetailViewService,
    PlayerCharacterDetail,
)
from services.character_view_service import CharacterViewService


def _service(tmp_path) -> CharacterDetailViewService:
    registry = WindowRegistry()
    registry.register_character(
        "stable-character",
        "小古",
        group="14支",
        role="古",
        note="守紀優先",
    )
    profiles = (
        Character(
            "stable-character",
            "舊名稱",
            120,
            CharacterImportance.PRIMARY,
        ),
    )
    return CharacterDetailViewService(CharacterViewService(registry, profiles))


def test_detail_snapshot_contains_only_confirmed_player_fields(tmp_path) -> None:
    assert _service(tmp_path).all() == (
        PlayerCharacterDetail(
            display_name="小古",
            group="14支",
            level=120,
            importance="主號",
            role="古",
            note="守紀優先",
        ),
    )


def test_control_layer_can_pair_detail_with_stable_identity(tmp_path) -> None:
    service = _service(tmp_path)
    detail = service.all()[0]

    assert service.all_with_identities() == (("stable-character", detail),)
    assert not hasattr(detail, "character_id")


def test_get_by_identity_returns_latest_note(tmp_path) -> None:
    registry = WindowRegistry()
    registry.register_character("char-a", "角色甲", note="原本備註")
    service = CharacterDetailViewService(CharacterViewService(registry, ()))

    registry.set_note("char-a", "更新後備註")

    assert service.get_by_identity("char-a").note == "更新後備註"


def test_control_pairing_does_not_guess_from_duplicate_display_names(
    tmp_path,
) -> None:
    registry = WindowRegistry()
    registry.register_character("char-a", "同名角色", group="甲組", note="甲的備註")
    registry.register_character("char-b", "同名角色", group="乙組", note="乙的備註")
    service = CharacterDetailViewService(CharacterViewService(registry, ()))

    paired = service.all_with_identities()

    assert tuple(character_id for character_id, _detail in paired) == (
        "char-a",
        "char-b",
    )
    assert tuple(detail.note for _character_id, detail in paired) == (
        "甲的備註",
        "乙的備註",
    )
    assert all(not hasattr(detail, "character_id") for _, detail in paired)


def test_detail_snapshot_excludes_frozen_and_future_fields(tmp_path) -> None:
    detail = _service(tmp_path).all()[0]

    for name in (
        "character_id",
        "window_handle",
        "life_soul",
        "pet",
        "artifact",
        "obsidian",
        "inventory",
    ):
        assert not hasattr(detail, name)


def test_detail_snapshot_is_read_only(tmp_path) -> None:
    detail = _service(tmp_path).all()[0]

    with pytest.raises(FrozenInstanceError):
        detail.level = 160


def test_detail_service_requires_existing_read_only_services(tmp_path) -> None:
    with pytest.raises(TypeError, match="CharacterViewService"):
        CharacterDetailViewService(object())
