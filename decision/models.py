"""決策服務的不可變輸入與輸出。"""

from dataclasses import dataclass
from datetime import timedelta
from enum import Enum, IntEnum

from cards.priority import CardPriorityReason
from domain.character import CharacterImportance


class DecisionOutput(str, Enum):
    REMIND = "提醒"
    SUGGEST = "建議"
    QUIET = "保持安靜"


class DecisionCategory(IntEnum):
    SAFETY_AND_DISCONNECTION = 1
    TIME_LIMIT = 2
    LOSS_RISK = 3
    CURRENT_FOCUS = 4
    INTERRUPTED_RECOVERY = 5
    CURRENT_GROUP_PROGRESS = 6
    IMPORTANT_TODAY = 7
    DEFERRABLE = 8
    SUGGESTION = 9
    QUIET = 10


@dataclass(frozen=True, slots=True)
class DecisionCandidate:
    candidate_id: str
    priority_reason: CardPriorityReason
    character_importance: CharacterImportance = CharacterImportance.SECONDARY
    remaining_time: timedelta | None = None
    evidence_confirmed: bool = True
    context_permits_notification: bool = True
    requires_player_action: bool = False
    is_current_focus: bool = False
    interrupted_recoverable: bool = False
    is_current_group_progress: bool = False
    is_important_today: bool = False
    is_deferrable: bool = False
    suggestion_only: bool = False
    has_new_information: bool = True
    already_reminded_without_change: bool = False
    player_cancelled: bool = False

    def __post_init__(self) -> None:
        candidate_id = self.candidate_id.strip()
        if not candidate_id:
            raise ValueError("candidate_id must not be empty.")
        if not isinstance(self.priority_reason, CardPriorityReason):
            raise TypeError("priority_reason must be CardPriorityReason.")
        if not isinstance(self.character_importance, CharacterImportance):
            raise TypeError("character_importance must be CharacterImportance.")
        if self.remaining_time is not None:
            if not isinstance(self.remaining_time, timedelta):
                raise TypeError("remaining_time must be timedelta or None.")
            if self.remaining_time < timedelta(0):
                raise ValueError("remaining_time cannot be negative.")
        boolean_fields = (
            "evidence_confirmed",
            "context_permits_notification",
            "requires_player_action",
            "is_current_focus",
            "interrupted_recoverable",
            "is_current_group_progress",
            "is_important_today",
            "is_deferrable",
            "suggestion_only",
            "has_new_information",
            "already_reminded_without_change",
            "player_cancelled",
        )
        if any(not isinstance(getattr(self, field), bool) for field in boolean_fields):
            raise TypeError("Decision flags must be bool.")
        object.__setattr__(self, "candidate_id", candidate_id)


@dataclass(frozen=True, slots=True)
class DecisionResult:
    candidate_id: str
    output: DecisionOutput
    category: DecisionCategory
    reason_code: str
    explanation: str
