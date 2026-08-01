from __future__ import annotations

from datetime import datetime
from typing import Sequence

from .models import QueueRunError, QueueState, TaskCandidate, TaskStatus
from .parser import parse_queue_state, parse_task_comment


class DuplicateClaimError(QueueRunError):
    pass


def _stamp(value: str) -> tuple[datetime, int]:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")), 0
    except ValueError:
        return datetime.min.replace(tzinfo=None), 0


def _key(raw: dict) -> tuple[datetime, int]:
    stamp, _ = _stamp(str(raw.get("created_at", "")))
    return stamp, int(raw.get("id", 0))


def latest_states(raw_comments: Sequence[dict]) -> dict[str, QueueState]:
    states: dict[str, QueueState] = {}
    for raw in sorted(raw_comments, key=_key):
        state = parse_queue_state(str(raw.get("body", "")), int(raw.get("id", 0)), str(raw.get("created_at", "")))
        if state:
            states[state.queue_id] = state
    return states


def collect_candidates(raw_comments: Sequence[dict], require_owner: str) -> list[TaskCandidate]:
    states, tasks = latest_states(raw_comments), {}
    for raw in sorted(raw_comments, key=_key):
        author = str((raw.get("user") or {}).get("login", ""))
        if author != require_owner:
            continue
        try:
            task = parse_task_comment(str(raw.get("body", "")))
        except QueueRunError:
            continue
        task.comment_id, task.comment_author, task.comment_created_at = int(raw.get("id", 0)), author, str(raw.get("created_at", ""))
        tasks[task.queue_id] = TaskCandidate(task, task.comment_id, author, task.comment_created_at)
    candidates: list[TaskCandidate] = []
    for queue_id, candidate in tasks.items():
        state = states.get(queue_id)
        if not state:
            continue
        candidate.task.status, candidate.task.state_comment_id, candidate.task.workflow_run_id = state.status, state.comment_id, state.workflow_run_id
        candidate.created_at = state.created_at
        candidates.append(candidate)
    return sorted(candidates, key=lambda item: _key({"created_at": item.created_at, "id": item.comment_id}))


def select_task(candidates: Sequence[TaskCandidate], queue_id: str | None = None) -> TaskCandidate:
    for candidate in candidates:
        if (not queue_id or candidate.task.queue_id == queue_id) and candidate.task.status in TaskStatus.claimable():
            return candidate
    raise QueueRunError("目前沒有可認領任務")


def claim_belongs_to_run(raw_comments: Sequence[dict], queue_id: str, workflow_run_id: str) -> bool:
    state = latest_states(raw_comments).get(queue_id)
    return bool(state and state.status == TaskStatus.CLAIMED and state.workflow_run_id == workflow_run_id)
