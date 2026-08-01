from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
from typing import Iterable, Sequence

from .models import QueueRunError, Task, TaskCandidate, TaskStatus
from .parser import parse_task_comment


class DuplicateClaimError(QueueRunError):
    """同一 QUEUE_ID 有多筆可認領任務。"""


def _created_timestamp(value: str) -> datetime:
    if not value:
        return datetime.min
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min


def _parse_task_candidate(raw: dict, require_owner: str | None = None) -> TaskCandidate | None:
    comment_body = raw.get("body", "")
    comment_user = raw.get("user", {}) or {}
    author = comment_user.get("login", "")

    task = parse_task_comment(comment_body)
    task.comment_id = int(raw.get("id", 0))
    task.comment_author = author
    task.comment_created_at = raw.get("created_at", "")

    if require_owner and author != require_owner:
        return None

    return TaskCandidate(
        task=task,
        comment_id=task.comment_id,
        comment_author=author,
        created_at=task.comment_created_at or "",
    )


def collect_candidates(
    raw_comments: Sequence[dict],
    require_owner: str,
) -> list[TaskCandidate]:
    candidates: list[TaskCandidate] = []
    for raw in raw_comments:
        try:
            candidate = _parse_task_candidate(raw, require_owner=require_owner)
        except Exception:
            continue
        if candidate is not None:
            candidates.append(candidate)

    candidates.sort(key=lambda item: _created_timestamp(item.created_at))
    return candidates


def select_task(candidates: Sequence[TaskCandidate]) -> TaskCandidate:
    blocked: set[str] = set()
    ready_by_queue: dict[str, TaskCandidate] = {}

    for candidate in candidates:
        status = candidate.task.status
        qid = candidate.task.queue_id

        if status in TaskStatus.non_claimable():
            blocked.add(qid)
            continue

        if status not in TaskStatus.claimable():
            continue

        if qid in blocked:
            continue

        if qid in ready_by_queue:
            raise DuplicateClaimError(f"Queue 重複認領: {qid}")

        ready_by_queue[qid] = candidate

    if not ready_by_queue:
        raise QueueRunError("目前沒有可認領任務")

    # 確保一次只回傳一筆
    return next(iter(ready_by_queue.values()))
