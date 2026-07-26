import pytest

from domain.character import Character, CharacterImportance


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
