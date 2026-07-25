import pytest

from core.target_window_observation import TargetWindowObservation
from presentation.target_window_status import (
    target_window_player_message,
    target_window_summary,
)


def _observation(
    *,
    configured: bool,
    safe: bool,
    code: str,
) -> TargetWindowObservation:
    return TargetWindowObservation(
        configured=configured,
        safe=safe,
        code=code,
    )


@pytest.mark.parametrize(
    ("code", "expected"),
    (
        ("window.not_found", "目前找不到"),
        ("window.ambiguous", "找到多個"),
        ("window.minimized", "已最小化"),
        ("window.not_foreground", "不在前景"),
        ("window.unknown", "無法安全辨識"),
    ),
)
def test_unsafe_status_uses_conservative_chinese_message(
    code: str,
    expected: str,
) -> None:
    message = target_window_player_message(
        _observation(configured=True, safe=False, code=code)
    )

    assert expected in message
    assert "操作保持停用" in message
    assert code not in message


def test_ready_unconfigured_and_not_observed_have_distinct_messages() -> None:
    ready = _observation(
        configured=True,
        safe=True,
        code="window.ready",
    )
    unconfigured = _observation(
        configured=False,
        safe=False,
        code="window.not_configured",
    )
    not_observed = TargetWindowObservation.not_observed()

    assert target_window_player_message(ready) == (
        "已找到可安全辨識的遊戲視窗；遊戲輸入仍保持停用。"
    )
    assert target_window_player_message(unconfigured) == (
        "尚未設定遊戲主視窗；所有遊戲操作保持停用。"
    )
    assert target_window_player_message(not_observed) == (
        "尚未完成本次遊戲視窗檢查；所有遊戲操作保持停用。"
    )
    assert target_window_summary(ready) == "已找到遊戲視窗"
    assert target_window_summary(unconfigured) == "尚未設定遊戲視窗"
    assert target_window_summary(not_observed) == "尚未完成視窗檢查"


def test_presentation_rejects_untrusted_objects() -> None:
    with pytest.raises(TypeError, match="TargetWindowObservation"):
        target_window_player_message(object())
    with pytest.raises(TypeError, match="TargetWindowObservation"):
        target_window_summary(object())

