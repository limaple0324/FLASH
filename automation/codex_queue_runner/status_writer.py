from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .models import Task


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_waiting_review_comment(task: Task, test_result: Optional[str] = None) -> str:
    return "\n".join(
        [
            f"QUEUE_ID: {task.queue_id}",
            "STATUS: WAITING_REVIEW",
            f"SOURCE_ISSUE: {task.source_issue}",
            f"SOURCE_PR: {task.source_pr}",
            f"TARGET_BRANCH: {task.target_branch}",
            "RESULT_COMMIT: DRY-RUN",  # 實際提交 id 由後續 push 角色補寫
            f"TEST_RESULT: {test_result or 'DRY-RUN PASS'}",
            f"NEXT_ROLE: {task.next_role or 'BATCH_CONTROL'}",
        ]
    )


def build_blocked_comment(
    task: Task,
    summary: str,
    exact_error: str,
    evidence: str,
    role: str,
    step: str,
    head: str = "NONE",
) -> str:
    blocker_id = f"{task.queue_id}-{role}-{_utcnow_iso()}"
    return "\n".join(
        [
            f"BLOCKER_ID: {blocker_id}",
            "STATUS: OPEN",
            f"ROLE: {role}",
            f"SOURCE_ISSUE: {task.source_issue}",
            f"SOURCE_PR: {task.source_pr}",
            f"BRANCH: {task.target_branch}",
            f"HEAD: {head}",
            f"STEP: {step}",
            "CLASS: CODE",
            f"SUMMARY: {summary}",
            f"EXACT_ERROR: {exact_error}",
            f"EVIDENCE: {evidence}",
            "TRIED: NONE",
            "SAFE_TO_CONTINUE: NO",
            "SAFE_NEXT_ACTION: 先修正後補件再補回 ISSUE #18",
            "NEEDS_USER_DECISION: NO",
            "DECISION_NEEDED: NONE",
            "CHANGED_FILES: NONE",
            "WORKTREE: DIRTY",
            "SECRETS_INCLUDED: NO",
        ]
    )
