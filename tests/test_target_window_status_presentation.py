import pytest

from core.target_window_observation import TargetWindowObservation
from presentation.target_window_status import target_window_summary


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


def test_safe_and_unsafe_statuses_are_summarized() -> None:
    ready = _observation(configured=True, safe=True, code="window.ready")
    unsafe = _observation(
        configured=True,
        safe=False,
        code="window.not_found",
    )

    assert target_window_summary(ready) == "已找到遊戲視窗"
    assert target_window_summary(unsafe) == "遊戲視窗目前不可操作"


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
        target_window_summary(object())
