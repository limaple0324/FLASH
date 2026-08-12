"""只觀察、提醒並等待玩家決定，不操作遊戲的習慣服務。"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from habit.preference_models import (
    ASK_LATER_MINUTES,
    MINIMUM_DISTINCT_DAYS,
    MINIMUM_OCCURRENCES,
    OBSERVATION_RETENTION_DAYS,
    HabitDecision,
    HabitKind,
    PlayerHabitCandidate,
    PlayerHabitMemory,
    PlayerHabitObservation,
    PlayerHabitPreference,
    PlayerHabitSettings,
)
from habit.preference_store import PlayerHabitStore


@dataclass(frozen=True, slots=True)
class PlayerHabitPreferenceView:
    preference_id: str
    kind: str
    subject: str
    values: tuple[str, ...]
    description: str
    decision: str


@dataclass(frozen=True, slots=True)
class PlayerHabitObservationView:
    observation_id: str
    observed_at: datetime
    kind: str
    subject: str
    values: tuple[str, ...]
    is_exception: bool
    source_event_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlayerHabitSettingsView:
    observation_days: int
    minimum_occurrences: int
    minimum_distinct_days: int
    preferences: tuple[PlayerHabitPreferenceView, ...]
    observations: tuple[PlayerHabitObservationView, ...] = ()


class PlayerHabitPreferenceService:
    """保存觀察與玩家選擇；本服務沒有任何遊戲輸入能力。"""

    def __init__(self, store: PlayerHabitStore):
        if not isinstance(store, PlayerHabitStore):
            raise TypeError("store must be PlayerHabitStore.")
        self.store = store
        self._memory = store.load()

    def snapshot(self) -> PlayerHabitMemory:
        return self._memory

    @staticmethod
    def _candidate_id(
        kind: HabitKind,
        subject: str,
        values: tuple[str, ...],
    ) -> str:
        canonical = json.dumps(
            [kind.value, subject.strip(), list(values)],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _observation_id(observation: PlayerHabitObservation) -> str:
        canonical = json.dumps(
            observation.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _save(
        self,
        *,
        settings: PlayerHabitSettings | None = None,
        observations: tuple[PlayerHabitObservation, ...] | None = None,
        preferences: tuple[PlayerHabitPreference, ...] | None = None,
    ) -> PlayerHabitMemory:
        memory = PlayerHabitMemory(
            settings=settings or self._memory.settings,
            observations=(
                observations
                if observations is not None
                else self._memory.observations
            ),
            preferences=(
                preferences
                if preferences is not None
                else self._memory.preferences
            ),
        )
        self.store.save(memory)
        self._memory = memory
        return memory

    def set_observation_days(self, days: int) -> PlayerHabitMemory:
        return self._save(settings=PlayerHabitSettings(days))

    def record_activity_completion(
        self,
        activity_id: str,
        subject_id: str,
        observed_at: datetime,
        *,
        source_event_id: str,
        activity_name: str | None = None,
    ) -> tuple[PlayerHabitObservation, ...]:
        source_event_id = source_event_id.strip()
        if not source_event_id:
            raise ValueError("source_event_id must not be empty.")
        activity_label = (
            activity_name.strip()
            if activity_name is not None
            else activity_id.strip()
        )
        if not activity_label:
            raise ValueError("activity_name must not be empty.")
        if any(
            source_event_id in item.source_event_ids
            for item in self._memory.observations
        ):
            return ()

        activity_time = PlayerHabitObservation(
            observed_at=observed_at,
            kind=HabitKind.ACTIVITY_TIME,
            subject=activity_label,
            values=(observed_at.strftime("%H:%M"),),
            source_event_ids=(source_event_id,),
        )
        local_day = observed_at.date()
        current_order = next(
            (
                item
                for item in self._memory.observations
                if item.kind is HabitKind.CHARACTER_ORDER
                and item.subject == subject_id.strip()
                and item.observed_at.date() == local_day
            ),
            None,
        )
        if current_order is None:
            order_values = (activity_label,)
            source_event_ids = (source_event_id,)
        else:
            order_values = current_order.values
            if activity_label not in order_values:
                order_values += (activity_label,)
            source_event_ids = current_order.source_event_ids + (
                source_event_id,
            )
        daily_order = PlayerHabitObservation(
            observed_at=observed_at,
            kind=HabitKind.CHARACTER_ORDER,
            subject=subject_id,
            values=order_values,
            source_event_ids=source_event_ids,
        )
        retained = tuple(
            item
            for item in self._memory.observations
            if item is not current_order
        )
        self._save(
            observations=retained + (activity_time, daily_order)
        )
        return activity_time, daily_order

    def candidates(self, as_of: datetime) -> tuple[PlayerHabitCandidate, ...]:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware.")
        self.cleanup_expired(as_of)
        grouped: dict[
            tuple[HabitKind, str, tuple[str, ...]],
            list[PlayerHabitObservation],
        ] = defaultdict(list)
        for observation in self._memory.observations:
            if observation.is_exception or observation.observed_at > as_of:
                continue
            grouped[
                (
                    observation.kind,
                    observation.subject,
                    observation.values,
                )
            ].append(observation)

        eligible: list[PlayerHabitCandidate] = []
        for (kind, subject, values), observations in grouped.items():
            days = {item.observed_at.date() for item in observations}
            first = min(days)
            last = max(days)
            if len(observations) < MINIMUM_OCCURRENCES:
                continue
            if len(days) < MINIMUM_DISTINCT_DAYS:
                continue
            if as_of.date() < first + timedelta(
                days=self._memory.settings.observation_days
            ):
                continue
            eligible.append(
                PlayerHabitCandidate(
                    candidate_id=self._candidate_id(kind, subject, values),
                    kind=kind,
                    subject=subject,
                    values=values,
                    occurrence_count=len(observations),
                    distinct_days=len(days),
                    first_observed_on=first,
                    last_observed_on=last,
                )
            )

        # 同一類型與主題若同時有兩種都達完整門檻的結果，即屬明顯相反，
        # 不替玩家挑選其中一種。
        eligible_counts: dict[tuple[HabitKind, str], int] = defaultdict(int)
        for candidate in eligible:
            eligible_counts[(candidate.kind, candidate.subject)] += 1

        decisions = {
            preference.candidate_id: preference
            for preference in self._memory.preferences
        }
        visible: list[PlayerHabitCandidate] = []
        for candidate in eligible:
            if eligible_counts[(candidate.kind, candidate.subject)] > 1:
                continue
            preference = decisions.get(candidate.candidate_id)
            if preference is None:
                visible.append(candidate)
                continue
            if preference.decision in (
                HabitDecision.ADOPTED,
                HabitDecision.NEVER_ASK,
            ):
                continue
            if (
                preference.decision is HabitDecision.TODAY_ONLY
                and preference.applies_on == as_of.date()
            ):
                continue
            if (
                preference.decision is HabitDecision.SNOOZED
                and preference.remind_after is not None
                and as_of < preference.remind_after
            ):
                continue
            visible.append(candidate)
        return tuple(
            sorted(
                visible,
                key=lambda item: (
                    item.kind.value,
                    item.subject,
                    item.values,
                ),
            )
        )

    def choose(
        self,
        candidate: PlayerHabitCandidate,
        decision: HabitDecision,
        decided_at: datetime,
    ) -> PlayerHabitPreference:
        if not isinstance(candidate, PlayerHabitCandidate):
            raise TypeError("candidate must be PlayerHabitCandidate.")
        if not isinstance(decision, HabitDecision):
            raise TypeError("decision must be HabitDecision.")
        if decided_at.tzinfo is None or decided_at.utcoffset() is None:
            raise ValueError("decided_at must be timezone-aware.")
        preference = PlayerHabitPreference(
            candidate_id=candidate.candidate_id,
            kind=candidate.kind,
            subject=candidate.subject,
            values=candidate.values,
            decision=decision,
            decided_at=decided_at,
            applies_on=(
                decided_at.date()
                if decision is HabitDecision.TODAY_ONLY
                else None
            ),
            remind_after=(
                decided_at + timedelta(minutes=ASK_LATER_MINUTES)
                if decision is HabitDecision.SNOOZED
                else None
            ),
        )
        remaining = tuple(
            item
            for item in self._memory.preferences
            if item.candidate_id != candidate.candidate_id
        )
        self._save(preferences=remaining + (preference,))
        return preference

    def modify_preference(
        self,
        preference_id: str,
        values: tuple[str, ...],
        changed_at: datetime,
    ) -> PlayerHabitPreference:
        if changed_at.tzinfo is None or changed_at.utcoffset() is None:
            raise ValueError("changed_at must be timezone-aware.")
        current = next(
            (
                item
                for item in self._memory.preferences
                if item.candidate_id == preference_id
            ),
            None,
        )
        if current is None:
            raise KeyError(preference_id)
        replacement = PlayerHabitPreference(
            candidate_id=current.candidate_id,
            kind=current.kind,
            subject=current.subject,
            values=values,
            decision=current.decision,
            decided_at=changed_at,
            applies_on=current.applies_on,
            remind_after=current.remind_after,
        )
        self._save(
            preferences=tuple(
                replacement if item.candidate_id == preference_id else item
                for item in self._memory.preferences
            )
        )
        return replacement

    def remove_preference(self, preference_id: str) -> bool:
        remaining = tuple(
            item
            for item in self._memory.preferences
            if item.candidate_id != preference_id
        )
        if remaining == self._memory.preferences:
            return False
        self._save(preferences=remaining)
        return True

    def remove_observation(self, observation_id: str) -> bool:
        observation_id = observation_id.strip()
        if not observation_id:
            raise ValueError("observation_id must not be empty.")
        observations = tuple(
            item
            for item in self._memory.observations
            if self._observation_id(item) != observation_id
        )
        if observations == self._memory.observations:
            return False
        self._save(observations=observations)
        return True

    def clear_all(self) -> int:
        count = len(self._memory.preferences) + len(
            self._memory.observations
        )
        if count:
            self._save(observations=(), preferences=())
        return count

    def cleanup_expired(self, as_of: datetime) -> int:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware.")
        cutoff = as_of.date() - timedelta(days=OBSERVATION_RETENTION_DAYS)
        observations = tuple(
            item
            for item in self._memory.observations
            if item.observed_at.date() > cutoff
        )
        preferences = tuple(
            item
            for item in self._memory.preferences
            if (
                item.decision is HabitDecision.ADOPTED
                or (
                    item.decision is HabitDecision.NEVER_ASK
                    and item.decided_at.date() > cutoff
                )
                or (
                    item.decision is HabitDecision.TODAY_ONLY
                    and item.applies_on is not None
                    and item.applies_on >= as_of.date()
                )
                or (
                    item.decision is HabitDecision.SNOOZED
                    and item.remind_after is not None
                    and item.remind_after > as_of
                )
            )
        )
        removed = (
            len(self._memory.observations)
            - len(observations)
            + len(self._memory.preferences)
            - len(preferences)
        )
        if removed:
            self._save(
                observations=observations,
                preferences=preferences,
            )
        return removed

    def settings_view(self) -> PlayerHabitSettingsView:
        views = tuple(
            PlayerHabitPreferenceView(
                preference_id=item.candidate_id,
                kind=item.kind.value,
                subject=item.subject,
                values=item.values,
                description=(
                    f"{item.kind.value}｜{item.subject}｜"
                    f"{' → '.join(item.values)}"
                ),
                decision=item.decision.value,
            )
            for item in self._memory.preferences
        )
        observations = tuple(
            PlayerHabitObservationView(
                observation_id=self._observation_id(item),
                observed_at=item.observed_at,
                kind=item.kind.value,
                subject=item.subject,
                values=item.values,
                is_exception=item.is_exception,
                source_event_ids=item.source_event_ids,
            )
            for item in reversed(self._memory.observations)
        )
        return PlayerHabitSettingsView(
            observation_days=self._memory.settings.observation_days,
            minimum_occurrences=MINIMUM_OCCURRENCES,
            minimum_distinct_days=MINIMUM_DISTINCT_DAYS,
            preferences=views,
            observations=observations,
        )
