from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .git_ops import create_and_push
from .github_client import GitHubRestClient
from .models import ROLE_TRANSITIONS, AgentResult, QueueRunError, Role, Task, TaskStatus, task_from_mapping, task_to_mapping
from .parser import parse_task_comment
from .prompt_builder import render_prompt
from .role_output import dry_agent_result, output_schema, parse_agent_result, result_mapping
from .selector import claim_belongs_to_run, collect_candidates, is_trusted_state, stale_claims, select_task
from .status_writer import build_blocked_comment, build_blocked_status, build_claimed_comment, build_needs_fix, build_ready_handoff, build_waiting_review
from .test_command_guard import pytest_argv

_SHA = re.compile(r"^[0-9a-f]{40}$"); _NUMBER = re.compile(r"^#?(\d+)$")


@dataclass
class RunnerConfig:
    repository: str = "limaple0324/FLASH"
    target_issue: int = 19
    owner_only: str = "limaple0324"
    workflow_run_id: str = "dry-run"
    mode: str = "dry-run"
    allow_writeback: bool = False
    lease_seconds: int = 3600

    @property
    def dry_run(self) -> bool: return self.mode == "dry-run"


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
    if task.full_regression or task.windows_build: raise QueueRunError("FULL_REGRESSION 或 WINDOWS_BUILD 尚未有專用安全流程")
    if task.role.is_manual_gate(): raise QueueRunError("BATCH_CONTROL 與 INTEGRATION 為 manual gate")


def validate_remote_source(client: Any, task: Task) -> None:
    validate_task_shape(task)
    if not client.get_issue(_number(task.source_issue, "SOURCE_ISSUE")): raise QueueRunError("SOURCE_ISSUE 不存在")
    if task.source_pr != "NONE":
        pr = client.get_pull_request(_number(task.source_pr, "SOURCE_PR")); head = pr.get("head") or {}
        if str(pr.get("state", "")).lower() != "open" or head.get("ref") != task.target_branch or head.get("sha") != task.base_commit: raise QueueRunError("SOURCE_PR head branch 或 SHA 與任務不一致")
    if client.get_branch_sha(task.target_branch) != task.base_commit: raise QueueRunError("遠端 TARGET_BRANCH head 與 BASE_COMMIT 不一致")


def build_context_snapshot(client: Any, task: Task, comments: list[dict]) -> dict:
    issue = client.get_issue(_number(task.source_issue, "SOURCE_ISSUE")); pr: dict = {}; files: list[str] = []
    if task.source_pr != "NONE":
        pr = client.get_pull_request(_number(task.source_pr, "SOURCE_PR")); files = [str(item.get("filename", "")) for item in client.list_pull_request_files(_number(task.source_pr, "SOURCE_PR"))]
    evidence = [str(comment.get("body", ""))[:1000] for comment in comments if is_trusted_state(comment) and task.queue_id in str(comment.get("body", "")) and "EVIDENCE:" in str(comment.get("body", ""))]
    return {"queue_id": task.queue_id, "source_issue": {"number": task.source_issue, "title": issue.get("title", ""), "body": issue.get("body", "")}, "source_pr": {"number": task.source_pr, "base": (pr.get("base") or {}).get("sha", ""), "head": (pr.get("head") or {}).get("sha", ""), "branch": (pr.get("head") or {}).get("ref", "")}, "changed_files": files, "prior_evidence": evidence}


def _reconcile_claim(client: Any, task: Task) -> bool:
    try:
        head = client.get_branch_sha(task.target_branch)
        if head == task.base_commit: return False
        commit = client.get_commit(head); parents = commit.get("parents") or []; message = str((commit.get("commit") or {}).get("message", ""))
        if len(parents) == 1 and parents[0].get("sha") == task.base_commit and message.startswith(f"[Codex Queue Runner] {task.queue_id} parent {task.base_commit}"):
            next_role = ROLE_TRANSITIONS.get(task.role)
            if next_role: client.post_issue_comment(19, build_ready_handoff(task, next_role, head, "reconciliation"))
            else: client.post_issue_comment(19, build_waiting_review(task, head, "reconciliation"))
            return True
    except Exception: return False
    return False


def recover_stale_claims(client: Any, comments: list[dict], config: RunnerConfig) -> list[str]:
    recovered: list[str] = []
    for candidate in stale_claims(collect_candidates(comments, config.owner_only), datetime.now(timezone.utc), config.lease_seconds, client.get_workflow_run):
        task = candidate.task
        if _reconcile_claim(client, task): recovered.append(task.queue_id); continue
        client.post_issue_comment(19, build_needs_fix(task, "stale CLAIMED workflow completed")); recovered.append(task.queue_id)
    return recovered


