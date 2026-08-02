"""活動進度、原子保存、單次事件與每日 00:00 重置。"""

from dataclasses import dataclass
from datetime import datetime

from domain.activity import ActivityDefinition
from domain.progress import ActivityProgress, TAIPEI_TIMEZONE
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
        loaded = store.load()
        self._progress = {(item.activity_id, item.subject_id): item for item in loaded}
        self._definitions: dict[str, ActivityDefinition] = {}

    def register_definition(self, definition: ActivityDefinition) -> None:
        current = self._definitions.get(definition.activity_id)
        if current is not None and current != definition:
            raise ValueError("Activity ID is already registered with another definition.")
        self._definitions[definition.activity_id] = definition

    def definition(self, activity_id: str) -> ActivityDefinition:
        try:
            return self._definitions[activity_id.strip()]
        except KeyError as exc:
            raise KeyError(f"Unknown activity: {activity_id}") from exc

    def get(self, activity_id: str, subject_id: str) -> ActivityProgress:
        key = (activity_id.strip(), subject_id.strip())
        progress = self._progress.get(key)
        if progress is None:
            progress = ActivityProgress(activity_id=key[0], subject_id=key[1])
        return progress

    def start(self, activity_id: str, subject_id: str, at: datetime) -> ActivityProgress:
        self.definition(activity_id)
        previous = self.get(activity_id, subject_id)
        progress = previous.start(at)
        return self._replace(previous, progress, "started", at)

    def record_completion(
        self,
        activity_id: str,
        subject_id: str,
        at: datetime,
    ) -> ActivityProgress:
        definition = self.definition(activity_id)
        previous = self.get(activity_id, subject_id)
        progress = previous.record_completion(definition, at)
        return self._replace(
            previous,
            progress,
            "completion_recorded",
            at,
        )

    def reset_due(self, now: datetime) -> tuple[ActivityProgress, ...]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must include timezone information.")
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
        if changes:
            self.store.save(
                tuple(candidate[key] for key in sorted(candidate))
            )
            self._progress = candidate
            for change in changes:
                self._publish(change)
        return self.all()

    def all(self) -> tuple[ActivityProgress, ...]:
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

    def _publish(self, change: ActivityProgressChange) -> None:
        if self._event_bus is not None:
            self._event_bus.publish(
                ACTIVITY_PROGRESS_CHANGED_EVENT,
                change,
            )
