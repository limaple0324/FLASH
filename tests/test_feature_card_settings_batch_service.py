from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from PIL import Image

from config.config_manager import ConfigManager
from services.background_image_service import BackgroundImageService
from services.feature_card_layout_service import FeatureCardLayoutService
from services.feature_card_settings_batch_service import (
    FeatureCardSettingsBatchService,
)
from services.group_configuration_service import GroupConfigurationService


CARD_ID = "sync.input"
FEATURE_HOTKEYS_KEY = "feature_hotkeys"


def _services(tmp_path):
    config = ConfigManager(tmp_path / "config" / "settings.json")
    feature_hotkeys = {
        "sync": "F1",
        "reconnect": "F2",
        "auto_click": "",
    }
    config.set(FEATURE_HOTKEYS_KEY, dict(feature_hotkeys))
    layout = FeatureCardLayoutService(config)
    backgrounds = BackgroundImageService(config, tmp_path / "data")
    groups = GroupConfigurationService(tmp_path / "data" / "groups.json")
    groups.create_group("14支")
    service = FeatureCardSettingsBatchService(
        config=config,
        feature_card_layout_service=layout,
        background_image_service=backgrounds,
        configured_feature_hotkeys=feature_hotkeys,
        feature_hotkeys_config_key=FEATURE_HOTKEYS_KEY,
        group_configuration_service=groups,
    )
    return config, layout, backgrounds, groups, feature_hotkeys, service


def _prepared_background(
    backgrounds: BackgroundImageService,
    source: Path,
    color: str,
) -> Path:
    image = Image.new("RGB", (8, 6), color)
    try:
        image.save(source)
    finally:
        image.close()
    result = backgrounds.prepare(source)
    assert result.succeeded is True
    assert result.managed_path is not None
    return result.managed_path


def _set_existing_background(
    backgrounds: BackgroundImageService,
    source: Path,
    color: str = "#203050",
) -> Path:
    prepared = _prepared_background(backgrounds, source, color)
    result = backgrounds.commit_prepared_to_card(prepared, CARD_ID)
    assert result.succeeded is True
    assert result.managed_path is not None
    return result.managed_path


def test_name_background_and_feature_hotkey_publish_once(
    tmp_path,
    monkeypatch,
) -> None:
    (
        config,
        layout,
        backgrounds,
        _groups,
        feature_hotkeys,
        service,
    ) = _services(tmp_path)
    prepared = _prepared_background(
        backgrounds,
        tmp_path / "新背景.png",
        "#406080",
    )
    writes: list[dict[str, object]] = []
    original_save = config._save_now

    def record_save() -> None:
        writes.append(deepcopy(config.data))
        original_save()

    monkeypatch.setattr(config, "_save_now", record_save)

    result = service.save(
        card_id=CARD_ID,
        title="同步輸入新名稱",
        reset_title=False,
        pending_background_path=prepared,
        clear_background=False,
        hotkey_feature="sync",
        hotkey="F3",
        group_name=None,
    )

    assert result.succeeded is True
    assert len(writes) == 1
    assert layout.preference(CARD_ID, "同步輸入").title == "同步輸入新名稱"
    assert backgrounds.current_card_background(CARD_ID) == prepared
    assert feature_hotkeys["sync"] == "F3"
    assert config.get(FEATURE_HOTKEYS_KEY)["sync"] == "F3"


