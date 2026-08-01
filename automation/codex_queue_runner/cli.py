from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .models import (
    QueueRunError,
    RunResult,
    Role,
    Task,
    Task,
    TaskStatus,
)
from .parser import parse_task_comment
from .prompt_builder import render_prompt
from .scope_guard import ensure_scope
from .selector import DuplicateClaimError, collect_candidates, select_task
from .status_writer import build_blocked_comment, build_waiting_review_comment
from .test_command_guard import validate_test_commands


@dataclass
class RunnerConfig:
    repo_root: Path
    dry_run: bool = True
    openai_api_key: Optional[str] = None
    github_token: Optional[str] = None
    target_issue: int = 19
    owner_only: str = "limaple0324"


class GitHubClient:
    def list_issue_comments(self, issue_number: int) -> Sequence[dict]:
        raise NotImplementedError

    def post_issue_comment(self, issue_number: int, body: str) -> None:
        raise NotImplementedError

    def update_issue_comment(self, comment_id: int, body: str) -> None:
        raise NotImplementedError


def _read_event_payload(path: Optional[str]) -> dict:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _parse_task_from_comment(comment: dict) -> Task:
    body = comment.get("body", "")
    if not body:
        raise QueueRunError("comment 缺少 body")

    task = parse_task_comment(body)
    task.comment_id = int(comment.get("id", 0))
    task.comment_author = comment.get("user", {}).get("login", "")
    task.comment_created_at = comment.get("created_at", "")
    return task


def _validate_task(task: Task, owner_only: str) -> tuple[bool, str]:
    if task.comment_author != owner_only:
        return False, "只能處理 limaple0324 建立的任務"

    if task.status not in TaskStatus.claimable():
        return False, f"STATUS 不可認領: {task.status.value}"

    if task.target_branch == "main":
        return False, "不得以 main 作為 TARGET_BRANCH"

    if task.target_branch.startswith("release/"):
        return False, "不得使用 release/ 前綴的 TARGET_BRANCH"

    if not re.fullmatch(r"[0-9a-f]{40}", task.base_commit):
        return False, "BASE_COMMIT 必須是 40 碼 SHA1"

    if not re.fullmatch(r"#?\d+", task.source_issue.strip()):
        return False, "SOURCE_ISSUE 格式不正確"

    if not re.fullmatch(r"#?\d+", task.source_pr.strip()):
        return False, "SOURCE_PR 格式不正確"

    if not task.owned_files:
        return False, "OWNED_FILES 不能是空"

    return True, ""


def _run_codex(task: Task, dry_run: bool, openai_key: Optional[str]) -> tuple[bool, str, list[str]]:
    _ = render_prompt(task)

    if task.role.needs_patch():
        if dry_run:
            return True, "DRY-RUN PATCH", task.owned_files.copy()

    if task.role.needs_patch() and not openai_key:
        return False, "缺少 OPENAI_API_KEY", []

    if task.role.can_use_openai() and not dry_run:
        # 實際版只做 dry-run，不在本階段發起對外呼叫
        return False, "非 DRY-RUN 模式未啟用正式 Codex 呼叫", []

    return True, "DRY-RUN NO-CHANGES", []


def _run_tests(task: Task, dry_run: bool) -> tuple[bool, str]:
    ok, issues = validate_test_commands(task.minimum_tests)
    if not ok:
        return False, "; ".join(item.reason for item in issues)

    if dry_run:
        return True, "DRY-RUN TESTS PASS"

    for command in task.minimum_tests:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return False, proc.stderr.strip() or proc.stdout.strip() or "測試失敗"

    return True, "TEST PASSED"


def _build_push_command(task: Task) -> list[str]:
    return ["git", "push", "origin", f"HEAD:{task.target_branch}", "--ff-only"]


def _is_force_command(command: list[str]) -> bool:
    return any(item in {"--force", "-f", "--force-with-lease"} for item in command)


