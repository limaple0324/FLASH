import json

from core.window_registry import WindowRegistry
from core.window_registry_store import WindowRegistryStore
from domain.character import CharacterImportance
from domain.character_store import CharacterStore
from services.character_view_service import CharacterViewService
from services.group_character_registration_service import (
    GroupCharacterRegistrationService,
)
from services.group_configuration_service import GroupConfigurationService


def _configuration(tmp_path):
    names = (
        "120古",
        "120靈",
        "120射",
        "120福",
        "120獵",
        "100古",
        "100靈",
        "100福",
        "100獵",
        "160福",
        "160帥",
        "大排",
        "和尚",
        "餐廳",
    )
    shortcuts = []
    for name in names:
        path = tmp_path / f"{name}.lnk"
        path.write_bytes(b"shortcut")
        shortcuts.append(path)
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "name": "14支",
                        "launch_entries": [
                            {"path": str(path)}
                            for path in shortcuts
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return GroupConfigurationService(
        tmp_path / "groups.json",
        legacy_config_path=legacy,
    )


def test_selected_group_populates_character_page_data_without_guessing_windows(
    tmp_path,
):
    registry = WindowRegistry()
    registry_store = WindowRegistryStore(tmp_path / "registry.json")
    character_store = CharacterStore(tmp_path / "characters.json")
    service = GroupCharacterRegistrationService(
        registry,
        registry_store,
        character_store,
        _configuration(tmp_path),
    )

    profiles = service.ensure_group("14支", ())
    views = CharacterViewService(registry, profiles).all("14支")

    assert len(views) == 14
    assert {item.display_name for item in views} >= {
        "100古",
        "120古",
        "160帥",
        "亞洛",
    }
    assert {
        (item.level, item.importance)
        for item in views
    } == {
        (100, CharacterImportance.SECONDARY.value),
        (120, CharacterImportance.PRIMARY.value),
        (160, CharacterImportance.PRIMARY.value),
    }
    assert registry_store.load().all()
    assert character_store.load() == profiles


def test_group_registration_is_idempotent_and_preserves_saved_note(tmp_path):
    registry = WindowRegistry()
    registry_store = WindowRegistryStore(tmp_path / "registry.json")
    character_store = CharacterStore(tmp_path / "characters.json")
    service = GroupCharacterRegistrationService(
        registry,
        registry_store,
        character_store,
        _configuration(tmp_path),
    )
    profiles = service.ensure_group("14支", ())
    character_id = next(
        record.character_id
        for record in registry.all()
        if record.display_name == "120古"
    )
    registry.set_note(character_id, "保留備註")
    registry_store.save(registry)

    repeated = service.ensure_group("14支", profiles)

    assert repeated == profiles
    assert len(registry.all()) == 14
    assert registry.get(character_id).note == "保留備註"


def test_existing_ungrouped_records_are_backfilled_without_new_identities(
    tmp_path,
):
    configuration = _configuration(tmp_path)
    group = configuration.group("14支")
    registry = WindowRegistry()
    registry_store = WindowRegistryStore(tmp_path / "registry.json")
    character_store = CharacterStore(tmp_path / "characters.json")
    for entry in group.entries:
        registry.register_character(
            entry.entry_id,
            (
                "亞洛"
                if entry.display_name == "160福"
                else entry.display_name
            ),
            note=(
                "原備註"
                if entry.display_name == "120古"
                else None
            ),
        )
    registry_store.save(registry)
    identities_before = {
        record.character_id for record in registry.all()
    }
    service = GroupCharacterRegistrationService(
        registry,
        registry_store,
        character_store,
        configuration,
    )

    profiles = service.ensure_group("14支", ())
    restored = registry_store.load()

    assert {
        record.character_id for record in restored.all()
    } == identities_before
    assert {record.group for record in restored.all()} == {"14支"}
    assert len(CharacterViewService(registry, profiles).all("14支")) == 14
    ancient = next(
        record
        for record in restored.all()
        if record.display_name == "120古"
    )
    assert ancient.note == "原備註"
    assert ancient.role == CharacterImportance.PRIMARY.value
