from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import QueueRunError, Task
from .scope_guard import GitChange, parse_raw_changes, validate_git_changes
from .test_command_guard import pytest_argv


@dataclass(frozen=True)
class ValidationResult:
    patch_sha256: str
    changed_files: list[str]
    test_output: str
    has_patch: bool


def _git(repo: Path, *args: str, input_bytes: bytes | None = None, env: dict[str, str] | None = None) -> bytes:
    result = subprocess.run(["git", *args], cwd=repo, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False)
    if result.returncode: raise QueueRunError(f"Git 指令失敗: {' '.join(args)}: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout


def _clean(repo: Path) -> None:
    if _git(repo, "status", "--porcelain=v1", "-z"): raise QueueRunError("驗證工作樹不是乾淨基準")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _check(task: Task, changes: list[GitChange]) -> list[str]:
    ok, errors = validate_git_changes(changes, task.owned_files, task.forbidden)
    if not ok: raise QueueRunError("; ".join(errors))
    return [path for change in changes for path in change.paths]


def validate_patch(repo: Path, task: Task, patch_path: Path, output_dir: Path) -> ValidationResult:
    _clean(repo); _git(repo, "checkout", "--detach", task.base_commit); _clean(repo)
    patch = patch_path.read_bytes()
    if patch: _git(repo, "apply", "--index", "--binary", input_bytes=patch)
    changes = parse_raw_changes(_git(repo, "diff", "--cached", "--raw", "-z", "--no-abbrev", "-M", task.base_commit))
    files = _check(task, changes) if changes else []
    output = "未執行（只讀角色）"
    if task.role.value in {"WORKER_A", "TEST_VALIDATION"}:
        result = subprocess.run(pytest_argv(task.minimum_tests), cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
        output = result.stdout[-12000:]
        if result.returncode: raise QueueRunError(f"最小測試失敗: {output}")
    if _git(repo, "diff", "--name-only") or _git(repo, "ls-files", "--others", "--exclude-standard", "-z"):
        raise QueueRunError("測試產生未暫存變更或未追蹤檔案")
    output_dir.mkdir(parents=True, exist_ok=True)
    validated = output_dir / "validated.patch"; validated.write_bytes(_git(repo, "diff", "--cached", "--binary", task.base_commit))
    digest = sha256_file(validated)
    (output_dir / "manifest.sha256").write_text(f"{digest}  validated.patch\n", encoding="ascii")
    (output_dir / "report.txt").write_text(f"files={','.join(files) or 'NONE'}\n{output}", encoding="utf-8")
    return ValidationResult(digest, files, output, bool(changes))


def remote_head(repo: Path, branch: str) -> str:
    return _git(repo, "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}").decode("ascii", "replace").split()[0]


def create_and_push(repo: Path, task: Task, patch: Path, manifest: Path, message: str) -> tuple[str, list[str], list[str]]:
    if sha256_file(patch) != manifest.read_text(encoding="ascii").split()[0]: raise QueueRunError("已驗證產物雜湊不一致")
    _clean(repo); _git(repo, "checkout", "--detach", task.base_commit)
    content = patch.read_bytes()
    if not content: return "", [], []
    _git(repo, "apply", "--cached", "--binary", input_bytes=content)
    changes = parse_raw_changes(_git(repo, "diff", "--cached", "--raw", "-z", "--no-abbrev", "-M", task.base_commit)); files = _check(task, changes)
    tree = _git(repo, "write-tree").decode("ascii").strip(); environment = os.environ.copy()
    environment.update({"GIT_AUTHOR_NAME": "codex-queue-runner", "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com", "GIT_COMMITTER_NAME": "codex-queue-runner", "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com"})
    commit = _git(repo, "commit-tree", tree, "-p", task.base_commit, "-m", message, env=environment).decode("ascii").strip()
    if remote_head(repo, task.target_branch) != task.base_commit: raise QueueRunError("遠端 TARGET_BRANCH 已變更，拒絕推送")
    command = ["git", "push", "origin", f"{commit}:refs/heads/{task.target_branch}"]
    subprocess.run(command, cwd=repo, check=True)
    return commit, files, command
