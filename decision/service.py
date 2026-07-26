"""依已確認產品順序輸出提醒、建議或保持安靜。"""

from __future__ import annotations

from collections.abc import Iterable
from math import inf

from cards.priority import CardPriorityReason
from decision.models import (
    DecisionCandidate,
    DecisionCategory,
    DecisionOutput,
    DecisionResult,
)
from domain.character import CharacterImportance


_IMPORTANCE_ORDER = {
    CharacterImportance.PRIMARY: 0,
    CharacterImportance.SECONDARY: 1,
    CharacterImportance.RESERVE: 2,
}


class DecisionService:
    """不執行輸入；只產生可說明、可取消的決策結果。"""

    def decide(
        self,
        candidates: Iterable[DecisionCandidate],
    ) -> tuple[DecisionResult, ...]:
        pairs: list[tuple[DecisionCandidate, DecisionResult]] = []
        seen_ids: set[str] = set()
        for candidate in tuple(candidates):
            if not isinstance(candidate, DecisionCandidate):
                raise TypeError("candidates must contain DecisionCandidate values.")
            if candidate.candidate_id in seen_ids:
                raise ValueError("candidate_id values must be unique.")
            seen_ids.add(candidate.candidate_id)
            pairs.append((candidate, self._decide_one(candidate)))
        pairs.sort(key=lambda pair: self._sort_key(*pair))
        return tuple(result for _candidate, result in pairs)

    def _decide_one(self, candidate: DecisionCandidate) -> DecisionResult:
        category = self._category(candidate)

        if not candidate.evidence_confirmed:
            return self._result(
                candidate,
                DecisionOutput.QUIET,
                DecisionCategory.QUIET,
                "evidence.unknown",
                "資訊尚未確認，不做假設。",
            )
        if candidate.player_cancelled:
            return self._result(
                candidate,
                DecisionOutput.QUIET,
                DecisionCategory.QUIET,
                "player.cancelled",
                "玩家已取消，保留最後決定權。",
            )
        if (
            candidate.already_reminded_without_change
            and not candidate.has_new_information
        ):
            return self._result(
                candidate,
                DecisionOutput.QUIET,
                DecisionCategory.QUIET,
                "reminder.unchanged",
                "已提醒且狀況沒有改變，不重複打擾。",
            )

        urgent = category in {
            DecisionCategory.SAFETY_AND_DISCONNECTION,
            DecisionCategory.TIME_LIMIT,
            DecisionCategory.LOSS_RISK,
        }
        if urgent:
            return self._result(
                candidate,
                DecisionOutput.REMIND,
                category,
                f"priority.{category.name.lower()}",
                "安全、時間或損失風險需要立即看見。",
            )

        if not candidate.context_permits_notification:
            return self._result(
                candidate,
                DecisionOutput.QUIET,
                DecisionCategory.QUIET,
                "context.quiet",
                "目前情境不適合打擾，且沒有更高安全風險。",
            )

        if category is DecisionCategory.CURRENT_FOCUS:
            if candidate.requires_player_action:
                return self._result(
                    candidate,
                    DecisionOutput.REMIND,
                    category,
                    "focus.action_required",
                    "玩家目前焦點出現需要處理的新狀況。",
                )
            return self._result(
                candidate,
                DecisionOutput.QUIET,
                DecisionCategory.QUIET,
                "focus.proceeding",
                "玩家正在順利進行，不增加干擾。",
            )

        if category in {
            DecisionCategory.INTERRUPTED_RECOVERY,
            DecisionCategory.CURRENT_GROUP_PROGRESS,
            DecisionCategory.IMPORTANT_TODAY,
        }:
            if candidate.requires_player_action:
                return self._result(
                    candidate,
                    DecisionOutput.REMIND,
                    category,
                    f"priority.{category.name.lower()}",
                    "已有新資訊且需要玩家處理。",
                )
            return self._result(
                candidate,
                DecisionOutput.QUIET,
                DecisionCategory.QUIET,
                "action.not_required",
                "目前不需要玩家處理。",
            )

        if category in {
            DecisionCategory.DEFERRABLE,
            DecisionCategory.SUGGESTION,
        }:
            if not candidate.has_new_information:
                return self._result(
                    candidate,
                    DecisionOutput.QUIET,
                    DecisionCategory.QUIET,
                    "suggestion.no_new_information",
                    "沒有新的有價值資訊。",
                )
            return self._result(
                candidate,
                DecisionOutput.SUGGEST,
                category,
                f"priority.{category.name.lower()}",
                "這是可選擇的建議，不取代玩家決定。",
            )

        return self._result(
            candidate,
            DecisionOutput.QUIET,
            DecisionCategory.QUIET,
            "quiet.no_value",
            "沒有值得顯示的新狀況。",
        )

    @staticmethod
    def _category(candidate: DecisionCandidate) -> DecisionCategory:
        if candidate.priority_reason in {
            CardPriorityReason.DISCONNECTION,
            CardPriorityReason.RECOVERY,
        }:
            return DecisionCategory.SAFETY_AND_DISCONNECTION
        if candidate.priority_reason is CardPriorityReason.TIME_LIMIT:
            return DecisionCategory.TIME_LIMIT
        if candidate.priority_reason is CardPriorityReason.LOSS_RISK:
            return DecisionCategory.LOSS_RISK
        if candidate.is_current_focus:
            return DecisionCategory.CURRENT_FOCUS
        if candidate.interrupted_recoverable:
            return DecisionCategory.INTERRUPTED_RECOVERY
        if candidate.is_current_group_progress:
            return DecisionCategory.CURRENT_GROUP_PROGRESS
        if candidate.is_important_today:
            return DecisionCategory.IMPORTANT_TODAY
        if candidate.is_deferrable:
            return DecisionCategory.DEFERRABLE
        if candidate.suggestion_only:
            return DecisionCategory.SUGGESTION
        return DecisionCategory.QUIET

    @staticmethod
    def _result(
        candidate: DecisionCandidate,
        output: DecisionOutput,
        category: DecisionCategory,
        reason_code: str,
        explanation: str,
    ) -> DecisionResult:
        return DecisionResult(
            candidate_id=candidate.candidate_id,
            output=output,
            category=category,
            reason_code=reason_code,
            explanation=explanation,
        )

    @staticmethod
    def _sort_key(
        candidate: DecisionCandidate,
        result: DecisionResult,
    ) -> tuple[int, float, int, str]:
        remaining_seconds = (
            candidate.remaining_time.total_seconds()
            if candidate.remaining_time is not None
            else inf
        )
        return (
            int(result.category),
            remaining_seconds,
            _IMPORTANCE_ORDER[candidate.character_importance],
            candidate.candidate_id,
        )
