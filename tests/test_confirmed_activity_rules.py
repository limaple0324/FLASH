from datetime import datetime, timezone

import pytest

from domain.character import Character
from domain.confirmed_activity_rules import (
    ConfirmedActivityEvent,
    ConfirmedActivityEventType,
    ConfirmedActivityKind,
    ConfirmedActivityRecord,
)
from domain.group import CharacterGroup


def _group() -> CharacterGroup:
    return CharacterGroup(
        "group-14",
        "14支",
        (
            Character("role-120", "120古", 120),
            Character("role-160", "160嶽", 160),
        ),
    )


def test_typed_events_reject_unknown_activity_event_pairs_and_unbound_roles():
    group = _group()
    at = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="not confirmed"):
        ConfirmedActivityEvent(
            ConfirmedActivityKind.MAGIC_SOLDIERS,
            ConfirmedActivityEventType.TRAINING_STARTED,
            group,
            at,
            "role-120",
        )
    with pytest.raises(ValueError, match="belong"):
        ConfirmedActivityEvent(
            ConfirmedActivityKind.DIMENSION_SPACE,
            ConfirmedActivityEventType.DIMENSION_ENTERED,
            group,
            at,
            "unregistered-role",
            floor=1,
        )
    with pytest.raises(ValueError, match="floor"):
        ConfirmedActivityEvent(
            ConfirmedActivityKind.DIMENSION_SPACE,
            ConfirmedActivityEventType.DIMENSION_ENTERED,
            group,
            at,
            "role-120",
            floor=3,
        )
    for invalid_floor in (True, 1.0):
        with pytest.raises(ValueError, match="floor"):
            ConfirmedActivityEvent(
                ConfirmedActivityKind.DIMENSION_SPACE,
                ConfirmedActivityEventType.DIMENSION_ENTERED,
                group,
                at,
                "role-120",
                floor=invalid_floor,
            )
    with pytest.raises(ValueError, match="group-level"):
        ConfirmedActivityEvent(
            ConfirmedActivityKind.ESTATE_FIRST_ROUND,
            ConfirmedActivityEventType.ESTATE_FIRST_OPENED,
            group,
            at,
            "role-120",
        )


def test_record_round_trip_keeps_only_stable_subject_group_and_timestamps():
    group = _group()
    record = ConfirmedActivityRecord(
        record_id="fantasy-training:role-120",
        activity=ConfirmedActivityKind.FANTASY_TRAINING,
        group=group,
        scope_id="role-120",
        subject_id="role-120",
        day=datetime(2026, 8, 3, tzinfo=timezone.utc).date(),
        started_at=datetime(2026, 8, 3, 12, tzinfo=timezone.utc),
        duration_seconds=600,
        stage="修練中",
    )

    restored = ConfirmedActivityRecord.from_dict(record.to_dict())

    assert restored == record
