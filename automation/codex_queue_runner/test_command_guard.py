from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandIssue:
    command: str
    reason: str


_SELECTOR = re.compile(r"^tests/[A-Za-z0-9_./-]+\.py(?:::[A-Za-z_][A-Za-z0-9_]*)*$")
_FORBIDDEN = ("\n", "\r", "\t", " ", ";", "|", "&", ">", "<", "`", "$", "(", ")", "\\")


def validate_test_selector(value: str) -> tuple[bool, str]:
    if not value: return False, "測試選擇器不能為空"
    if any(token in value for token in _FORBIDDEN): return False, "測試選擇器包含空白、換行或 Shell 字元"
    if value.startswith("-") or ".." in value.split("/") or not _SELECTOR.fullmatch(value): return False, "只允許 tests/ 下的 pytest 選擇器"
    return True, ""


def validate_test_commands(values: list[str]) -> tuple[bool, list[CommandIssue]]:
    issues = [CommandIssue(value, reason) for value in values for ok, reason in [validate_test_selector(value)] if not ok]
    return not issues, issues


def pytest_argv(selectors: list[str]) -> list[str]:
    ok, issues = validate_test_commands(selectors)
    if not ok: raise ValueError("; ".join(item.reason for item in issues))
    return ["python", "-m", "pytest", "-q", *selectors]
