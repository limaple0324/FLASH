import pytest

from domain.status import ActivityStatus


def test_activity_status_contains_only_the_confirmed_three_states():
    assert [item.value for item in ActivityStatus] == ["待命中", "執行中", "已完成"]


def test_activity_status_rejects_unconfirmed_extra_state():
    with pytest.raises(ValueError):
        ActivityStatus("中斷中")
