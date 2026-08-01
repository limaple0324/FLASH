from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from automation.codex_queue_runner.cli import RunnerConfig, select_and_claim, validate_remote_source
from automation.codex_queue_runner.git_ops import create_and_push, validate_patch
from automation.codex_queue_runner.models import QueueRunError, TaskStatus
from automation.codex_queue_runner.parser import parse_task_comment
from automation.codex_queue_runner.scope_guard import parse_raw_changes, validate_git_changes
from automation.codex_queue_runner.selector import claim_belongs_to_run, collect_candidates, select_task
from automation.codex_queue_runner.status_writer import build_blocked_comment, build_waiting_review_comment
from automation.codex_queue_runner.test_command_guard import pytest_argv, validate_test_commands


def comment(queue_id="Q-1", status="READY", role="WORKER_A", author="limaple0324", number=1, created="2026-08-01T01:00:00Z", source_pr="#21", branch="automation/task", base="a" * 40, owned="tests/test_fixture.py", forbidden="automation/", selectors="tests/test_fixture.py"):
    return {"id": number, "created_at": created, "user": {"login": author}, "body": textwrap.dedent(f"""
        ```
        QUEUE_ID: {queue_id}
        STATUS: {status}
        ROLE: {role}
        SOURCE_ISSUE: #20
        SOURCE_PR: {source_pr}
        BASE_COMMIT: {base}
        TARGET_BRANCH: {branch}
        SCOPE: fixture task
        OWNED_FILES: {owned}
        FORBIDDEN: {forbidden}
        ACCEPTANCE: pass
        MINIMUM_TESTS: {selectors}
        BLOCKER_INBOX: #18
        NEXT_ROLE: CODE_REVIEW
        ```
    """).strip()}


def status(queue_id, value, number, created, run=""):
    fields = [f"QUEUE_ID: {queue_id}", f"STATUS: {value}"]
    if run: fields.append(f"WORKFLOW_RUN_ID: {run}")
    return {"id": number, "created_at": created, "user": {"login": "github-actions[bot]"}, "body": "\n".join(fields)}


class FakeClient:
    def __init__(self, comments, head="a" * 40, pr=True):
        self.comments, self.head, self.pr, self.posts, self.dispatches = comments, head, pr, [], []
    def list_issue_comments(self, _): return self.comments
    def post_issue_comment(self, issue, body):
        self.posts.append((issue, body)); value = {"id": 1000 + len(self.posts), "created_at": "2026-08-01T02:00:00Z", "user": {"login": "github-actions[bot]"}, "body": body}; self.comments.append(value); return value
    def get_issue(self, _): return {"state": "open"}
    def get_pull_request(self, _): return {"state": "open", "head": {"ref": "automation/task", "sha": self.head}} if self.pr else {"state": "closed", "head": {}}
    def get_branch_sha(self, _): return self.head
    def write_blocker(self, body): self.posts.append((18, body))
    def dispatch_next(self, queue): self.dispatches.append(queue)


def test_only_issue_19_is_accepted():
    result = select_and_claim(FakeClient([comment()]), {"event_name": "issue_comment", "issue": {"number": 20}}, RunnerConfig())
    assert result["selected"] is False


def test_rejects_non_owner_and_missing_fields():
    assert collect_candidates([comment(author="other")], "limaple0324") == []
    with pytest.raises(QueueRunError): parse_task_comment("QUEUE_ID: Q\nSTATUS: READY")


@pytest.mark.parametrize("branch", ["main", "release/latest", "release/sp1", "release/other"])
def test_rejects_protected_branches(branch):
    with pytest.raises(QueueRunError): validate_remote_source(FakeClient([comment(branch=branch)]), parse_task_comment(comment(branch=branch)["body"]))


def test_latest_state_overrides_ready_and_needs_fix_reclaims():
    values = [comment(number=1), status("Q-1", "CLAIMED", 2, "2026-08-01T01:01:00Z", "r1")]
    with pytest.raises(QueueRunError): select_task(collect_candidates(values, "limaple0324"))
    values.append(status("Q-1", "NEEDS_FIX", 3, "2026-08-01T01:02:00Z"))
    assert select_task(collect_candidates(values, "limaple0324")).task.status == TaskStatus.NEEDS_FIX


def test_claim_is_single_and_belongs_to_one_run():
    client = FakeClient([comment()]); config = RunnerConfig(dry_run=False, allow_writeback=True, workflow_run_id="run-a")
    assert select_and_claim(client, {"event_name": "schedule"}, config)["selected"]
    assert claim_belongs_to_run(client.comments, "Q-1", "run-a")
    with pytest.raises(QueueRunError): select_and_claim(client, {"event_name": "schedule"}, config)


def test_source_pr_none_and_remote_mismatch_blocked():
    task = parse_task_comment(comment(source_pr="NONE")["body"])
    validate_remote_source(FakeClient([comment(source_pr="NONE")]), task)
    with pytest.raises(QueueRunError): validate_remote_source(FakeClient([comment()], head="b" * 40), parse_task_comment(comment()["body"]))


def test_missing_openai_secret_blocks_before_agent():
    with pytest.raises(QueueRunError): select_and_claim(FakeClient([comment()]), {"event_name": "schedule"}, RunnerConfig(dry_run=False), openai_secret_available=False)


