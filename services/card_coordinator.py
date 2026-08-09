"""協調可見提醒卡與斷線歷史。"""

from datetime import datetime, timedelta, timezone

from cards.lifecycle import CardPresentationOrder
from cards.models import GroupCard
from cards.service import CardService
from decision.models import DecisionCandidate, DecisionOutput
from decision.service import DecisionService
from domain.character import CharacterImportance, character_importance_rank
from services.card_history_service import CardHistoryService


class CardCoordinator:
    def __init__(
        self,
        cards: CardService,
        history: CardHistoryService,
        decision_service: DecisionService | None = None,
    ) -> None:
        if not isinstance(cards, CardService):
            raise TypeError("cards must be CardService.")
        if not isinstance(history, CardHistoryService):
            raise TypeError("history must be CardHistoryService.")
        if decision_service is not None and not isinstance(
            decision_service,
            DecisionService,
        ):
            raise TypeError("decision_service must be DecisionService.")
        self.cards = cards
        self.history = history
        self.decision_service = decision_service or DecisionService()

    @staticmethod
    def candidate_for_card(
        card: GroupCard,
        *,
        remaining_time: timedelta | None = None,
        is_current_focus: bool = False,
        interrupted_recoverable: bool = False,
        is_current_group_progress: bool = False,
        is_important_today: bool = False,
        is_deferrable: bool = False,
        suggestion_only: bool = False,
        has_new_information: bool = True,
        already_reminded_without_change: bool = False,
        player_cancelled: bool = False,
    ) -> DecisionCandidate:
        if not isinstance(card, GroupCard):
            raise TypeError("card must be GroupCard.")
        matched = tuple(
            character
            for character in card.group.characters
            if character.character_id in card.affected_character_ids
        )
        importance = (
            min(
                (character.importance for character in matched),
                key=character_importance_rank,
            )
            if matched
            else CharacterImportance.SECONDARY
        )
        return DecisionCandidate(
            candidate_id=card.card_id,
            priority_reason=card.priority_reason,
            character_importance=importance,
            remaining_time=remaining_time,
            requires_player_action=card.requires_player_action,
            is_current_focus=is_current_focus,
            interrupted_recoverable=interrupted_recoverable,
            is_current_group_progress=is_current_group_progress,
            is_important_today=is_important_today,
            is_deferrable=is_deferrable,
            suggestion_only=suggestion_only,
            has_new_information=has_new_information,
            already_reminded_without_change=already_reminded_without_change,
            player_cancelled=player_cancelled,
        )

    def submit(
        self,
        candidate: DecisionCandidate,
        card: GroupCard,
        shown_at: datetime | None = None,
        *,
        lifetime: timedelta | None = None,
    ) -> GroupCard | None:
        if not isinstance(candidate, DecisionCandidate):
            raise TypeError("candidate must be DecisionCandidate.")
        if not isinstance(card, GroupCard):
            raise TypeError("card must be GroupCard.")
        if candidate.candidate_id != card.card_id:
            raise ValueError("candidate_id must match card_id.")
        results = self.decision_service.decide((candidate,))
        if len(results) != 1 or results[0].candidate_id != card.card_id:
            raise RuntimeError("decision result must match exactly one card.")
        result = results[0]
        if result.output is DecisionOutput.QUIET:
            self.cards.remove(card.card_id)
            return None
        presentation_order = CardPresentationOrder(
            category=result.category,
            remaining_time=candidate.remaining_time,
            character_importance=candidate.character_importance,
            event_id=candidate.candidate_id,
        )
        return self._write(
            card,
            shown_at=shown_at,
            lifetime=lifetime,
            presentation_order=presentation_order,
        )

    def _write(
        self,
        card: GroupCard,
        shown_at: datetime | None = None,
        *,
        lifetime: timedelta | None = None,
        presentation_order: CardPresentationOrder,
    ) -> GroupCard:
        previous = next(
            (
                current
                for current in self.cards.all_cards
                if current.card_id == card.card_id
            ),
            None,
        )
        result = self.cards.upsert(
            card,
            shown_at=shown_at,
            lifetime=lifetime,
            presentation_order=presentation_order,
        )
        if previous is None or previous.priority_reason is not card.priority_reason:
            entry = next(
                item
                for item in (
                    self.cards.entries + self.cards.pending_entries
                )
                if item.card.card_id == card.card_id
            )
            recorded_at = (
                entry.shown_at
                if previous is None
                else shown_at or datetime.now(timezone.utc)
            )
            self.history.record(card, recorded_at)
        return result
