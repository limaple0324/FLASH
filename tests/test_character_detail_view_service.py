from dataclasses import FrozenInstanceError

import pytest

from core.window_registry import WindowRegistry
from domain.character import Character, CharacterImportance
from services.character_detail_view_service import (
    CharacterDetailViewService,
    PlayerCharacterDetail,
)
from services.character_view_service import CharacterViewService


def _service() -> CharacterDetailViewService:
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


def test_detail_snapshot_contains_only_confirmed_player_fields() -> None:
    assert _service().all() == (
        PlayerCharacterDetail(
            display_name="小古",
            group="14支",
            level=120,
            importance="主號",
            role="古",
            note="守紀優先",
        ),
    )


def test_detail_snapshot_does_not_guess_future_record_fields() -> None:
    detail = _service().all()[0]

    for name in (
        "character_id",
        "window_handle",
        "pet",
        "soul_stone",
        "life_soul",
        "artifact",
        "inventory",
    ):
        assert not hasattr(detail, name)


def test_detail_snapshot_is_read_only() -> None:
    detail = _service().all()[0]

    with pytest.raises(FrozenInstanceError):
        detail.level = 160


def test_detail_service_requires_existing_read_only_character_service() -> None:
    with pytest.raises(TypeError, match="CharacterViewService"):
        CharacterDetailViewService(object())