@pytest.mark.parametrize("selector", ["tests/test_x.py\nwhoami", "tests/test_x.py;whoami", "tests/test_x.py -p evil", "tests/test_x.py::test_x$(x)"])
def test_rejects_shell_and_pytest_injection(selector):
    ok, issues = validate_test_commands([selector])
    assert not ok and issues


def test_builds_fixed_pytest_argv():
    assert pytest_argv(["tests/test_x.py::test_ok"]) == ["python", "-m", "pytest", "-q", "tests/test_x.py::test_ok"]


def test_raw_git_guard_rejects_rename_symlink_submodule_and_workflow():
    raw = b":100644 100644 a b R100\0tests/old.py\0.github/workflows/x.yml\0:100644 120000 a b M\0tests/link.py\0:160000 160000 a b M\0tests/sub\0"
    ok, errors = validate_git_changes(parse_raw_changes(raw), ["tests/"], [])
    assert not ok and any("OWNED_FILES" in item for item in errors)
    assert any("符號連結" in item for item in errors) and any("子模組" in item for item in errors)


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, stdout=subprocess.PIPE, text=True).stdout.strip()


@pytest.fixture
def git_fixture(tmp_path):
    remote, repo = tmp_path / "remote.git", tmp_path / "repo"
    git(tmp_path, "init", "--bare", str(remote)); git(tmp_path, "clone", str(remote), str(repo))
    git(repo, "config", "user.email", "fixture@example.test"); git(repo, "config", "user.name", "fixture")
    (repo / "tests").mkdir(); (repo / "tests" / "test_fixture.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8"); (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    git(repo, "add", "."); git(repo, "commit", "-m", "base"); git(repo, "branch", "-M", "automation/task"); git(repo, "push", "-u", "origin", "automation/task")
    git(tmp_path, "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/automation/task")
    return repo


def candidate_patch(repo, path):
    target = repo / "tests" / "test_fixture.py"; target.write_text("def test_ok():\n    assert 1 == 1\n", encoding="utf-8")
    patch = subprocess.run(["git", "diff", "--binary"], cwd=repo, check=True, stdout=subprocess.PIPE).stdout
    path.write_bytes(patch); git(repo, "checkout", "--", "tests/test_fixture.py")


def test_agent_patch_to_clean_validate_to_commit_tree(git_fixture, tmp_path):
    repo, base = git_fixture, git(git_fixture, "rev-parse", "HEAD")
    task = parse_task_comment(comment(base=base, source_pr="NONE")["body"]); patch, output = tmp_path / "candidate.patch", tmp_path / "validated"
    candidate_patch(repo, patch); result = validate_patch(repo, task, patch, output)
    assert result.has_patch and result.changed_files == ["tests/test_fixture.py"]
    git(repo, "reset", "--hard", base)
    commit, files, command = create_and_push(repo, task, output / "validated.patch", output / "manifest.sha256", "fixture")
    assert git(repo, "rev-parse", f"{commit}^") == base and files == ["tests/test_fixture.py"]
    assert "--force" not in command and "--ff-only" not in command and "-f" not in command


def test_remote_head_change_rejects_push(git_fixture, tmp_path):
    repo, base = git_fixture, git(git_fixture, "rev-parse", "HEAD")
    task = parse_task_comment(comment(base=base, source_pr="NONE")["body"]); patch, output = tmp_path / "candidate.patch", tmp_path / "validated"
    candidate_patch(repo, patch); validate_patch(repo, task, patch, output); git(repo, "reset", "--hard", base)
    other = tmp_path / "other"; git(tmp_path, "clone", str(repo.parent / "remote.git"), str(other)); git(other, "config", "user.email", "x@y.z"); git(other, "config", "user.name", "x")
    (other / "x").write_text("x", encoding="utf-8"); git(other, "add", "."); git(other, "commit", "-m", "advance"); git(other, "push")
    with pytest.raises(QueueRunError): create_and_push(repo, task, output / "validated.patch", output / "manifest.sha256", "fixture")


def test_status_formats_are_waiting_review_and_issue_18_blocker():
    task = parse_task_comment(comment()["body"])
    assert "STATUS: WAITING_REVIEW" in build_waiting_review_comment(task, "abc", [], "PASS")
    blocker = build_blocked_comment(task, "bad", "error", "test", "ROLE", "step")
    assert "STATUS: OPEN" in blocker and "SECRETS_INCLUDED: NO" in blocker


def test_workflow_has_four_isolated_jobs_and_no_release_or_merge():
    workflow = Path(".github/workflows/codex-queue-runner.yml").read_text(encoding="utf-8")
    for name in ("select_claim:", "agent:", "validate:", "push_writeback:", "codex-queue-runner-global", "repository_dispatch:", "CODEX_QUEUE_ENABLED", "openai/codex-action@b11346a6fa031e2e164ab4b7c7ea201afffd7d59"):
        assert name in workflow
    assert "persist-credentials: false" in workflow and "safety-strategy: drop-sudo" in workflow
    assert "danger-full-access" not in workflow and "full-auto" not in workflow
    assert "git merge" not in workflow.lower() and "gh pr merge" not in workflow.lower()
    assert "OPENAI_API_KEY" not in workflow.split("push_writeback:", 1)[1]
