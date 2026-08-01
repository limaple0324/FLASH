from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class TaskStatus(str, Enum):
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
    def non_claimable(cls) -> frozenset["TaskStatus"]:
        return frozenset(
            {cls.CLAIMED, cls.WAITING_REVIEW, cls.VERIFIED, cls.CLOSED, cls.BLOCKED}
        )

    @classmethod
    def completed(cls) -> frozenset["TaskStatus"]:
        return frozenset({cls.WAITING_REVIEW, cls.VERIFIED, cls.CLOSED, cls.BLOCKED})


class Role(str, Enum):
    WORKER_A = "WORKER_A"
    REQUIREMENTS_AUDIT = "REQUIREMENTS_AUDIT"
    CODE_REVIEW = "CODE_REVIEW"
    TEST_VALIDATION = "TEST_VALIDATION"
    BATCH_CONTROL = "BATCH_CONTROL"
    INTEGRATION = "INTEGRATION"

    def needs_patch(self) -> bool:
        return self == Role.WORKER_A

    def needs_test_execution(self) -> bool:
        return self == Role.TEST_VALIDATION

    def can_write_github(self) -> bool:
        return self in {Role.BATCH_CONTROL, Role.INTEGRATION}

    def can_use_openai(self) -> bool:
        return self in {
            Role.WORKER_A,
            Role.REQUIREMENTS_AUDIT,
            Role.CODE_REVIEW,
            Role.TEST_VALIDATION,
        }


ALLOWED_ROLES = frozenset({r.value for r in Role})


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
    owned_files: List[str] = field(default_factory=list)
    forbidden: List[str] = field(default_factory=list)
    acceptance: str = ""
    minimum_tests: List[str] = field(default_factory=list)
    full_regression: bool = False
    windows_build: bool = False
    next_role: Optional[str] = None
    blocker_inbox: str = "#18"
    comment_id: Optional[int] = None
    comment_author: Optional[str] = None
    comment_created_at: Optional[str] = None
    raw: Optional[str] = None


@dataclass
class TaskCandidate:
    task: Task
    comment_id: int
    comment_author: str
    created_at: str


@dataclass
class RunResult:
    status: TaskStatus
    next_role: Optional[str]
    status_comment: Optional[str]
    blocker_comment: Optional[str] = None
    changed_files: List[str] = field(default_factory=list)
    test_result: Optional[str] = None
    branch_update: Optional[str] = None
    dry_run: bool = True


class QueueRunError(ValueError):
    pass
