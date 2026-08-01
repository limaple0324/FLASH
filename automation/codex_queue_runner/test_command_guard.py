from __future__ import annotations

import shlex
from dataclasses import dataclass


@dataclass
class CommandIssue:
    command: str
    reason: str


_FORBIDDEN = {
    "&&",
    "||",
    ";",
    "|",
    "$()",
    "`",
    ">",
    "<",
    "rm ",
    " del ",
}


def _contains_forbidden(command: str) -> str | None:
    for token in _FORBIDDEN:
        if token in command:
            return token
    return None


def _is_allowed_python_pytest(tokens: list[str]) -> bool:
    return (
        len(tokens) >= 2
        and tokens[0] == "python"
        and tokens[1] == "-m"
        and len(tokens) >= 3
        and tokens[2] == "pytest"
    )


def validate_test_command(command: str) -> tuple[bool, str]:
    command = command.strip()
    if not command:
        return False, "測試命令不能為空"

    forbidden = _contains_forbidden(command)
    if forbidden:
        return False, f"測試命令包含禁用字元: {forbidden}"

    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        return False, f"測試命令無法解析: {exc}"

    if not tokens:
        return False, "測試命令不能為空"

    if tokens[0] == "pytest":
        return True, ""

    if _is_allowed_python_pytest(tokens):
        return True, ""

    return False, "只允許 pytest 或 python -m pytest"


def validate_test_commands(commands: list[str]) -> tuple[bool, list[CommandIssue]]:
    issues: list[CommandIssue] = []
    for command in commands:
        ok, reason = validate_test_command(command)
        if not ok:
            issues.append(CommandIssue(command=command, reason=reason))
    return len(issues) == 0, issues
