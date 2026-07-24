import json

from cards.service import CardService
from cards.settings import (
    CARD_LIFETIME_SECONDS_CONFIG_KEY,
    DEFAULT_CARD_LIFETIME_SECONDS,
    CardDisplaySettings,
    CardDisplaySettingsResolution,
)
from main import build_services
from services.app_context import AppContext


def _write_settings(tmp_path, value) -> None:
    config_path = tmp_path / "config" / "settings.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps({CARD_LIFETIME_SECONDS_CONFIG_KEY: value}),
        encoding="utf-8",
    )


def test_missing_lifetime_setting_uses_unconfigured_default(tmp_path) -> None:
    build_services(root=tmp_path)

    settings = AppContext.get(CardDisplaySettings)
    resolution = AppContext.get(CardDisplaySettingsResolution)
    cards = AppContext.get(CardService)

    assert settings.lifetime_seconds == DEFAULT_CARD_LIFETIME_SECONDS
    assert cards.settings is settings
    assert resolution.configured is False
    assert resolution.recovered_from_invalid is False


def test_valid_lifetime_setting_is_registered_for_card_service(tmp_path) -> None:
    _write_settings(tmp_path, 75)

    build_services(root=tmp_path)

    settings = AppContext.get(CardDisplaySettings)
    resolution = AppContext.get(CardDisplaySettingsResolution)
    cards = AppContext.get(CardService)

    assert settings.lifetime_seconds == 75
    assert cards.settings is settings
    assert resolution.configured is True
    assert resolution.recovered_from_invalid is False


def test_invalid_lifetime_setting_falls_back_and_writes_diagnostic(tmp_path) -> None:
    _write_settings(tmp_path, "seventy-five")

    paths, _logger = build_services(root=tmp_path)

    settings = AppContext.get(CardDisplaySettings)
    resolution = AppContext.get(CardDisplaySettingsResolution)

    assert settings.lifetime_seconds == DEFAULT_CARD_LIFETIME_SECONDS
    assert resolution.configured is True
    assert resolution.recovered_from_invalid is True
    assert "Card lifetime setting is invalid" in paths.log_file("flash.log").read_text(
        encoding="utf-8"
    )
