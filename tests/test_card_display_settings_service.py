import json

import pytest

from cards.service import CardService
from cards.settings import (
    CARD_LIFETIME_SECONDS_CONFIG_KEY,
    CardDisplaySettings,
    CardDisplaySettingsResolution,
)
from config.config_manager import ConfigManager
from main import build_services
from services.app_context import AppContext
from services.card_display_settings_service import CardDisplaySettingsService


def _resolution(seconds: int = 30, *, configured: bool = False):
    return CardDisplaySettingsResolution(
        settings=CardDisplaySettings(seconds),
        configured=configured,
        recovered_from_invalid=False,
    )


def test_update_persists_and_applies_new_lifetime(tmp_path) -> None:
    config = ConfigManager(tmp_path / "settings.json")
    cards = CardService()
    changes = []
    service = CardDisplaySettingsService(
        config,
        cards,
        _resolution(),
        on_changed=changes.append,
    )

    result = service.update_lifetime_seconds(75)

    assert result.settings.lifetime_seconds == 75
    assert result.configured is True
    assert result.recovered_from_invalid is False
    assert service.snapshot() is result
    assert cards.settings is result.settings
    assert changes == [result]
    assert json.loads(config.config_path.read_text(encoding="utf-8"))[
        CARD_LIFETIME_SECONDS_CONFIG_KEY
    ] == 75
    assert not config.config_path.with_suffix(".json.tmp").exists()


@pytest.mark.parametrize("value", [True, "75", 0, -1])
def test_invalid_update_does_not_change_or_save_current_setting(
    tmp_path,
    value,
) -> None:
    config = ConfigManager(tmp_path / "settings.json")
    initial = _resolution(45, configured=True)
    cards = CardService(initial.settings)
    service = CardDisplaySettingsService(config, cards, initial)
    before = config.config_path.read_text(encoding="utf-8")

    with pytest.raises((TypeError, ValueError)):
        service.update_lifetime_seconds(value)

    assert service.snapshot() is initial
    assert cards.settings is initial.settings
    assert config.config_path.read_text(encoding="utf-8") == before


def test_save_failure_rolls_back_memory_and_skips_notification(
    tmp_path,
    monkeypatch,
) -> None:
    config = ConfigManager(tmp_path / "settings.json")
    config.set(CARD_LIFETIME_SECONDS_CONFIG_KEY, 45)
    initial = _resolution(45, configured=True)
    cards = CardService(initial.settings)
    changes = []
    service = CardDisplaySettingsService(
        config,
        cards,
        initial,
        on_changed=changes.append,
    )

    def fail_save() -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(config, "save", fail_save)

    with pytest.raises(OSError, match="disk unavailable"):
        service.update_lifetime_seconds(75)

    assert config.data[CARD_LIFETIME_SECONDS_CONFIG_KEY] == 45
    assert service.snapshot() is initial
    assert cards.settings is initial.settings
    assert changes == []
    assert json.loads(config.config_path.read_text(encoding="utf-8"))[
        CARD_LIFETIME_SECONDS_CONFIG_KEY
    ] == 45


def test_build_services_registers_and_keeps_current_settings_in_sync(tmp_path) -> None:
    build_services(root=tmp_path)
    service = AppContext.get(CardDisplaySettingsService)

    result = service.update_lifetime_seconds(90)

    assert AppContext.get(CardDisplaySettings) is result.settings
    assert AppContext.get(CardDisplaySettingsResolution) is result
    assert AppContext.get(CardService).settings is result.settings
    assert ConfigManager(tmp_path / "config" / "settings.json").get(
        CARD_LIFETIME_SECONDS_CONFIG_KEY
    ) == 90
