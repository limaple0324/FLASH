from pathlib import Path

import pytest

from tools.devspace_bridge.bridge import (
    ALLOWED_ACTIONS,
    BridgeConfig,
    BridgeError,
    TaskExecutor,
)


def config(tmp_path: Path) -> BridgeConfig:
    return BridgeConfig(tmp_path, "automation/devspace-queue", tmp_path / "state")


def test_action_allowlist_does_not_include_arbitrary_shell():
    assert "shell" not in ALLOWED_ACTIONS
    assert "exec" not in ALLOWED_ACTIONS
    assert "kill_process" not in ALLOWED_ACTIONS
    assert "mouse_click" not in ALLOWED_ACTIONS


def test_task_validation_rejects_unknown_action(tmp_path):
    executor = TaskExecutor(config(tmp_path))
    with pytest.raises(BridgeError):
        executor.validate({"schema_version": 1, "task_id": "task-0001", "action": "shell"})


def test_task_validation_accepts_ping(tmp_path):
    executor = TaskExecutor(config(tmp_path))
    assert executor.validate({"schema_version": 1, "task_id": "task-0001", "action": "ping"}) == ("task-0001", "ping")


def test_relative_path_cannot_escape_worktree(tmp_path):
    executor = TaskExecutor(config(tmp_path))
    with pytest.raises(BridgeError):
        executor._safe_relative("../secret.txt")
    with pytest.raises(BridgeError):
        executor._safe_relative("/secret.txt")
    assert executor._safe_relative("tests/test_main.py") == Path("tests/test_main.py")


def test_ping_is_read_only_and_structured(tmp_path):
    executor = TaskExecutor(config(tmp_path))
    result = executor.execute({"schema_version": 1, "task_id": "task-0001", "action": "ping"})
    assert result["ok"] is True
    assert result["result"]["message"] == "pong"
