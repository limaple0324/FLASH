import pytest

from core.target_window_observation import TargetWindowObservation
from ui.home import HomeView, _target_window_text


def _observation(
    *,
    configured: bool,
    safe: bool,
    code: str,
) -> TargetWindowObservation:
    return TargetWindowObservation.from_detection(
        {
            "configured": configured,
            "safe": safe,
            "code": code,
        }
    )


class _FakeLabel:
    def __init__(self) -> None:
        self.text = ""

    def configure(self, *, text: str) -> None:
        self.text = text


@pytest.mark.parametrize(
    ("observation", "expected"),
    (
        (
            TargetWindowObservation.not_observed(),
            "目前狀態\n● 尚未完成視窗檢查",
        ),
        (
            _observation(
                configured=False,
                safe=False,
                code="window.not_configured",
            ),
            "目前狀態\n● 尚未設定遊戲視窗",
        ),
        (
            _observation(
                configured=True,
                safe=True,
                code="window.ready",
            ),
            "目前狀態\n● 已找到遊戲視窗",
        ),
        (
            _observation(
                configured=True,
                safe=False,
                code="window.ambiguous",
            ),
            "目前狀態\n● 遊戲視窗不可操作",
        ),
    ),
)
def test_target_window_text_uses_only_the_safe_observation(
    observation: TargetWindowObservation,
    expected: str,
) -> None:
    status = {
        "self_check_passed": True,
        "target_window": {
            "configured": True,
            "safe": True,
            "details": {"handle": 999, "title": "private"},
        },
    }

    text = _target_window_text(status, observation)

    assert text == expected
    assert "999" not in text
    assert "private" not in text
    assert observation.code not in text


def test_failed_self_check_remains_the_highest_priority_status() -> None:
    observation = _observation(
        configured=True,
        safe=True,
        code="window.ready",
    )

    assert _target_window_text(
        {"self_check_passed": False},
        observation,
    ) == "目前狀態\n● 需要檢查"


def test_refresh_reads_event_backed_snapshot_and_updates_existing_label() -> None:
    states = iter(
        (
            _observation(
                configured=False,
                safe=False,
                code="window.not_configured",
            ),
            _observation(
                configured=True,
                safe=True,
                code="window.ready",
            ),
        )
    )
    view = HomeView(
        None,
        {"self_check_passed": True},
        target_window_state_provider=lambda: next(states),
    )
    label = _FakeLabel()
    view._target_window_label = label

    assert "尚未設定遊戲視窗" in view.refresh_target_window()
    refreshed = view.refresh_target_window()

    assert refreshed == "目前狀態\n● 已找到遊戲視窗"
    assert label.text == refreshed


def test_refresh_failure_keeps_last_good_observation_and_reports_error() -> None:
    ready = _observation(
        configured=True,
        safe=True,
        code="window.ready",
    )
    errors: list[Exception] = []
    calls = 0

    def provider() -> TargetWindowObservation:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ready
        raise OSError(r"C:\private\target-window.txt")

    view = HomeView(
        None,
        {"self_check_passed": True},
        target_window_state_provider=provider,
        on_target_window_error=errors.append,
    )
    label = _FakeLabel()
    view._target_window_label = label

    previous = view.refresh_target_window()
    failed = view.refresh_target_window()

    assert failed == previous
    assert label.text == previous
    assert view.target_window_state is ready
    assert len(errors) == 1
    assert isinstance(errors[0], OSError)


def test_invalid_provider_value_is_rejected_without_replacing_state() -> None:
    previous = _observation(
        configured=False,
        safe=False,
        code="window.not_configured",
    )
    errors: list[Exception] = []
    view = HomeView(
        None,
        {"self_check_passed": True},
        target_window_state=previous,
        target_window_state_provider=lambda: object(),
        on_target_window_error=errors.append,
    )

    text = view.refresh_target_window()

    assert view.target_window_state is previous
    assert "尚未設定遊戲視窗" in text
    assert len(errors) == 1
    assert isinstance(errors[0], TypeError)


def test_refresh_raises_when_no_error_boundary_is_available() -> None:
    view = HomeView(
        None,
        {"self_check_passed": True},
        target_window_state_provider=lambda: object(),
    )

    with pytest.raises(TypeError, match="TargetWindowObservation"):
        view.refresh_target_window()


def test_label_update_failure_keeps_previous_snapshot_and_reports_error() -> None:
    previous = _observation(
        configured=False,
        safe=False,
        code="window.not_configured",
    )
    ready = _observation(
        configured=True,
        safe=True,
        code="window.ready",
    )
    errors: list[Exception] = []

    class _FailingLabel:
        def configure(self, *, text: str) -> None:
            raise RuntimeError(f"label unavailable: {text}")

    view = HomeView(
        None,
        {"self_check_passed": True},
        target_window_state=previous,
        target_window_state_provider=lambda: ready,
        on_target_window_error=errors.append,
    )
    view._target_window_label = _FailingLabel()

    text = view.refresh_target_window()

    assert view.target_window_state is previous
    assert text == "目前狀態\n● 尚未設定遊戲視窗"
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
