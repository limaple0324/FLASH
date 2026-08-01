from __future__ import annotations

import pytest
import textwrap
from pathlib import Path

from automation.codex_queue_runner.cli import _build_push_command, run_from_event, RunnerConfig
from automation.codex_queue_runner.models import QueueRunError, TaskStatus
from automation.codex_queue_runner.parser import parse_task_comment
from automation.codex_queue_runner.selector import collect_candidates, select_task
from automation.codex_queue_runner.scope_guard import ensure_scope
from automation.codex_queue_runner.test_command_guard import validate_test_commands


class DummyGitHubClient:
    def __init__(self, comments):
        self.comments = comments
        self.posts = []
        self.updates = []

    def list_issue_comments(self, issue_number: int):
        return self.comments

    def post_issue_comment(self, issue_number: int, body: str) -> None:
        self.posts.append((issue_number, body))

    def update_issue_comment(self, comment_id: int, body: str) -> None:
        self.updates.append((comment_id, body))


def _comment(
    queue_id: str = "Q-001",
    status: str = "READY",
    role: str = "WORKER_A",
    source_issue: str = "#19",
    source_pr: str = "#21",
    base_commit: str = "a" * 40,
    target_branch: str = "automation/codex-queue-runner",
    scope: str = "補強自動化流程",
    owned_files: str = "automation/codex_queue_runner/cli.py,automation/codex_queue_runner/parser.py",
    forbidden: str = "S1/",
    acceptance: str = "功能可運作",
    minimum_tests: str = "pytest -q tests/test_codex_queue_runner.py",
    blocker_inbox: str = "#18",
    full_regression: str = "NO",
    windows_build: str = "NO",
    next_role: str = "BATCH_CONTROL",
    author: str = "limaple0324",
    issue_id: int = 100,
    created_at: str = "2026-08-01T01:00:00Z",
) -> dict:
    text = textwrap.dedent(
        f"""
        ```
        QUEUE_ID: {queue_id}
        STATUS: {status}
        ROLE: {role}
        SOURCE_ISSUE: {source_issue}
        SOURCE_PR: {source_pr}
        BASE_COMMIT: {base_commit}
        TARGET_BRANCH: {target_branch}
        SCOPE: {scope}
        OWNED_FILES: {owned_files}
        FORBIDDEN: {forbidden}
        ACCEPTANCE: {acceptance}
        MINIMUM_TESTS: {minimum_tests}
        BLOCKER_INBOX: {blocker_inbox}
        FULL_REGRESSION: {full_regression}
        WINDOWS_BUILD: {windows_build}
        NEXT_ROLE: {next_role}
        ```
        """
    ).strip()

    return {
        "id": issue_id,
        "body": text,
        "created_at": created_at,
        "user": {"login": author},
    }


def _payload_for_comment(comment: dict, issue_number: int = 19, action: str = "created"):
    return {
        "action": action,
        "issue": {"number": issue_number},
        "comment": comment,
    }


def test_only_accept_issue_19():
    comment = _comment()
    result = run_from_event(
        event_payload=_payload_for_comment(comment, issue_number=20),
        config=RunnerConfig(repo_root=Path("."), dry_run=True),
    )
    assert result is None


def test_reject_non_owner_task():
    comment = _comment(author="other")
    result = run_from_event(
        event_payload=_payload_for_comment(comment),
        config=RunnerConfig(repo_root=Path("."), dry_run=True),
    )
    assert result is not None
    assert result.status == TaskStatus.BLOCKED
    assert result.blocker_comment is not None
    assert "BLOCKER_ID" in result.blocker_comment


def test_missing_required_fields():
    bad_body = _comment()
    lines = []
    for line in bad_body["body"].splitlines():
        if not line.strip().startswith("OWNED_FILES:"):
            lines.append(line)
    bad_body["body"] = "\n".join(lines)
    with pytest.raises(QueueRunError):
        parse_task_comment(bad_body["body"])


def test_reject_invalid_target_branch():
    for branch in ["main", "release/latest", "release/sp1"]:
        comment = _comment(queue_id=f"Q-{branch.replace('/', '-')}", target_branch=branch)
        result = run_from_event(
            event_payload=_payload_for_comment(comment),
            config=RunnerConfig(repo_root=Path("."), dry_run=True),
        )
        assert result is not None
        assert result.status == TaskStatus.BLOCKED


