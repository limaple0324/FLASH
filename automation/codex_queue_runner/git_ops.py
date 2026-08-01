from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .models import QueueRunError, Task
from .scope_guard import GitChange, parse_raw_changes, validate_git_changes
from .test_command_guard import pytest_argv


@dataclass(frozen=True)
class ValidationResult:
    patch_sha256: str
    changed_files: list[str]
    test_output: str
    has_patch: bool


def _git(repo: Path, *args: str, input_bytes: bytes | None = None, env: dict | None = None) -> bytes:
    result = subprocess.run(["git", *args], cwd=repo, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False)
    if result.returncode:
        raise QueueRunError(f"git failed: {' '.join(args)}: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _check(task: Task, changes: list[GitChange]) -> list[str]:
    ok, errors = validate_git_changes(changes, task.owned_files, task.forbidden)
    if not ok:
        raise QueueRunError("; ".join(errors))
    return [path for change in changes for path in change.paths]


def _clean(repo: Path) -> None:
    if _git(repo, "status", "--porcelain=v1", "-z"):
        raise QueueRunError("validation repository is not clean")


def _extract(archive: bytes, directory: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        for member in tar.getmembers():
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
                raise QueueRunError("unsafe path in git archive")
        tar.extractall(directory)


def _sandbox(workspace: Path, argv: list[str]) -> list[str]:
    bwrap = shutil.which("bwrap")
    if os.name == "nt" or not bwrap:
        raise QueueRunError("BLOCKED: filesystem and network isolation is unavailable")
    return [bwrap, "--die-with-parent", "--unshare-all", "--new-session", "--ro-bind", "/", "/", "--dir", "/work", "--bind", str(workspace), "/work", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--chdir", "/work", "--", *argv]


def _test(workspace: Path, argv: list[str], runner: Callable[[Path, list[str]], tuple[int, str]] | None) -> str:
    if runner:
        code, output = runner(workspace, argv)
    else:
        environment = {"PATH": os.environ.get("PATH", ""), "HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1", "http_proxy": "", "https_proxy": "", "HTTP_PROXY": "", "HTTPS_PROXY": ""}
        result = subprocess.run(_sandbox(workspace, argv), cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=environment, check=False)
        code, output = result.returncode, result.stdout
    if code:
        raise QueueRunError(f"candidate tests failed or sandbox rejected execution: {output[-12000:]}")
    return output[-12000:]


def validate_patch(repo: Path, task: Task, patch: bytes, output_dir: Path, *, run_tests: bool = True, test_runner: Callable[[Path, list[str]], tuple[int, str]] | None = None) -> ValidationResult:
    _clean(repo)
    _git(repo, "checkout", "--detach", task.base_commit)
    _clean(repo)
    if patch:
        _git(repo, "apply", "--index", "--binary", input_bytes=patch)
    raw = _git(repo, "diff", "--cached", "--raw", "-z", "--no-abbrev", "-M")
    changes = parse_raw_changes(raw)
    files = _check(task, changes) if changes else []
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact = output_dir / "validated.patch"
    artifact.write_bytes(_git(repo, "diff", "--cached", "--binary", task.base_commit))
    digest = sha256_file(artifact)
    (output_dir / "manifest.sha256").write_text(f"{digest}  validated.patch\n", encoding="ascii")
    test_output = "not-run"
    if run_tests and task.role.value in {"WORKER_A", "TEST_VALIDATION"}:
        with tempfile.TemporaryDirectory(prefix="codex-queue-test-") as temporary:
            workspace = Path(temporary) / "target"
            workspace.mkdir()
            _extract(_git(repo, "archive", "--format=tar", task.base_commit), workspace)
            if (workspace / ".git").exists():
                raise QueueRunError("disposable test workspace contains .git")
            if patch:
                result = subprocess.run(["git", "apply", "--binary"], cwd=workspace, input=artifact.read_bytes(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
                if result.returncode:
                    raise QueueRunError("cannot apply patch in disposable workspace")
            test_output = _test(workspace, pytest_argv(task.minimum_tests), test_runner)
    if _git(repo, "diff", "--cached", "--raw", "-z", "--no-abbrev", "-M") != raw or _git(repo, "diff", "--name-only") or _git(repo, "ls-files", "--others", "--exclude-standard", "-z"):
        raise QueueRunError("candidate tests changed validated repository state")
    if sha256_file(artifact) != digest:
        raise QueueRunError("candidate tests changed validated.patch")
    _check(task, parse_raw_changes(raw)) if raw else None
    (output_dir / "report.txt").write_text(f"files={','.join(files) or 'NONE'}\n{test_output}", encoding="utf-8")
    return ValidationResult(digest, files, test_output, bool(changes))


def remote_head(repo: Path, branch: str) -> str:
    return _git(repo, "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}").decode().split()[0]


def commit_message(task: Task) -> str:
    return f"[Codex Queue Runner] {task.queue_id} parent {task.base_commit}"


def find_reconciled_commit(repo: Path, task: Task) -> str | None:
    head = remote_head(repo, task.target_branch)
    message = _git(repo, "show", "-s", "--format=%s", head).decode().strip()
    parents = _git(repo, "show", "-s", "--format=%P", head).decode().split()
    return head if message == commit_message(task) and parents == [task.base_commit] else None


def create_and_push(repo: Path, task: Task, validated_patch: Path, manifest: Path) -> tuple[str, list[str], list[str]]:
    if sha256_file(validated_patch) != manifest.read_text(encoding="ascii").split()[0]:
        raise QueueRunError("validated patch hash mismatch")
    existing = find_reconciled_commit(repo, task)
    if existing:
        return existing, [], []
    _clean(repo)
    _git(repo, "checkout", "--detach", task.base_commit)
    patch = validated_patch.read_bytes()
    if not patch:
        return "", [], []
    _git(repo, "apply", "--cached", "--binary", input_bytes=patch)
    files = _check(task, parse_raw_changes(_git(repo, "diff", "--cached", "--raw", "-z", "--no-abbrev", "-M")))
    environment = os.environ.copy()
    environment.update({"GIT_AUTHOR_NAME": "codex-queue-runner", "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com", "GIT_COMMITTER_NAME": "codex-queue-runner", "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com"})
    commit = _git(repo, "commit-tree", _git(repo, "write-tree").decode().strip(), "-p", task.base_commit, "-m", commit_message(task), env=environment).decode().strip()
    if remote_head(repo, task.target_branch) != task.base_commit:
        raise QueueRunError("target branch changed before push")
    command = ["git", "push", "origin", f"{commit}:refs/heads/{task.target_branch}"]
    result = subprocess.run(command, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
    if result.returncode:
        raise QueueRunError(f"ordinary push failed: {result.stdout[-12000:]}")
    return commit, files, command
