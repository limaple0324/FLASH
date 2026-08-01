from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    CLAIMED = "CLAIMED"
    WAITING_REVIEW = "WAITING_REVIEW"
    NEEDS_FIX = "NEEDS_FIX"
    VERIFIED = "VERIFIED"
    CLOSED = "CLOSED"
    BLOCKED = "BLOCKED"

    @classmethod
    def claimable(cls) -> frozenset["TaskStatus"]:
        return frozenset({cls.READY, cls.NEEDS_FIX})

    @classmethod
    def completed(cls) -> frozenset["TaskStatus"]:
        return frozenset({cls.VERIFIED, cls.CLOSED})


class Role(str, Enum):
    WORKER_A = "WORKER_A"
    REQUIREMENTS_AUDIT = "REQUIREMENTS_AUDIT"
    CODE_REVIEW = "CODE_REVIEW"
    TEST_VALIDATION = "TEST_VALIDATION"
    BATCH_CONTROL = "BATCH_CONTROL"
    INTEGRATION = "INTEGRATION"

    def requires_codex(self) -> bool:
        return self in {
            Role.WORKER_A,
            Role.REQUIREMENTS_AUDIT,
            Role.CODE_REVIEW,
            Role.INTEGRATION,
        }

    def sandbox(self) -> str:
        return "workspace-write" if self in {Role.WORKER_A, Role.INTEGRATION} else "read-only"

    def is_manual_gate(self) -> bool:
        return self is Role.BATCH_CONTROL


ROLE_TRANSITIONS: dict[Role, Optional[Role]] = {
    Role.REQUIREMENTS_AUDIT: Role.INTEGRATION,
    Role.WORKER_A: Role.TEST_VALIDATION,
    Role.TEST_VALIDATION: Role.CODE_REVIEW,
    Role.CODE_REVIEW: Role.BATCH_CONTROL,
}

ALLOWED_ROLES = frozenset(role.value for role in Role)


@dataclass
class Task:
    queue_id: str
    status: TaskStatus
    role: Role
    source_issue: str
    source_pr: str
    base_commit: str
    target_branch: str
    scope: str
    owned_files: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    acceptance: str = ""
    minimum_tests: list[str] = field(default_factory=list)
    full_regression: bool = False
    windows_build: bool = False
    next_role: Optional[str] = None
    blocker_inbox: str = "#18"
    plan_id: str = ""
    item_id: str = ""
    item_title: str = ""
    item_index: int = 0
    group_index: int = 0
    group_size: int = 3
    total_items: int = 0
    total_groups: int = 0
    comment_id: Optional[int] = None
    comment_author: Optional[str] = None
    comment_created_at: Optional[str] = None
    state_comment_id: Optional[int] = None
    workflow_run_id: Optional[str] = None
    raw: Optional[str] = None

    @property
    def is_batch_item(self) -> bool:
        return bool(self.plan_id and self.item_id and self.item_index)


@dataclass
class TaskCandidate:
    task: Task
    comment_id: int
    comment_author: str
    created_at: str


@dataclass
class QueueState:
    queue_id: str
    status: TaskStatus
    comment_id: int
    created_at: str
    workflow_run_id: Optional[str] = None
    source_comment_id: Optional[int] = None
    role: Optional[Role] = None
    base_commit: Optional[str] = None
    evidence: Optional[str] = None


@dataclass(frozen=True)
class AgentResult:
    role: Role
    result: str
    summary: str
    patch: str = ""
    reasons: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    severity: str = "none"
    findings: tuple[str, ...] = ()
    route: str = ""


class QueueRunError(ValueError):
    pass


def task_to_mapping(task: Task) -> dict[str, Any]:
    value = asdict(task)
    value["status"] = task.status.value
    value["role"] = task.role.value
    return value


def task_from_mapping(value: dict[str, Any]) -> Task:
    copied = dict(value)
    copied["status"] = TaskStatus(copied["status"])
    copied["role"] = Role(copied["role"])
    return Task(**copied)
