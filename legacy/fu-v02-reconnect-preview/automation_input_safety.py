"""Exact HWND lifecycle guards shared by embedded automation input leaves."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from contextlib import contextmanager
from dataclasses import dataclass
import threading
import uuid


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.SetPropW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.HANDLE]
user32.SetPropW.restype = wintypes.BOOL
user32.GetPropW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
user32.GetPropW.restype = wintypes.HANDLE
user32.RemovePropW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
user32.RemovePropW.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.GetProcessTimes.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
]
kernel32.GetProcessTimes.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


@dataclass(frozen=True)
class ExactTargetIdentity:
    lifecycle: tuple[int, int, int]
    property_name: str
    property_value: int


class ExactTargetFailure(RuntimeError):
    """Base class for a target that can no longer be proven safe."""


class ExactTargetReplaced(ExactTargetFailure):
    """The numeric HWND no longer denotes the marked native window object."""


class ExactTargetUnverified(ExactTargetFailure):
    """The marked native target could not be verified at an input boundary."""


_BOUNDARY_STATE = threading.local()


@contextmanager
def authorized_boundary(runner):
    """Install a per-thread atomic authorization runner for native dispatches."""
    stack = getattr(_BOUNDARY_STATE, "runners", None)
    if stack is None:
        stack = []
        _BOUNDARY_STATE.runners = stack
    stack.append(runner)
    try:
        yield
    finally:
        if stack and stack[-1] is runner:
            stack.pop()
        else:
            try:
                stack.remove(runner)
            except ValueError:
                pass


def run_authorized(action):
    """Run one input dispatch atomically through all active authorization gates."""
    if int(getattr(_BOUNDARY_STATE, "release_depth", 0) or 0) > 0:
        return action()
    runners = tuple(getattr(_BOUNDARY_STATE, "runners", ()) or ())

    def invoke(index: int):
        if index >= len(runners):
            return action()
        return runners[index](lambda: invoke(index + 1))

    return invoke(0)


def run_exact_authorized(
    hwnd: int,
    expected: ExactTargetIdentity | None,
    action,
    revalidate=None,
):
    """Atomically recheck the exact target and authorization, then dispatch."""
    def checked():
        status = target_status(int(hwnd), expected)
        if status == "replaced":
            raise ExactTargetReplaced("原生輸入窗口已被替換")
        if status != "same":
            raise ExactTargetUnverified("無法確認原生輸入窗口")
        if revalidate is not None and not revalidate():
            raise ExactTargetUnverified("原生輸入窗口重新解析不一致")
        return action()

    return run_authorized(checked)


def run_exact_cleanup(
    hwnd: int,
    expected: ExactTargetIdentity | None,
    action,
    revalidate=None,
):
    """Run narrowly scoped cleanup after rechecking its exact native target.

    Cleanup must remain possible after the controller suspends new automation
    input.  This intentionally skips authorization runners, but never skips the
    HWND property/PID/TID/process-creation marker or the optional state check.
    """
    status = target_status(int(hwnd), expected)
    if status == "replaced":
        raise ExactTargetReplaced("清理目標原生窗口已被替換")
    if status != "same":
        raise ExactTargetUnverified("無法確認清理目標原生窗口")
    if revalidate is not None and not revalidate():
        raise ExactTargetUnverified("清理目標狀態已改變")
    return action()


@contextmanager
def _release_debt_boundary():
    depth = int(getattr(_BOUNDARY_STATE, "release_depth", 0) or 0)
    _BOUNDARY_STATE.release_depth = depth + 1
    try:
        yield
    finally:
        _BOUNDARY_STATE.release_depth = depth


def _handle_value(value) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return int(value)
    return int(getattr(value, "value", 0) or 0)


def _cheap_identity(hwnd: int) -> tuple[int, int] | None:
    if not hwnd or not user32.IsWindow(int(hwnd)):
        return None
    pid = wintypes.DWORD(0)
    tid = int(user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid)) or 0)
    if not pid.value or not tid or not user32.IsWindow(int(hwnd)):
        return None
    return int(pid.value), tid


def _lifecycle_identity(hwnd: int) -> tuple[int, int, int] | None:
    cheap = _cheap_identity(hwnd)
    if cheap is None:
        return None
    pid, tid = cheap
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    created = wintypes.FILETIME()
    exited = wintypes.FILETIME()
    kernel = wintypes.FILETIME()
    user = wintypes.FILETIME()
    try:
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user),
        ):
            return None
    finally:
        kernel32.CloseHandle(handle)
    if _cheap_identity(hwnd) != (pid, tid):
        return None
    created_value = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
    return pid, tid, created_value


def mark_target(hwnd: int) -> ExactTargetIdentity | None:
    if not hwnd or not user32.IsWindow(int(hwnd)):
        return None
    name = f"FuMagic.AutomationDebt.{uuid.uuid4().hex}"
    bits = ctypes.sizeof(ctypes.c_void_p) * 8 - 1
    value = (uuid.uuid4().int & ((1 << bits) - 1)) or 1
    property_set = False
    try:
        if not user32.SetPropW(int(hwnd), name, wintypes.HANDLE(value)):
            return None
        property_set = True
        lifecycle = _lifecycle_identity(int(hwnd))
        if lifecycle is None or _handle_value(user32.GetPropW(int(hwnd), name)) != value:
            user32.RemovePropW(int(hwnd), name)
            return None
        return ExactTargetIdentity(lifecycle, name, value)
    except Exception:
        if property_set:
            try:
                user32.RemovePropW(int(hwnd), name)
            except Exception:
                pass
        return None


def clear_target(hwnd: int, expected: ExactTargetIdentity | None) -> None:
    if not isinstance(expected, ExactTargetIdentity):
        return
    try:
        if _handle_value(user32.GetPropW(int(hwnd), expected.property_name)) == expected.property_value:
            user32.RemovePropW(int(hwnd), expected.property_name)
    except Exception:
        pass


def target_status(hwnd: int, expected: ExactTargetIdentity | None) -> str:
    if not isinstance(expected, ExactTargetIdentity):
        return "unknown"
    if not hwnd or not user32.IsWindow(int(hwnd)):
        return "replaced"
    try:
        marker = _handle_value(user32.GetPropW(int(hwnd), expected.property_name))
    except Exception:
        return "unknown"
    if marker != expected.property_value:
        return "replaced"
    current = _lifecycle_identity(int(hwnd))
    if current is not None:
        return "same" if current == expected.lifecycle else "replaced"
    cheap = _cheap_identity(int(hwnd))
    if cheap is None or cheap != expected.lifecycle[:2]:
        return "replaced"
    return "unknown"


def release_exact(
    hwnd: int,
    expected: ExactTargetIdentity,
    send_release,
    attempts: int = 3,
    clear_marker: bool = True,
) -> str:
    """Return released/replaced/failed and always remove the owned marker."""
    try:
        for attempt in range(max(1, int(attempts))):
            status = target_status(hwnd, expected)
            if status == "replaced":
                return "replaced"
            if status == "same":
                try:
                    with _release_debt_boundary():
                        released = send_release()
                    if released:
                        return "released"
                except Exception:
                    pass
            if attempt + 1 < attempts:
                threading.Event().wait(0.01)
        return "failed"
    finally:
        if clear_marker:
            clear_target(hwnd, expected)
