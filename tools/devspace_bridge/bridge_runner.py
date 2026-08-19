"""Run the Devspace bridge with explicit queue refresh and hidden Windows children."""
from __future__ import annotations

import subprocess
import sys

import bridge as core


def _hidden_run(
    args: list[str],
    *,
    cwd,
    timeout: float = 120.0,
    check: bool = True,
    env: dict[str, str] | None = None,
):
    creationflags = 0
    if sys.platform == "win32":
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
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


def main() -> int:
    core._run = _hidden_run
    core.QueueClient.fetch = _fixed_fetch
    print("DEVSPACE BRIDGE ONLINE", flush=True)
    print("QUEUE REFRESH FIX ACTIVE", flush=True)
    print("NO WINDOW CHILD PROCESS ACTIVE", flush=True)
    return core.main()


if __name__ == "__main__":
    raise SystemExit(main())
