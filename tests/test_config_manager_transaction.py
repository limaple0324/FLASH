from __future__ import annotations

import json

import pytest

from config.config_manager import ConfigManager


def test_transaction_publishes_related_changes_with_one_write(
    tmp_path,
    monkeypatch,
) -> None:
    config = ConfigManager(tmp_path / "settings.json")
    writes: list[dict[str, object]] = []
    original_save = config._save_now

    def record_save() -> None:
        writes.append(dict(config.data))
        original_save()

    monkeypatch.setattr(config, "_save_now", record_save)

    with config.transaction():
        config.set("name", "同步輸入")
        config.set("hotkey", "F1")
        config.update_values({"background": "managed.png"})

    assert len(writes) == 1
    assert json.loads(config.config_path.read_text(encoding="utf-8")) == {
        "name": "同步輸入",
        "hotkey": "F1",
        "background": "managed.png",
    }


def test_transaction_exception_keeps_memory_and_file_unchanged(
    tmp_path,
) -> None:
    config = ConfigManager(tmp_path / "settings.json")
    config.set("name", "原名稱")
    before = config.config_path.read_bytes()

    with pytest.raises(RuntimeError, match="背景失敗"):
        with config.transaction():
            config.set("name", "新名稱")
            config.set("hotkey", "F2")
            raise RuntimeError("背景失敗")

    assert config.data == {"name": "原名稱"}
    assert config.config_path.read_bytes() == before


def test_transaction_publish_failure_restores_memory_and_prior_file(
    tmp_path,
    monkeypatch,
) -> None:
    config = ConfigManager(tmp_path / "settings.json")
    config.set("name", "原名稱")
    before = config.config_path.read_bytes()

    def fail_save() -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(config, "_save_now", fail_save)

    with pytest.raises(OSError, match="disk unavailable"):
        with config.transaction():
            config.set("name", "新名稱")

    assert config.data == {"name": "原名稱"}
    assert config.config_path.read_bytes() == before
