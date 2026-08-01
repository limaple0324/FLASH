from __future__ import annotations

from datetime import datetime, timezone

from .models import Role, Task, TaskStatus


def _now() -> str: return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _state(task: Task, status: TaskStatus, role: Role | None = None, base_commit: str | None = None, workflow_run_id: str | None = None, evidence: str = "") -> str:
    lines = [f"QUEUE_ID: {task.queue_id}", f"STATUS: {status.value}", f"SOURCE_COMMENT_ID: {task.comment_id}", "STATE_WRITER: CODEX_QUEUE_RUNNER"]
    if role: lines.append(f"ROLE: {role.value}")
    if base_commit: lines.append(f"BASE_COMMIT: {base_commit}")
    if workflow_run_id: lines.append(f"WORKFLOW_RUN_ID: {workflow_run_id}")
    if evidence: lines.append(f"EVIDENCE: {evidence}")
    return "\n".join(lines)


def build_claimed_comment(task: Task, workflow_run_id: str) -> str: return _state(task, TaskStatus.CLAIMED, task.role, task.base_commit, workflow_run_id)
def build_ready_handoff(task: Task, role: Role, base_commit: str, evidence: str) -> str: return _state(task, TaskStatus.READY, role, base_commit, evidence=evidence)
def build_needs_fix(task: Task, evidence: str) -> str: return _state(task, TaskStatus.NEEDS_FIX, Role.WORKER_A, task.base_commit, evidence=evidence)
def build_waiting_review(task: Task, commit: str, evidence: str) -> str: return _state(task, TaskStatus.WAITING_REVIEW, base_commit=commit, evidence=evidence)
def build_blocked_status(task: Task, reason: str) -> str: return _state(task, TaskStatus.BLOCKED, evidence=reason)


def build_blocked_comment(task: Task, summary: str, exact_error: str, evidence: str, role: str, step: str, head: str = "NONE") -> str:
    return "\n".join((f"BLOCKER_ID: {task.queue_id}-{role}-{_now()}", "STATUS: OPEN", f"ROLE: {role}", f"SOURCE_ISSUE: {task.source_issue}", f"SOURCE_PR: {task.source_pr}", f"BRANCH: {task.target_branch}", f"HEAD: {head}", f"STEP: {step}", "CLASS: SECURITY", f"SUMMARY: {summary}", f"EXACT_ERROR: {exact_error}", f"EVIDENCE: {evidence}", "TRIED: STOPPED", "SAFE_TO_CONTINUE: NO", "SAFE_NEXT_ACTION: 修正後重新派工", "NEEDS_USER_DECISION: NO", "DECISION_NEEDED: NONE", "CHANGED_FILES: NONE", "WORKTREE: CLEAN", "SECRETS_INCLUDED: NO"))
