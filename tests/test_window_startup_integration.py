from core.sp1_boundaries import ExternalAdapter, OperationResult
from main import _normalize_window_keywords, detect_target_window, format_window_status
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
    assert "仍未啟用輸入" in text


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
