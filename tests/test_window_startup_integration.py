import sys
import uuid
from pathlib import Path

import pytest

import main as main_module
from config.config_manager import ConfigManager
from core.sp1_boundaries import ExternalAdapter, OperationResult
from main import (
    TARGET_WINDOW_FINGERPRINT_KEY,
    TARGET_WINDOW_KEY,
    _normalize_window_fingerprint,
    _normalize_window_keywords,
    build_services,
    detect_target_window,
    format_window_status,
)
from services.app_context import AppContext


class FakeAdapter:
    def __init__(self, result: OperationResult):
        self._result = result

    @property
    def name(self) -> str:
        return "fake_window"

    def health_check(self) -> OperationResult:
        return self._result

    def shutdown(self) -> None:
        return None


def test_normalize_window_keywords_accepts_string_and_list():
    assert _normalize_window_keywords("  game  ") == ["game"]
    assert _normalize_window_keywords([" game ", "", 123, "server"]) == ["game", "server"]
    assert _normalize_window_keywords({"game": True}) == []


def test_normalize_window_fingerprint_accepts_only_complete_sha256():
    assert _normalize_window_fingerprint("  " + "A" * 64 + "  ") == "a" * 64
    assert _normalize_window_fingerprint("a" * 63) is None
    assert _normalize_window_fingerprint("g" * 64) is None
    assert _normalize_window_fingerprint(123) is None


def test_build_services_connects_persisted_fingerprint_to_window_adapter(tmp_path):
    fingerprint = "6" * 64
    config = ConfigManager(tmp_path / "config" / "settings.json")
    config.update_values(
        {
            TARGET_WINDOW_KEY: ["Adobe Flash Player"],
            TARGET_WINDOW_FINGERPRINT_KEY: fingerprint.upper(),
        }
    )

    build_services(root=tmp_path)

    adapter = AppContext.get(ExternalAdapter)
    assert adapter is not None
    assert adapter._launch_fingerprint == fingerprint
    assert adapter._fingerprint_configured is True


def test_build_services_keeps_invalid_persisted_fingerprint_fail_closed(tmp_path):
    config = ConfigManager(tmp_path / "config" / "settings.json")
    config.update_values(
        {
            TARGET_WINDOW_KEY: ["Adobe Flash Player"],
            TARGET_WINDOW_FINGERPRINT_KEY: "invalid",
        }
    )

    build_services(root=tmp_path)

    adapter = AppContext.get(ExternalAdapter)
    result = adapter.health_check()
    assert result.success is False
    assert result.code == "window.identity_invalid"


def test_unconfigured_window_detection_remains_unsafe():
    AppContext.clear()

    status = detect_target_window()

    assert status["configured"] is False
    assert status["safe"] is False
    assert status["code"] == "window.not_configured"
    assert "不會執行任何遊戲操作" in status["message"]


def test_ready_window_is_reported_but_input_is_not_enabled():
    AppContext.clear()
    adapter = FakeAdapter(
        OperationResult(
            success=True,
            code="window.ready",
            message="Target identified.",
            details={"title": "Game", "handle": 100, "rect": (0, 0, 800, 600)},
        )
    )
    AppContext.register(ExternalAdapter, adapter)

    status = detect_target_window()
    text = format_window_status({"target_window": status})

    assert status["configured"] is True
    assert status["safe"] is True
    assert status["code"] == "window.ready"
    assert status["details"]["handle"] == 100
    assert "同步輸入仍需玩家手動執行" in text


def test_ambiguous_window_keeps_operation_disabled():
    AppContext.clear()
    adapter = FakeAdapter(
        OperationResult(
            success=False,
            code="window.ambiguous",
            message="Multiple matches.",
            details={"count": 2},
        )
    )
    AppContext.register(ExternalAdapter, adapter)

    status = detect_target_window()
    text = format_window_status({"target_window": status})

    assert status["safe"] is False
    assert status["code"] == "window.ambiguous"
    assert "不可操作" in text


def test_duplicate_ui_instance_exits_before_application_services(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        main_module,
        "acquire_main_instance_lock",
        lambda: None,
    )
    monkeypatch.setattr(
        main_module,
        "_run_application",
        lambda **_kwargs: calls.append("started") or 9,
    )

    assert main_module.run() == 0
    assert calls == []


def test_ui_instance_lock_is_released_only_after_complete_run(monkeypatch):
    events: list[str] = []

    class Lock:
        def release(self) -> None:
            events.append("released")

    monkeypatch.setattr(
        main_module,
        "acquire_main_instance_lock",
        lambda: Lock(),
    )
    monkeypatch.setattr(
        main_module,
        "_run_application",
        lambda **_kwargs: events.append("application_complete") or 7,
    )

    assert main_module.run() == 7
    assert events == ["application_complete", "released"]


@pytest.mark.parametrize(
    "arguments",
    (
        {"self_check_only": True},
        {"target_desktop_verify_only": True},
        {"background_image_verify_path": Path("sample.png")},
    ),
)
def test_verification_commands_bypass_ui_instance_lock(monkeypatch, arguments):
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        main_module,
        "acquire_main_instance_lock",
        lambda: pytest.fail("驗證命令不應取得主介面鎖"),
    )
    monkeypatch.setattr(
        main_module,
        "_run_application",
        lambda **kwargs: calls.append(kwargs) or 0,
    )

    assert main_module.run(**arguments) == 0
    assert len(calls) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="只適用 Windows 命名互斥鎖")
def test_real_named_instance_lock_rejects_second_handle_until_release():
    name = rf"Local\Limaple.Fu.Test.{uuid.uuid4()}"
    first = main_module.acquire_main_instance_lock(name)
    assert first is not None
    try:
        assert main_module.acquire_main_instance_lock(name) is None
    finally:
        first.release()
    restored = main_module.acquire_main_instance_lock(name)
    assert restored is not None
    restored.release()
