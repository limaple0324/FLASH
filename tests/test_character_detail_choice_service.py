from dataclasses import FrozenInstanceError

import pytest

from core.window_registry import WindowRegistry
from domain.soul_stone import SoulStoneRecord
from domain.soul_stone_store import SoulStoneStore
from services.character_detail_choice_service import (
    CharacterDetailChoiceService,
    PlayerCharacterDetailChoice,
)
from services.character_detail_view_service import (
    CharacterDetailViewService,
    PlayerCharacterDetail,
)
from services.character_view_service import CharacterViewService
from services.soul_stone_service import SoulStoneService


def _detail_service(tmp_path) -> CharacterDetailViewService:
    registry = WindowRegistry()
    registry.register_character("char-a", "同名角色", group="甲組")
    registry.register_character("char-b", "同名角色", group="乙組")
    store = SoulStoneStore(tmp_path / "soul_stones.json")
    store.save(
        (
            SoulStoneRecord("char-a", "甲的紀錄"),
            SoulStoneRecord("char-b", "乙的紀錄"),
        )
    )
    return CharacterDetailViewService(
        CharacterViewService(registry, ()),
        SoulStoneService(store),
    )


def test_choices_bind_duplicate_names_to_exact_stable_identities(tmp_path) -> None:
    selected = []
    choices = CharacterDetailChoiceService(
        _detail_service(tmp_path),
        lambda character_id, detail: selected.append((character_id, detail)),
    ).all()

    assert tuple(choice.detail.group for choice in choices) == ("甲組", "乙組")
    assert tuple(choice.detail.soul_stone for choice in choices) == (
        "甲的紀錄",
        "乙的紀錄",
    )

    choices[1].select()
    choices[0].select()

    assert tuple(character_id for character_id, _detail in selected) == (
        "char-b",
        "char-a",
    )
    assert selected[0][1] is choices[1].detail
    assert selected[1][1] is choices[0].detail


def test_choice_exposes_no_character_identity_field(tmp_path) -> None:
    choice = CharacterDetailChoiceService(
        _detail_service(tmp_path),
        lambda _character_id, _detail: None,
    ).all()[0]

    assert not hasattr(choice, "character_id")
    assert not hasattr(choice.detail, "character_id")
    assert set(choice.__slots__) == {"detail", "select"}


def test_choice_is_read_only(tmp_path) -> None:
    choice = CharacterDetailChoiceService(
        _detail_service(tmp_path),
        lambda _character_id, _detail: None,
    ).all()[0]

    with pytest.raises(FrozenInstanceError):
        choice.detail = choice.detail


def test_choice_validates_player_snapshot_and_command() -> None:
    detail = PlayerCharacterDetail(
        display_name="小古",
        group=None,
        level=None,
        importance=None,
        role=None,
        note=None,
    )

    with pytest.raises(TypeError, match="PlayerCharacterDetail"):
        PlayerCharacterDetailChoice(object(), lambda: None)
    with pytest.raises(TypeError, match="select must be callable"):
        PlayerCharacterDetailChoice(detail, object())


def test_choice_service_requires_detail_service_and_handler(tmp_path) -> None:
    details = _detail_service(tmp_path)

    with pytest.raises(TypeError, match="CharacterDetailViewService"):
        CharacterDetailChoiceService(object(), lambda _id, _detail: None)
    with pytest.raises(TypeError, match="on_select must be callable"):
        CharacterDetailChoiceService(details, object())
