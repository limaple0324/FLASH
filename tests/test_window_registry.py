import pytest

from core.window_registry import WindowHealth, WindowRegistry


def test_register_character_does_not_bind_a_window_automatically():
    registry = WindowRegistry()

    record = registry.register_character("160-ancient", "160古")

    assert record.display_name == "160古"
    assert record.handle is None
    assert record.confirmed is False
    assert record.health is WindowHealth.UNKNOWN
    assert record.created_at_utc


def test_register_character_can_generate_permanent_uuid():
    registry = WindowRegistry()

    record = registry.register_character(None, "160古")

    assert len(record.character_id) == 36
    assert registry.get(record.character_id).display_name == "160古"


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
    assert registry.character_for_handle(321).character_id == "160-ancient"


def test_same_window_cannot_bind_to_two_characters():
    registry = WindowRegistry()
    registry.register_character("character-a", "160古")
    registry.register_character("character-b", "160法")
    registry.confirm_window(
        "character-a",
        handle=321,
        rect=(0, 0, 800, 600),
        health=WindowHealth.READY,
    )

    with pytest.raises(ValueError, match="already bound"):
        registry.confirm_window(
            "character-b",
            handle=321,
            rect=(0, 0, 800, 600),
            health=WindowHealth.READY,
        )

    assert registry.get("character-a").handle == 321
    assert registry.get("character-b").handle is None


def test_offline_character_releases_window_for_another_character():
    registry = WindowRegistry()
    registry.register_character("character-a", "160古")
    registry.register_character("character-b", "160法")
    registry.confirm_window(
        "character-a",
        handle=321,
        rect=(0, 0, 800, 600),
        health=WindowHealth.READY,
    )

    registry.mark_offline("character-a")
    rebound = registry.confirm_window(
        "character-b",
        handle=321,
        rect=(0, 0, 800, 600),
        health=WindowHealth.WARNING,
    )

    assert rebound.handle == 321
    assert registry.character_for_handle(321).character_id == "character-b"


def test_duplicate_character_id_cannot_change_identity_silently():
    registry = WindowRegistry()
    registry.register_character("160-ancient", "160古")

    with pytest.raises(ValueError):
        registry.register_character("160-ancient", "120古")


def test_rename_keeps_identity_and_saves_previous_name_as_alias():
    registry = WindowRegistry()
    original = registry.register_character(None, "160古")

    renamed = registry.rename_character(original.character_id, "160戰神")

    assert renamed.character_id == original.character_id
    assert renamed.display_name == "160戰神"
    assert renamed.aliases == ("160古",)


def test_repeated_rename_keeps_unique_alias_history():
    registry = WindowRegistry()
    record = registry.register_character("character-a", "160古")
    registry.rename_character(record.character_id, "160古神")
    renamed = registry.rename_character(record.character_id, "160戰神")

    assert renamed.aliases == ("160古", "160古神")


def test_locked_character_rejects_rename():
    registry = WindowRegistry()
    record = registry.register_character("main-character", "主號", locked=True)

    with pytest.raises(PermissionError):
        registry.rename_character(record.character_id, "主號改名")


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

    assert data["schema_version"] == 2
    assert [item["display_name"] for item in data["characters"]] == ["100古", "160古"]


def test_schema_v1_data_migrates_without_trusting_old_handle():
    registry = WindowRegistry.from_dict(
        {
            "schema_version": 1,
            "characters": [
                {
                    "character_id": "legacy-160",
                    "display_name": "160古",
                    "handle": 999,
                    "process_id": 123,
                    "window_class": "ShockwaveFlash",
                    "rect": [0, 0, 800, 600],
                    "confirmed": True,
                }
            ],
        }
    )

    record = registry.get("legacy-160")
    assert record.display_name == "160古"
    assert record.aliases == ()
    assert record.handle is None
    assert record.confirmed is False
    assert record.process_id == 123
    assert registry.to_dict()["schema_version"] == 2
