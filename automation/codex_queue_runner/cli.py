from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .git_ops import create_and_push, validate_patch
from .github_client import GitHubRestClient
from .models import QueueRunError, Task, task_from_mapping, task_to_mapping
from .prompt_builder import render_prompt
from .selector import claim_belongs_to_run, collect_candidates, select_task
from .status_writer import build_blocked_comment, build_blocked_status, build_claimed_comment, build_next_ready, build_waiting_review_comment
from .test_command_guard import pytest_argv

_SHA = re.compile(r"^[0-9a-f]{40}$"); _NUMBER = re.compile(r"^#?(\d+)$")


@dataclass
class RunnerConfig:
    repository: str = "limaple0324/FLASH"
    target_issue: int = 19
    owner_only: str = "limaple0324"
    workflow_run_id: str = "dry-run"
    dry_run: bool = True
    allow_writeback: bool = False


def _number(value: str, name: str) -> int:
    found = _NUMBER.fullmatch(value.strip())
    if not found: raise QueueRunError(f"{name} 必須是數字")
    return int(found.group(1))


def validate_task_shape(task: Task) -> None:
    if not _SHA.fullmatch(task.base_commit): raise QueueRunError("BASE_COMMIT 必須是完整 40 位元提交")
    if task.target_branch == "main" or task.target_branch.startswith("release/"): raise QueueRunError("TARGET_BRANCH 不得為 main 或 release/")
    _number(task.source_issue, "SOURCE_ISSUE")
    if task.source_pr != "NONE": _number(task.source_pr, "SOURCE_PR")
    if not task.owned_files: raise QueueRunError("OWNED_FILES 不可為空")
    pytest_argv(task.minimum_tests)


def validate_remote_source(client: Any, task: Task) -> None:
    validate_task_shape(task)
    if not client.get_issue(_number(task.source_issue, "SOURCE_ISSUE")): raise QueueRunError("SOURCE_ISSUE 不存在")
    if task.source_pr != "NONE":
        pr = client.get_pull_request(_number(task.source_pr, "SOURCE_PR")); head = pr.get("head") or {}
        if str(pr.get("state", "")).lower() != "open" or head.get("ref") != task.target_branch or head.get("sha") != task.base_commit: raise QueueRunError("SOURCE_PR head branch 或 SHA 與任務不一致")
    if client.get_branch_sha(task.target_branch) != task.base_commit: raise QueueRunError("遠端 TARGET_BRANCH head 與 BASE_COMMIT 不一致")


def select_and_claim(client: Any, event: dict[str, Any], config: RunnerConfig, queue_id: str | None = None, openai_secret_available: bool = True) -> dict[str, Any]:
    if (event.get("event_name") or os.getenv("GITHUB_EVENT_NAME")) == "issue_comment" and int((event.get("issue") or {}).get("number", 0)) != config.target_issue: return {"selected": False, "reason": "非 Issue #19"}
    task = select_task(collect_candidates(client.list_issue_comments(config.target_issue), config.owner_only), queue_id or None).task
    try:
        validate_remote_source(client, task)
        if not config.dry_run and task.role.requires_codex() and not openai_secret_available: raise QueueRunError("缺少 OPENAI_API_KEY，禁止啟動 Agent")
    except QueueRunError as exc:
        if not config.dry_run and config.allow_writeback:
            client.post_issue_comment(config.target_issue, build_blocked_status(task, "選取階段阻擋"))
            client.write_blocker(build_blocked_comment(task, "選取階段阻擋", str(exc), "select_claim", "SELECT_CLAIM", "select_claim"))
        raise
    if not config.dry_run and config.allow_writeback:
        latest = select_task(collect_candidates(client.list_issue_comments(config.target_issue), config.owner_only), task.queue_id)
        if latest.task.state_comment_id != task.state_comment_id: raise QueueRunError("認領前任務狀態已變更")
        client.post_issue_comment(config.target_issue, build_claimed_comment(task, config.workflow_run_id))
        if not claim_belongs_to_run(client.list_issue_comments(config.target_issue), task.queue_id, config.workflow_run_id): raise QueueRunError("CLAIMED 未屬於本 workflow run")
    return {"selected": True, "mode": "dry-run" if config.dry_run else "live", "allow_writeback": config.allow_writeback, "sandbox": task.role.sandbox(), "task": task_to_mapping(task), "workflow_run_id": config.workflow_run_id}


def _read(path: Path) -> dict[str, Any]: return json.loads(path.read_text(encoding="utf-8"))
def _write(path: Path, value: dict[str, Any]) -> None: path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
def _task(path: Path) -> Task:
    value = _read(path); return task_from_mapping(value["task"] if "task" in value else value)
