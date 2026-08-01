from __future__ import annotations

import re

from .models import ALLOWED_ROLES, Role, Task, TaskStatus, QueueRunError


_KEY_RE = re.compile(r"^\s*([A-Z_]+)\s*:\s*(.*)\s*$")


def _split_list(value: str) -> list[str]:
    if not value:
        return []
    items: list[str] = []
    for chunk in re.split(r"[,;\n]", value):
        item = chunk.strip().strip("`").strip()
        if item:
            items.append(item)
    return items


def _parse_bool(value: str) -> bool:
    value = value.strip().upper()
    if value in {"YES", "TRUE", "1"}:
        return True
    if value in {"NO", "FALSE", "0", ""}:
        return False
    raise QueueRunError(f"布林值欄位格式不正確: {value}")


def _require(value: str | None, key: str) -> str:
    if value is None or not value.strip():
        raise QueueRunError(f"欄位缺漏: {key}")
    return value.strip()


def parse_task_comment(body: str) -> Task:
    fields = {}
    in_block = False

    for line in body.splitlines():
        plain = line.strip()
        if plain.startswith("```"):
            in_block = not in_block
            continue

        if not in_block and plain:
            # 同一份派工樣板會用程式碼區塊包住欄位；若使用者未包也可逐行解讀
            pass

        if not in_block:
            continue

        match = _KEY_RE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        fields[key] = value

    if not fields:
        # 退場：某些情況 comment 沒有包 code block，仍嘗試直接解析
        for line in body.splitlines():
            match = _KEY_RE.match(line)
            if not match:
                continue
            key, value = match.group(1), match.group(2).strip()
            fields[key] = value

    required = {
        "QUEUE_ID",
        "STATUS",
        "ROLE",
        "SOURCE_ISSUE",
        "SOURCE_PR",
        "BASE_COMMIT",
        "TARGET_BRANCH",
        "SCOPE",
        "OWNED_FILES",
        "FORBIDDEN",
        "ACCEPTANCE",
        "MINIMUM_TESTS",
        "BLOCKER_INBOX",
    }
    missing = sorted(required - fields.keys())
    if missing:
        raise QueueRunError(f"欄位缺漏: {','.join(missing)}")

    status_raw = _require(fields.get("STATUS"), "STATUS")
    role_raw = _require(fields.get("ROLE"), "ROLE")

    try:
        status = TaskStatus(status_raw)
    except ValueError as exc:
        raise QueueRunError(f"不支援的 STATUS: {status_raw}") from exc

    if role_raw not in ALLOWED_ROLES:
        raise QueueRunError(f"不支援的 ROLE: {role_raw}")

    return Task(
        queue_id=_require(fields.get("QUEUE_ID"), "QUEUE_ID"),
        status=status,
        role=Role(role_raw),
        source_issue=_require(fields.get("SOURCE_ISSUE"), "SOURCE_ISSUE"),
        source_pr=_require(fields.get("SOURCE_PR"), "SOURCE_PR"),
        base_commit=_require(fields.get("BASE_COMMIT"), "BASE_COMMIT"),
        target_branch=_require(fields.get("TARGET_BRANCH"), "TARGET_BRANCH"),
        scope=_require(fields.get("SCOPE"), "SCOPE"),
        owned_files=_split_list(fields.get("OWNED_FILES", "")),
        forbidden=_split_list(fields.get("FORBIDDEN", "")),
        acceptance=_require(fields.get("ACCEPTANCE"), "ACCEPTANCE"),
        minimum_tests=_split_list(fields.get("MINIMUM_TESTS", "")),
        full_regression=_parse_bool(fields.get("FULL_REGRESSION", "NO")),
        windows_build=_parse_bool(fields.get("WINDOWS_BUILD", "NO")),
        next_role=(fields.get("NEXT_ROLE") or "BATCH_CONTROL").strip() or None,
        blocker_inbox=_require(fields.get("BLOCKER_INBOX"), "BLOCKER_INBOX"),
        raw=body,
    )
