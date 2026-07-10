import pytest

from core.window_registry import WindowHealth, WindowRegistry


def test_register_character_does_not_bind_a_window_automatically():
    registry = WindowRegistry()

    record = registry.register_character("160-ancient", "160古")

    assert record.display_name == "160古"
    assert record.handle is None
    assert record.confirmed is False
    assert record.health is WindowHealth.UNKNOWN


def test_confirm_window_records_current_observation():
    registry = WindowRegistry()
    registry.register_character("160-ancient", "160古")

    record = registry.confirm_window(
        "160-ancient",
        handle=321,
        process_id=9520,
        window_class="ShockwaveFlash",
        rect=(10, 20, 810, 620),
        health=WindowHealth.READY,
    )

    assert record.handle == 321
    assert record.process_id == 9520
    assert record.confirmed is True
    assert record.last_seen_utc


def test_duplicate_character_id_cannot_change_identity_silently():
    registry = WindowRegistry()
    registry.register_character("160-ancient", "160古")

    with pytest.raises(ValueError):
        registry.register_character("160-ancient", "120古")


def test_invalid_window_observation_is_rejected():
    registry = WindowRegistry()
    registry.register_character("160-ancient", "160古")

    with pytest.raises(ValueError):
        registry.confirm_window(
            "160-ancient",
            handle=0,
            rect=(0, 0, 100, 100),
            health=WindowHealth.READY,
        )

    with pytest.raises(ValueError):
        registry.confirm_window(
            "160-ancient",
            handle=10,
            rect=(100, 100, 100, 200),
            health=WindowHealth.READY,
        )


def test_mark_offline_clears_transient_window_binding():
    registry = WindowRegistry()
    registry.register_character("160-ancient", "160古")
    registry.confirm_window(
        "160-ancient",
        handle=321,
        rect=(10, 20, 810, 620),
        health=WindowHealth.READY,
    )

    offline = registry.mark_offline("160-ancient")

    assert offline.health is WindowHealth.OFFLINE
    assert offline.handle is None
    assert offline.confirmed is False


def test_registry_exports_player_facing_identity():
    registry = WindowRegistry()
    registry.register_character("100-ancient", "100古")
    registry.register_character("160-ancient", "160古")

    data = registry.to_dict()

    assert [item["display_name"] for item in data["characters"]] == ["100古", "160古"]