def test_background_failure_keeps_entire_batch_unchanged(
    tmp_path,
    monkeypatch,
) -> None:
    (
        config,
        layout,
        backgrounds,
        _groups,
        feature_hotkeys,
        service,
    ) = _services(tmp_path)
    layout.set_title(CARD_ID, "原名稱")
    existing = _set_existing_background(
        backgrounds,
        tmp_path / "原背景.png",
    )
    before_data = deepcopy(config.data)
    before_file = config.config_path.read_bytes()
    before_background = existing.read_bytes()
    writes = 0
    original_save = config._save_now

    def record_save() -> None:
        nonlocal writes
        writes += 1
        original_save()

    monkeypatch.setattr(config, "_save_now", record_save)

    result = service.save(
        card_id=CARD_ID,
        title="不應儲存的新名稱",
        reset_title=False,
        pending_background_path=tmp_path / "不存在.png",
        clear_background=False,
        hotkey_feature="sync",
        hotkey="F3",
        group_name=None,
    )

    assert result.succeeded is False
    assert writes == 0
    assert config.data == before_data
    assert config.config_path.read_bytes() == before_file
    assert layout.preference(CARD_ID, "預設").title == "原名稱"
    assert backgrounds.current_card_background(CARD_ID) == existing
    assert existing.read_bytes() == before_background
    assert feature_hotkeys["sync"] == "F1"


def test_shortcut_conflicts_do_not_write_any_setting(
    tmp_path,
    monkeypatch,
) -> None:
    (
        config,
        layout,
        backgrounds,
        groups,
        feature_hotkeys,
        service,
    ) = _services(tmp_path)
    groups.set_launch_hotkey("14支", "F4")
    prepared = _prepared_background(
        backgrounds,
        tmp_path / "待套用.png",
        "#705030",
    )
    before_data = deepcopy(config.data)
    before_file = config.config_path.read_bytes()
    writes = 0

    def unexpected_save() -> None:
        nonlocal writes
        writes += 1

    monkeypatch.setattr(config, "_save_now", unexpected_save)

    feature_conflict = service.save(
        card_id=CARD_ID,
        title="不應儲存",
        reset_title=False,
        pending_background_path=prepared,
        clear_background=False,
        hotkey_feature="sync",
        hotkey="F2",
        group_name=None,
    )
    group_conflict = service.save(
        card_id=CARD_ID,
        title="仍不應儲存",
        reset_title=False,
        pending_background_path=prepared,
        clear_background=False,
        hotkey_feature="reconnect",
        hotkey="F4",
        group_name=None,
    )

    assert feature_conflict.succeeded is False
    assert group_conflict.succeeded is False
    assert writes == 0
    assert config.data == before_data
    assert config.config_path.read_bytes() == before_file
    assert layout.preference(CARD_ID, "同步輸入").title == "同步輸入"
    assert backgrounds.current_card_background(CARD_ID) is None
    assert feature_hotkeys == {
        "sync": "F1",
        "reconnect": "F2",
        "auto_click": "",
    }


def test_clear_background_false_keeps_saved_background(
    tmp_path,
) -> None:
    (
        _config,
        _layout,
        backgrounds,
        _groups,
        _feature_hotkeys,
        service,
    ) = _services(tmp_path)
    existing = _set_existing_background(
        backgrounds,
        tmp_path / "保留背景.png",
    )
    before = existing.read_bytes()

    result = service.save(
        card_id=CARD_ID,
        title="只改名稱",
        reset_title=False,
        pending_background_path=None,
        clear_background=False,
        hotkey_feature=None,
        hotkey="",
        group_name=None,
    )

    assert result.succeeded is True
    assert backgrounds.current_card_background(CARD_ID) == existing
    assert existing.read_bytes() == before


def test_clear_background_publish_failure_restores_file_and_settings(
    tmp_path,
    monkeypatch,
) -> None:
    (
        config,
        layout,
        backgrounds,
        _groups,
        feature_hotkeys,
        service,
    ) = _services(tmp_path)
    layout.set_title(CARD_ID, "原名稱")
    existing = _set_existing_background(
        backgrounds,
        tmp_path / "不可提前刪除.png",
    )
    before_data = deepcopy(config.data)
    before_file = config.config_path.read_bytes()
    before_background = existing.read_bytes()

    def fail_publish() -> None:
        raise OSError("設定檔無法寫入")

    monkeypatch.setattr(config, "_save_now", fail_publish)

    result = service.save(
        card_id=CARD_ID,
        title="不應儲存",
        reset_title=False,
        pending_background_path=None,
        clear_background=True,
        hotkey_feature="sync",
        hotkey="F3",
        group_name=None,
    )

    assert result.succeeded is False
    assert config.data == before_data
    assert config.config_path.read_bytes() == before_file
    assert layout.preference(CARD_ID, "預設").title == "原名稱"
    assert backgrounds.current_card_background(CARD_ID) == existing
    assert existing.read_bytes() == before_background
    assert feature_hotkeys["sync"] == "F1"


