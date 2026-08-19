"""Devspace local bridge for GPT-controlled, GitHub-mediated Windows tasks.

The bridge intentionally uses an allowlist and isolated Git worktrees. It does
not execute arbitrary shell, terminate processes, or send game input.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_QUEUE_BRANCH = "automation/devspace-queue"
DEFAULT_POLL_SECONDS = 3.0
MAX_TEXT_BYTES = 512 * 1024
MAX_OUTPUT_CHARS = 200_000
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{3,96}$")
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")
SAFE_TEST_RE = re.compile(r"^[A-Za-z0-9_./\\:-]+$")
ALLOWED_ACTIONS = frozenset({
    "ping", "repo_snapshot", "read_text", "run_tests", "build_candidate",
    "installed_fu_hash", "installed_fu_self_check", "process_snapshot",
})
OBSERVABLE_PROCESS_NAMES = frozenset({
    "FLASH.exe", "flashplayer_11_sa.exe", "flashplayer.exe", "輔V0.2.exe",
})

class BridgeError(RuntimeError):
    pass

@dataclass(frozen=True, slots=True)
class BridgeConfig:
    repo_root: Path
    queue_branch: str
    state_root: Path
    remote: str = "origin"
    poll_seconds: float = DEFAULT_POLL_SECONDS

    @property
    def queue_ref(self) -> str:
        return f"{self.remote}/{self.queue_branch}"

    @property
    def inbox_prefix(self) -> str:
        return "tools/devspace_bridge/queue/inbox/"

    @property
    def result_prefix(self) -> str:
        return "tools/devspace_bridge/queue/results/"

    @property
    def installed_fu(self) -> Path:
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        return base / "Programs" / "輔" / "完整累積版" / "FLASH.exe"

def _clip(value: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n...（輸出截斷，原長度 {len(value)}）"

def _run(args: list[str], *, cwd: Path, timeout: float = 120.0,
         check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args, cwd=str(cwd), text=True, encoding="utf-8", errors="replace",
            capture_output=True, timeout=timeout, env=env, shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BridgeError(f"命令逾時：{args[0]}") from exc
    except OSError as exc:
        raise BridgeError(f"無法啟動：{args[0]}") from exc
    if check and result.returncode != 0:
        output = _clip((result.stdout or "") + "\n" + (result.stderr or ""))
        raise BridgeError(f"命令失敗({result.returncode})：{' '.join(args[:4])}\n{output}")
    return result

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest().upper()

def _repo_root(start: Path) -> Path:
    return Path(_run(["git", "rev-parse", "--show-toplevel"], cwd=start, timeout=15).stdout.strip()).resolve()

def _default_state_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) / "輔" / "Devspace") if base else (Path.home() / ".fu_devspace")

def _load_config(repo_root: Path, queue_branch: str | None, poll_seconds: float | None) -> BridgeConfig:
    state_root = _default_state_root()
    state_root.mkdir(parents=True, exist_ok=True)
    return BridgeConfig(
        repo_root=repo_root,
        queue_branch=queue_branch or os.environ.get("DEVSPACE_QUEUE_BRANCH") or DEFAULT_QUEUE_BRANCH,
        state_root=state_root,
        poll_seconds=float(poll_seconds if poll_seconds is not None else os.environ.get("DEVSPACE_POLL_SECONDS", DEFAULT_POLL_SECONDS)),
    )

class QueueClient:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.writer_root = config.state_root / "queue-writer"
        self.writer_branch = "devspace-local-writer"

    def fetch(self) -> None:
        _run(["git", "fetch", "--quiet", self.config.remote, self.config.queue_branch], cwd=self.config.repo_root, timeout=60)

    def _list(self, prefix: str) -> tuple[str, ...]:
        result = _run(["git", "ls-tree", "-r", "--name-only", self.config.queue_ref, prefix], cwd=self.config.repo_root, timeout=30)
        return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())

    def inbox_paths(self) -> tuple[str, ...]:
        return tuple(path for path in self._list(self.config.inbox_prefix) if path.endswith(".json"))

    def result_paths(self) -> frozenset[str]:
        return frozenset(path for path in self._list(self.config.result_prefix) if path.endswith(".json"))

    def read_json(self, path: str) -> dict[str, Any]:
        result = _run(["git", "show", f"{self.config.queue_ref}:{path}"], cwd=self.config.repo_root, timeout=30)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise BridgeError(f"任務 JSON 無法解析：{path}") from exc
        if not isinstance(payload, dict):
            raise BridgeError("任務必須是 JSON 物件")
        return payload

    def _prepare_writer(self) -> None:
        self.fetch()
        if self.writer_root.exists():
            try:
                _run(["git", "worktree", "remove", "--force", str(self.writer_root)], cwd=self.config.repo_root, timeout=30)
            except BridgeError:
                shutil.rmtree(self.writer_root, ignore_errors=True)
        _run(["git", "worktree", "add", "--force", "-B", self.writer_branch, str(self.writer_root), self.config.queue_ref], cwd=self.config.repo_root, timeout=60)
        _run(["git", "config", "user.name", "Devspace Bridge"], cwd=self.writer_root, timeout=10)
        _run(["git", "config", "user.email", "devspace@local.invalid"], cwd=self.writer_root, timeout=10)

    def publish_result(self, task_id: str, result_payload: dict[str, Any]) -> None:
        target_rel = Path("tools/devspace_bridge/queue/results") / f"{task_id}.json"
        for attempt in range(1, 4):
            self._prepare_writer()
            target = self.writer_root / target_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(result_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            _run(["git", "add", str(target_rel)], cwd=self.writer_root, timeout=15)
            status = _run(["git", "status", "--porcelain"], cwd=self.writer_root, timeout=15)
            if not status.stdout.strip():
                return
            _run(["git", "commit", "-m", f"Devspace result {task_id}"], cwd=self.writer_root, timeout=30)
            pushed = _run(["git", "push", self.config.remote, f"HEAD:{self.config.queue_branch}"], cwd=self.writer_root, timeout=60, check=False)
            if pushed.returncode == 0:
                return
            if attempt == 3:
                raise BridgeError("結果推送失敗，已重試 3 次")
            time.sleep(0.5)

class TaskExecutor:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.runs_root = config.state_root / "runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def validate(task: dict[str, Any]) -> tuple[str, str]:
        if task.get("schema_version") != SCHEMA_VERSION:
            raise BridgeError("schema_version 不支援")
        task_id = task.get("task_id")
        action = task.get("action")
        if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
            raise BridgeError("task_id 無效")
        if action not in ALLOWED_ACTIONS:
            raise BridgeError(f"action 未允許：{action!r}")
        return task_id, str(action)

    def _exact_commit(self, task: dict[str, Any]) -> str:
        commit = task.get("target_commit")
        if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
            raise BridgeError("此動作必須指定完整 40 碼 target_commit")
        _run(["git", "fetch", "--quiet", self.config.remote, commit], cwd=self.config.repo_root, timeout=60, check=False)
        verify = _run(["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=self.config.repo_root, timeout=15, check=False)
        if verify.returncode != 0:
            raise BridgeError("target_commit 不存在於本機 Git 物件庫")
        return commit.lower()

    def _worktree_for(self, task_id: str, commit: str) -> Path:
        run_root = self.runs_root / task_id
        worktree = run_root / "repo"
        if worktree.exists():
            try:
                _run(["git", "worktree", "remove", "--force", str(worktree)], cwd=self.config.repo_root, timeout=30)
            except BridgeError:
                shutil.rmtree(worktree, ignore_errors=True)
        run_root.mkdir(parents=True, exist_ok=True)
        _run(["git", "worktree", "add", "--detach", "--force", str(worktree), commit], cwd=self.config.repo_root, timeout=90)
        return worktree

    def execute(self, task: dict[str, Any]) -> dict[str, Any]:
        task_id, action = self.validate(task)
        started = time.time()
        base = {"schema_version": SCHEMA_VERSION, "task_id": task_id, "action": action, "bridge_pid": os.getpid(), "started_at_unix": started}
        try:
            payload = getattr(self, f"_action_{action}")(task, task_id)
            return {**base, "ok": True, "finished_at_unix": time.time(), "result": payload}
        except Exception as exc:
            return {**base, "ok": False, "finished_at_unix": time.time(), "error": str(exc), "error_type": type(exc).__name__, "traceback": _clip(traceback.format_exc(), 40_000)}

    def _action_ping(self, task: dict[str, Any], task_id: str) -> dict[str, Any]:
        return {"message": "pong", "platform": sys.platform, "python": sys.version.split()[0], "repo_root": str(self.config.repo_root), "installed_fu": str(self.config.installed_fu)}

    def _action_repo_snapshot(self, task: dict[str, Any], task_id: str) -> dict[str, Any]:
        commit = self._exact_commit(task)
        worktree = self._worktree_for(task_id, commit)
        head = _run(["git", "rev-parse", "HEAD"], cwd=worktree, timeout=15).stdout.strip()
        status = _run(["git", "status", "--porcelain=v1"], cwd=worktree, timeout=15).stdout
        return {"commit": head, "clean": not bool(status.strip()), "status": _clip(status, 20_000), "worktree": str(worktree)}

    @staticmethod
    def _safe_relative(raw: Any) -> Path:
        if not isinstance(raw, str) or not raw.strip():
            raise BridgeError("path 必須是非空白相對路徑")
        relative = Path(raw)
        if relative.is_absolute() or ".." in relative.parts:
            raise BridgeError("path 不得離開隔離工作樹")
        return relative

    def _action_read_text(self, task: dict[str, Any], task_id: str) -> dict[str, Any]:
        commit = self._exact_commit(task)
        worktree = self._worktree_for(task_id, commit)
        relative = self._safe_relative((task.get("args") or {}).get("path"))
        path = (worktree / relative).resolve()
        if not path.is_relative_to(worktree.resolve()):
            raise BridgeError("path 不得離開隔離工作樹")
        if not path.is_file():
            raise BridgeError("指定檔案不存在")
        if path.stat().st_size > MAX_TEXT_BYTES:
            raise BridgeError("檔案超過唯讀大小限制")
        return {"path": str(relative), "sha256": _sha256(path), "content": path.read_text(encoding="utf-8", errors="replace")}

    def _action_run_tests(self, task: dict[str, Any], task_id: str) -> dict[str, Any]:
        commit = self._exact_commit(task)
        args = task.get("args") or {}
        tests = args.get("tests")
        if not isinstance(tests, list) or not tests or len(tests) > 40:
            raise BridgeError("tests 必須是 1～40 個測試路徑")
        normalized: list[str] = []
        for item in tests:
            if not isinstance(item, str) or not SAFE_TEST_RE.fullmatch(item):
                raise BridgeError(f"不允許的測試路徑：{item!r}")
            if item.startswith(("/", "\\")) or ".." in Path(item).parts:
                raise BridgeError("測試路徑不得離開隔離工作樹")
            normalized.append(item)
        timeout = float(args.get("timeout_seconds", 900))
        if not 10 <= timeout <= 3600:
            raise BridgeError("timeout_seconds 必須介於 10～3600")
        worktree = self._worktree_for(task_id, commit)
        result = _run([sys.executable, "-m", "pytest", "-q", *normalized], cwd=worktree, timeout=timeout, check=False)
        return {"commit": commit, "returncode": result.returncode, "passed": result.returncode == 0, "stdout": _clip(result.stdout or ""), "stderr": _clip(result.stderr or "")}

    def _action_build_candidate(self, task: dict[str, Any], task_id: str) -> dict[str, Any]:
        commit = self._exact_commit(task)
        args = task.get("args") or {}
        timeout = float(args.get("timeout_seconds", 1800))
        if not 60 <= timeout <= 3600:
            raise BridgeError("timeout_seconds 必須介於 60～3600")
        worktree = self._worktree_for(task_id, commit)
        output_dir = self.runs_root / task_id / "candidate"
        cache_dir = self.config.state_root / "build-cache"
        lock_file = self.config.state_root / "build.lock"
        output_dir.mkdir(parents=True, exist_ok=True)
        result = _run([
            sys.executable, str(worktree / "scripts" / "build_coordinator.py"),
            "--root", str(worktree), "--output-dir", str(output_dir),
            "--cache-dir", str(cache_dir), "--lock-file", str(lock_file),
            "--timeout-seconds", str(timeout),
        ], cwd=worktree, timeout=timeout + 60, check=False)
        exe = output_dir / "FLASH.exe"
        return {"commit": commit, "returncode": result.returncode, "built": result.returncode == 0 and exe.is_file(), "candidate_path": str(exe) if exe.is_file() else None, "sha256": _sha256(exe) if exe.is_file() else None, "stdout": _clip(result.stdout or ""), "stderr": _clip(result.stderr or "")}

    def _action_installed_fu_hash(self, task: dict[str, Any], task_id: str) -> dict[str, Any]:
        path = self.config.installed_fu
        if not path.is_file():
            raise BridgeError(f"正式版不存在：{path}")
        return {"path": str(path), "sha256": _sha256(path), "size": path.stat().st_size}

    def _action_installed_fu_self_check(self, task: dict[str, Any], task_id: str) -> dict[str, Any]:
        path = self.config.installed_fu
        if not path.is_file():
            raise BridgeError(f"正式版不存在：{path}")
        timeout = float((task.get("args") or {}).get("timeout_seconds", 120))
        if not 10 <= timeout <= 600:
            raise BridgeError("timeout_seconds 必須介於 10～600")
        result = _run([str(path), "--self-check"], cwd=path.parent, timeout=timeout, check=False)
        return {"path": str(path), "sha256": _sha256(path), "returncode": result.returncode, "passed": result.returncode == 0, "stdout": _clip(result.stdout or ""), "stderr": _clip(result.stderr or "")}

    def _action_process_snapshot(self, task: dict[str, Any], task_id: str) -> dict[str, Any]:
        if os.name != "nt":
            return {"supported": False, "processes": []}
        result = _run(["tasklist", "/FO", "CSV", "/NH"], cwd=self.config.repo_root, timeout=30, check=False)
        allowed = {name.casefold() for name in OBSERVABLE_PROCESS_NAMES}
        processes: list[dict[str, Any]] = []
        for row in csv.reader(result.stdout.splitlines()):
            if len(row) < 2 or row[0].casefold() not in allowed:
                continue
            try:
                pid = int(row[1])
            except ValueError:
                continue
            processes.append({"image": row[0], "pid": pid})
        return {"supported": True, "processes": processes}

class Bridge:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.queue = QueueClient(config)
        self.executor = TaskExecutor(config)
        self.stop_request = config.state_root / "stop.request"
        self.pid_file = config.state_root / "bridge.pid"

    def run_once(self) -> int:
        self.queue.fetch()
        results = self.queue.result_paths()
        pending: list[str] = []
        for path in self.queue.inbox_paths():
            task_id = Path(path).stem
            if f"{self.config.result_prefix}{task_id}.json" not in results:
                pending.append(path)
        for path in pending:
            try:
                task = self.queue.read_json(path)
                task_id = str(task.get("task_id") or Path(path).stem)
                result = self.executor.execute(task)
            except Exception as exc:
                task_id = Path(path).stem
                result = {"schema_version": SCHEMA_VERSION, "task_id": task_id, "ok": False, "error": str(exc), "error_type": type(exc).__name__, "finished_at_unix": time.time()}
            self.queue.publish_result(task_id, result)
        return len(pending)

    def serve(self) -> int:
        self.config.state_root.mkdir(parents=True, exist_ok=True)
        self.stop_request.unlink(missing_ok=True)
        self.pid_file.write_text(str(os.getpid()) + "\n", encoding="ascii")
        try:
            while not self.stop_request.exists():
                try:
                    self.run_once()
                except KeyboardInterrupt:
                    return 0
                except Exception:
                    log = self.config.state_root / "bridge-error.log"
                    log.parent.mkdir(parents=True, exist_ok=True)
                    log.write_text(traceback.format_exc(), encoding="utf-8")
                deadline = time.monotonic() + self.config.poll_seconds
                while time.monotonic() < deadline:
                    if self.stop_request.exists():
                        return 0
                    time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
            return 0
        finally:
            self.pid_file.unlink(missing_ok=True)
            self.stop_request.unlink(missing_ok=True)

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--queue-branch")
    parser.add_argument("--poll-seconds", type=float)
    parser.add_argument("--once", action="store_true")
    return parser

def main() -> int:
    args = _parser().parse_args()
    root = _repo_root(args.repo.resolve())
    config = _load_config(root, args.queue_branch, args.poll_seconds)
    bridge = Bridge(config)
    if args.once:
        bridge.run_once()
        return 0
    return bridge.serve()

if __name__ == "__main__":
    raise SystemExit(main())