def _run_without_github_write(task: Task, config: RunnerConfig) -> RunResult:
    ok, reason = _validate_task(task, config.owner_only)
    if not ok:
        return RunResult(
            status=TaskStatus.BLOCKED,
            next_role=task.next_role,
            status_comment=None,
            blocker_comment=build_blocked_comment(
                task=task,
                summary="任務前置驗證失敗",
                exact_error=reason,
                evidence="validation",
                role=task.role.value,
                step="task_validate",
            ),
            dry_run=config.dry_run,
        )

    if task.role.needs_patch() or task.role in {Role.REQUIREMENTS_AUDIT, Role.CODE_REVIEW}:
        changed_ok = True
        changed_files: list[str] = []

        if task.role.needs_patch():
            codex_ok, codex_result, changed_files = _run_codex(task, config.dry_run, config.openai_api_key)
            if not codex_ok:
                return RunResult(
                    status=TaskStatus.BLOCKED,
                    next_role=task.next_role,
                    status_comment=None,
                    blocker_comment=build_blocked_comment(
                        task=task,
                        summary="Codex 執行失敗",
                        exact_error=codex_result,
                        evidence="codex",
                        role=task.role.value,
                        step="codex_exec",
                    ),
                    dry_run=config.dry_run,
                )
        else:
            # 審核與需求核對為只讀角色，不應修改產品程式
            changed_files = []

        scoped_ok, scope_errors = ensure_scope(
            changed_files if changed_files else [],
            task.owned_files,
            task.forbidden,
        )
        if not scoped_ok:
            return RunResult(
                status=TaskStatus.BLOCKED,
                next_role=task.next_role,
                status_comment=None,
                blocker_comment=build_blocked_comment(
                    task=task,
                    summary="範圍檢查失敗",
                    exact_error="; ".join(scope_errors),
                    evidence="scope_guard",
                    role=task.role.value,
                    step="scope_guard",
                ),
                changed_files=changed_files,
                dry_run=config.dry_run,
            )

    if task.role == Role.TEST_VALIDATION:
        test_ok, test_result = _run_tests(task, config.dry_run)
        if not test_ok:
            return RunResult(
                status=TaskStatus.BLOCKED,
                next_role=task.next_role,
                status_comment=None,
                blocker_comment=build_blocked_comment(
                    task=task,
                    summary="測試失敗",
                    exact_error=test_result,
                    evidence="test_validation",
                    role=task.role.value,
                    step="minimum_tests",
                ),
                dry_run=config.dry_run,
            )

        return RunResult(
            status=TaskStatus.WAITING_REVIEW,
            next_role=task.next_role or "BATCH_CONTROL",
            status_comment=build_waiting_review_comment(task, test_result="dry-run-test"),
            test_result=test_result,
            dry_run=config.dry_run,
        )

    # 讀取階段角色無修改檔案
    return RunResult(
        status=TaskStatus.WAITING_REVIEW,
        next_role=task.next_role or "BATCH_CONTROL",
        status_comment=build_waiting_review_comment(task),
        dry_run=config.dry_run,
    )


def _run_with_github_write(task: Task, config: RunnerConfig) -> RunResult:
    ok, reason = _validate_task(task, config.owner_only)
    if not ok:
        return RunResult(
            status=TaskStatus.BLOCKED,
            next_role=task.next_role,
            status_comment=None,
            blocker_comment=build_blocked_comment(
                task=task,
                summary="任務前置驗證失敗",
                exact_error=reason,
                evidence="validation",
                role=task.role.value,
                step="task_validate",
            ),
            dry_run=config.dry_run,
        )

    if config.openai_api_key:
        return RunResult(
            status=TaskStatus.BLOCKED,
            next_role=task.next_role,
            status_comment=None,
            blocker_comment=build_blocked_comment(
                task=task,
                summary="環境限制",
                exact_error="推送階段不得持有 OPENAI_API_KEY",
                evidence="environment",
                role=task.role.value,
                step="env_guard",
            ),
            dry_run=config.dry_run,
        )

    if task.role == Role.INTEGRATION:
        push = _build_push_command(task)
    else:
        push = _build_push_command(task)

    if _is_force_command(push):
        return RunResult(
            status=TaskStatus.BLOCKED,
            next_role=task.next_role,
            status_comment=None,
            blocker_comment=build_blocked_comment(
                task=task,
                summary="推送命令不安全",
                exact_error="不允許 force push",
                evidence=",".join(push),
                role=task.role.value,
                step="push_guard",
            ),
            branch_update=" ".join(push),
            dry_run=config.dry_run,
        )

    if not config.dry_run:
        proc = subprocess.run(
            push,
            check=False,
            text=True,
            capture_output=True,
        )
        if proc.returncode != 0:
            return RunResult(
                status=TaskStatus.BLOCKED,
                next_role=task.next_role,
                status_comment=None,
                blocker_comment=build_blocked_comment(
                    task=task,
                    summary="推送失敗",
                    exact_error=(proc.stderr or proc.stdout or "push failed").strip(),
                    evidence="git_push",
                    role=task.role.value,
                    step="git_push",
                ),
                branch_update=" ".join(push),
                dry_run=config.dry_run,
            )

    return RunResult(
        status=TaskStatus.WAITING_REVIEW,
        next_role=task.next_role or "BATCH_CONTROL",
        status_comment=build_waiting_review_comment(task),
        branch_update=" ".join(push),
        dry_run=config.dry_run,
    )


