from __future__ import annotations

import json
import re
from pathlib import Path

from .models import ROLE_TRANSITIONS, QueueRunError, Role, Task

_PROMPTS = {Role.WORKER_A: "worker.md", Role.REQUIREMENTS_AUDIT: "requirements_audit.md", Role.CODE_REVIEW: "code_review.md", Role.TEST_VALIDATION: "test_validation.md"}
MAX_PROMPT_CONTEXT_BYTES = 32_768
_SECRET_PATTERNS = (
    re.compile(r"OPENAI_API_KEY", re.IGNORECASE),
    re.compile(r"GITHUB_TOKEN", re.IGNORECASE),
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    re.compile(r"Authorization\s*:\s*Bearer\s+\S+", re.IGNORECASE),
)


def _context_strings(value: object):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _context_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _context_strings(item)
    elif isinstance(value, str):
        yield value


def validate_prompt_context(context: dict) -> None:
    try:
        encoded = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise QueueRunError("prompt context is not serializable") from exc
    if len(encoded.encode("utf-8")) > MAX_PROMPT_CONTEXT_BYTES:
        raise QueueRunError("prompt context exceeds the fixed size limit")
    if any(pattern.search(text) for text in _context_strings(context) for pattern in _SECRET_PATTERNS):
        raise QueueRunError("prompt context contains prohibited secret material")


def render_prompt(task: Task, context: dict | None = None) -> str:
    context = context if context is not None else {}
    validate_prompt_context(context)
    path = Path(__file__).resolve().parent.parent / "prompts" / _PROMPTS[task.role]
    next_role = ROLE_TRANSITIONS.get(task.role)
    return path.read_text(encoding="utf-8").format(queue_id=task.queue_id, source_issue=task.source_issue, source_pr=task.source_pr, base_commit=task.base_commit, target_branch=task.target_branch, scope=task.scope, owned_files="\n".join(f"- {item}" for item in task.owned_files), forbidden="\n".join(f"- {item}" for item in task.forbidden), acceptance=task.acceptance, minimum_tests="\n".join(f"- {item}" for item in task.minimum_tests), next_role=next_role.value if next_role else "WAITING_REVIEW", context=json.dumps(context, ensure_ascii=False, indent=2))
