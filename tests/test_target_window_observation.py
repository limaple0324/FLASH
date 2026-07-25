from dataclasses import FrozenInstanceError

import pytest

from core.target_window_observation import TargetWindowObservation


def test_detection_becomes_a_player_safe_immutable_observation() -> None:
    observation = TargetWindowObservation.from_detection(
        {
            "configured": True,
            "safe": True,
            "code": "window.ready",
            "message": "raw adapter message",
            "details": {
                "handle": 918273,
                "process_id": 456,
                "rect": (0, 0, 800, 600),
                "path": r"C:\private\game.exe",
            },
        }
    )

    assert observation.configured is True
    assert observation.safe is True
    assert observation.code == "window.ready"
    assert not hasattr(observation, "details")
    assert not hasattr(observation, "player_message")
    with pytest.raises(FrozenInstanceError):
        observation.safe = False


def test_unsafe_detection_keeps_only_the_safe_structured_fact() -> None:
    observation = TargetWindowObservation.from_detection(
        {
            "configured": True,
            "safe": False,
            "code": "window.ambiguous",
            "message": "private English adapter message",
            "details": {"handle": 111},
        }
    )

    assert observation == TargetWindowObservation(
        configured=True,
        safe=False,
        code="window.ambiguous",
    )
    assert not hasattr(observation, "details")


def test_unconfigured_detection_never_becomes_safe() -> None:
    observation = TargetWindowObservation.from_detection(
        {
            "configured": False,
            "safe": False,
            "code": "window.not_configured",
            "details": {"keywords": ["secret"]},
        }
    )

    assert observation == TargetWindowObservation(
        configured=False,
        safe=False,
        code="window.not_configured",
    )

    with pytest.raises(ValueError, match="cannot be safe"):
        TargetWindowObservation.from_detection(
            {
                "configured": False,
                "safe": True,
                "code": "window.ready",
            }
        )


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {"configured": "yes", "safe": False, "code": "window.not_found"},
        {"configured": True, "safe": 0, "code": "window.not_found"},
        {"configured": True, "safe": False, "code": ""},
    ),
)
def test_untrusted_detection_payload_is_rejected(payload: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        TargetWindowObservation.from_detection(payload)

