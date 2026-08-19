from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

WAIT_TIMEOUT = 0x00000102
SYNCHRONIZE = 0x00100000


def _pythonw() -> Path:
    candidate = Path(sys.executable).with_name("pythonw.exe")
    if candidate.is_file():
        return candidate
    raise RuntimeError("找不到 pythonw.exe")


def _state_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = (Path(base) / "輔" / "Devspace") if base else (Path.home() / ".fu_devspace")
    root.mkdir(parents=True, exist_ok=True)
    return root


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
    runner = repo / "tools" / "devspace_bridge" / "bridge_runner.py"
    command = f'"{pythonw}" "{runner}" --repo "{repo}"'
    script = (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.Run "{_quote_vbs(command)}", 0, False\r\n'
    )
    return target, script


def _read_pid(path: Path) -> int | None:
    try:
        value = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    return value if value > 0 else None


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong)
    open_process.restype = ctypes.c_void_p
    wait = kernel32.WaitForSingleObject
    wait.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
    wait.restype = ctypes.c_ulong
    close = kernel32.CloseHandle
    close.argtypes = (ctypes.c_void_p,)
    close.restype = ctypes.c_bool
    handle = open_process(SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        return wait(handle, 0) == WAIT_TIMEOUT
    finally:
        close(handle)


def _stop_existing_bridge(state_root: Path) -> None:
    pid_files = (state_root / "bridge.pid", state_root / "background.pid")
    pids = tuple(
        dict.fromkeys(
            pid for path in pid_files if (pid := _read_pid(path)) is not None
        )
    )
    stop_request = state_root / "stop.request"
    if pids:
        stop_request.write_text("stop\n", encoding="ascii")
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if not any(_pid_alive(pid) for pid in pids):
                break
            time.sleep(0.1)
        alive = tuple(pid for pid in pids if _pid_alive(pid))
        if alive:
            raise RuntimeError(f"舊背景橋接未能安全停止：{alive}")
    for path in (*pid_files, stop_request):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    time.sleep(0.5)


def _launch_background(repo: Path, pythonw: Path, state_root: Path) -> int:
    runner = repo / "tools" / "devspace_bridge" / "bridge_runner.py"
    if not runner.is_file():
        raise RuntimeError(f"找不到橋接主程序：{runner}")
    creationflags = 0
    for name in ("CREATE_NO_WINDOW", "DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        creationflags |= int(getattr(subprocess, name, 0))
    process = subprocess.Popen(
        [str(pythonw), str(runner), "--repo", str(repo)],
        cwd=str(repo),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        current_pid = _read_pid(state_root / "bridge.pid")
        if current_pid is not None and _pid_alive(current_pid):
            (state_root / "background.pid").write_text(
                str(current_pid) + "\n", encoding="ascii"
            )
            return current_pid
        code = process.poll()
        if code is not None and code != 0:
            raise RuntimeError(f"背景橋接啟動失敗：{code}")
        time.sleep(0.1)
    raise RuntimeError("背景橋接沒有建立在線 PID")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve()
    pythonw = _pythonw()
    state_root = _state_root()

    target, script = _startup_vbs(repo, pythonw)
    target.write_text(script, encoding="utf-8")

    _stop_existing_bridge(state_root)
    pid = _launch_background(repo, pythonw, state_root)

    print(f"BACKGROUND_OK PID={pid}")
    print(f"STARTUP={target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
