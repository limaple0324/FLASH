import pytest

from domain.character import (
    Character,
    CharacterImportance,
    character_importance_rank,
    character_priority_key,
)


def test_character_keeps_stable_identity_and_player_facing_fields():
    character = Character(
        character_id=" char-120-a ",
        display_name=" 敖云一煞 ",
        level=120,
        importance=CharacterImportance.SECONDARY,
    )

    assert character.character_id == "char-120-a"
    assert character.display_name == "敖云一煞"
    assert character.to_dict() == {
        "character_id": "char-120-a",
        "display_name": "敖云一煞",
        "level": 120,
        "importance": "次要",
    }


@pytest.mark.parametrize(
    ("character_id", "display_name", "level"),
    [("", "角色", 120), ("id", " ", 120), ("id", "角色", 0), ("id", "角色", True)],
)
def test_character_rejects_invalid_identity(character_id, display_name, level):
    with pytest.raises(ValueError):
        Character(character_id=character_id, display_name=display_name, level=level)


def test_character_importance_contains_the_confirmed_roles():
    assert [item.value for item in CharacterImportance] == ["主號", "次要", "備用"]


def test_project_wide_character_importance_order_is_primary_secondary_reserve():
    assert [
        character_importance_rank(item)
        for item in CharacterImportance
    ] == [0, 1, 2]


def test_character_priority_key_uses_role_then_level_then_stable_identity():
    characters = (
        Character("reserve", "備用", 200, CharacterImportance.RESERVE),
        Character("secondary", "分號", 300, CharacterImportance.SECONDARY),
        Character("primary-low", "主號低等", 120, CharacterImportance.PRIMARY),
        Character("primary-high", "主號高等", 160, CharacterImportance.PRIMARY),
    )

    assert [
        item.character_id
        for item in sorted(characters, key=character_priority_key)
    ] == ["primary-high", "primary-low", "secondary", "reserve"]
