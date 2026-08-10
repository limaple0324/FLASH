from __future__ import annotations

import re

from .models import ALLOWED_ROLES, QueueRunError, QueueState, Role, Task, TaskStatus

_KEY_RE = re.compile(r"^\s*([A-Z_]+)\s*:\s*(.*?)\s*$")
_LIST_FIELDS = {"OWNED_FILES", "FORBIDDEN", "MINIMUM_TESTS"}
_BATCH_FIELDS = (
    "PLAN_ID",
    "ITEM_ID",
    "ITEM_TITLE",
    "ITEM_INDEX",
    "GROUP_INDEX",
    "GROUP_SIZE",
    "TOTAL_ITEMS",
    "TOTAL_GROUPS",
)


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


def _positive_integer(fields: dict[str, str], name: str) -> int:
    value = _required(fields, name)
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise QueueRunError(f"{name} must be a positive integer")
    return int(value)


def _batch_values(fields: dict[str, str]) -> dict[str, str | int]:
    present = [name for name in _BATCH_FIELDS if name in fields]
    if not present:
        return {}
    missing = [name for name in _BATCH_FIELDS if name not in fields or not fields[name].strip()]
    if missing:
        raise QueueRunError(f"missing required batch fields: {','.join(missing)}")

    item_index = _positive_integer(fields, "ITEM_INDEX")
    group_index = _positive_integer(fields, "GROUP_INDEX")
    group_size = _positive_integer(fields, "GROUP_SIZE")
    total_items = _positive_integer(fields, "TOTAL_ITEMS")
    total_groups = _positive_integer(fields, "TOTAL_GROUPS")
    if group_size != 3:
        raise QueueRunError("GROUP_SIZE must be 3")
    if item_index > total_items:
        raise QueueRunError("ITEM_INDEX must not exceed TOTAL_ITEMS")
    if group_index > total_groups:
        raise QueueRunError("GROUP_INDEX must not exceed TOTAL_GROUPS")
    if total_groups != (total_items + group_size - 1) // group_size:
        raise QueueRunError("TOTAL_GROUPS does not match TOTAL_ITEMS and GROUP_SIZE")
    if ((item_index - 1) // group_size) + 1 != group_index:
        raise QueueRunError("ITEM_INDEX does not belong to GROUP_INDEX")
    return {
        "plan_id": _required(fields, "PLAN_ID"),
        "item_id": _required(fields, "ITEM_ID"),
        "item_title": _required(fields, "ITEM_TITLE"),
        "item_index": item_index,
        "group_index": group_index,
        "group_size": group_size,
        "total_items": total_items,
        "total_groups": total_groups,
    }


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
    return Task(
        queue_id=_required(fields, "QUEUE_ID"),
        status=status,
        role=role,
        source_issue=_required(fields, "SOURCE_ISSUE"),
        source_pr=_required(fields, "SOURCE_PR").upper(),
        base_commit=_required(fields, "BASE_COMMIT").lower(),
        target_branch=_required(fields, "TARGET_BRANCH"),
        scope=_required(fields, "SCOPE"),
        owned_files=_list(fields["OWNED_FILES"]),
        forbidden=_list(fields["FORBIDDEN"]),
        acceptance=_required(fields, "ACCEPTANCE"),
        minimum_tests=_list(fields["MINIMUM_TESTS"]),
        full_regression=_boolean(fields.get("FULL_REGRESSION", "NO")),
        windows_build=_boolean(fields.get("WINDOWS_BUILD", "NO")),
        next_role=fields.get("NEXT_ROLE", "").strip() or None,
        blocker_inbox=_required(fields, "BLOCKER_INBOX"),
        raw=body,
        **_batch_values(fields),
    )
