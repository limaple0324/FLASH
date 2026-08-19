from __future__ import annotations

import argparse
import ctypes
import os
import runpy
import subprocess
import sys
from pathlib import Path

BRIDGE_BRANCH = "work/devspace-gpt-bridge-20260819"
QUEUE_BRANCH = "automation/devspace-queue"
MUTEX_NAME = r"Local\Limaple.DevspaceBridge.Background"
ERROR_ALREADY_EXISTS = 183


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(
        args,
        cwd=str(cwd),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _acquire_mutex():
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    create_mutex.restype = ctypes.c_void_p
    ctypes.set_last_error(0)
    handle = create_mutex(None, False, MUTEX_NAME)
    if not handle:
        return None
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    return handle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve()

    mutex = _acquire_mutex()
    if mutex is False:
        return 0

    try:
        _run(["git", "fetch", "origin", BRIDGE_BRANCH], repo)
        _run(["git", "checkout", "-q", BRIDGE_BRANCH], repo)
        _run(["git", "reset", "--hard", f"origin/{BRIDGE_BRANCH}"], repo)
        _run(
            [
                "git",
                "fetch",
                "origin",
                f"+refs/heads/{QUEUE_BRANCH}:refs/remotes/origin/{QUEUE_BRANCH}",
            ],
            repo,
        )

        runner = repo / "tools" / "devspace_bridge" / "bridge_runner.py"
        sys.argv = [str(runner), "--repo", str(repo)]
        runpy.run_path(str(runner), run_name="__main__")
        return 0
    finally:
        if os.name == "nt" and mutex not in (None, False):
            try:
                ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(mutex)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