def select_and_claim(client: Any | None, event: dict[str, Any], config: RunnerConfig, queue_id: str | None = None, fixture: Path | None = None) -> dict:
    event_name = event.get("event_name") or os.getenv("GITHUB_EVENT_NAME", "")
    if event_name == "issue_comment" and int((event.get("issue") or {}).get("number", 0)) != config.target_issue: return {"selected": False, "reason": "非 Issue #19"}
    if config.mode == "live" and not config.allow_writeback: raise QueueRunError("live 模式必須 allow_writeback=true，禁止啟動 Agent")
    if fixture:
        task = parse_task_comment(fixture.read_text(encoding="utf-8")); task.comment_id = 1; task.comment_author = config.owner_only
        validate_task_shape(task); context = {"fixture": True, "source_issue": {"body": "dry-run"}, "source_pr": {}, "changed_files": [], "prior_evidence": []}
    else:
        if client is None: raise QueueRunError("live 選取需要 GitHub client")
        comments = client.list_issue_comments(config.target_issue)
        if event_name in {"schedule", "repository_dispatch"}: recover_stale_claims(client, comments, config); comments = client.list_issue_comments(config.target_issue)
        task = select_task(collect_candidates(comments, config.owner_only), queue_id or None).task
        try:
            validate_remote_source(client, task); context = build_context_snapshot(client, task, comments)
        except QueueRunError as exc:
            if config.mode == "live" and config.allow_writeback:
                client.post_issue_comment(config.target_issue, build_blocked_status(task, "選取階段阻擋")); client.write_blocker(build_blocked_comment(task, "選取階段阻擋", str(exc), "select_claim", "SELECT_CLAIM", "select_claim"))
            raise
    if config.mode == "live":
        if task.role.requires_codex() and not os.getenv("OPENAI_SECRET_AVAILABLE") == "true": raise QueueRunError("缺少 OPENAI_API_KEY，禁止啟動 Agent")
        assert client is not None
        latest = select_task(collect_candidates(client.list_issue_comments(config.target_issue), config.owner_only), task.queue_id)
        if latest.task.state_comment_id != task.state_comment_id: raise QueueRunError("認領前任務狀態已變更")
        client.post_issue_comment(config.target_issue, build_claimed_comment(task, config.workflow_run_id))
        if not claim_belongs_to_run(client.list_issue_comments(config.target_issue), task.queue_id, config.workflow_run_id, task.comment_id): raise QueueRunError("CLAIMED 不屬於本 workflow run 或 source comment")
    return {"selected": True, "mode": config.mode, "allow_writeback": config.allow_writeback, "sandbox": task.role.sandbox(), "task": task_to_mapping(task), "context": context, "workflow_run_id": config.workflow_run_id}


def _read(path: Path) -> dict: return json.loads(path.read_text(encoding="utf-8"))
def _write(path: Path, value: dict) -> None: path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
def _task(path: Path) -> Task:
    value = _read(path); return task_from_mapping(value.get("task", value))
def _client(repository: str) -> GitHubRestClient: return GitHubRestClient(repository, os.getenv("GITHUB_TOKEN", ""))


def command_select(args: argparse.Namespace) -> int:
    config = RunnerConfig(args.repository, workflow_run_id=os.getenv("GITHUB_RUN_ID", "manual"), mode=args.mode, allow_writeback=args.allow_writeback.lower() == "true", lease_seconds=args.lease_seconds)
    try: value = select_and_claim(None if args.fixture else _client(args.repository), _read(Path(args.event)), config, args.queue_id, Path(args.fixture) if args.fixture else None)
    except QueueRunError as exc: value = {"selected": False, "mode": config.mode, "allow_writeback": config.allow_writeback, "error": str(exc)}
    directory = Path(args.output_dir); _write(directory / "selection.json", value)
    if value.get("selected"): _write(directory / "task.json", {"task": value["task"]}); _write(directory / "context.json", value["context"])
    return 0


def command_render(args: argparse.Namespace) -> int:
    Path(args.output).write_text(render_prompt(_task(Path(args.task)), _read(Path(args.context))), encoding="utf-8"); return 0
def command_schema(args: argparse.Namespace) -> int:
    Path(args.output).write_text(json.dumps(output_schema(_task(Path(args.task)).role), ensure_ascii=False), encoding="utf-8"); return 0
def command_dry_agent(args: argparse.Namespace) -> int:
    value = dry_agent_result(_task(Path(args.task))); Path(args.github_output).open("a", encoding="utf-8").write(f"final-message={value}\n"); return 0


