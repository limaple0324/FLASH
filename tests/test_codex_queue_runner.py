from __future__ import annotations

import argparse
import json
import subprocess
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from automation.codex_queue_runner.cli import RunnerConfig, build_context_snapshot, command_select, command_validate, command_writeback, select_and_claim, validate_remote_source
from automation.codex_queue_runner.git_ops import create_and_push, find_reconciled_commit, validate_patch
from automation.codex_queue_runner.github_client import GitHubRestClient
from automation.codex_queue_runner.models import QueueRunError, Role, TaskStatus, task_from_mapping, task_to_mapping
from automation.codex_queue_runner.parser import parse_task_comment
from automation.codex_queue_runner.role_output import dry_agent_result, output_schema, parse_agent_result
from automation.codex_queue_runner.prompt_builder import MAX_PROMPT_CONTEXT_BYTES, render_prompt, validate_prompt_context
from automation.codex_queue_runner.scope_guard import parse_raw_changes, validate_git_changes
from automation.codex_queue_runner.selector import claim_belongs_to_run, collect_candidates, select_task, stale_claims
from automation.codex_queue_runner.test_command_guard import pytest_argv, validate_test_commands


def comment(queue_id="Q-1", status="READY", role="WORKER_A", author="limaple0324", number=1, created="2026-08-01T01:00:00Z", source_pr="#21", branch="automation/task", base="a" * 40, owned="tests/test_fixture.py", forbidden="automation/", selectors="tests/test_fixture.py", full="NO", windows="NO"):
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
        FULL_REGRESSION: {full}
        WINDOWS_BUILD: {windows}
        BLOCKER_INBOX: #18
        ```
    """).strip()}


def state(queue_id, value, number, created, run="", source=1, role="", base="", writer=True, author="github-actions[bot]"):
    fields = [f"QUEUE_ID: {queue_id}", f"STATUS: {value}", f"SOURCE_COMMENT_ID: {source}"]
    if run: fields.append(f"WORKFLOW_RUN_ID: {run}")
    if role: fields.append(f"ROLE: {role}")
    if base: fields.append(f"BASE_COMMIT: {base}")
    if writer: fields.append("STATE_WRITER: CODEX_QUEUE_RUNNER")
    return {"id": number, "created_at": created, "user": {"login": author}, "body": "\n".join(fields)}


class FakeClient:
    def __init__(self, comments, head="a" * 40, run=None):
        self.comments, self.head, self.run, self.posts, self.dispatches, self.operations = comments, head, run or {"status": "completed"}, [], [], []
        self.fail_source_pr = False; self.fail_ready = False
    def list_issue_comments(self, _): return self.comments
    def list_pull_request_files(self, _): return [{"filename": "tests/test_fixture.py"}]
    def post_issue_comment(self, issue, body):
        if self.fail_ready and "STATUS: READY" in body: raise QueueRunError("simulated READY write failure")
        self.operations.append(("issue", issue, body))
        self.posts.append((issue, body)); value = {"id": 1000 + len(self.posts), "created_at": "2026-08-01T03:00:00Z", "user": {"login": "github-actions[bot]"}, "body": body}; self.comments.append(value); return value
    def get_issue(self, _): return {"title": "issue", "body": "issue body"}
    def get_pull_request(self, _): return {"state": "open", "base": {"sha": "base"}, "head": {"ref": "automation/task", "sha": self.head}}
    def get_branch_sha(self, _): return self.head
    def get_workflow_run(self, _): return self.run
    def get_commit(self, _): return {"parents": [], "commit": {"message": "other"}}
    def write_blocker(self, body): self.operations.append(("blocker", 18, body)); self.posts.append((18, body))
    def write_source_pr(self, number, body):
        if self.fail_source_pr: raise QueueRunError("simulated source PR write failure")
        self.operations.append(("source_pr", number, body)); self.posts.append((number, body))
    def dispatch_next(self, queue): self.operations.append(("dispatch", queue)); self.dispatches.append(queue)


def test_only_issue_19_owner_and_trusted_state_are_accepted():
    result = select_and_claim(FakeClient([comment()]), {"event_name": "issue_comment", "issue": {"number": 20}}, RunnerConfig())
    assert result["selected"] is False
    assert collect_candidates([comment(author="other")]) == []
    values = [comment(), state("Q-1", "CLOSED", 2, "2026-08-01T01:02:00Z", writer=False)]
    assert select_task(collect_candidates(values)).task.status is TaskStatus.READY


def test_latest_state_claim_binding_and_needs_fix_reclaim():
    values = [comment(), state("Q-1", "CLAIMED", 2, "2026-08-01T01:01:00Z", "run-a")]
    assert claim_belongs_to_run(values, "Q-1", "run-a", 1)
    with pytest.raises(QueueRunError): select_task(collect_candidates(values))
    values.append(state("Q-1", "NEEDS_FIX", 3, "2026-08-01T01:02:00Z", role="WORKER_A"))
    task = select_task(collect_candidates(values)).task
    assert task.status is TaskStatus.NEEDS_FIX and task.role is Role.WORKER_A


def test_chinese_semicolon_and_multiline_lists_are_parsed():
    body = comment(owned="a.py；b.py;c.py,\n- d.py", forbidden="x/；y/")["body"]
    task = parse_task_comment(body)
    assert task.owned_files == ["a.py", "b.py", "c.py", "d.py"] and task.forbidden == ["x/", "y/"]


@pytest.mark.parametrize("branch", ["main", "release/latest", "release/sp1", "release/other"])
def test_protected_branches_manual_gates_and_flags_block(branch):
    with pytest.raises(QueueRunError): validate_remote_source(FakeClient([comment(branch=branch)]), parse_task_comment(comment(branch=branch)["body"]))
    with pytest.raises(QueueRunError): validate_remote_source(FakeClient([comment(role="BATCH_CONTROL")]), parse_task_comment(comment(role="BATCH_CONTROL")["body"]))
    with pytest.raises(QueueRunError): validate_remote_source(FakeClient([comment(full="YES")]), parse_task_comment(comment(full="YES")["body"]))
    with pytest.raises(QueueRunError): validate_remote_source(FakeClient([comment(windows="YES")]), parse_task_comment(comment(windows="YES")["body"]))


def test_source_pr_none_and_live_without_writeback_block():
    task = parse_task_comment(comment(source_pr="NONE")["body"]); validate_remote_source(FakeClient([comment(source_pr="NONE")]), task)
    with pytest.raises(QueueRunError): select_and_claim(FakeClient([comment()]), {"event_name": "schedule"}, RunnerConfig(mode="live", allow_writeback=False))


def test_live_claim_and_context_snapshot_have_no_token(monkeypatch):
    monkeypatch.setenv("OPENAI_SECRET_AVAILABLE", "true")
    client = FakeClient([comment()]); result = select_and_claim(client, {"event_name": "schedule"}, RunnerConfig(mode="live", allow_writeback=True, workflow_run_id="run-a"))
    assert result["selected"] and claim_belongs_to_run(client.comments, "Q-1", "run-a", 1)
    assert result["context"]["changed_files"] == ["tests/test_fixture.py"]
    assert "token" not in json.dumps(result["context"]).lower()


def test_stale_claim_recovers_only_completed_run():
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    values = [comment(created=old), state("Q-1", "CLAIMED", 2, old, "dead")]
    candidates = collect_candidates(values)
    assert stale_claims(candidates, datetime.now(timezone.utc), 60, lambda _: {"status": "completed"})
    assert not stale_claims(candidates, datetime.now(timezone.utc), 60, lambda _: {"status": "in_progress"})


@pytest.mark.parametrize("selector", ["tests/test_x.py\nwhoami", "tests/test_x.py;whoami", "tests/test_x.py -p evil", "tests/test_x.py::test_x$(x)"])
def test_shell_and_pytest_injection_are_rejected(selector):
    ok, issues = validate_test_commands([selector]); assert not ok and issues
    assert pytest_argv(["tests/test_x.py::test_ok"]) == ["python", "-m", "pytest", "-q", "tests/test_x.py::test_ok"]


def test_real_git_guard_rejects_rename_symlink_submodule_and_workflow():
    raw = b":100644 100644 a b R100\0tests/old.py\0.github/workflows/x.yml\0:100644 120000 a b M\0tests/link.py\0:160000 160000 a b M\0tests/sub\0"
    ok, errors = validate_git_changes(parse_raw_changes(raw), ["tests/"], [])
    assert not ok and any("OWNED_FILES" in item for item in errors) and any("符號連結" in item for item in errors) and any("子模組" in item for item in errors)


def test_structured_worker_audit_review_outputs_and_role_transition():
    worker = parse_task_comment(comment(role="WORKER_A")["body"])
    audit = parse_task_comment(comment(role="REQUIREMENTS_AUDIT")["body"])
    review = parse_task_comment(comment(role="CODE_REVIEW")["body"])
    assert "patch" in output_schema(Role.WORKER_A)["properties"]
    with pytest.raises(QueueRunError): parse_agent_result('{"role":"WORKER_A","result":"pass","summary":"x","patch":"","evidence":["e"]}', worker)
    assert parse_agent_result('{"role":"REQUIREMENTS_AUDIT","result":"fail","summary":"x","reasons":["bad"],"evidence":["e"]}', audit).result == "fail"
    value = parse_agent_result('{"role":"CODE_REVIEW","result":"pass","summary":"x","severity":"none","findings":[],"evidence":["e"]}', review)
    assert value.severity == "none"
    with pytest.raises(QueueRunError): parse_agent_result('{"role":"CODE_REVIEW","result":"pass","summary":"x","evidence":[]}', review)


class PagedClient(GitHubRestClient):
    def __init__(self): super().__init__("owner/repo", "token"); self.calls = []
    def _request(self, method, path, body=None):
        self.calls.append(path)
        if "page=2" in path: return [{"id": 101}], {}
        return [{"id": item} for item in range(100)], {"Link": '<https://api.github.com/repos/owner/repo/issues/19/comments?per_page=100&page=2>; rel="next"'}


def test_issue_comments_paginate_past_101():
    client = PagedClient(); assert len(client.list_issue_comments(19)) == 101 and len(client.calls) == 2


def git(repo, *args): return subprocess.run(["git", *args], cwd=repo, check=True, stdout=subprocess.PIPE, text=True).stdout.strip()


@pytest.fixture
def git_fixture(tmp_path):
    remote, repo = tmp_path / "remote.git", tmp_path / "repo"; git(tmp_path, "init", "--bare", str(remote)); git(tmp_path, "clone", str(remote), str(repo))
    git(repo, "config", "user.email", "fixture@example.test"); git(repo, "config", "user.name", "fixture")
    (repo / "tests").mkdir(); (repo / "tests" / "test_fixture.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8"); (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    git(repo, "add", "."); git(repo, "commit", "-m", "base"); git(repo, "branch", "-M", "automation/task"); git(repo, "push", "-u", "origin", "automation/task"); git(tmp_path, "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/automation/task")
    return repo


def worker_patch(repo):
    target = repo / "tests" / "test_fixture.py"; target.write_text("def test_ok():\n    assert 1 == 1\n", encoding="utf-8")
    patch = subprocess.run(["git", "diff", "--binary"], cwd=repo, check=True, stdout=subprocess.PIPE).stdout; git(repo, "checkout", "--", "tests/test_fixture.py"); return patch


def test_runner_can_validate_target_without_automation_and_reconcile_once(git_fixture, tmp_path):
    repo, base = git_fixture, git(git_fixture, "rev-parse", "HEAD")
    assert not (repo / "automation").exists()
    task = parse_task_comment(comment(base=base, source_pr="NONE")["body"]); output = tmp_path / "validated"; checked = validate_patch(repo, task, worker_patch(repo), output, run_tests=False)
    assert checked.has_patch and checked.changed_files == ["tests/test_fixture.py"]
    git(repo, "reset", "--hard", base); commit, _, command = create_and_push(repo, task, output / "validated.patch", output / "manifest.sha256")
    assert commit == find_reconciled_commit(repo, task) and "--force" not in command and "--ff-only" not in command and "-f" not in command
    repeat, _, second = create_and_push(repo, task, output / "validated.patch", output / "manifest.sha256")
    assert repeat == commit and second == []


def test_fresh_clone_reconciles_exact_remote_commit_without_second_push(git_fixture, tmp_path):
    repo, base = git_fixture, git(git_fixture, "rev-parse", "HEAD")
    task = parse_task_comment(comment(base=base, source_pr="NONE")["body"])
    output = tmp_path / "validated"; validate_patch(repo, task, worker_patch(repo), output, run_tests=False)
    git(repo, "reset", "--hard", base); commit, _, _ = create_and_push(repo, task, output / "validated.patch", output / "manifest.sha256")
    fresh = tmp_path / "fresh"; git(tmp_path, "clone", str(tmp_path / "remote.git"), str(fresh)); git(fresh, "checkout", "--detach", base)
    repeated, _, command = create_and_push(fresh, task, output / "validated.patch", output / "manifest.sha256")
    assert repeated == commit and command == []


@pytest.mark.parametrize("role", ["REQUIREMENTS_AUDIT", "CODE_REVIEW"])
def test_audit_and_review_fail_write_needs_fix_with_full_fields(tmp_path, monkeypatch, role):
    task = parse_task_comment(comment(role=role)["body"]); task_path = tmp_path / "task.json"; validated = tmp_path / "validated"; validated.mkdir()
    task_path.write_text(json.dumps({"task": task_to_mapping(task)}), encoding="utf-8")
    result = {"agent_result": "fail", "result": {"role": role, "result": "fail", "summary": "blocked finding", "patch": "", "reasons": ["reason"] if role == "REQUIREMENTS_AUDIT" else [], "evidence": ["evidence"], "severity": "none" if role == "REQUIREMENTS_AUDIT" else "high", "findings": [] if role == "REQUIREMENTS_AUDIT" else ["finding"]}, "test_result": "not-run"}
    (validated / "result.json").write_text(json.dumps(result), encoding="utf-8")
    client = FakeClient([]); monkeypatch.setattr("automation.codex_queue_runner.cli._client", lambda _: client)
    assert command_writeback(argparse.Namespace(task=str(task_path), validated_dir=str(validated), target_repo=str(tmp_path), repository="owner/repo", mode="live", report=str(tmp_path / "report"))) == 0
    issue_body = client.posts[0][1]; pr_body = client.posts[1][1]
    assert "STATUS: NEEDS_FIX" in issue_body and "ROLE: WORKER_A" in issue_body and "ROLE: " + role in pr_body and "SUMMARY: blocked finding" in pr_body and not client.dispatches


def test_manual_dry_run_e2e_has_no_external_write(tmp_path):
    fixture = Path("tests/fixtures/codex_queue_dry_task.txt"); selected = select_and_claim(None, {"event_name": "workflow_dispatch"}, RunnerConfig(mode="dry-run"), fixture=fixture)
    task = task_from_mapping(selected["task"]); raw = dry_agent_result(task); parse_agent_result(raw, task)
    task_path, output = tmp_path / "task.json", tmp_path / "validated"; task_path.write_text(json.dumps({"task": selected["task"]}), encoding="utf-8")
    assert command_validate(argparse.Namespace(task=str(task_path), target_repo=str(tmp_path), agent_output=raw, output_dir=str(output))) == 0
    report = tmp_path / "report.json"
    assert command_writeback(argparse.Namespace(task=str(task_path), validated_dir=str(output), target_repo=str(tmp_path), repository="owner/repo", mode="dry-run", report=str(report))) == 0
    assert json.loads(report.read_text(encoding="utf-8"))["external_writes"] == 0


def test_workflow_supply_chain_permissions_and_agent_last_step():
    workflow = Path(".github/workflows/codex-queue-runner.yml").read_text(encoding="utf-8")
    for name in ("select_claim:", "agent:", "validate:", "push_writeback:", "runner-src", "target-repo", "PYTHONPATH", "output-schema-file", "codex-version: \"0.146.0\"", "CODEX_QUEUE_ENABLED"):
        assert name in workflow
    assert "uses: actions/checkout@v5" not in workflow and "uses: actions/setup-python@v5" not in workflow and "uses: actions/upload-artifact@v4" not in workflow and "uses: actions/download-artifact@v4" not in workflow
    agent = workflow.split("  validate:", 1)[0].split("  agent:", 1)[1]
    assert "steps.codex.outputs['final-message']" in workflow and "steps.codex.outputs.final-message" not in workflow
    assert agent.rstrip().endswith("allow-bot-users: ${{ github.event_name == 'repository_dispatch' && 'github-actions[bot]' || '' }}")
    assert "GITHUB_TOKEN" not in agent and "OPENAI_API_KEY" not in workflow.split("  push_writeback:", 1)[1]
    assert "unset GITHUB_TOKEN" in workflow and "--force" not in workflow and "--ff-only" not in workflow
    assert "QUEUE_MODE: ${{ github.event_name == 'workflow_dispatch' && inputs.mode || 'live' }}" in workflow


def test_isolation_environment_is_pinned_and_has_no_unsafe_fallback(monkeypatch):
    import automation.codex_queue_runner.git_ops as git_ops
    monkeypatch.setattr(git_ops.shutil, "which", lambda _: "bwrap")
    monkeypatch.setattr(git_ops.os, "name", "posix")
    from automation.codex_queue_runner.git_ops import _sandbox
    workspace = Path("/tmp/source")
    command = _sandbox(workspace, ["true"])
    for value in ("--uid", "0", "--gid", "--cap-drop", "ALL", "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-net", "--unshare-uts", "--unshare-cgroup-try", "--new-session", "--die-with-parent"):
        assert value in command
    assert "--disable-userns" not in command
    assert "/work" not in command
    ro_bind = command.index("--ro-bind")
    tmpfs = command.index("--tmpfs")
    work_dir = command.index("--dir")
    bind = command.index("--bind")
    chdir = command.index("--chdir")
    assert command[ro_bind : ro_bind + 3] == ["--ro-bind", "/", "/"]
    assert command[tmpfs : tmpfs + 2] == ["--tmpfs", "/tmp"]
    assert command[work_dir : work_dir + 2] == ["--dir", "/tmp/work"]
    assert command[bind : bind + 3] == ["--bind", str(workspace), "/tmp/work"]
    assert command[chdir : chdir + 2] == ["--chdir", "/tmp/work"]
    assert ro_bind < tmpfs < work_dir < bind < chdir
    workflow = Path(".github/workflows/codex-queue-runner.yml").read_text(encoding="utf-8")
    for job in ("pr_isolation_dry_run", "validate"):
        section = workflow.split(f"  {job}:", 1)[1]
        assert "runs-on: ubuntu-22.04" in section
    validate = workflow.split("  validate:", 1)[1].split("  push_writeback:", 1)[0]
    assert "Prepare trusted candidate isolation" in validate and "bubblewrap" in validate and "isolation-probe" in validate
    assert "env -u GITHUB_TOKEN -u OPENAI_API_KEY" in workflow and "--unshare-net" in Path("automation/codex_queue_runner/git_ops.py").read_text(encoding="utf-8")


def test_handoffs_dispatch_only_the_same_nonterminal_queue(tmp_path, monkeypatch):
    task = parse_task_comment(comment(role="REQUIREMENTS_AUDIT")["body"]); task_path = tmp_path / "task.json"; output = tmp_path / "validated"; output.mkdir()
    task_path.write_text(json.dumps({"task": task_to_mapping(task)}), encoding="utf-8")
    result = {"agent_result": "pass", "result": {"role": "REQUIREMENTS_AUDIT", "result": "pass", "summary": "ok", "patch": "", "reasons": [], "evidence": ["e"], "severity": "none", "findings": []}, "has_patch": False, "changed_files": [], "test_result": "not-run"}
    (output / "result.json").write_text(json.dumps(result), encoding="utf-8")
    client = FakeClient([]); monkeypatch.setattr("automation.codex_queue_runner.cli._client", lambda _: client)
    assert command_writeback(argparse.Namespace(task=str(task_path), validated_dir=str(output), target_repo=str(tmp_path), repository="owner/repo", mode="live", report=str(tmp_path / "report"))) == 0
    assert client.dispatches == [task.queue_id] and [operation[0] for operation in client.operations] == ["source_pr", "issue", "dispatch"] and "STATUS: READY" in client.operations[1][2]


def test_source_pr_none_dispatches_only_after_ready_write(tmp_path, monkeypatch):
    task = parse_task_comment(comment(role="REQUIREMENTS_AUDIT", source_pr="NONE")["body"]); task_path = tmp_path / "task.json"; output = tmp_path / "validated"; output.mkdir()
    task_path.write_text(json.dumps({"task": task_to_mapping(task)}), encoding="utf-8")
    result = {"agent_result": "pass", "result": {"role": "REQUIREMENTS_AUDIT", "result": "pass", "summary": "ok", "patch": "", "reasons": [], "evidence": ["e"], "severity": "none", "findings": []}, "has_patch": False, "changed_files": [], "test_result": "not-run"}
    (output / "result.json").write_text(json.dumps(result), encoding="utf-8")
    client = FakeClient([]); monkeypatch.setattr("automation.codex_queue_runner.cli._client", lambda _: client)
    assert command_writeback(argparse.Namespace(task=str(task_path), validated_dir=str(output), target_repo=str(tmp_path), repository="owner/repo", mode="live", report=str(tmp_path / "report"))) == 0
    assert [operation[0] for operation in client.operations] == ["issue", "dispatch"] and client.dispatches == [task.queue_id]


def test_terminal_test_validation_waits_without_dispatch(tmp_path, monkeypatch):
    task = parse_task_comment(comment(role="TEST_VALIDATION")["body"]); task_path = tmp_path / "task.json"; output = tmp_path / "validated"; output.mkdir()
    task_path.write_text(json.dumps({"task": task_to_mapping(task)}), encoding="utf-8")
    result = {"agent_result": "pass", "result": {"role": "TEST_VALIDATION", "result": "pass", "summary": "ok", "patch": "", "reasons": [], "evidence": ["e"], "severity": "none", "findings": []}, "has_patch": False, "changed_files": [], "test_result": "passed"}
    (output / "result.json").write_text(json.dumps(result), encoding="utf-8")
    client = FakeClient([]); monkeypatch.setattr("automation.codex_queue_runner.cli._client", lambda _: client)
    assert command_writeback(argparse.Namespace(task=str(task_path), validated_dir=str(output), target_repo=str(tmp_path), repository="owner/repo", mode="live", report=str(tmp_path / "report"))) == 0
    assert not client.dispatches and [operation[0] for operation in client.operations] == ["source_pr", "issue"] and "STATUS: WAITING_REVIEW" in client.operations[1][2]


@pytest.mark.parametrize("failure", ["source_pr", "ready"])
def test_handoff_failures_block_without_dispatch(tmp_path, monkeypatch, failure):
    task = parse_task_comment(comment(role="REQUIREMENTS_AUDIT")["body"]); task_path = tmp_path / "task.json"; output = tmp_path / "validated"; output.mkdir()
    task_path.write_text(json.dumps({"task": task_to_mapping(task)}), encoding="utf-8")
    result = {"agent_result": "pass", "result": {"role": "REQUIREMENTS_AUDIT", "result": "pass", "summary": "ok", "patch": "", "reasons": [], "evidence": ["e"], "severity": "none", "findings": []}, "has_patch": False, "changed_files": [], "test_result": "not-run"}
    (output / "result.json").write_text(json.dumps(result), encoding="utf-8")
    client = FakeClient([]); setattr(client, f"fail_{failure}", True); monkeypatch.setattr("automation.codex_queue_runner.cli._client", lambda _: client)
    assert command_writeback(argparse.Namespace(task=str(task_path), validated_dir=str(output), target_repo=str(tmp_path), repository="owner/repo", mode="live", report=str(tmp_path / "report"))) == 1
    assert not client.dispatches and any(target == 19 and "STATUS: BLOCKED" in body for target, body in client.posts) and any(target == 18 for target, _ in client.posts)


def test_repository_dispatch_selects_only_authoritative_queue_id(monkeypatch):
    monkeypatch.setenv("OPENAI_SECRET_AVAILABLE", "true")
    client = FakeClient([comment(queue_id="Q-1", number=1), comment(queue_id="Q-2", number=2)])
    config = RunnerConfig(mode="live", allow_writeback=True, workflow_run_id="run-q")
    event = {"event_name": "repository_dispatch", "client_payload": {"queue_id": "Q-2"}}
    assert select_and_claim(client, event, config, "Q-2")["task"]["queue_id"] == "Q-2"
    with pytest.raises(QueueRunError): select_and_claim(FakeClient([comment(queue_id="Q-1", number=1), comment(queue_id="Q-2", number=2)]), event, config, "Q-1")
    with pytest.raises(QueueRunError): select_and_claim(FakeClient([comment(), comment(queue_id="Q-2", number=2)]), {"event_name": "repository_dispatch", "client_payload": {"queue_id": " "}}, config)


def test_prompt_context_rejects_secrets_limits_and_rechecks_before_render(monkeypatch):
    task = parse_task_comment(comment()["body"]); secret = "ghp_" + "a" * 36
    client = FakeClient([comment()]); client.get_issue = lambda _: {"title": "issue", "body": secret}
    monkeypatch.setenv("OPENAI_SECRET_AVAILABLE", "true")
    with pytest.raises(QueueRunError) as raised: select_and_claim(client, {"event_name": "issue_comment", "issue": {"number": 19}}, RunnerConfig(mode="live", allow_writeback=True, workflow_run_id="run-secret"))
    assert secret not in str(raised.value) and [target for target, _ in client.posts] == [19, 18]
    with pytest.raises(QueueRunError): validate_prompt_context({"nested": ["x" * (MAX_PROMPT_CONTEXT_BYTES + 1)]})
    with pytest.raises(QueueRunError): render_prompt(task, {"prior_evidence": ["-----BEGIN PRIVATE KEY-----"]})
    assert "normal" in render_prompt(task, {"source_issue": {"body": "normal"}})


def test_prompt_guard_failure_writes_no_context_or_prompt_file(tmp_path, monkeypatch):
    client = FakeClient([comment()]); client.get_issue = lambda _: {"title": "issue", "body": "OPENAI_API_KEY=simulated"}
    monkeypatch.setattr("automation.codex_queue_runner.cli._client", lambda _: client); monkeypatch.setenv("GITHUB_RUN_ID", "run-prompt")
    event = tmp_path / "event.json"; event.write_text(json.dumps({"event_name": "issue_comment", "issue": {"number": 19}}), encoding="utf-8")
    output = tmp_path / "selection"
    assert command_select(argparse.Namespace(repository="owner/repo", event=str(event), output_dir=str(output), mode="live", queue_id="", allow_writeback="true", fixture="", lease_seconds=3600)) == 0
    assert not (output / "context.json").exists() and not (output / "task.json").exists()


def test_prior_evidence_is_complete_or_blocks_without_silent_truncation(tmp_path, monkeypatch):
    task = parse_task_comment(comment()["body"]); tail = "EVIDENCE-TAIL"; evidence = state("Q-1", "READY", 2, "2026-08-01T01:02:00Z")
    evidence["body"] += "\nEVIDENCE: " + "x" * 1_100 + tail
    context = build_context_snapshot(FakeClient([comment(), evidence]), task, [comment(), evidence])
    assert context["prior_evidence"][0].endswith(tail) and len(context["prior_evidence"][0]) > 1_000 and tail in render_prompt(task, context)
    oversized = state("Q-1", "READY", 2, "2026-08-01T01:02:00Z"); oversized["body"] += "\nEVIDENCE: " + "x" * MAX_PROMPT_CONTEXT_BYTES
    with pytest.raises(QueueRunError) as raised: build_context_snapshot(FakeClient([comment(), oversized]), task, [comment(), oversized])
    assert str(raised.value) == "prompt context exceeds the fixed size limit" and "x" * 100 not in str(raised.value)
    client = FakeClient([comment(), oversized]); monkeypatch.setattr("automation.codex_queue_runner.cli._client", lambda _: client); monkeypatch.setenv("GITHUB_RUN_ID", "run-oversized")
    event = tmp_path / "event.json"; event.write_text(json.dumps({"event_name": "issue_comment", "issue": {"number": 19}}), encoding="utf-8"); output = tmp_path / "selection"
    assert command_select(argparse.Namespace(repository="owner/repo", event=str(event), output_dir=str(output), mode="live", queue_id="", allow_writeback="true", fixture="", lease_seconds=3600)) == 0
    assert not (output / "context.json").exists() and not (output / "task.json").exists() and [target for target, _ in client.posts[-2:]] == [19, 18]
    assert "[:1000]" not in Path("automation/codex_queue_runner/cli.py").read_text(encoding="utf-8")


class ClaimVerificationFailureClient(FakeClient):
    def __init__(self, comments): super().__init__(comments); self.reads = 0
    def list_issue_comments(self, issue):
        self.reads += 1
        if self.reads == 3: raise QueueRunError("simulated GitHub read failure")
        return super().list_issue_comments(issue)


def test_post_claim_failure_preserves_context_and_routes_to_failure_writeback(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_SECRET_AVAILABLE", "true")
    client = ClaimVerificationFailureClient([comment()]); event = {"event_name": "issue_comment", "issue": {"number": 19}}
    result = select_and_claim(client, event, RunnerConfig(mode="live", allow_writeback=True, workflow_run_id="run-claimed"))
    assert result["selected"] and result["claimed"] and not result["selection_ok"] and result["workflow_run_id"] == "run-claimed" and result["task"] and result["context"] and result["error"] == "claim verification failed"
    monkeypatch.setattr("automation.codex_queue_runner.cli._client", lambda _: ClaimVerificationFailureClient([comment()]))
    event_path = tmp_path / "event.json"; event_path.write_text(json.dumps(event), encoding="utf-8"); output = tmp_path / "selection"
    assert command_select(argparse.Namespace(repository="owner/repo", event=str(event_path), output_dir=str(output), mode="live", queue_id="", allow_writeback="true", fixture="", lease_seconds=3600)) == 0
    stored = json.loads((output / "selection.json").read_text(encoding="utf-8"))
    assert stored["selected"] and stored["claimed"] and not stored["selection_ok"] and (output / "task.json").exists() and (output / "context.json").exists()
    workflow = Path(".github/workflows/codex-queue-runner.yml").read_text(encoding="utf-8")
    assert "outputs.claimed == 'true'" in workflow and "outputs.selection_ok == 'true'" in workflow and "claim_belongs_to_run(client.list_issue_comments(19)" in workflow and "github.event.client_payload.queue_id" in workflow
