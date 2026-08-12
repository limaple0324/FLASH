import pytest

from services.reconnect_failure_status_service import (
    ReconnectFailureStatusService,
)


def test_same_role_updates_one_status_row() -> None:
    service = ReconnectFailureStatusService()

    service.report("role:a", "120古")
    service.report("role:a", "120古")

    assert tuple(item.message for item in service.snapshot()) == (
        "120古－重連失敗",
    )


def test_independent_roles_remain_independent_and_clear_on_success() -> None:
    service = ReconnectFailureStatusService()
    service.report("role:a", "120古")
    service.report("role:b", "120靈")

    assert service.clear("role:a") is True
    assert tuple(item.message for item in service.snapshot()) == (
        "120靈－重連失敗",
    )
    assert service.clear("role:a") is False


def test_invalid_player_visible_subject_is_rejected() -> None:
    service = ReconnectFailureStatusService()

    with pytest.raises(ValueError):
        service.report("role:a", "\n")
