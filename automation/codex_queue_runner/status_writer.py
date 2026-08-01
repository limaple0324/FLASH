from __future__ import annotations

from datetime import datetime, timezone

from .models import Role, Task


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_claimed_comment(task: Task, workflow_run_id: str) -> str:
    return "\n".join((f"QUEUE_ID: {task.queue_id}", "STATUS: CLAIMED", f"WORKFLOW_RUN_ID: {workflow_run_id}", f"SOURCE_COMMENT_ID: {task.comment_id}", f"BASE_COMMIT: {task.base_commit}", f"TARGET_BRANCH: {task.target_branch}", "STATE_WRITER: CODEX_QUEUE_RUNNER"))


def build_waiting_review_comment(task: Task, commit: str, files: list[str], test_result: str) -> str:
    return "\n".join((f"QUEUE_ID: {task.queue_id}", "STATUS: WAITING_REVIEW", f"SOURCE_COMMENT_ID: {task.comment_id}", f"RESULT_COMMIT: {commit}", f"CHANGED_FILES: {','.join(files) or 'NONE'}", f"TEST_RESULT: {test_result}", f"NEXT_ROLE: {task.next_role or 'BATCH_CONTROL'}", "STATE_WRITER: CODEX_QUEUE_RUNNER"))


def build_blocked_status(task: Task, reason: str) -> str:
    return "\n".join((f"QUEUE_ID: {task.queue_id}", "STATUS: BLOCKED", f"SOURCE_COMMENT_ID: {task.comment_id}", f"SUMMARY: {reason}", "STATE_WRITER: CODEX_QUEUE_RUNNER"))


def build_next_ready(task: Task, commit: str) -> str:
    role = task.next_role or Role.BATCH_CONTROL.value
    return "\n".join(("```", f"QUEUE_ID: {task.queue_id}-NEXT", "STATUS: READY", f"ROLE: {role}", f"SOURCE_ISSUE: {task.source_issue}", f"SOURCE_PR: {task.source_pr}", f"BASE_COMMIT: {commit}", f"TARGET_BRANCH: {task.target_branch}", f"SCOPE: 承接 {task.queue_id} 的 {role} 階段", f"OWNED_FILES: {','.join(task.owned_files)}", f"FORBIDDEN: {','.join(task.forbidden)}", f"ACCEPTANCE: {task.acceptance}", f"MINIMUM_TESTS: {','.join(task.minimum_tests)}", "FULL_REGRESSION: NO", "WINDOWS_BUILD: NO", f"NEXT_ROLE: {role}", "BLOCKER_INBOX: #18", "```"))


def build_blocked_comment(task: Task, summary: str, exact_error: str, evidence: str, role: str, step: str, head: str = "NONE") -> str:
    return "\n".join((f"BLOCKER_ID: {task.queue_id}-{role}-{_now()}", "STATUS: OPEN", f"ROLE: {role}", f"SOURCE_ISSUE: {task.source_issue}", f"SOURCE_PR: {task.source_pr}", f"BRANCH: {task.target_branch}", f"HEAD: {head}", f"STEP: {step}", "CLASS: SECURITY", f"SUMMARY: {summary}", f"EXACT_ERROR: {exact_error}", f"EVIDENCE: {evidence}", "TRIED: STOPPED", "SAFE_TO_CONTINUE: NO", "SAFE_NEXT_ACTION: 修正後重新派工", "NEEDS_USER_DECISION: NO", "DECISION_NEEDED: NONE", "CHANGED_FILES: NONE", "WORKTREE: CLEAN", "SECRETS_INCLUDED: NO"))