def command_validate(args: argparse.Namespace) -> int:
    task = _task(Path(args.task)); raw = args.agent_output or dry_agent_result(task)
    result = parse_agent_result(raw, task) if task.role.requires_codex() else AgentResult(task.role, "pass", "test validation")
    directory = Path(args.output_dir); directory.mkdir(parents=True, exist_ok=True)
    value = {"agent_result": result.result, "role": task.role.value, "result": result_mapping(result), "has_patch": False, "changed_files": [], "test_result": "not-run"}
    if result.result == "pass" and task.role is Role.WORKER_A:
        from .git_ops import validate_patch
        checked = validate_patch(Path(args.target_repo), task, result.patch.encode("utf-8"), directory); value.update({"has_patch": checked.has_patch, "changed_files": checked.changed_files, "test_result": checked.test_output})
    elif result.result == "pass" and task.role is Role.TEST_VALIDATION:
        from .git_ops import validate_patch
        checked = validate_patch(Path(args.target_repo), task, b"", directory); value["test_result"] = checked.test_output
    _write(directory / "result.json", value); return 0


def command_writeback(args: argparse.Namespace) -> int:
    task, value = _task(Path(args.task)), _read(Path(args.validated_dir) / "result.json")
    if args.mode == "dry-run":
        _write(Path(args.report), {"mode": "dry-run", "queue_id": task.queue_id, "selected": True, "agent_result": value["agent_result"], "writeback": "simulated", "external_writes": 0, "pushes": 0}); return 0
    client = _client(args.repository)
    try:
        if value["agent_result"] != "pass":
            client.post_issue_comment(19, build_needs_fix(task, "; ".join(value.get("evidence", [])) or value["summary"])); client.dispatch_next(task.queue_id); return 0
        commit = task.base_commit; files = value.get("changed_files", [])
        if value.get("has_patch"): commit, files, _ = create_and_push(Path(args.target_repo), task, Path(args.validated_dir) / "validated.patch", Path(args.validated_dir) / "manifest.sha256")
        next_role = ROLE_TRANSITIONS.get(task.role)
        if next_role: client.post_issue_comment(19, build_ready_handoff(task, next_role, commit, f"files={','.join(files) or 'NONE'}"))
        else: client.post_issue_comment(19, build_waiting_review(task, commit, f"files={','.join(files) or 'NONE'}"))
        if task.source_pr != "NONE": client.write_source_pr(_number(task.source_pr, "SOURCE_PR"), f"QUEUE_ID: {task.queue_id}\nRESULT_COMMIT: {commit}\nFILES: {','.join(files) or 'NONE'}")
        client.dispatch_next(task.queue_id)
    except Exception as exc:
        client.post_issue_comment(19, build_blocked_status(task, "Queue Runner 阻擋")); client.write_blocker(build_blocked_comment(task, "Queue Runner 阻擋", str(exc), "push_writeback", "QUEUE_RUNNER", "push_writeback")); return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(); commands = parser.add_subparsers(dest="command", required=True)
    select = commands.add_parser("select-claim"); select.add_argument("--event", required=True); select.add_argument("--output-dir", required=True); select.add_argument("--repository", required=True); select.add_argument("--mode", choices=["dry-run", "live"], required=True); select.add_argument("--queue-id", default=""); select.add_argument("--allow-writeback", default="false"); select.add_argument("--fixture", default=""); select.add_argument("--lease-seconds", type=int, default=3600)
    render = commands.add_parser("render-prompt"); render.add_argument("--task", required=True); render.add_argument("--context", required=True); render.add_argument("--output", required=True)
    schema = commands.add_parser("write-schema"); schema.add_argument("--task", required=True); schema.add_argument("--output", required=True)
    dry = commands.add_parser("dry-agent-output"); dry.add_argument("--task", required=True); dry.add_argument("--github-output", required=True)
    validate = commands.add_parser("validate"); validate.add_argument("--task", required=True); validate.add_argument("--target-repo", required=True); validate.add_argument("--agent-output", default=""); validate.add_argument("--output-dir", required=True)
    writeback = commands.add_parser("writeback"); writeback.add_argument("--task", required=True); writeback.add_argument("--validated-dir", required=True); writeback.add_argument("--target-repo", required=True); writeback.add_argument("--repository", required=True); writeback.add_argument("--mode", choices=["dry-run", "live"], required=True); writeback.add_argument("--report", required=True)
    args = parser.parse_args(); return {"select-claim": command_select, "render-prompt": command_render, "write-schema": command_schema, "dry-agent-output": command_dry_agent, "validate": command_validate, "writeback": command_writeback}[args.command](args)


if __name__ == "__main__": raise SystemExit(main())
