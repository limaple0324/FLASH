"""只觀察、等待玩家確認，不自行套用活動順序的習慣服務。"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

from habit.models import (
    ActivityOrderHabitMemory,
    ActivityOrderObservation,
    ActivityOrderReview,
    HabitReviewState,
)
from habit.store import ActivityOrderHabitStore


MIN_OBSERVATION_DAYS = 7


class ActivityOrderHabitService:
    def __init__(self, store: ActivityOrderHabitStore):
        if not isinstance(store, ActivityOrderHabitStore):
            raise TypeError("store must be ActivityOrderHabitStore.")
        self.store = store
        self._memory = store.load()

    def snapshot(self) -> ActivityOrderHabitMemory:
        return self._memory

    def record_daily_order(
        self,
        observed_on: date,
        activity_ids: tuple[str, ...],
        *,
        is_exception: bool = False,
    ) -> ActivityOrderObservation:
        observation = ActivityOrderObservation(
            observed_on=observed_on,
            activity_ids=activity_ids,
            is_exception=is_exception,
        )
        observations = {
            item.observed_on: item for item in self._memory.observations
        }
        observations[observed_on] = observation
        return self._replace(
            observations=tuple(observations.values()),
        ).observations[
            tuple(item.observed_on for item in self._memory.observations).index(
                observed_on
            )
        ]

    def remove_observation(self, observed_on: date) -> bool:
        observations = tuple(
            item for item in self._memory.observations if item.observed_on != observed_on
        )
        if observations == self._memory.observations:
            return False
        self._replace(observations=observations)
        return True

    def review(self, as_of: date) -> ActivityOrderReview:
        if not isinstance(as_of, date):
            raise TypeError("as_of must be date.")
        observations = tuple(
            item for item in self._memory.observations if item.observed_on <= as_of
        )
        valid = tuple(item for item in observations if not item.is_exception)
        counts = Counter(item.activity_ids for item in valid)
        order_counts = tuple(
            sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        )

        if self._memory.accepted_order is not None:
            state = (
                HabitReviewState.PAUSED
                if self._memory.paused
                else HabitReviewState.ACCEPTED
            )
        elif self._memory.dismissed_through is not None and not any(
            item.observed_on > self._memory.dismissed_through for item in valid
        ):
            state = HabitReviewState.DISMISSED
        elif len(valid) < MIN_OBSERVATION_DAYS:
            state = HabitReviewState.OBSERVING
        elif as_of < valid[0].observed_on + timedelta(days=MIN_OBSERVATION_DAYS):
            state = HabitReviewState.OBSERVING
        else:
            state = HabitReviewState.REVIEW_READY

        return ActivityOrderReview(
            state=state,
            total_observed_days=len(observations),
            valid_observed_days=len(valid),
            order_counts=order_counts,
            accepted_order=self._memory.accepted_order,
        )

    def accept(self, activity_ids: tuple[str, ...]) -> ActivityOrderHabitMemory:
        return self._replace(
            accepted_order=tuple(activity_ids),
            paused=False,
            dismissed_through=None,
        )

    def modify(self, activity_ids: tuple[str, ...]) -> ActivityOrderHabitMemory:
        if self._memory.accepted_order is None:
            raise ValueError("No accepted activity-order habit to modify.")
        return self.accept(activity_ids)

    def set_paused(self, paused: bool) -> ActivityOrderHabitMemory:
        if not isinstance(paused, bool):
            raise TypeError("paused must be bool.")
        if self._memory.accepted_order is None:
            raise ValueError("No accepted activity-order habit to pause.")
        return self._replace(paused=paused)

    def dismiss_review(self) -> ActivityOrderHabitMemory:
        if not self._memory.observations:
            return self._memory
        return self._replace(
            dismissed_through=self._memory.observations[-1].observed_on
        )

    def clear_all(self) -> ActivityOrderHabitMemory:
        return self._set_memory(ActivityOrderHabitMemory())

    def _replace(self, **changes: object) -> ActivityOrderHabitMemory:
        values = self._memory.to_dict()
        values.update(changes)
        memory = ActivityOrderHabitMemory(
            observations=tuple(
                values["observations"]
                if isinstance(values["observations"], tuple)
                else self._memory.observations
            ),
            accepted_order=(
                values["accepted_order"]
                if isinstance(values["accepted_order"], tuple)
                or values["accepted_order"] is None
                else tuple(values["accepted_order"])
            ),
            paused=bool(values["paused"]),
            dismissed_through=(
                values["dismissed_through"]
                if isinstance(values["dismissed_through"], date)
                or values["dismissed_through"] is None
                else date.fromisoformat(str(values["dismissed_through"]))
            ),
        )
        return self._set_memory(memory)

    def _set_memory(
        self,
        memory: ActivityOrderHabitMemory,
    ) -> ActivityOrderHabitMemory:
        self.store.save(memory)
        self._memory = memory
        return memory
