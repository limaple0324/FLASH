from __future__ import annotations

import json
from pathlib import Path

from .models import ROLE_TRANSITIONS, Role, Task

_PROMPTS = {Role.WORKER_A: "worker.md", Role.REQUIREMENTS_AUDIT: "requirements_audit.md", Role.CODE_REVIEW: "code_review.md", Role.TEST_VALIDATION: "test_validation.md"}


def render_prompt(task: Task, context: dict | None = None) -> str:
    path = Path(__file__).resolve().parent.parent / "prompts" / _PROMPTS[task.role]
    next_role = ROLE_TRANSITIONS.get(task.role)
    return path.read_text(encoding="utf-8").format(queue_id=task.queue_id, source_issue=task.source_issue, source_pr=task.source_pr, base_commit=task.base_commit, target_branch=task.target_branch, scope=task.scope, owned_files="\n".join(f"- {item}" for item in task.owned_files), forbidden="\n".join(f"- {item}" for item in task.forbidden), acceptance=task.acceptance, minimum_tests="\n".join(f"- {item}" for item in task.minimum_tests), next_role=next_role.value if next_role else "WAITING_REVIEW", context=json.dumps(context or {}, ensure_ascii=False, indent=2))