def test_reject_duplicate_claimable_queue_id():
    first = _comment(queue_id="Q-dup", created_at="2026-08-01T01:00:00Z", issue_id=1)
    second = _comment(
        queue_id="Q-dup",
        created_at="2026-08-01T01:01:00Z",
        issue_id=2,
    )
    client = DummyGitHubClient([first, second])
    result = run_from_event(
        event_payload={},
        github_client=client,
        config=RunnerConfig(repo_root=Path("."), dry_run=True),
    )
    assert result is not None
    assert result.status == TaskStatus.BLOCKED
    assert "重複認領" in (result.blocker_comment or "")


def test_select_only_one_task_from_multiple_candidates():
    first = _comment(queue_id="Q-101", created_at="2026-08-01T01:00:00Z", issue_id=1)
    second = _comment(queue_id="Q-102", created_at="2026-08-01T01:01:00Z", issue_id=2)
    client = DummyGitHubClient([first, second])
    result = run_from_event(
        event_payload={},
        github_client=client,
        config=RunnerConfig(repo_root=Path("."), dry_run=True),
    )
    assert result is not None
    assert result.status == TaskStatus.WAITING_REVIEW
    assert result.status_comment is not None
    assert "Q-101" in result.status_comment


def test_scope_guard_outside_owned_files_fails():
    ok, errors = ensure_scope(
        ["automation/codex_queue_runner/models.py"],
        ["automation/codex_queue_runner/cli.py"],
        [],
    )
    assert not ok
    assert "超出 OWNED_FILES" in ";".join(errors)


def test_scope_guard_forbidden_fails():
    ok, errors = ensure_scope(
        ["automation/codex_queue_runner/cli.py"],
        ["automation/codex_queue_runner/cli.py"],
        ["automation/codex_queue_runner/*.py"],
    )
    assert not ok
    assert "禁用" in ";".join(errors)


def test_reject_dangerous_test_command():
    ok, issues = validate_test_commands(["pytest -q tests/test_codex_queue_runner.py; rm -rf /"])
    assert not ok
    assert issues
    assert "禁用字元" in issues[0].reason


def test_push_command_not_force_push():
    task = parse_task_comment(_comment(queue_id="Q-push")["body"])
    cmd = _build_push_command(task)
    assert "--force" not in cmd
    assert "-f" not in cmd


def test_agent_stage_does_not_write_github():
    comment = _comment()

    class NoWriteClient(DummyGitHubClient):
        def post_issue_comment(self, issue_number: int, body: str) -> None:
            raise AssertionError("agent stage should not write")

    client = NoWriteClient([])
    result = run_from_event(
        event_payload=_payload_for_comment(comment),
        github_client=client,
        config=RunnerConfig(repo_root=Path("."), dry_run=True),
    )
    assert result is not None
    assert result.status == TaskStatus.WAITING_REVIEW


def test_push_stage_rejects_openai_key():
    comment = _comment(role="BATCH_CONTROL")
    result = run_from_event(
        event_payload=_payload_for_comment(comment),
        config=RunnerConfig(
            repo_root=Path("."),
            dry_run=False,
            openai_api_key="fake-key",
        ),
    )
    assert result is not None
    assert result.status == TaskStatus.BLOCKED
    assert "OPENAI" in (result.blocker_comment or "")


def test_successful_result_is_waiting_review():
    comment = _comment()
    result = run_from_event(
        event_payload=_payload_for_comment(comment),
        config=RunnerConfig(repo_root=Path("."), dry_run=True),
    )
    assert result is not None
    assert result.status == TaskStatus.WAITING_REVIEW


def test_blocked_result_uses_issue_18_format():
    comment = _comment(role="WORKER_A", author="other")
    result = run_from_event(
        event_payload=_payload_for_comment(comment),
        config=RunnerConfig(repo_root=Path("."), dry_run=True),
    )
    assert result is not None
    assert result.status == TaskStatus.BLOCKED
    assert result.blocker_comment is not None
    assert "BLOCKER_ID" in result.blocker_comment
    assert "STATUS: OPEN" in result.blocker_comment
    assert "EXACT_ERROR" in result.blocker_comment


def test_workflow_has_no_merge_or_release_steps():
    workflow = open(".github/workflows/codex-queue-runner.yml", "r", encoding="utf-8").read()
    lower = workflow.lower()
    assert "gh pr merge" not in lower
    assert "git merge" not in lower
    assert "release/latest" not in workflow.lower()
    assert "release/sp1" not in workflow.lower()


def test_selector_helpers():
    comments = [
        _comment(queue_id="Q-a", created_at="2026-08-01T01:00:00Z", issue_id=1),
        _comment(queue_id="Q-b", created_at="2026-08-01T01:01:00Z", issue_id=2),
    ]
    candidates = collect_candidates(comments, require_owner="limaple0324")
    assert len(candidates) == 2
    picked = select_task(candidates)
    assert picked.task.queue_id == "Q-a"