def run_from_event(
    event_payload: Optional[dict] = None,
    github_client: Optional[GitHubClient] = None,
    config: Optional[RunnerConfig] = None,
) -> Optional[RunResult]:
    if event_payload is None:
        event_payload = _read_event_payload(os.getenv("GITHUB_EVENT_PATH"))

    if config is None:
        config = RunnerConfig(
            repo_root=Path("."),
            dry_run=os.getenv("CODEX_QUEUE_DRY_RUN", "1") == "1",
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            github_token=os.getenv("GITHUB_TOKEN"),
        )

    event_name = event_payload.get("action") or event_payload.get("event_name") or ""
    issue = event_payload.get("issue") or {}
    issue_number = int(issue.get("number", 0))

    if issue_number and issue_number != config.target_issue:
        return None

    task: Optional[Task] = None
    comment_id: Optional[int] = None

    if event_name == "issue_comment" or event_payload.get("comment"):
        comment = event_payload.get("comment", {})
        if not comment:
            return None

        task = _parse_task_from_comment(comment)
        comment_id = task.comment_id
    else:
        if github_client is None:
            raise QueueRunError("non-issue_comment 需提供 GitHub client")

        raw_comments = github_client.list_issue_comments(config.target_issue)
        candidates = collect_candidates(raw_comments, require_owner=config.owner_only)

        if not candidates:
            return None

        try:
            candidate = select_task(candidates)
        except DuplicateClaimError as exc:
            first = candidates[0].task
            return RunResult(
                status=TaskStatus.BLOCKED,
                next_role=first.next_role,
                status_comment=None,
                blocker_comment=build_blocked_comment(
                    task=first,
                    summary="重複認領",
                    exact_error=str(exc),
                    evidence="selector",
                    role="SCHEDULER",
                    step="task_selection",
                ),
                dry_run=config.dry_run,
            )

        task = candidate.task
        comment_id = candidate.comment_id

    if task is None or task.comment_id is None:
        return None

    task.comment_id = int(comment_id)

    if task.role.can_write_github():
        result = _run_with_github_write(task, config)
    else:
        result = _run_without_github_write(task, config)

    if config.dry_run:
        return result

    if github_client is None:
        return result

    # 在非 dry-run 且允許的角色才寫回進度，符合階段權限定義
    if not task.role.can_write_github():
        return result

    if result.status_comment:
        github_client.post_issue_comment(config.target_issue, result.status_comment)

    if result.blocker_comment:
        github_client.post_issue_comment(18, result.blocker_comment)

    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", default=os.getenv("GITHUB_EVENT_PATH"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args()

    payload = _read_event_payload(args.event)
    config = RunnerConfig(
        repo_root=Path(args.repo_root),
        dry_run=args.dry_run,
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        github_token=os.getenv("GITHUB_TOKEN"),
    )

    result = run_from_event(payload, config=config)
    if result is None:
        print("no-task")
        return 0

    if result.status_comment:
        print(result.status_comment)
    if result.blocker_comment:
        print(result.blocker_comment)
    print(f"status={result.status.value}")
    if result.branch_update:
        print(f"branch_update={result.branch_update}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
