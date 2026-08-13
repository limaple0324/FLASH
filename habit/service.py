"""只觀察、等待玩家確認，不自行套用活動順序的習慣服務。"""

from __future__ import annotations

from habit.store import ActivityOrderHabitStore


class ActivityOrderHabitService:
    def __init__(self, store: ActivityOrderHabitStore):
        if not isinstance(store, ActivityOrderHabitStore):
            raise TypeError("store must be ActivityOrderHabitStore.")
        store.load()
