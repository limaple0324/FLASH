# -*- coding: utf-8 -*-
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import re
import os
from pathlib import Path
import subprocess
import time
from typing import Iterable

from session_identity import process_identity as _session_process_identity

try:
    import winreg
except Exception:  # pragma: no cover - Windows only at runtime
    winreg = None

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
APPCOMPAT_LAYERS = r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
USER_DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "SmartReconnect"
USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_PATH = USER_DATA_DIR / "dpi_appcompat_backup.json"
POLICY_STATE_PATH = USER_DATA_DIR / "dpi_unified_policy.json"

DPI_TOKENS = {
    "HIGHDPIAWARE",
    "DPIUNAWARE",
    "GDIDPISCALING",
    "PERPROCESSSYSTEMDPIFORCEOFF",
    "PERPROCESSSYSTEMDPIFORCEON",
}
PRIVILEGE_TOKENS = {"RUNASADMIN", "RUNASINVOKER", "ELEVATECREATEPROCESS"}
COMPAT_TOKENS = {"WIN95", "WIN98", "WINXPSP2", "WINXPSP3", "VISTARTM", "VISTASP1", "VISTASP2", "WIN7RTM", "WIN8RTM"}


def _norm_path(path: str | os.PathLike) -> str:
    try:
        return str(Path(os.path.expandvars(str(path))).expanduser().resolve())
    except Exception:
        return os.path.abspath(os.path.expandvars(str(path)))


def _read_json(path: Path) -> dict:
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _write_json_atomic(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _merge_high_dpi_layer(existing: str) -> str:
    """Only replace DPI-related compatibility flags. Preserve unrelated flags."""
    raw = str(existing or "").strip()
    tokens = [x for x in raw.split() if x and x != "~"]
    kept = [x for x in tokens if x.upper() not in DPI_TOKENS]

    privilege = [x for x in kept if x.upper() in PRIVILEGE_TOKENS]
    compat = [x for x in kept if x.upper() in COMPAT_TOKENS]
    settings = [x for x in kept if x.upper() not in PRIVILEGE_TOKENS and x.upper() not in COMPAT_TOKENS]

    out = ["~"]
    if privilege:
        out.append(privilege[0])
    # DPI override is a Settings item: force Application / no bitmap DPI virtualization.
    out.append("HIGHDPIAWARE")
    out.extend(settings)
    if compat:
        out.append(compat[-1])
    return " ".join(out)


def _backup_original(exe_path: str, had_value: bool, value: str) -> None:
    data = _read_json(BACKUP_PATH)
    entries = data.get("entries") if isinstance(data.get("entries"), dict) else {}
    key = exe_path.lower()
    if key not in entries:
        entries[key] = {
            "exe_path": exe_path,
            "had_value": bool(had_value),
            "value": str(value or ""),
            "backed_up_at": time.time(),
        }
        data = {"version": 1, "entries": entries}
        _write_json_atomic(BACKUP_PATH, data)


def appcompat_layer(exe_path: str) -> str:
    if winreg is None:
        return ""
    exe = _norm_path(exe_path)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPCOMPAT_LAYERS, 0, winreg.KEY_READ) as key:
            value, _kind = winreg.QueryValueEx(key, exe)
            return str(value or "")
    except FileNotFoundError:
        return ""
    except Exception:
        return ""


def apply_high_dpi_aware(exe_path: str) -> dict:
    """Apply per-user High DPI scaling override: Application to one executable."""
    exe = _norm_path(exe_path)
    result = {"exe_path": exe, "ok": False, "changed": False, "old": "", "new": "", "reason": ""}
    if winreg is None:
        result["reason"] = "非 Windows 執行環境"
        return result
    if not exe.lower().endswith(".exe") or not Path(exe).is_file():
        result["reason"] = "不是可用的 EXE 路徑"
        return result
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, APPCOMPAT_LAYERS, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            had = True
            try:
                old, _kind = winreg.QueryValueEx(key, exe)
                old = str(old or "")
            except FileNotFoundError:
                had = False
                old = ""
            new = _merge_high_dpi_layer(old)
            _backup_original(exe, had, old)
            if old.strip() != new.strip():
                winreg.SetValueEx(key, exe, 0, winreg.REG_SZ, new)
                result["changed"] = True
            result.update({"ok": True, "old": old, "new": new})
    except Exception as e:
        result["reason"] = str(e)
    return result


