import pytest

from core.window_registry import WindowHealth, WindowRegistry
from domain.character import Character, CharacterImportance
from services.character_view_service import CharacterViewService, PlayerCharacterView
from services.identity_data_transaction_coordinator import (
    IdentityDataTransactionCoordinator,
)


def _character(
    character_id: str,
    display_name: str,
    level: int,
    importance: CharacterImportance,
) -> Character:
    return Character(character_id, display_name, level, importance)


def _view(
    registry: WindowRegistry,
    characters,
    *,
    coordinator: IdentityDataTransactionCoordinator | None = None,
    **options,
) -> CharacterViewService:
    return CharacterViewService(
        registry,
        characters,
        coordinator or IdentityDataTransactionCoordinator(),
        **options,
    )


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

    assert _view(registry, (profile,)).all() == (
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

    assert _view(registry, ()).all() == (
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
    view = _view(
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
        _view(registry, profiles)


def test_view_uses_project_wide_role_priority_instead_of_registry_order() -> None:
    registry = WindowRegistry()
    registry.register_character("reserve", "備用", group="14支")
    registry.register_character("secondary", "分號", group="14支")
    registry.register_character("primary-low", "主號低等", group="14支")
    registry.register_character("primary-high", "主號高等", group="14支")
    profiles = (
        _character("reserve", "備用", 200, CharacterImportance.RESERVE),
        _character(
            "secondary",
            "分號",
            300,
            CharacterImportance.SECONDARY,
        ),
        _character(
            "primary-low",
            "主號低等",
            120,
            CharacterImportance.PRIMARY,
        ),
        _character(
            "primary-high",
            "主號高等",
            160,
            CharacterImportance.PRIMARY,
        ),
    )

    assert [
        item.display_name
        for item in _view(registry, profiles).all()
    ] == ["主號高等", "主號低等", "分號", "備用"]


def test_view_uses_confirmed_group_order_inside_the_same_role() -> None:
    registry = WindowRegistry()
    registry.register_character("160", "160帥", group="14支")
    registry.register_character("100", "100古", group="14支")
    registry.register_character("120", "120古", group="14支")
    profiles = (
        _character("160", "160帥", 160, CharacterImportance.PRIMARY),
        _character("100", "100古", 100, CharacterImportance.SECONDARY),
        _character("120", "120古", 120, CharacterImportance.PRIMARY),
    )

    assert [
        item.display_name
        for item in _view(
            registry,
            profiles,
            confirmed_group_orders={
                "14支": ("100古", "120古", "160帥"),
            },
        ).all("14支")
    ] == ["120古", "160帥", "100古"]


def test_view_can_limit_results_to_current_group() -> None:
    registry = WindowRegistry()
    registry.register_character("a", "甲", group="甲組")
    registry.register_character("b", "乙", group="乙組")

    assert [
        item.display_name
        for item in _view(registry, ()).all("乙組")
    ] == ["乙"]


def test_each_public_view_read_uses_exactly_one_coordinator_snapshot(
    monkeypatch,
) -> None:
    registry = WindowRegistry()
    registry.register_character("a", "甲", group="甲組")
    coordinator = IdentityDataTransactionCoordinator()
    service = _view(registry, (), coordinator=coordinator)
    original_snapshot = coordinator.snapshot
    calls = 0

    def count_snapshot(reader):
        nonlocal calls
        calls += 1
        return original_snapshot(reader)

    monkeypatch.setattr(coordinator, "snapshot", count_snapshot)

    assert service.all("甲組")[0].display_name == "甲"
    assert calls == 1
    assert service.all_with_identities("甲組")[0][0] == "a"
    assert calls == 2


def test_stage_replace_exposes_profiles_inside_same_transaction() -> None:
    registry = WindowRegistry()
    coordinator = IdentityDataTransactionCoordinator()
    original = _character("a", "甲", 100, CharacterImportance.SECONDARY)
    replacement = _character("b", "乙", 120, CharacterImportance.PRIMARY)
    service = _view(registry, (original,), coordinator=coordinator)

    observed = coordinator.execute(
        lambda transaction: (
            service.stage_replace(transaction, (replacement,)),
            service.profiles_in_transaction(transaction),
        )[1]
    )

    assert observed == (original,)
    committed = coordinator.execute(service.profiles_in_transaction)
    assert committed == (replacement,)


def test_character_view_memory_failure_rolls_back_original_profiles(
    monkeypatch,
) -> None:
    registry = WindowRegistry()
    coordinator = IdentityDataTransactionCoordinator()
    original = _character("a", "甲", 100, CharacterImportance.SECONDARY)
    replacement = _character("b", "乙", 120, CharacterImportance.PRIMARY)
    service = _view(registry, (original,), coordinator=coordinator)
    install = service._install_characters
    calls = 0

    def fail_first(candidate):
        nonlocal calls
        calls += 1
        install(candidate)
        if calls == 1:
            raise OSError("view publish interrupted")

    monkeypatch.setattr(service, "_install_characters", fail_first)

    with pytest.raises(OSError, match="view publish interrupted"):
        service.replace_characters((replacement,))

    assert service._characters == {"a": original}
