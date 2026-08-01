from __future__ import annotations

from datetime import datetime
from typing import Sequence

from .models import QueueRunError, QueueState, TaskCandidate, TaskStatus
from .parser import _fields, parse_queue_state, parse_task_comment

OWNER = "limaple0324"
BOT = "github-actions[bot]"
STATE_WRITER = "CODEX_QUEUE_RUNNER"


def _key(raw: dict) -> tuple[datetime, int]:
    try:
        stamp = datetime.fromisoformat(str(raw.get("created_at", "")).replace("Z", "+00:00"))
    except ValueError:
        stamp = datetime.min.replace(tzinfo=None)
    return stamp, int(raw.get("id", 0))


def is_trusted_state(raw: dict, owner: str = OWNER) -> bool:
    author = str((raw.get("user") or {}).get("login", ""))
    return author == owner or (author == BOT and _fields(str(raw.get("body", ""))).get("STATE_WRITER") == STATE_WRITER)


def latest_states(raw_comments: Sequence[dict], owner: str = OWNER) -> dict[str, QueueState]:
    states: dict[str, QueueState] = {}
    for raw in sorted(raw_comments, key=_key):
        if is_trusted_state(raw, owner):
            state = parse_queue_state(str(raw.get("body", "")), int(raw.get("id", 0)), str(raw.get("created_at", "")))
            if state:
                states[state.queue_id] = state
    return states


def collect_candidates(raw_comments: Sequence[dict], require_owner: str = OWNER) -> list[TaskCandidate]:
    states, tasks = latest_states(raw_comments, require_owner), {}
    for raw in sorted(raw_comments, key=_key):
        if str((raw.get("user") or {}).get("login", "")) != require_owner:
            continue
        try:
            task = parse_task_comment(str(raw.get("body", "")))
        except QueueRunError:
            continue
        task.comment_id = int(raw.get("id", 0))
        task.comment_author = require_owner
        task.comment_created_at = str(raw.get("created_at", ""))
        tasks[task.queue_id] = TaskCandidate(task, task.comment_id, require_owner, task.comment_created_at)
    candidates = []
    for queue_id, candidate in tasks.items():
        state = states.get(queue_id)
        if not state or (state.source_comment_id is not None and state.source_comment_id != candidate.comment_id):
            continue
        task = candidate.task
        task.status, task.state_comment_id, task.workflow_run_id = state.status, state.comment_id, state.workflow_run_id
        if state.role:
            task.role = state.role
        if state.base_commit:
            task.base_commit = state.base_commit
        candidate.created_at = state.created_at
        candidates.append(candidate)
    _validate_batch_uniqueness(candidates)
    return sorted(candidates, key=lambda item: _key({"created_at": item.created_at, "id": item.comment_id}))


def _validate_batch_uniqueness(candidates: Sequence[TaskCandidate]) -> None:
    item_ids: set[tuple[str, str]] = set()
    item_indexes: set[tuple[str, int]] = set()
    for candidate in candidates:
        task = candidate.task
        if not task.is_batch_item:
            continue
        item_id_key = (task.plan_id, task.item_id)
        item_index_key = (task.plan_id, task.item_index)
        if item_id_key in item_ids:
            raise QueueRunError("duplicate ITEM_ID in PLAN_ID")
        if item_index_key in item_indexes:
            raise QueueRunError("duplicate ITEM_INDEX in PLAN_ID")
        item_ids.add(item_id_key)
        item_indexes.add(item_index_key)


def select_task(candidates: Sequence[TaskCandidate], queue_id: str | None = None) -> TaskCandidate:
    for candidate in candidates:
        if (not queue_id or candidate.task.queue_id == queue_id) and candidate.task.status in TaskStatus.claimable():
            return candidate
    raise QueueRunError("no claimable task")


def claim_belongs_to_run(raw_comments: Sequence[dict], queue_id: str, workflow_run_id: str, source_comment_id: int, owner: str = OWNER) -> bool:
    state = latest_states(raw_comments, owner).get(queue_id)
    return bool(state and state.status is TaskStatus.CLAIMED and state.workflow_run_id == workflow_run_id and state.source_comment_id == source_comment_id)


def stale_claims(candidates: Sequence[TaskCandidate], now: datetime, lease_seconds: int, run_lookup) -> list[TaskCandidate]:
    result = []
    for candidate in candidates:
        task = candidate.task
        if task.status is not TaskStatus.CLAIMED or not task.workflow_run_id:
            continue
        try:
            started = datetime.fromisoformat(candidate.created_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if (now - started).total_seconds() >= lease_seconds and str(run_lookup(task.workflow_run_id).get("status", "")) == "completed":
            result.append(candidate)
    return result
