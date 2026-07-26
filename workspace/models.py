"""工作區當下需要呈現的純資料狀態。"""

from dataclasses import dataclass

from domain.activity import ActivityDefinition
from domain.group import CharacterGroup


@dataclass(frozen=True, slots=True)
class WorkspaceState:
    """保存目前組別、活動與已知下一步，不在此層做決策。"""

    current_group: CharacterGroup | None = None
    current_activity: ActivityDefinition | None = None
    next_step: str | None = None

    def __post_init__(self) -> None:
        if self.current_group is not None and not isinstance(
            self.current_group, CharacterGroup
        ):
            raise TypeError("current_group must be CharacterGroup or None.")
        if self.current_activity is not None and not isinstance(
            self.current_activity, ActivityDefinition
        ):
            raise TypeError("current_activity must be ActivityDefinition or None.")
        if self.next_step is not None:
            if not isinstance(self.next_step, str):
                raise TypeError("next_step must be str or None.")
            next_step = self.next_step.strip()
            if not next_step:
                raise ValueError("next_step must not be empty.")
            object.__setattr__(self, "next_step", next_step)

    def to_dict(self) -> dict[str, object]:
        return {
            "current_group": (
                self.current_group.to_dict() if self.current_group is not None else None
            ),
            "current_activity": (
                self.current_activity.to_dict()
                if self.current_activity is not None
                else None
            ),
            "next_step": self.next_step,
        }
