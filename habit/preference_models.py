"""玩家習慣觀察、候選與確認結果的不可變資料模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Mapping


DEFAULT_OBSERVATION_DAYS = 7
MINIMUM_OCCURRENCES = 7
MINIMUM_DISTINCT_DAYS = 7
ASK_LATER_MINUTES = 10
OBSERVATION_RETENTION_DAYS = 30


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be str.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty.")
    return normalized


def _required_values(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    normalized = tuple(_required_text(value, field) for value in values)
    if not normalized:
        raise ValueError(f"{field} must not be empty.")
    return normalized


def _aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware.")
    return value


class HabitKind(str, Enum):
    ACTIVITY_TIME = "活動時間"
    CHARACTER_ORDER = "角色操作順序"


class HabitDecision(str, Enum):
    ADOPTED = "採用"
    TODAY_ONLY = "只限今天"
    NEVER_ASK = "不要記住"
    SNOOZED = "稍後再問"


@dataclass(frozen=True, slots=True)
class PlayerHabitSettings:
    observation_days: int = DEFAULT_OBSERVATION_DAYS

    def __post_init__(self) -> None:
        if (
            isinstance(self.observation_days, bool)
            or not isinstance(self.observation_days, int)
            or not DEFAULT_OBSERVATION_DAYS <= self.observation_days <= 365
        ):
            raise ValueError(
                "observation_days must be between 7 and 365."
            )

    def to_dict(self) -> dict[str, object]:
        return {"observation_days": self.observation_days}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "PlayerHabitSettings":
        stored_days = int(
            payload.get("observation_days", DEFAULT_OBSERVATION_DAYS)
        )
        return cls(
            observation_days=max(DEFAULT_OBSERVATION_DAYS, stored_days)
        )


@dataclass(frozen=True, slots=True)
class PlayerHabitObservation:
    observed_at: datetime
    kind: HabitKind
    subject: str
    values: tuple[str, ...]
    is_exception: bool = False
    source_event_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observed_at",
            _aware(self.observed_at, "observed_at"),
        )
        if not isinstance(self.kind, HabitKind):
            raise TypeError("kind must be HabitKind.")
        object.__setattr__(
            self,
            "subject",
            _required_text(self.subject, "subject"),
        )
        object.__setattr__(
            self,
            "values",
            _required_values(tuple(self.values), "values"),
        )
        if not isinstance(self.is_exception, bool):
            raise TypeError("is_exception must be bool.")
        source_event_ids = tuple(
            _required_text(value, "source_event_ids")
            for value in self.source_event_ids
        )
        if len(source_event_ids) != len(set(source_event_ids)):
            raise ValueError("source_event_ids must not contain duplicates.")
        object.__setattr__(self, "source_event_ids", source_event_ids)

    def to_dict(self) -> dict[str, object]:
        return {
            "observed_at": self.observed_at.isoformat(),
            "kind": self.kind.value,
            "subject": self.subject,
            "values": list(self.values),
            "is_exception": self.is_exception,
            "source_event_ids": list(self.source_event_ids),
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
    ) -> "PlayerHabitObservation":
        raw_values = payload.get("values")
        raw_source_event_ids = payload.get("source_event_ids", [])
        if not isinstance(raw_values, list) or any(
            not isinstance(value, str) for value in raw_values
        ):
            raise ValueError("values must be a list of strings.")
        if not isinstance(raw_source_event_ids, list) or any(
            not isinstance(value, str) for value in raw_source_event_ids
        ):
            raise ValueError("source_event_ids must be a list of strings.")
        return cls(
            observed_at=datetime.fromisoformat(
                _required_text(str(payload.get("observed_at", "")), "observed_at")
            ),
            kind=HabitKind(
                _required_text(str(payload.get("kind", "")), "kind")
            ),
            subject=_required_text(str(payload.get("subject", "")), "subject"),
            values=tuple(raw_values),
            is_exception=bool(payload.get("is_exception", False)),
            source_event_ids=tuple(raw_source_event_ids),
        )


@dataclass(frozen=True, slots=True)
class PlayerHabitCandidate:
    candidate_id: str
    kind: HabitKind
    subject: str
    values: tuple[str, ...]
    first_observed_on: date
    last_observed_on: date

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candidate_id",
            _required_text(self.candidate_id, "candidate_id"),
        )
        if not isinstance(self.kind, HabitKind):
            raise TypeError("kind must be HabitKind.")
        object.__setattr__(
            self,
            "subject",
            _required_text(self.subject, "subject"),
        )
        object.__setattr__(
            self,
            "values",
            _required_values(tuple(self.values), "values"),
        )
        if not isinstance(self.first_observed_on, date) or not isinstance(
            self.last_observed_on,
            date,
        ):
            raise TypeError("candidate dates must be date values.")


@dataclass(frozen=True, slots=True)
class PlayerHabitPreference:
    candidate_id: str
    kind: HabitKind
    subject: str
    values: tuple[str, ...]
    decision: HabitDecision
    decided_at: datetime
    applies_on: date | None = None
    remind_after: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candidate_id",
            _required_text(self.candidate_id, "candidate_id"),
        )
        if not isinstance(self.kind, HabitKind):
            raise TypeError("kind must be HabitKind.")
        object.__setattr__(
            self,
            "subject",
            _required_text(self.subject, "subject"),
        )
        object.__setattr__(
            self,
            "values",
            _required_values(tuple(self.values), "values"),
        )
        if not isinstance(self.decision, HabitDecision):
            raise TypeError("decision must be HabitDecision.")
        object.__setattr__(
            self,
            "decided_at",
            _aware(self.decided_at, "decided_at"),
        )
        if self.applies_on is not None and not isinstance(self.applies_on, date):
            raise TypeError("applies_on must be date or None.")
        if self.remind_after is not None:
            object.__setattr__(
                self,
                "remind_after",
                _aware(self.remind_after, "remind_after"),
            )
        if self.decision is HabitDecision.TODAY_ONLY and self.applies_on is None:
            raise ValueError("TODAY_ONLY requires applies_on.")
        if self.decision is HabitDecision.SNOOZED and self.remind_after is None:
            raise ValueError("SNOOZED requires remind_after.")

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "kind": self.kind.value,
            "subject": self.subject,
            "values": list(self.values),
            "decision": self.decision.value,
            "decided_at": self.decided_at.isoformat(),
            "applies_on": self.applies_on.isoformat() if self.applies_on else None,
            "remind_after": (
                self.remind_after.isoformat() if self.remind_after else None
            ),
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
    ) -> "PlayerHabitPreference":
        raw_values = payload.get("values")
        if not isinstance(raw_values, list) or any(
            not isinstance(value, str) for value in raw_values
        ):
            raise ValueError("values must be a list of strings.")
        applies_on = payload.get("applies_on")
        remind_after = payload.get("remind_after")
        return cls(
            candidate_id=_required_text(
                str(payload.get("candidate_id", "")),
                "candidate_id",
            ),
            kind=HabitKind(
                _required_text(str(payload.get("kind", "")), "kind")
            ),
            subject=_required_text(str(payload.get("subject", "")), "subject"),
            values=tuple(raw_values),
            decision=HabitDecision(
                _required_text(str(payload.get("decision", "")), "decision")
            ),
            decided_at=datetime.fromisoformat(
                _required_text(str(payload.get("decided_at", "")), "decided_at")
            ),
            applies_on=(
                date.fromisoformat(applies_on)
                if isinstance(applies_on, str)
                else None
            ),
            remind_after=(
                datetime.fromisoformat(remind_after)
                if isinstance(remind_after, str)
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class PlayerHabitMemory:
    settings: PlayerHabitSettings = PlayerHabitSettings()
    observations: tuple[PlayerHabitObservation, ...] = ()
    preferences: tuple[PlayerHabitPreference, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.settings, PlayerHabitSettings):
            raise TypeError("settings must be PlayerHabitSettings.")
        if any(
            not isinstance(item, PlayerHabitObservation)
            for item in self.observations
        ):
            raise TypeError("observations must contain PlayerHabitObservation.")
        if any(
            not isinstance(item, PlayerHabitPreference)
            for item in self.preferences
        ):
            raise TypeError("preferences must contain PlayerHabitPreference.")
        ids = [item.candidate_id for item in self.preferences]
        if len(ids) != len(set(ids)):
            raise ValueError("Only one decision is allowed per candidate.")
        object.__setattr__(
            self,
            "observations",
            tuple(sorted(self.observations, key=lambda item: item.observed_at)),
        )
        object.__setattr__(
            self,
            "preferences",
            tuple(sorted(self.preferences, key=lambda item: item.decided_at)),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "settings": self.settings.to_dict(),
            "observations": [item.to_dict() for item in self.observations],
            "preferences": [item.to_dict() for item in self.preferences],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "PlayerHabitMemory":
        raw_settings = payload.get("settings", {})
        raw_observations = payload.get("observations", [])
        raw_preferences = payload.get("preferences", [])
        if not isinstance(raw_settings, Mapping):
            raise ValueError("settings must be an object.")
        if not isinstance(raw_observations, list) or any(
            not isinstance(item, Mapping) for item in raw_observations
        ):
            raise ValueError("observations must be a list of objects.")
        if not isinstance(raw_preferences, list) or any(
            not isinstance(item, Mapping) for item in raw_preferences
        ):
            raise ValueError("preferences must be a list of objects.")
        return cls(
            settings=PlayerHabitSettings.from_dict(raw_settings),
            observations=tuple(
                PlayerHabitObservation.from_dict(item)
                for item in raw_observations
            ),
            preferences=tuple(
                PlayerHabitPreference.from_dict(item)
                for item in raw_preferences
            ),
        )
