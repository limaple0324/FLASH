from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


def _pythonw() -> Path:
    candidate = Path(sys.executable).with_name("pythonw.exe")
    if candidate.is_file():
        return candidate
    raise RuntimeError("找不到 pythonw.exe")


def _quote_vbs(value: str) -> str:
    return value.replace('"', '""')


def _startup_vbs(repo: Path, pythonw: Path) -> tuple[Path, str]:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("找不到 APPDATA")
    startup = (
        Path(appdata)
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
    )
    startup.mkdir(parents=True, exist_ok=True)
    target = startup / "DevspaceBridge.vbs"
    bootstrap = repo / "tools" / "devspace_bridge" / "background_bootstrap.py"
    command = f'"{pythonw}" "{bootstrap}" --repo "{repo}"'
    script = (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.Run "{_quote_vbs(command)}", 0, False\r\n'
    )
    return target, script


def _launch_background(repo: Path, pythonw: Path) -> int:
    bootstrap = repo / "tools" / "devspace_bridge" / "background_bootstrap.py"
    if not bootstrap.is_file():
        raise RuntimeError(f"找不到背景橋接入口：{bootstrap}")
    creationflags = 0
    for name in ("CREATE_NO_WINDOW", "DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        creationflags |= int(getattr(subprocess, name, 0))
    process = subprocess.Popen(
        [str(pythonw), str(bootstrap), "--repo", str(repo)],
        cwd=str(repo),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )
    time.sleep(1.5)
    if process.poll() is not None:
        raise RuntimeError(f"背景橋接啟動後立即退出：{process.returncode}")
    state_root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "輔" / "Devspace"
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "background.pid").write_text(str(process.pid) + "\n", encoding="ascii")
    return int(process.pid)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve()
    pythonw = _pythonw()
    target, script = _startup_vbs(repo, pythonw)
    target.write_text(script, encoding="utf-8")
    pid = _launch_background(repo, pythonw)
    print(f"BACKGROUND_OK PID={pid}")
    print(f"STARTUP={target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