def _client(repository: str) -> GitHubRestClient: return GitHubRestClient(repository, os.getenv("GITHUB_TOKEN", ""))


def command_select(args: argparse.Namespace) -> int:
    config = RunnerConfig(args.repository, workflow_run_id=os.getenv("GITHUB_RUN_ID", "manual"), dry_run=args.mode == "dry-run", allow_writeback=args.allow_writeback.lower() == "true")
    try: result = select_and_claim(_client(args.repository), _read(Path(args.event)), config, args.queue_id, args.openai_secret_available.lower() == "true")
    except QueueRunError as exc: result = {"selected": False, "mode": args.mode, "allow_writeback": config.allow_writeback, "error": str(exc)}
    _write(Path(args.output), result); return 0


def command_render(args: argparse.Namespace) -> int:
    Path(args.output).write_text(render_prompt(_task(Path(args.task))), encoding="utf-8"); return 0


def command_events(args: argparse.Namespace) -> int:
    task = _task(Path(args.task)); Path(args.output).write_text(json.dumps({"event": "agent_finished", "queue_id": task.queue_id, "role": task.role.value}, ensure_ascii=False) + "\n", encoding="utf-8"); return 0


def command_validate(args: argparse.Namespace) -> int:
    result = validate_patch(Path("."), _task(Path(args.task)), Path(args.patch), Path(args.output_dir)); _write(Path(args.output_dir) / "result.json", {"has_patch": result.has_patch, "changed_files": result.changed_files, "test_result": result.test_output}); return 0


def command_push(args: argparse.Namespace) -> int:
    task, client = _task(Path(args.task)), _client(args.repository)
    try:
        if args.validate_result != "success": raise QueueRunError("validate job 失敗")
        if not claim_belongs_to_run(client.list_issue_comments(19), task.queue_id, os.getenv("GITHUB_RUN_ID", "")): raise QueueRunError("推送前 CLAIMED 已不屬於本 workflow run")
        directory, result = Path(args.validated_dir), _read(Path(args.validated_dir) / "result.json")
        commit, files = ("NONE", [])
        if result["has_patch"]: commit, files, _ = create_and_push(Path("."), task, directory / "validated.patch", directory / "manifest.sha256", f"[自動化] {task.queue_id}")
        report = (directory / "report.txt").read_text(encoding="utf-8")[:4000]
        if task.source_pr != "NONE": client.write_source_pr(_number(task.source_pr, "SOURCE_PR"), f"QUEUE_ID: {task.queue_id}\nRESULT_COMMIT: {commit}\nFILES: {','.join(files) or 'NONE'}\nREPORT: {report}")
        client.post_issue_comment(19, build_waiting_review_comment(task, commit, files, "PASS")); client.post_issue_comment(19, build_next_ready(task, commit if commit != "NONE" else task.base_commit)); client.dispatch_next(task.queue_id)
    except Exception as exc:
        client.post_issue_comment(19, build_blocked_status(task, "Queue Runner 阻擋")); client.write_blocker(build_blocked_comment(task, "Queue Runner 阻擋", str(exc), "push_writeback", "QUEUE_RUNNER", "push_writeback")); return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(); commands = parser.add_subparsers(dest="command", required=True)
    select = commands.add_parser("select-claim"); select.add_argument("--event", required=True); select.add_argument("--output", required=True); select.add_argument("--repository", required=True); select.add_argument("--mode", choices=["dry-run", "live"], default="dry-run"); select.add_argument("--queue-id", default=""); select.add_argument("--allow-writeback", default="false"); select.add_argument("--openai-secret-available", default="false")
    render = commands.add_parser("render-prompt"); render.add_argument("--task", required=True); render.add_argument("--output", required=True)
    events = commands.add_parser("write-agent-events"); events.add_argument("--task", required=True); events.add_argument("--output", required=True)
    validate = commands.add_parser("validate"); validate.add_argument("--task", required=True); validate.add_argument("--patch", required=True); validate.add_argument("--output-dir", required=True)
    push = commands.add_parser("push-writeback"); push.add_argument("--task", required=True); push.add_argument("--validated-dir", required=True); push.add_argument("--repository", required=True); push.add_argument("--validate-result", required=True)
    return {"select-claim": command_select, "render-prompt": command_render, "write-agent-events": command_events, "validate": command_validate, "push-writeback": command_push}[parser.parse_args().command](parser.parse_args())


if __name__ == "__main__": raise SystemExit(main())
