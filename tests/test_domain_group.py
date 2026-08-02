import pytest

from domain.character import Character
from domain.group import CharacterGroup


def _character(character_id: str, name: str, level: int = 120) -> Character:
    return Character(character_id=character_id, display_name=name, level=level)


def test_group_is_the_primary_container_for_multiple_characters():
    first = _character("a", "敖云一煞")
    second = _character("b", "120古")
    group = CharacterGroup(group_id="14-windows", name="14支", characters=(first, second))

    assert group.name == "14支"
    assert group.character_ids == ("a", "b")
    assert [item["display_name"] for item in group.to_dict()["characters"]] == ["敖云一煞", "120古"]


def test_group_add_character_returns_a_new_group_without_mutating_the_original():
    group = CharacterGroup(group_id="dimension", name="魔心次元組")
    updated = group.add_character(_character("c", "次元角色"))

    assert group.character_ids == ()
    assert updated.character_ids == ("c",)


def test_group_rejects_duplicate_character_identity():
    character = _character("same", "角色")
    with pytest.raises(ValueError):
        CharacterGroup(group_id="g", name="組別", characters=(character, character))
