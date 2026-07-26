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


def test_safe_status_explains_that_preflight_still_applies() -> None:
    ready = _observation(configured=True, safe=True, code="window.ready")

    assert target_window_summary(ready) == "已找到遊戲視窗"
    assert "每批安全預檢" in target_window_player_message(ready)


@pytest.mark.parametrize(
    ("code", "expected"),
    (
        ("window.not_found", "目前找不到"),
        ("window.ambiguous", "多個候選"),
        ("window.minimized", "已最小化"),
        ("window.not_foreground", "不在前景"),
        ("window.unknown", "無法安全辨識"),
    ),
)
def test_unsafe_status_uses_player_facing_chinese(
    code: str,
    expected: str,
) -> None:
    observation = _observation(configured=True, safe=False, code=code)

    assert expected in target_window_player_message(observation)
    assert "不會送出任何操作" in target_window_player_message(observation)
    assert code not in target_window_player_message(observation)


def test_unconfigured_and_not_observed_are_distinct() -> None:
    unconfigured = _observation(
        configured=False,
        safe=False,
        code="window.not_configured",
    )
    not_observed = TargetWindowObservation.not_observed()

    assert target_window_summary(unconfigured) == "尚未設定遊戲視窗"
    assert target_window_summary(not_observed) == "尚未完成視窗檢查"


def test_presentation_rejects_untrusted_objects() -> None:
    with pytest.raises(TypeError, match="TargetWindowObservation"):
        target_window_player_message(object())
    with pytest.raises(TypeError, match="TargetWindowObservation"):
        target_window_summary(object())
