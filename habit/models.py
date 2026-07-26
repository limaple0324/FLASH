"""玩家每日活動順序的不可變觀察、記憶與回顧狀態。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Mapping


def _activity_order(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    normalized = tuple(item.strip() for item in values)
    if not normalized or any(not item for item in normalized):
        raise ValueError(f"{field} must contain non-empty activity IDs.")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field} cannot contain duplicate activity IDs.")
    return normalized


class HabitReviewState(str, Enum):
    OBSERVING = "觀察中"
    REVIEW_READY = "等待玩家確認"
    DISMISSED = "玩家暫不採用"
    ACCEPTED = "玩家已採用"
    PAUSED = "玩家已暫停"


@dataclass(frozen=True, slots=True)
class ActivityOrderObservation:
    observed_on: date
    activity_ids: tuple[str, ...]
    is_exception: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.observed_on, date):
            raise TypeError("observed_on must be date.")
        if not isinstance(self.is_exception, bool):
            raise TypeError("is_exception must be bool.")
        object.__setattr__(
            self,
            "activity_ids",
            _activity_order(tuple(self.activity_ids), "activity_ids"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "observed_on": self.observed_on.isoformat(),
            "activity_ids": list(self.activity_ids),
            "is_exception": self.is_exception,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ActivityOrderObservation":
        observed_on = payload.get("observed_on")
        activity_ids = payload.get("activity_ids")
        is_exception = payload.get("is_exception", False)
        if not isinstance(observed_on, str):
            raise ValueError("observed_on must be an ISO date string.")
        if not isinstance(activity_ids, list) or any(
            not isinstance(item, str) for item in activity_ids
        ):
            raise ValueError("activity_ids must be a list of strings.")
        if not isinstance(is_exception, bool):
            raise ValueError("is_exception must be bool.")
        return cls(
            observed_on=date.fromisoformat(observed_on),
            activity_ids=tuple(activity_ids),
            is_exception=is_exception,
        )


@dataclass(frozen=True, slots=True)
class ActivityOrderHabitMemory:
    observations: tuple[ActivityOrderObservation, ...] = ()
    accepted_order: tuple[str, ...] | None = None
    paused: bool = False
    dismissed_through: date | None = None

    def __post_init__(self) -> None:
        observations = tuple(self.observations)
        if any(not isinstance(item, ActivityOrderObservation) for item in observations):
            raise TypeError("observations must contain ActivityOrderObservation values.")
        dates = [item.observed_on for item in observations]
        if len(dates) != len(set(dates)):
            raise ValueError("Only one activity-order observation is allowed per day.")
        if not isinstance(self.paused, bool):
            raise TypeError("paused must be bool.")
        if self.dismissed_through is not None and not isinstance(
            self.dismissed_through, date
        ):
            raise TypeError("dismissed_through must be date or None.")
        accepted_order = self.accepted_order
        if accepted_order is not None:
            accepted_order = _activity_order(tuple(accepted_order), "accepted_order")
        elif self.paused:
            raise ValueError("A missing accepted habit cannot be paused.")
        object.__setattr__(
            self,
            "observations",
            tuple(sorted(observations, key=lambda item: item.observed_on)),
        )
        object.__setattr__(self, "accepted_order", accepted_order)

    def to_dict(self) -> dict[str, object]:
        return {
            "observations": [item.to_dict() for item in self.observations],
            "accepted_order": (
                list(self.accepted_order) if self.accepted_order is not None else None
            ),
            "paused": self.paused,
            "dismissed_through": (
                self.dismissed_through.isoformat() if self.dismissed_through else None
            ),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ActivityOrderHabitMemory":
        raw_observations = payload.get("observations", [])
        accepted_order = payload.get("accepted_order")
        paused = payload.get("paused", False)
        dismissed_through = payload.get("dismissed_through")
        if not isinstance(raw_observations, list) or any(
            not isinstance(item, Mapping) for item in raw_observations
        ):
            raise ValueError("observations must be a list of objects.")
        if accepted_order is not None and (
            not isinstance(accepted_order, list)
            or any(not isinstance(item, str) for item in accepted_order)
        ):
            raise ValueError("accepted_order must be a list of strings or null.")
        if not isinstance(paused, bool):
            raise ValueError("paused must be bool.")
        if dismissed_through is not None and not isinstance(dismissed_through, str):
            raise ValueError("dismissed_through must be an ISO date string or null.")
        return cls(
            observations=tuple(
                ActivityOrderObservation.from_dict(item) for item in raw_observations
            ),
            accepted_order=(
                tuple(accepted_order) if accepted_order is not None else None
            ),
            paused=paused,
            dismissed_through=(
                date.fromisoformat(dismissed_through)
                if dismissed_through is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ActivityOrderReview:
    state: HabitReviewState
    total_observed_days: int
    valid_observed_days: int
    order_counts: tuple[tuple[tuple[str, ...], int], ...]
    accepted_order: tuple[str, ...] | None
