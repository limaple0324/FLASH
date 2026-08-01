from __future__ import annotations

import re

from .models import ALLOWED_ROLES, QueueRunError, QueueState, Role, Task, TaskStatus

_KEY_RE = re.compile(r"^\s*([A-Z_]+)\s*:\s*(.*?)\s*$")


def _fields(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    in_block = False
    for line in body.splitlines():
        if line.strip().startswith("```"):
            in_block = not in_block
            continue
        if in_block:
            match = _KEY_RE.match(line)
            if match:
                fields[match.group(1)] = match.group(2)
    if fields:
        return fields
    for line in body.splitlines():
        match = _KEY_RE.match(line)
        if match:
            fields[match.group(1)] = match.group(2)
    return fields


def _list(value: str) -> list[str]:
    return [item.strip().strip("`") for item in value.split(",") if item.strip().strip("`")]


def _required(fields: dict[str, str], name: str) -> str:
    value = fields.get(name, "").strip()
    if not value:
        raise QueueRunError(f"欄位缺漏: {name}")
    return value


def _boolean(value: str) -> bool:
    if value.strip().upper() in {"", "NO", "FALSE", "0"}:
        return False
    if value.strip().upper() in {"YES", "TRUE", "1"}:
        return True
    raise QueueRunError(f"布林欄位格式不正確: {value}")


def parse_queue_state(body: str, comment_id: int = 0, created_at: str = "") -> QueueState | None:
    fields = _fields(body)
    if not fields.get("QUEUE_ID") or not fields.get("STATUS"):
        return None
    try:
        status = TaskStatus(fields["STATUS"].strip())
    except ValueError:
        return None
    source_id = fields.get("SOURCE_COMMENT_ID", "").strip()
    return QueueState(fields["QUEUE_ID"].strip(), status, comment_id, created_at, fields.get("WORKFLOW_RUN_ID", "").strip() or None, int(source_id) if source_id.isdigit() else None)


def parse_task_comment(body: str) -> Task:
    fields = _fields(body)
    required = {"QUEUE_ID", "STATUS", "ROLE", "SOURCE_ISSUE", "SOURCE_PR", "BASE_COMMIT", "TARGET_BRANCH", "SCOPE", "OWNED_FILES", "FORBIDDEN", "ACCEPTANCE", "MINIMUM_TESTS", "BLOCKER_INBOX"}
    missing = sorted(required - fields.keys())
    if missing:
        raise QueueRunError(f"欄位缺漏: {','.join(missing)}")
    try:
        status = TaskStatus(_required(fields, "STATUS"))
    except ValueError as exc:
        raise QueueRunError(f"不支援的 STATUS: {fields['STATUS']}") from exc
    role = _required(fields, "ROLE")
    if role not in ALLOWED_ROLES:
        raise QueueRunError(f"不支援的 ROLE: {role}")
    return Task(_required(fields, "QUEUE_ID"), status, Role(role), _required(fields, "SOURCE_ISSUE"), _required(fields, "SOURCE_PR").upper(), _required(fields, "BASE_COMMIT").lower(), _required(fields, "TARGET_BRANCH"), _required(fields, "SCOPE"), _list(fields["OWNED_FILES"]), _list(fields["FORBIDDEN"]), _required(fields, "ACCEPTANCE"), _list(fields["MINIMUM_TESTS"]), _boolean(fields.get("FULL_REGRESSION", "NO")), _boolean(fields.get("WINDOWS_BUILD", "NO")), fields.get("NEXT_ROLE", "BATCH_CONTROL").strip() or "BATCH_CONTROL", _required(fields, "BLOCKER_INBOX"), raw=body)
