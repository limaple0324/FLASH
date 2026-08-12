"""活動進度、原子保存、單次事件與每日 00:00 重置。"""

from dataclasses import dataclass
from datetime import datetime
from threading import RLock

from domain.activity import ActivityDefinition
from domain.progress import (
    ActivityInterruptionReason,
    ActivityProgress,
    TAIPEI_TIMEZONE,
)
from domain.progress_store import ActivityProgressStore
from domain.status import ActivityStatus
from services.event_bus import EventBus


ACTIVITY_PROGRESS_CHANGED_EVENT = "activity_progress_changed"


@dataclass(frozen=True, slots=True)
class ActivityProgressChange:
    reason: str
    changed_at: datetime
    previous: ActivityProgress
    current: ActivityProgress


class ActivityProgressService:
    def __init__(
        self,
        store: ActivityProgressStore,
        event_bus: EventBus | None = None,
    ):
        if not isinstance(store, ActivityProgressStore):
            raise TypeError("store must be ActivityProgressStore.")
        if event_bus is not None and not isinstance(event_bus, EventBus):
            raise TypeError("event_bus must be EventBus.")
        self.store = store
        self._event_bus = event_bus
        self._lock = RLock()
        loaded = store.load()
        self._progress = {(item.activity_id, item.subject_id): item for item in loaded}
        self._definitions: dict[str, ActivityDefinition] = {}

    def register_definition(self, definition: ActivityDefinition) -> None:
        with self._lock:
            current = self._definitions.get(definition.activity_id)
            if current is not None and current != definition:
                raise ValueError("Activity ID is already registered with another definition.")
            self._definitions[definition.activity_id] = definition

    def definition(self, activity_id: str) -> ActivityDefinition:
        with self._lock:
            try:
                return self._definitions[activity_id.strip()]
            except KeyError as exc:
                raise KeyError(f"Unknown activity: {activity_id}") from exc

    def start(self, activity_id: str, subject_id: str, at: datetime) -> ActivityProgress:
        with self._lock:
            self.definition(activity_id)
            key = (activity_id.strip(), subject_id.strip())
            previous = self._progress.get(key) or ActivityProgress(
                activity_id=key[0], subject_id=key[1]
            )
            progress = previous.start(at)
            return self._replace(previous, progress, "started", at)

    def record_completion(
        self,
        activity_id: str,
        subject_id: str,
        at: datetime,
    ) -> ActivityProgress:
        with self._lock:
            definition = self.definition(activity_id)
            key = (activity_id.strip(), subject_id.strip())
            previous = self._progress.get(key) or ActivityProgress(
                activity_id=key[0], subject_id=key[1]
            )
            progress = previous.record_completion(definition, at)
            return self._replace(
                previous,
                progress,
                "completion_recorded",
                at,
            )

    @staticmethod
    def _subject_id(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("subject_id must be a non-empty string.")
        return value.strip()

    @staticmethod
    def _changed_at(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("changed_at must include timezone information.")
        return value

    def record_interruption(
        self,
        subject_id: object,
        reason: ActivityInterruptionReason,
        changed_at: datetime,
    ) -> tuple[ActivityProgress, ...]:
        subject_id = self._subject_id(subject_id)
        if not isinstance(reason, ActivityInterruptionReason):
            raise TypeError("reason must be ActivityInterruptionReason.")
        changed_at = self._changed_at(changed_at)
        with self._lock:
            candidate = dict(self._progress)
            changes: list[ActivityProgressChange] = []
            for key, progress in tuple(candidate.items()):
                if progress.subject_id != subject_id:
                    continue
                current = progress.record_interruption(reason, changed_at)
                if current == progress:
                    continue
                candidate[key] = current
                changes.append(
                    ActivityProgressChange(
                        reason="interrupted",
                        changed_at=changed_at,
                        previous=progress,
                        current=current,
                    )
                )
            return self._replace_many(candidate, changes)

    def clear_interruption(
        self,
        subject_id: object,
        changed_at: datetime,
    ) -> tuple[ActivityProgress, ...]:
        subject_id = self._subject_id(subject_id)
        changed_at = self._changed_at(changed_at)
        with self._lock:
            candidate = dict(self._progress)
            changes: list[ActivityProgressChange] = []
            for key, progress in tuple(candidate.items()):
                if progress.subject_id != subject_id:
                    continue
                current = progress.clear_interruption()
                if current == progress:
                    continue
                candidate[key] = current
                changes.append(
                    ActivityProgressChange(
                        reason="interruption_cleared",
                        changed_at=changed_at,
                        previous=progress,
                        current=current,
                    )
                )
            return self._replace_many(candidate, changes)

    def reset_due(self, now: datetime) -> tuple[ActivityProgress, ...]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must include timezone information.")
        with self._lock:
            local_now = now.astimezone(TAIPEI_TIMEZONE)
            candidate = dict(self._progress)
            changes: list[ActivityProgressChange] = []
            for key, progress in tuple(candidate.items()):
                definition = self._definitions.get(progress.activity_id)
                if definition is None:
                    continue
                reset = progress.reset_if_due(definition, local_now)
                if reset != progress:
                    candidate[key] = reset
                    changes.append(
                        ActivityProgressChange(
                            reason=(
                                "running_carried_over"
                                if progress.status is ActivityStatus.RUNNING
                                else "daily_reset"
                            ),
                            changed_at=local_now,
                            previous=progress,
                            current=reset,
                        )
                    )
            self._replace_many(candidate, changes)
            return self.all()

    def all(self) -> tuple[ActivityProgress, ...]:
        with self._lock:
            return tuple(self._progress[key] for key in sorted(self._progress))

    def _replace(
        self,
        previous: ActivityProgress,
        current: ActivityProgress,
        reason: str,
        changed_at: datetime,
    ) -> ActivityProgress:
        if current == previous:
            return previous
        candidate = dict(self._progress)
        candidate[(current.activity_id, current.subject_id)] = current
        self.store.save(tuple(candidate[key] for key in sorted(candidate)))
        self._progress = candidate
        self._publish(
            ActivityProgressChange(
                reason=reason,
                changed_at=changed_at,
                previous=previous,
                current=current,
            )
        )
        return current

    def _replace_many(
        self,
        candidate: dict[tuple[str, str], ActivityProgress],
        changes: list[ActivityProgressChange],
    ) -> tuple[ActivityProgress, ...]:
        if not changes:
            return ()
        self.store.save(tuple(candidate[key] for key in sorted(candidate)))
        self._progress = candidate
        for change in changes:
            self._publish(change)
        return tuple(change.current for change in changes)

    def _publish(self, change: ActivityProgressChange) -> None:
        if self._event_bus is not None:
            self._event_bus.publish(
                ACTIVITY_PROGRESS_CHANGED_EVENT,
                change,
            )
