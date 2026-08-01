from __future__ import annotations

from pathlib import Path

from .models import Role, Task

_PROMPT_FILES = {
    Role.WORKER_A: "worker.md",
    Role.REQUIREMENTS_AUDIT: "requirements_audit.md",
    Role.CODE_REVIEW: "code_review.md",
    Role.TEST_VALIDATION: "test_validation.md",
}


def _read_prompt(role: Role) -> str:
    filename = _PROMPT_FILES.get(role)
    if filename is None:
        raise KeyError(f"不支援的角色: {role.value}")

    path = Path(__file__).resolve().parent.parent / "prompts" / filename
    return path.read_text(encoding="utf-8")


def render_prompt(task: Task) -> str:
    template = _read_prompt(task.role)
    return template.format(
        queue_id=task.queue_id,
        source_issue=task.source_issue,
        source_pr=task.source_pr,
        base_commit=task.base_commit,
        target_branch=task.target_branch,
        scope=task.scope,
        owned_files="\n".join(f"- {path}" for path in task.owned_files),
        forbidden="\n".join(f"- {path}" for path in task.forbidden),
        acceptance=task.acceptance,
        minimum_tests="\n".join(f"- {cmd}" for cmd in task.minimum_tests),
        next_role=task.next_role or "BATCH_CONTROL",
    )
