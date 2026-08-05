"""把已達門檻的玩家習慣候選轉成四選項提醒卡。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from cards.models import CardAction, GroupCard
from cards.priority import CardPriorityReason
from cards.service import CardService
from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.group import CharacterGroup
from habit.preference_models import (
    HabitDecision,
    PlayerHabitCandidate,
)
from habit.preference_service import PlayerHabitPreferenceService
from services.card_coordinator import CardCoordinator


HABIT_CARD_PREFIX = "habit:"
HABIT_ACTIONS = (
    CardAction("adopt", "採用"),
    CardAction("today", "只限今天"),
    CardAction("never", "不要記住"),
    CardAction("later", "稍後再問"),
)
_ACTION_DECISIONS = {
    "adopt": HabitDecision.ADOPTED,
    "today": HabitDecision.TODAY_ONLY,
    "never": HabitDecision.NEVER_ASK,
    "later": HabitDecision.SNOOZED,
}


class PlayerHabitReminderService:
    """只顯示及保存玩家選擇；不含任何遊戲控制介面。"""

    def __init__(
        self,
        habits: PlayerHabitPreferenceService,
        coordinator: CardCoordinator,
        cards: CardService,
        current_group_name: Callable[[], str | None],
        *,
        record_callback: Callable[[str, str, str], object] | None = None,
    ) -> None:
        if not isinstance(habits, PlayerHabitPreferenceService):
            raise TypeError("habits must be PlayerHabitPreferenceService.")
        if not isinstance(coordinator, CardCoordinator):
            raise TypeError("coordinator must be CardCoordinator.")
        if not isinstance(cards, CardService):
            raise TypeError("cards must be CardService.")
        if not callable(current_group_name):
            raise TypeError("current_group_name must be callable.")
        if record_callback is not None and not callable(record_callback):
            raise TypeError("record_callback must be callable.")
        self._habits = habits
        self._coordinator = coordinator
        self._cards = cards
        self._current_group_name = current_group_name
        self._record_callback = record_callback
        self._candidates: dict[str, PlayerHabitCandidate] = {}

    @staticmethod
    def _card_id(candidate: PlayerHabitCandidate) -> str:
        return f"{HABIT_CARD_PREFIX}{candidate.candidate_id}"

    @staticmethod
    def _candidate_text(candidate: PlayerHabitCandidate) -> str:
        return (
            f"{candidate.kind.value}｜{candidate.subject}｜"
            f"{' → '.join(candidate.values)}"
        )

    def refresh(self, now: datetime) -> tuple[GroupCard, ...]:
        candidates = self._habits.candidates(now)
        active_ids = {self._card_id(candidate) for candidate in candidates}
        for card in tuple(self._cards.all_cards):
            if (
                card.card_id.startswith(HABIT_CARD_PREFIX)
                and card.card_id not in active_ids
            ):
                self._cards.remove(card.card_id)
        self._candidates = {
            self._card_id(candidate): candidate for candidate in candidates
        }
        shown: list[GroupCard] = []
        group_name = self._current_group_name() or "目前組別"
        for candidate in candidates:
            card = GroupCard(
                card_id=self._card_id(candidate),
                group=CharacterGroup(
                    group_id=f"habit-{group_name}",
                    name=group_name,
                ),
                activity=ActivityDefinition(
                    activity_id=f"habit-{candidate.candidate_id}",
                    name="玩家習慣建議",
                    activity_type=ActivityType.PERMANENT,
                    reset_rule=ResetRule.NONE,
                ),
                current_progress=self._candidate_text(candidate),
                requires_player_action=True,
                priority_reason=CardPriorityReason.PREFERENCE,
                actions=HABIT_ACTIONS,
            )
            presented = self._coordinator.submit(
                self._coordinator.candidate_for_card(
                    card,
                    suggestion_only=True,
                ),
                card,
                shown_at=now,
            )
            if presented is not None:
                shown.append(presented)
        return tuple(shown)

    def handle_action(
        self,
        card_id: str,
        action_id: str,
        decided_at: datetime,
    ):
        if not card_id.startswith(HABIT_CARD_PREFIX):
            return None
        candidate = self._candidates.get(card_id)
        decision = _ACTION_DECISIONS.get(action_id)
        if candidate is None or decision is None:
            return None
        preference = self._habits.choose(candidate, decision, decided_at)
        self._cards.remove(card_id)
        if self._record_callback is not None:
            self._record_callback(
                "玩家習慣",
                candidate.subject,
                f"{self._candidate_text(candidate)}－{decision.value}",
            )
        return preference