def test_unreadable_old_background_refuses_batch_before_any_change(
    tmp_path,
    monkeypatch,
) -> None:
    (
        config,
        layout,
        backgrounds,
        _groups,
        feature_hotkeys,
        service,
    ) = _services(tmp_path)
    layout.set_title(CARD_ID, "原名稱")
    existing = _set_existing_background(
        backgrounds,
        tmp_path / "原背景.png",
    )
    replacement = _prepared_background(
        backgrounds,
        tmp_path / "新背景.png",
        "#806040",
    )
    before_data = deepcopy(config.data)
    before_file = config.config_path.read_bytes()
    before_background = existing.read_bytes()
    monkeypatch.setattr(service, "_read_bytes", lambda _path: None)

    result = service.save(
        card_id=CARD_ID,
        title="不應儲存",
        reset_title=False,
        pending_background_path=replacement,
        clear_background=False,
        hotkey_feature="sync",
        hotkey="F3",
        group_name=None,
    )

    assert result.succeeded is False
    assert "安全回復副本" in result.message
    assert "全部設定均未變更" in result.message
    assert config.data == before_data
    assert config.config_path.read_bytes() == before_file
    assert layout.preference(CARD_ID, "預設").title == "原名稱"
    assert backgrounds.current_card_background(CARD_ID) == existing
    assert existing.read_bytes() == before_background
    assert replacement.exists() is True
    assert feature_hotkeys["sync"] == "F1"


def test_group_hotkey_partial_failure_restores_everything(
    tmp_path,
    monkeypatch,
) -> None:
    (
        config,
        layout,
        backgrounds,
        groups,
        feature_hotkeys,
        service,
    ) = _services(tmp_path)
    layout.set_title(CARD_ID, "原名稱")
    groups.set_launch_hotkey("14支", "F8")
    existing = _set_existing_background(
        backgrounds,
        tmp_path / "原卡片背景.png",
    )
    replacement = _prepared_background(
        backgrounds,
        tmp_path / "新卡片背景.png",
        "#806040",
    )
    before_data = deepcopy(config.data)
    before_config_file = config.config_path.read_bytes()
    before_group_file = groups.path.read_bytes()
    before_group_backup = (
        groups.backup_path.read_bytes()
        if groups.backup_path.is_file()
        else None
    )
    before_background = existing.read_bytes()
    original_set_hotkey = groups.set_launch_hotkey

    def fail_after_group_write(group_name: object, hotkey: object) -> bool:
        changed = original_set_hotkey(group_name, hotkey)
        if hotkey == "F9":
            raise OSError("組別快捷鍵寫入中斷")
        return changed

    monkeypatch.setattr(groups, "set_launch_hotkey", fail_after_group_write)

    result = service.save(
        card_id=CARD_ID,
        title="不應保留的新名稱",
        reset_title=False,
        pending_background_path=replacement,
        clear_background=False,
        hotkey_feature="group_launch",
        hotkey="F9",
        group_name="14支",
    )

    assert result.succeeded is False
    assert config.data == before_data
    assert config.config_path.read_bytes() == before_config_file
    assert groups.group("14支").launch_hotkey == "F8"
    assert groups.path.read_bytes() == before_group_file
    if before_group_backup is None:
        assert groups.backup_path.exists() is False
    else:
        assert groups.backup_path.read_bytes() == before_group_backup
    assert layout.preference(CARD_ID, "預設").title == "原名稱"
    assert backgrounds.current_card_background(CARD_ID) == existing
    assert existing.read_bytes() == before_background
    assert replacement.exists() is False
    assert feature_hotkeys["sync"] == "F1"
