import json

import pytest

from config.config_manager import ConfigManager
from services.smart_reconnect_capture_settings_service import (
    MINIMIZED_CAPTURE_MODE,
    OBSCURED_CAPTURE_MODE,
    SMART_RECONNECT_CAPTURE_MODES_KEY,
    VISIBLE_CAPTURE_MODE,
    SmartReconnectCaptureSettings,
    SmartReconnectCaptureSettingsService,
)


def test_capture_settings_default_every_mode_to_enabled(tmp_path):
    config = ConfigManager(tmp_path / "settings.json")

    service = SmartReconnectCaptureSettingsService(config)

    assert service.snapshot() == SmartReconnectCaptureSettings()
    assert config.get(SMART_RECONNECT_CAPTURE_MODES_KEY) == {
        VISIBLE_CAPTURE_MODE: True,
        OBSCURED_CAPTURE_MODE: True,
        MINIMIZED_CAPTURE_MODE: True,
    }


def test_capture_settings_restore_independent_saved_choices(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                SMART_RECONNECT_CAPTURE_MODES_KEY: {
                    VISIBLE_CAPTURE_MODE: False,
                    OBSCURED_CAPTURE_MODE: True,
                    MINIMIZED_CAPTURE_MODE: False,
                }
            }
        ),
        encoding="utf-8",
    )

    service = SmartReconnectCaptureSettingsService(ConfigManager(path))

    assert service.snapshot() == SmartReconnectCaptureSettings(
        visible=False,
        obscured=True,
        minimized=False,
    )


def test_capture_settings_invalid_items_fail_to_safe_enabled_defaults(tmp_path):
    config = ConfigManager(tmp_path / "settings.json")
    config.set(
        SMART_RECONNECT_CAPTURE_MODES_KEY,
        {
            VISIBLE_CAPTURE_MODE: "false",
            OBSCURED_CAPTURE_MODE: 0,
            MINIMIZED_CAPTURE_MODE: None,
        },
    )

    service = SmartReconnectCaptureSettingsService(config)

    assert service.snapshot() == SmartReconnectCaptureSettings()
    assert config.get(SMART_RECONNECT_CAPTURE_MODES_KEY) == (
        SmartReconnectCaptureSettings().to_dict()
    )


def test_failed_persist_keeps_previous_runtime_selection(tmp_path, monkeypatch):
    config = ConfigManager(tmp_path / "settings.json")
    service = SmartReconnectCaptureSettingsService(config)
    previous = service.snapshot()
    previous_config = config.get(SMART_RECONNECT_CAPTURE_MODES_KEY)

    def fail_save():
        raise OSError("save failed")

    monkeypatch.setattr(config, "_save_now", fail_save)

    with pytest.raises(OSError):
        service.update(
            SmartReconnectCaptureSettings(
                visible=False,
                obscured=True,
                minimized=True,
            )
        )

    assert service.snapshot() == previous
    assert config.get(SMART_RECONNECT_CAPTURE_MODES_KEY) == previous_config