def restore_backups() -> list[dict]:
    out = []
    if winreg is None:
        return out
    data = _read_json(BACKUP_PATH)
    entries = data.get("entries") if isinstance(data.get("entries"), dict) else {}
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, APPCOMPAT_LAYERS, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            for item in entries.values():
                if not isinstance(item, dict):
                    continue
                exe = str(item.get("exe_path", "") or "")
                try:
                    if item.get("had_value"):
                        winreg.SetValueEx(key, exe, 0, winreg.REG_SZ, str(item.get("value", "") or ""))
                    else:
                        try:
                            winreg.DeleteValue(key, exe)
                        except FileNotFoundError:
                            pass
                    out.append({"exe_path": exe, "ok": True})
                except Exception as e:
                    out.append({"exe_path": exe, "ok": False, "reason": str(e)})
    except Exception as e:
        out.append({"exe_path": "", "ok": False, "reason": str(e)})
    return out




def _normalize_command_line(text: str) -> str:
    raw = os.path.expandvars(str(text or '')).strip().lower()
    raw = raw.replace('\\', '/')
    raw = raw.replace('"', '')
    raw = re.sub(r'\s+', ' ', raw)
    return raw.strip()


def get_process_command_line(pid: int) -> str:
    """Read one process command line without changing that process.

    This is used only when binding/re-associating a Flash window, not per frame.
    """
    if os.name != 'nt' or not int(pid or 0):
        return ''
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    script = (
        f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}';"
        "if($p){[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;[Console]::Write($p.CommandLine)}"
    )
    try:
        cp = subprocess.run(
            ['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-Command',script],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5.0,
            creationflags=flags,
        )
        return str(cp.stdout or '').strip()
    except Exception:
        return ''


def process_identities_for_pids(pids: Iterable[int]) -> dict[int, dict]:
    """Batch-read process identities in one PowerShell/CIM call.

    Auto-rebind may have many Flash windows; never spawn one PowerShell per window.
    """
    ids = sorted({int(x) for x in pids if int(x or 0) > 0})
    if os.name != 'nt' or not ids:
        return {}
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    id_text = ','.join(str(x) for x in ids)
    script = (
        f"$ids=@({id_text});"
        "$r=Get-CimInstance Win32_Process | Where-Object {$ids -contains [int]$_.ProcessId} | "
        "Select-Object ProcessId,ExecutablePath,CommandLine;"
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "if($r){$r|ConvertTo-Json -Compress}"
    )
    try:
        cp = subprocess.run(
            ['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-Command',script],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=8.0, creationflags=flags,
        )
        text = str(cp.stdout or '').strip()
        if not text:
            return {}
        obj = json.loads(text)
        rows = obj if isinstance(obj, list) else [obj]
        out = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                pid = int(row.get('ProcessId', 0) or 0)
            except Exception:
                continue
            if pid <= 0:
                continue
            exe = str(row.get('ExecutablePath', '') or '')
            if not exe:
                exe = get_process_exe_path(pid)
            cmd = str(row.get('CommandLine', '') or '')
            out[pid] = {
                'process_exe': _norm_path(exe) if exe else '',
                'process_identity': process_identity(exe, cmd),
            }
        return out
    except Exception:
        return {}


def window_process_identities(hwnds: Iterable[int]) -> dict[int, dict]:
    hwnd_to_pid = {}
    for hwnd in hwnds:
        try:
            v = wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(int(hwnd)), ctypes.byref(v))
            if int(v.value):
                hwnd_to_pid[int(hwnd)] = int(v.value)
        except Exception:
            pass
    by_pid = process_identities_for_pids(hwnd_to_pid.values())
    return {hwnd: dict(by_pid.get(pid, {})) for hwnd, pid in hwnd_to_pid.items() if by_pid.get(pid)}


def process_identity(exe_path: str, command_line: str) -> str:
    exe = _norm_path(exe_path).lower() if exe_path else ''
    cmd = _normalize_command_line(command_line)
    return _session_process_identity(exe, cmd) if exe and cmd else ''


def window_process_identity(hwnd: int) -> dict:
    exe = get_window_process_exe(int(hwnd))
    pid = 0
    try:
        v = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(int(hwnd)), ctypes.byref(v))
        pid = int(v.value)
    except Exception:
        pid = 0
    cmd = get_process_command_line(pid)
    return {
        'process_exe': exe,
        'process_identity': process_identity(exe, cmd),
    }


