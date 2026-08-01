from __future__ import annotations

import re

from .models import ALLOWED_ROLES, QueueRunError, QueueState, Role, Task, TaskStatus

_KEY_RE = re.compile(r"^\s*([A-Z_]+)\s*:\s*(.*?)\s*$")
_LIST_FIELDS = {"OWNED_FILES", "FORBIDDEN", "MINIMUM_TESTS"}


def _fields(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    in_block = False
    previous = ""
    for line in body.splitlines():
        if line.strip().startswith("```"):
            in_block = not in_block
            previous = ""
            continue
        if not in_block:
            continue
        match = _KEY_RE.match(line)
        if match:
            previous = match.group(1)
            fields[previous] = match.group(2)
        elif previous in _LIST_FIELDS and line.strip():
            fields[previous] += "\n" + line.strip().lstrip("- ")
    if fields:
        return fields
    previous = ""
    for line in body.splitlines():
        match = _KEY_RE.match(line)
        if match:
            previous = match.group(1)
            fields[previous] = match.group(2)
        elif previous in _LIST_FIELDS and line.strip():
            fields[previous] += "\n" + line.strip().lstrip("- ")
    return fields


def _list(value: str) -> list[str]:
    return [item.strip().strip("`") for item in re.split(r"[,;\uFF1B\n]", value) if item.strip().strip("`")]


def _required(fields: dict[str, str], name: str) -> str:
    value = fields.get(name, "").strip()
    if not value:
        raise QueueRunError(f"missing required field: {name}")
    return value


def _boolean(value: str) -> bool:
    normalized = value.strip().upper()
    if normalized in {"", "NO", "FALSE", "0"}:
        return False
    if normalized in {"YES", "TRUE", "1"}:
        return True
    raise QueueRunError(f"invalid boolean: {value}")


def parse_queue_state(body: str, comment_id: int = 0, created_at: str = "") -> QueueState | None:
    fields = _fields(body)
    if not fields.get("QUEUE_ID") or not fields.get("STATUS"):
        return None
    try:
        status = TaskStatus(fields["STATUS"].strip())
        state_role = Role(fields["ROLE"].strip()) if fields.get("ROLE", "").strip() else None
    except ValueError:
        return None
    source = fields.get("SOURCE_COMMENT_ID", "").strip()
    return QueueState(fields["QUEUE_ID"].strip(), status, comment_id, created_at, fields.get("WORKFLOW_RUN_ID", "").strip() or None, int(source) if source.isdigit() else None, state_role, fields.get("BASE_COMMIT", "").strip().lower() or None, fields.get("EVIDENCE", "").strip() or None)


def parse_task_comment(body: str) -> Task:
    fields = _fields(body)
    required = {"QUEUE_ID", "STATUS", "ROLE", "SOURCE_ISSUE", "SOURCE_PR", "BASE_COMMIT", "TARGET_BRANCH", "SCOPE", "OWNED_FILES", "FORBIDDEN", "ACCEPTANCE", "MINIMUM_TESTS", "BLOCKER_INBOX"}
    missing = sorted(required - fields.keys())
    if missing:
        raise QueueRunError(f"missing required fields: {','.join(missing)}")
    try:
        status = TaskStatus(_required(fields, "STATUS"))
        role = Role(_required(fields, "ROLE"))
    except ValueError as exc:
        raise QueueRunError("invalid STATUS or ROLE") from exc
    if role.value not in ALLOWED_ROLES:
        raise QueueRunError(f"invalid ROLE: {role.value}")
    return Task(_required(fields, "QUEUE_ID"), status, role, _required(fields, "SOURCE_ISSUE"), _required(fields, "SOURCE_PR").upper(), _required(fields, "BASE_COMMIT").lower(), _required(fields, "TARGET_BRANCH"), _required(fields, "SCOPE"), _list(fields["OWNED_FILES"]), _list(fields["FORBIDDEN"]), _required(fields, "ACCEPTANCE"), _list(fields["MINIMUM_TESTS"]), _boolean(fields.get("FULL_REGRESSION", "NO")), _boolean(fields.get("WINDOWS_BUILD", "NO")), fields.get("NEXT_ROLE", "").strip() or None, _required(fields, "BLOCKER_INBOX"), raw=body)
