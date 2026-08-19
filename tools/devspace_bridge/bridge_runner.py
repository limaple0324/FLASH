"""Run the Devspace bridge with a queue ref that is refreshed explicitly."""
from __future__ import annotations

import sys

import bridge as core


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
    core.QueueClient.fetch = _fixed_fetch
    print("DEVSPACE BRIDGE ONLINE", flush=True)
    print("QUEUE REFRESH FIX ACTIVE", flush=True)
    return core.main()


if __name__ == "__main__":
    raise SystemExit(main())
