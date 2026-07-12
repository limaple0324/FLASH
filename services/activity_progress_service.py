"""活動定義與進度的應用服務。"""

from datetime import datetime

from domain.activity import ActivityDefinition
from domain.progress import ActivityProgress
from domain.progress_store import ActivityProgressStore


class ActivityProgressService:
    def __init__(self, store: ActivityProgressStore):
        self.store = store
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
            self._progress[key] = progress
        return progress

    def start(self, activity_id: str, subject_id: str, at: datetime) -> ActivityProgress:
        self.definition(activity_id)
        progress = self.get(activity_id, subject_id).start(at)
        return self._replace(progress)

    def record_completion(self, activity_id: str, subject_id: str, at: datetime) -> ActivityProgress:
        definition = self.definition(activity_id)
        progress = self.get(activity_id, subject_id).record_completion(definition, at)
        return self._replace(progress)

    def reset_due(self, now: datetime) -> tuple[ActivityProgress, ...]:
        changed = False
        for key, progress in tuple(self._progress.items()):
            definition = self._definitions.get(progress.activity_id)
            if definition is None:
                continue
            reset = progress.reset_if_due(definition, now)
            if reset != progress:
                self._progress[key] = reset
                changed = True
        if changed:
            self._save()
        return self.all()

    def all(self) -> tuple[ActivityProgress, ...]:
        return tuple(self._progress[key] for key in sorted(self._progress))

    def _replace(self, progress: ActivityProgress) -> ActivityProgress:
        self._progress[(progress.activity_id, progress.subject_id)] = progress
        self._save()
        return progress

    def _save(self) -> None:
        self.store.save(self.all())
