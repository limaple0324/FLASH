from __future__ import annotations

import threading
from collections.abc import Iterator, MutableMapping
from copy import deepcopy
from pathlib import Path

from PIL import Image
import pytest

from config.config_manager import ConfigManager
from services.background_image_service import BackgroundImageService
from services.feature_card_layout_service import FeatureCardLayoutService
from services.feature_card_settings_batch_service import (
    FeatureCardSettingsBatchService,
)
from services.group_configuration_service import (
    GroupConfigurationService,
)
from services.identity_data_transaction_coordinator import (
    IdentityDataTransactionCoordinator,
)


CARD_ID = "sync.input"
FEATURE_HOTKEYS_KEY = "feature_hotkeys"


class _FailingHotkeyMapping(MutableMapping[str, str]):
    def __init__(self, values: dict[str, str], *, fail_after_write: bool):
        self._values = dict(values)
        self._fail_after_write = fail_after_write
        self._remaining_failures = 0

    def arm(self, failures: int = 1) -> None:
        self._remaining_failures = failures

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __setitem__(self, key: str, value: str) -> None:
        should_fail = key == "auto_click" and self._remaining_failures > 0
        if should_fail and self._fail_after_write:
            self._values[key] = value
        if should_fail:
            self._remaining_failures -= 1
            raise OSError("runtime hotkey mapping publish interrupted")
        self._values[key] = value

    def __delitem__(self, key: str) -> None:
        del self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


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
    coordinator = IdentityDataTransactionCoordinator()
    groups = GroupConfigurationService(
        tmp_path / "data" / "groups.json",
        coordinator,
    )
    groups.create_group("14支")
    service = FeatureCardSettingsBatchService(
        config=config,
        feature_card_layout_service=layout,
        background_image_service=backgrounds,
        configured_feature_hotkeys=feature_hotkeys,
        feature_hotkeys_config_key=FEATURE_HOTKEYS_KEY,
        coordinator=coordinator,
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


def test_name_background_publish_before_linearized_feature_hotkey(
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
    assert len(writes) == 2
    assert writes[0][FEATURE_HOTKEYS_KEY]["sync"] == "F1"
    assert writes[1][FEATURE_HOTKEYS_KEY]["sync"] == "F3"
    assert layout.preference(CARD_ID, "同步輸入").title == "同步輸入新名稱"
    assert backgrounds.current_card_background(CARD_ID) == prepared
    assert feature_hotkeys["sync"] == "F3"
    assert config.get(FEATURE_HOTKEYS_KEY)["sync"] == "F3"


def test_batch_service_rejects_group_service_using_another_coordinator(
    tmp_path,
) -> None:
    config, layout, backgrounds, groups, feature_hotkeys, _service = _services(tmp_path)

    with pytest.raises(ValueError, match="injected coordinator"):
        FeatureCardSettingsBatchService(
            config=config,
            feature_card_layout_service=layout,
            background_image_service=backgrounds,
            configured_feature_hotkeys=feature_hotkeys,
            feature_hotkeys_config_key=FEATURE_HOTKEYS_KEY,
            coordinator=IdentityDataTransactionCoordinator(),
            group_configuration_service=groups,
        )


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
    original_install = groups._install_state
    install_calls = 0

    def fail_after_group_write(candidate) -> None:
        nonlocal install_calls
        install_calls += 1
        original_install(candidate)
        if install_calls == 1:
            raise OSError("組別快捷鍵寫入中斷")

    monkeypatch.setattr(groups, "_install_state", fail_after_group_write)

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


def test_group_hotkey_commits_after_all_general_content(
    tmp_path,
    monkeypatch,
) -> None:
    config, _layout, _backgrounds, groups, _feature_hotkeys, service = _services(
        tmp_path
    )
    events: list[str] = []
    original_config_save = config._save_now
    original_group_install = groups._install_state

    def record_config_save() -> None:
        original_config_save()
        events.append("general")

    def record_group_install(candidate) -> None:
        events.append("group")
        original_group_install(candidate)

    monkeypatch.setattr(config, "_save_now", record_config_save)
    monkeypatch.setattr(groups, "_install_state", record_group_install)

    result = service.save(
        card_id=CARD_ID,
        title="先完成一般內容",
        reset_title=False,
        pending_background_path=None,
        clear_background=False,
        hotkey_feature="group_launch",
        hotkey="F9",
        group_name="14支",
    )

    assert result.succeeded is True
    assert events == ["general", "group"]
    assert groups.group("14支").launch_hotkey == "F9"


def test_title_and_background_only_save_after_identity_coordinator_closed(
    tmp_path,
) -> None:
    _config, layout, _backgrounds, groups, _feature_hotkeys, service = _services(
        tmp_path
    )
    assert groups.coordinator.close_and_wait()

    result = service.save(
        card_id=CARD_ID,
        title="關閉後仍可改名稱",
        reset_title=False,
        pending_background_path=None,
        clear_background=False,
        hotkey_feature=None,
        hotkey="",
        group_name=None,
    )

    assert result.succeeded is True
    assert layout.preference(CARD_ID, "預設").title == "關閉後仍可改名稱"


def test_nonempty_feature_hotkey_is_rejected_when_group_conflict_cannot_be_checked(
    tmp_path,
) -> None:
    config, layout, _backgrounds, groups, feature_hotkeys, service = _services(
        tmp_path
    )
    before = deepcopy(config.data)
    assert groups.coordinator.close_and_wait()

    result = service.save(
        card_id=CARD_ID,
        title="不得保存",
        reset_title=False,
        pending_background_path=None,
        clear_background=False,
        hotkey_feature="sync",
        hotkey="F3",
        group_name=None,
    )

    assert result.succeeded is False
    assert config.data == before
    assert layout.preference(CARD_ID, "預設").title == "預設"
    assert feature_hotkeys["sync"] == "F1"


def test_group_candidate_uses_latest_concurrent_group_state_without_overwrite(
    tmp_path,
    monkeypatch,
) -> None:
    config, _layout, _backgrounds, groups, _feature_hotkeys, service = _services(
        tmp_path
    )
    original_config_save = config._save_now
    injected = False

    def publish_general_then_concurrent_group() -> None:
        nonlocal injected
        original_config_save()
        if not injected:
            injected = True
            assert groups.create_group("乙組") is True

    monkeypatch.setattr(config, "_save_now", publish_general_then_concurrent_group)

    result = service.save(
        card_id=CARD_ID,
        title="一般內容先完成",
        reset_title=False,
        pending_background_path=None,
        clear_background=False,
        hotkey_feature="group_launch",
        hotkey="F8",
        group_name="14支",
    )

    assert result.succeeded is True
    assert groups.group("14支").launch_hotkey == "F8"
    assert groups.group("乙組") is not None


def test_product_has_no_direct_group_file_snapshot_or_restore_path() -> None:
    source = Path(
        "services/feature_card_settings_batch_service.py"
    ).read_text(encoding="utf-8")

    assert "_GroupFilesSnapshot" not in source
    assert "_snapshot_group_files" not in source
    assert "_restore_file" not in source


def test_direct_feature_hotkey_save_failure_keeps_file_and_runtime_mapping(
    tmp_path,
    monkeypatch,
) -> None:
    config, _layout, _backgrounds, _groups, feature_hotkeys, service = (
        _services(tmp_path)
    )
    before_mapping = dict(feature_hotkeys)
    before_data = config.snapshot()
    before_file = config.config_path.read_bytes()

    def fail_save() -> None:
        raise OSError("config write interrupted")

    monkeypatch.setattr(config, "_save_now", fail_save)

    result = service.change_feature_hotkey("auto_click", "F9")

    assert result.succeeded is False
    assert feature_hotkeys == before_mapping
    assert config.snapshot() == before_data
    assert config.config_path.read_bytes() == before_file


def test_concurrent_feature_and_group_hotkey_claim_allows_only_one_owner(
    tmp_path,
) -> None:
    config, _layout, _backgrounds, groups, feature_hotkeys, service = _services(
        tmp_path
    )
    start = threading.Barrier(3)
    results = {}
    errors = []

    def claim_feature() -> None:
        try:
            start.wait(2)
            results["feature"] = service.change_feature_hotkey(
                "auto_click",
                "F9",
            )
        except BaseException as error:
            errors.append(error)

    def claim_group() -> None:
        try:
            start.wait(2)
            results["group"] = service.change_group_launch_hotkey(
                "14支",
                "F9",
            )
        except BaseException as error:
            errors.append(error)

    feature_thread = threading.Thread(target=claim_feature)
    group_thread = threading.Thread(target=claim_group)
    feature_thread.start()
    group_thread.start()
    start.wait(2)
    feature_thread.join(2)
    group_thread.join(2)

    assert errors == []
    assert feature_thread.is_alive() is False
    assert group_thread.is_alive() is False
    assert set(results) == {"feature", "group"}
    assert sum(result.succeeded for result in results.values()) == 1
    feature_owns_key = results["feature"].succeeded
    assert feature_hotkeys["auto_click"] == ("F9" if feature_owns_key else "")
    assert config.get(FEATURE_HOTKEYS_KEY)["auto_click"] == (
        "F9" if feature_owns_key else ""
    )
    assert groups.group("14支").launch_hotkey == (
        "" if feature_owns_key else "F9"
    )


def test_batch_feature_and_group_hotkey_claim_allows_only_one_complete_batch(
    tmp_path,
) -> None:
    config, layout, _backgrounds, groups, feature_hotkeys, service = _services(
        tmp_path
    )
    start = threading.Barrier(3)
    results = {}
    errors = []

    def claim_feature_batch() -> None:
        try:
            start.wait(2)
            results["feature"] = service.save(
                card_id=CARD_ID,
                title="功能快捷鍵勝出",
                reset_title=False,
                pending_background_path=None,
                clear_background=False,
                hotkey_feature="auto_click",
                hotkey="F9",
                group_name=None,
            )
        except BaseException as error:
            errors.append(error)

    def claim_group() -> None:
        try:
            start.wait(2)
            results["group"] = service.change_group_launch_hotkey(
                "14支",
                "F9",
            )
        except BaseException as error:
            errors.append(error)

    feature_thread = threading.Thread(target=claim_feature_batch)
    group_thread = threading.Thread(target=claim_group)
    feature_thread.start()
    group_thread.start()
    start.wait(2)
    feature_thread.join(2)
    group_thread.join(2)

    assert errors == []
    assert feature_thread.is_alive() is False
    assert group_thread.is_alive() is False
    assert set(results) == {"feature", "group"}
    assert sum(result.succeeded for result in results.values()) == 1
    feature_owns_key = results["feature"].succeeded
    assert feature_hotkeys["auto_click"] == ("F9" if feature_owns_key else "")
    assert config.get(FEATURE_HOTKEYS_KEY)["auto_click"] == (
        "F9" if feature_owns_key else ""
    )
    assert groups.group("14支").launch_hotkey == (
        "" if feature_owns_key else "F9"
    )
    assert layout.preference(CARD_ID, "預設").title == (
        "功能快捷鍵勝出" if feature_owns_key else "預設"
    )


def test_direct_feature_hotkey_rejects_closed_coordinator_without_changes(
    tmp_path,
) -> None:
    config, _layout, _backgrounds, groups, feature_hotkeys, service = _services(
        tmp_path
    )
    before_mapping = dict(feature_hotkeys)
    before_data = config.snapshot()
    before_file = config.config_path.read_bytes()
    assert groups.coordinator.close_and_wait()

    result = service.change_feature_hotkey("auto_click", "F9")

    assert result.succeeded is False
    assert feature_hotkeys == before_mapping
    assert config.snapshot() == before_data
    assert config.config_path.read_bytes() == before_file


@pytest.mark.parametrize("entrypoint", ["direct", "batch"])
@pytest.mark.parametrize("fail_after_write", [False, True])
def test_runtime_mapping_publish_failure_restores_complete_external_state(
    tmp_path,
    entrypoint,
    fail_after_write,
) -> None:
    config, layout, backgrounds, _groups, feature_hotkeys, service = _services(
        tmp_path
    )
    runtime_mapping = _FailingHotkeyMapping(
        feature_hotkeys,
        fail_after_write=fail_after_write,
    )
    service._feature_hotkeys = runtime_mapping
    before_mapping = dict(runtime_mapping)
    before_data = config.snapshot()
    before_file = config.config_path.read_bytes()
    prepared = None
    if entrypoint == "batch":
        prepared = _prepared_background(
            backgrounds,
            tmp_path / "映射失敗背景.png",
            "#315070",
        )
    runtime_mapping.arm()

    if entrypoint == "direct":
        result = service.change_feature_hotkey("auto_click", "F9")
    else:
        result = service.save(
            card_id=CARD_ID,
            title="不得留下的名稱",
            reset_title=False,
            pending_background_path=prepared,
            clear_background=False,
            hotkey_feature="auto_click",
            hotkey="F9",
            group_name=None,
        )

    assert result.succeeded is False
    assert "全部設定均未變更" in result.message
    assert config.snapshot() == before_data
    assert config.config_path.read_bytes() == before_file
    assert dict(runtime_mapping) == before_mapping
    assert layout.preference(CARD_ID, "預設").title == "預設"
    assert backgrounds.current_card_background(CARD_ID) is None
    if prepared is not None:
        assert prepared.exists() is False


def test_runtime_mapping_rollback_failure_is_reported_and_logged(
    tmp_path,
) -> None:
    config, _layout, _backgrounds, _groups, feature_hotkeys, service = _services(
        tmp_path
    )
    runtime_mapping = _FailingHotkeyMapping(
        feature_hotkeys,
        fail_after_write=False,
    )
    messages = []
    service._feature_hotkeys = runtime_mapping
    service._error_logger = messages.append
    before_data = config.snapshot()
    before_file = config.config_path.read_bytes()
    runtime_mapping.arm(failures=2)

    result = service.change_feature_hotkey("auto_click", "F9")

    assert result.succeeded is False
    assert "回復未完成" in result.message
    assert "全部設定均未變更" not in result.message
    assert messages
    assert "還原未完整" in messages[0]
    assert config.snapshot() == before_data
    assert config.config_path.read_bytes() == before_file
