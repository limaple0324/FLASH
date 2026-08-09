from __future__ import annotations

import json
import threading

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


def test_nested_transaction_is_reentrant_and_inner_failure_restores_inner_only(
    tmp_path,
) -> None:
    config = ConfigManager(tmp_path / "settings.json")
    revision = config.revision

    with config.transaction():
        config.set("outer", 1)
        try:
            with config.transaction():
                config.set("inner", 2)
                raise RuntimeError("inner failed")
        except RuntimeError:
            pass
        config.set("after", 3)

    assert config.data == {"outer": 1, "after": 3}
    assert config.revision == revision + 1


def test_transaction_depth_is_thread_local_and_other_thread_waits_for_guard(
    tmp_path,
) -> None:
    config = ConfigManager(tmp_path / "settings.json")
    entered = threading.Event()
    release = threading.Event()
    second_finished = threading.Event()

    def first_writer() -> None:
        with config.transaction():
            config.set("first", 1)
            entered.set()
            assert release.wait(5)

    def second_writer() -> None:
        config.set("second", 2)
        second_finished.set()

    first = threading.Thread(target=first_writer)
    second = threading.Thread(target=second_writer)
    first.start()
    assert entered.wait(5)
    second.start()
    assert second_finished.wait(0.1) is False
    release.set()
    first.join(5)
    second.join(5)

    assert first.is_alive() is False
    assert second.is_alive() is False
    assert config.data == {"first": 1, "second": 2}


def test_snapshot_is_deep_copy_and_candidate_install_checks_revision(tmp_path) -> None:
    config = ConfigManager(tmp_path / "settings.json")
    config.set("nested", {"items": [1]})
    snapshot = config.snapshot()
    snapshot["nested"]["items"].append(2)

    assert config.get("nested") == {"items": [1]}

    with config.resource_guard():
        original = config.snapshot_state_locked()
        candidate = config.candidate_with_updates_locked(
            {"current_group_name": "甲組"},
            base=original.data,
        )
        content = config.serialize_candidate(candidate)
        assert config.validate_serialized_candidate(content, candidate) is True
        assert config.install_candidate_locked(
            candidate,
            expected_revision=original.revision,
        ) is True
        with pytest.raises(RuntimeError, match="changed"):
            config.install_candidate_locked(
                {"current_group_name": "乙組"},
                expected_revision=original.revision,
            )
        config.restore_state_locked(original)

    assert config.data == {"nested": {"items": [1]}}
    assert config.revision == original.revision