def _has_dpi_override(layer: str) -> bool:
    toks = {x.upper() for x in str(layer or '').replace('~',' ').split() if x}
    return bool(toks.intersection(DPI_TOKENS))


def ensure_high_dpi_aware_if_unset(exe_path: str) -> dict:
    """Prepare future launches without overwriting an existing user DPI override.

    If the user already chose any Windows DPI compatibility mode, preserve it.
    If no DPI override exists, add HIGHDPIAWARE once. Existing windows are never
    restarted here; the setting naturally applies the next time that executable starts.
    """
    exe = _norm_path(exe_path)
    old = appcompat_layer(exe)
    if _has_dpi_override(old):
        return {
            'exe_path': exe, 'ok': True, 'changed': False, 'old': old, 'new': old,
            'preserved_user_override': True, 'reason': '保留既有 DPI 相容設定',
        }
    out = apply_high_dpi_aware(exe)
    out['preserved_user_override'] = False
    return out

def get_process_exe_path(pid: int) -> str:
    if os.name != "nt" or not int(pid or 0):
        return ""
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return _norm_path(buf.value[: size.value])
    finally:
        k32.CloseHandle(handle)


def get_window_process_exe(hwnd: int) -> str:
    if os.name != "nt":
        return ""
    try:
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(int(hwnd)), ctypes.byref(pid))
        return get_process_exe_path(int(pid.value))
    except Exception:
        return ""


def get_window_dpi(hwnd: int) -> int:
    if os.name != "nt":
        return 96
    try:
        fn = ctypes.windll.user32.GetDpiForWindow
        fn.argtypes = [wintypes.HWND]
        fn.restype = wintypes.UINT
        dpi = int(fn(wintypes.HWND(int(hwnd))))
        return dpi if dpi >= 48 else 96
    except Exception:
        return 96


def get_monitor_dpi(hwnd: int) -> int:
    if os.name != "nt":
        return 96
    try:
        user32 = ctypes.windll.user32
        mon = user32.MonitorFromWindow(wintypes.HWND(int(hwnd)), 2)
        if mon:
            x = wintypes.UINT(); y = wintypes.UINT()
            fn = ctypes.windll.shcore.GetDpiForMonitor
            fn.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)]
            fn.restype = ctypes.c_long
            if int(fn(mon, 0, ctypes.byref(x), ctypes.byref(y))) == 0 and int(x.value) >= 48:
                return int(x.value)
    except Exception:
        pass
    return get_window_dpi(hwnd)


def window_needs_restart_for_dpi(hwnd: int) -> bool:
    return int(get_monitor_dpi(hwnd)) != int(get_window_dpi(hwnd))


def resolve_shortcut_target(shortcut_path: str) -> str:
    """Resolve .lnk TargetPath without changing the shortcut. Returns executable target when available."""
    p = Path(os.path.expandvars(str(shortcut_path or ""))).expanduser()
    if not p.exists():
        return ""
    if p.suffix.lower() == ".exe":
        return _norm_path(p)
    if p.suffix.lower() != ".lnk" or os.name != "nt":
        return ""
    escaped = str(p).replace("'", "''")
    script = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "$w=New-Object -ComObject WScript.Shell;"
        f"$s=$w.CreateShortcut('{escaped}');"
        "[Console]::Write($s.TargetPath)"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        cp = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=6.0,
            creationflags=flags,
        )
        target = (cp.stdout or "").strip()
        if target.lower().endswith(".exe") and Path(target).is_file():
            return _norm_path(target)
    except Exception:
        pass
    return ""


def apply_unified_policy(open_hwnds: Iterable[int] = (), shortcut_paths: Iterable[str] = ()) -> dict:
    """Apply policy to actual Flash hosts and shortcut targets. Idempotent and per-user only."""
    candidates: dict[str, str] = {}
    for hwnd in open_hwnds:
        exe = get_window_process_exe(int(hwnd))
        if exe:
            candidates[exe.lower()] = exe
    for shortcut in shortcut_paths:
        target = resolve_shortcut_target(str(shortcut or ""))
        if target:
            candidates[target.lower()] = target

    results = [ensure_high_dpi_aware_if_unset(exe) for exe in candidates.values()]
    state = {
        "version": 1,
        "updated_at": time.time(),
        "mode": "HIGHDPIAWARE_IF_UNSET",
        "targets": results,
    }
    try:
        _write_json_atomic(POLICY_STATE_PATH, state)
    except Exception:
        pass
    return state
