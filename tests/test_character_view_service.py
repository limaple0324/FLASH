import pytest

from core.window_registry import WindowHealth, WindowRegistry
from domain.character import Character, CharacterImportance
from services.character_view_service import CharacterViewService, PlayerCharacterView


def _character(
    character_id: str,
    display_name: str,
    level: int,
    importance: CharacterImportance,
) -> Character:
    return Character(character_id, display_name, level, importance)


def test_view_joins_role_data_by_stable_identity_not_display_name() -> None:
    registry = WindowRegistry()
    registry.register_character(
        "same-character",
        "目前顯示名稱",
        group="14支",
        role="古",
        note="守紀優先",
    )
    profile = _character(
        "same-character",
        "舊角色名稱",
        120,
        CharacterImportance.PRIMARY,
    )

    assert CharacterViewService(registry, (profile,)).all() == (
        PlayerCharacterView(
            display_name="目前顯示名稱",
            group="14支",
            level=120,
            importance="主號",
            role="古",
            note="守紀優先",
        ),
    )


def test_view_keeps_unmatched_registered_character_visible() -> None:
    registry = WindowRegistry()
    registry.register_character("registered-only", "待補資料", group="14支")

    assert CharacterViewService(registry, ()).all() == (
        PlayerCharacterView(
            display_name="待補資料",
            group="14支",
            level=None,
            importance=None,
            role=None,
            note=None,
        ),
    )


def test_view_never_contains_window_or_identity_internals() -> None:
    registry = WindowRegistry()
    registry.register_character("private-id", "小古", group="14支")
    registry.confirm_window(
        "private-id",
        handle=321,
        process_id=9520,
        window_class="GameWindow",
        rect=(0, 0, 800, 600),
        health=WindowHealth.READY,
    )
    view = CharacterViewService(
        registry,
        (
            _character(
                "private-id",
                "小古",
                100,
                CharacterImportance.SECONDARY,
            ),
        ),
    ).all()[0]

    assert not hasattr(view, "character_id")
    assert not hasattr(view, "handle")
    assert not hasattr(view, "process_id")
    assert not hasattr(view, "window_class")
    assert not hasattr(view, "rect")
    assert not hasattr(view, "health")


def test_view_rejects_duplicate_stable_character_profiles() -> None:
    registry = WindowRegistry()
    profiles = (
        _character("same", "角色甲", 100, CharacterImportance.PRIMARY),
        _character("same", "角色乙", 120, CharacterImportance.RESERVE),
    )

    with pytest.raises(ValueError, match="Duplicate stable character ID"):
        CharacterViewService(registry, profiles)
