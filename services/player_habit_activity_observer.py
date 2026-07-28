"""只把可信的活動完成事件轉成玩家習慣觀察。"""

from __future__ import annotations

from collections.abc import Callable

from domain.progress import TAIPEI_TIMEZONE
from habit.preference_models import PlayerHabitObservation
from habit.preference_service import PlayerHabitPreferenceService
from services.activity_progress_service import ActivityProgressChange


class PlayerHabitActivityObserver:
    """不接收滑鼠、鍵盤、斷線或未描述操作，只接收完成事件。"""

    def __init__(
        self,
        habits: PlayerHabitPreferenceService,
        *,
        activity_name_provider: Callable[[str], str] | None = None,
    ) -> None:
        if not isinstance(habits, PlayerHabitPreferenceService):
            raise TypeError("habits must be PlayerHabitPreferenceService.")
        self._habits = habits
        if activity_name_provider is not None and not callable(
            activity_name_provider
        ):
            raise TypeError("activity_name_provider must be callable.")
        self._activity_name_provider = activity_name_provider

    def handle(
        self,
        change: ActivityProgressChange,
    ) -> tuple[PlayerHabitObservation, ...]:
        if not isinstance(change, ActivityProgressChange):
            raise TypeError("change must be ActivityProgressChange.")
        if change.reason != "completion_recorded":
            return ()
        completed_at = change.current.completed_at
        if completed_at is None:
            return ()
        local_completed_at = completed_at.astimezone(TAIPEI_TIMEZONE)
        activity_name = (
            self._activity_name_provider(change.current.activity_id)
            if self._activity_name_provider is not None
            else change.current.activity_id
        )
        source_event_id = (
            "activity-progress:"
            f"{change.current.activity_id}:"
            f"{change.current.subject_id}:"
            f"{change.current.current_count}:"
            f"{completed_at.isoformat()}"
        )
        return self._habits.record_activity_completion(
            change.current.activity_id,
            change.current.subject_id,
            local_completed_at,
            source_event_id=source_event_id,
            activity_name=activity_name,
        )
