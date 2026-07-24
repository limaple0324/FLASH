from dataclasses import FrozenInstanceError

import pytest

from core.window_registry import WindowRegistry
from domain.character import Character, CharacterImportance
from domain.soul_stone import SoulStoneRecord
from domain.soul_stone_store import SoulStoneStore
from services.character_detail_view_service import (
    CharacterDetailViewService,
    PlayerCharacterDetail,
)
from services.character_view_service import CharacterViewService
from services.soul_stone_service import SoulStoneService


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
    soul_stone_store = SoulStoneStore(tmp_path / "soul_stones.json")
    soul_stone_store.save(
        (SoulStoneRecord("stable-character", "本週先保留稀有靈魂石"),)
    )
    return CharacterDetailViewService(
        CharacterViewService(registry, profiles),
        SoulStoneService(soul_stone_store),
    )


def test_detail_snapshot_contains_only_confirmed_player_fields(tmp_path) -> None:
    assert _service(tmp_path).all() == (
        PlayerCharacterDetail(
            display_name="小古",
            group="14支",
            level=120,
            importance="主號",
            role="古",
            note="守紀優先",
            soul_stone="本週先保留稀有靈魂石",
        ),
    )


def test_detail_snapshot_does_not_guess_future_record_fields(tmp_path) -> None:
    detail = _service(tmp_path).all()[0]

    for name in (
        "character_id",
        "window_handle",
        "pet",
        "life_soul",
        "artifact",
        "inventory",
    ):
        assert not hasattr(detail, name)


def test_detail_snapshot_is_read_only(tmp_path) -> None:
    detail = _service(tmp_path).all()[0]

    with pytest.raises(FrozenInstanceError):
        detail.level = 160


def test_detail_service_requires_existing_read_only_services(tmp_path) -> None:
    soul_stones = SoulStoneService(
        SoulStoneStore(tmp_path / "soul_stones.json")
    )
    with pytest.raises(TypeError, match="CharacterViewService"):
        CharacterDetailViewService(object(), soul_stones)
    with pytest.raises(TypeError, match="SoulStoneService"):
        CharacterDetailViewService(
            CharacterViewService(WindowRegistry(), ()),
            object(),
        )
