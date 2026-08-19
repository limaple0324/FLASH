"""Run the Devspace bridge as one hidden Windows singleton."""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys

import bridge as core

MUTEX_NAME = r"Local\Limaple.DevspaceBridge.Runner"
ERROR_ALREADY_EXISTS = 183


def _hidden_run(
    args: list[str],
    *,
    cwd,
    timeout: float = 120.0,
    check: bool = True,
    env: dict[str, str] | None = None,
):
    creationflags = (
        int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if sys.platform == "win32"
        else 0
    )
    try:
        result = subprocess.run(
            args,
            cwd=str(cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            env=env,
            shell=False,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        raise core.BridgeError(f"命令逾時：{args[0]}") from exc
    except OSError as exc:
        raise core.BridgeError(f"無法啟動：{args[0]}") from exc
    if check and result.returncode != 0:
        output = core._clip((result.stdout or "") + "\n" + (result.stderr or ""))
        raise core.BridgeError(
            f"命令失敗({result.returncode})：{' '.join(args[:4])}\n{output}"
        )
    return result


def _fixed_fetch(self: core.QueueClient) -> None:
    branch = self.config.queue_branch
    remote = self.config.remote
    core._run(
        [
            "git",
            "fetch",
            "--quiet",
            remote,
            f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}",
        ],
        cwd=self.config.repo_root,
        timeout=60,
    )


def _detached_writer(self: core.QueueClient) -> None:
    self.fetch()
    if self.writer_root.exists():
        try:
            core._run(
                ["git", "worktree", "remove", "--force", str(self.writer_root)],
                cwd=self.config.repo_root,
                timeout=30,
            )
        except core.BridgeError:
            shutil.rmtree(self.writer_root, ignore_errors=True)
    core._run(
        ["git", "worktree", "prune"],
        cwd=self.config.repo_root,
        timeout=30,
        check=False,
    )
    core._run(
        [
            "git",
            "worktree",
            "add",
            "--detach",
            "--force",
            str(self.writer_root),
            self.config.queue_ref,
        ],
        cwd=self.config.repo_root,
        timeout=60,
    )
    core._run(
        ["git", "config", "user.name", "Devspace Bridge"],
        cwd=self.writer_root,
        timeout=10,
    )
    core._run(
        ["git", "config", "user.email", "devspace@local.invalid"],
        cwd=self.writer_root,
        timeout=10,
    )


def _acquire_mutex():
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    create_mutex.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_bool
    ctypes.set_last_error(0)
    handle = create_mutex(None, False, MUTEX_NAME)
    if not handle:
        raise OSError(ctypes.get_last_error(), "Devspace mutex failed")
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        close_handle(handle)
        return False
    return handle


def main() -> int:
    mutex = _acquire_mutex()
    if mutex is False:
        return 0
    try:
        core._run = _hidden_run
        core.QueueClient.fetch = _fixed_fetch
        core.QueueClient._prepare_writer = _detached_writer
        return core.main()
    finally:
        if os.name == "nt" and mutex not in (None, False):
            try:
                ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(mutex)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
