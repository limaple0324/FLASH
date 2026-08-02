"""Run the explicitly approved B/C synchronized-input verification."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.windows_input_sync import (  # noqa: E402
    Win32KeyMessageBackend,
    WindowInputPolicy,
    WindowsInputSyncController,
)
from adapters.windows_launch_fingerprint import (  # noqa: E402
    PowerShellLaunchFingerprintResolver,
    normalize_launch_fingerprint,
)
from adapters.windows_window import Win32WindowBackend  # noqa: E402


MINIMIZED_INPUT_SETTLE_SECONDS = 2.0


class _SnapshotWindowBackend:
    """Keep one identity-validated group while reading foreground state live."""

    def __init__(self, windows):
        self._windows = tuple(windows)
        self._live = Win32WindowBackend()

    def list_windows(self):
        return self._windows

    def foreground_handle(self):
        return self._live.foreground_handle()

    def top_window_at(self, x, y):
        return self._live.top_window_at(x, y)


def _exact_flash_windows(
    *,
    expected_count: int,
    keywords: list[str],
    resolve_fingerprints: bool = False,
):
    """Return the exact anonymous target group or fail before changing state."""
    normalized_keywords = tuple(
        keyword.strip().casefold()
        for keyword in keywords
        if isinstance(keyword, str) and keyword.strip()
    )
    backend = Win32WindowBackend(
        PowerShellLaunchFingerprintResolver()
        if resolve_fingerprints
        else None
    )
    windows = [
        window
        for window in backend.list_windows()
        if all(keyword in window.title.casefold() for keyword in normalized_keywords)
        and window.window_class == "ShockwaveFlash"
    ]
    if len(windows) != expected_count:
        raise ValueError("Exact Flash window group is not available")
    handles = [window.handle for window in windows if window.handle]
    process_ids = [
        window.process_id
        for window in windows
        if isinstance(window.process_id, int) and window.process_id > 0
    ]
    if len(handles) != expected_count or len(set(handles)) != expected_count:
        raise ValueError("Flash window handles are missing or duplicated")
    if (
        len(process_ids) != expected_count
        or len(set(process_ids)) != expected_count
    ):
        raise ValueError("Flash process identities are missing or duplicated")
    if resolve_fingerprints:
        fingerprints = [
            normalize_launch_fingerprint(window.launch_fingerprint)
            for window in windows
        ]
        if (
            any(fingerprint is None for fingerprint in fingerprints)
            or len(set(fingerprints)) != expected_count
        ):
            raise ValueError("Flash fingerprints are missing or duplicated")
    return tuple(windows)


def _activate_one_flash_window_for_test(
    *,
    expected_count: int,
    keywords: list[str],
    windows=None,
) -> int:
    """Create the foreground-only test condition without emitting identifiers."""
    if os.name != "nt":
        raise OSError("Foreground activation is only supported on Windows")
    windows = tuple(windows or _exact_flash_windows(
        expected_count=expected_count,
        keywords=keywords,
    ))
    if len(windows) != expected_count:
        raise ValueError("Exact Flash window group is not available")
    handles = [window.handle for window in windows]

    user32 = ctypes.windll.user32
    user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    )
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = (
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.BOOL,
    )
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = (wintypes.HWND,)
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetActiveWindow.argtypes = (wintypes.HWND,)
    user32.SetActiveWindow.restype = wintypes.HWND
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentThreadId.argtypes = ()
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    candidates = sorted(
        (window for window in windows if not window.minimized),
        key=lambda item: item.handle,
    )
    if not candidates:
        raise ValueError("No restored Flash window is available for foreground")
    target = wintypes.HWND(candidates[0].handle)
    foreground = user32.GetForegroundWindow()
    current_thread = int(kernel32.GetCurrentThreadId())
    related_threads = {
        int(user32.GetWindowThreadProcessId(window, None))
        for window in (foreground, target)
        if window
    }
    attached_threads: list[int] = []
    for thread_id in related_threads:
        if (
            thread_id
            and thread_id != current_thread
            and user32.AttachThreadInput(current_thread, thread_id, True)
        ):
            attached_threads.append(thread_id)
    try:
        user32.BringWindowToTop(target)
        user32.SetActiveWindow(target)
        user32.SetForegroundWindow(target)
    finally:
        for thread_id in reversed(attached_threads):
            user32.AttachThreadInput(current_thread, thread_id, False)
    time.sleep(0.25)
    foreground = user32.GetForegroundWindow()
    foreground_value = int(getattr(foreground, "value", foreground) or 0)
    target_value = int(getattr(target, "value", target) or 0)
    if foreground_value != target_value:
        raise OSError("Flash window could not be verified as foreground")
    return target_value


def _minimize_flash_windows_for_test(
    *,
    expected_count: int,
    keywords: list[str],
    minimize_count: int,
) -> tuple[int, ...]:
    """Minimize an exact subset and return only handles this run must restore."""
    if os.name != "nt":
        raise OSError("Minimized input testing is only supported on Windows")
    if minimize_count < 0 or minimize_count > expected_count:
        raise ValueError("--minimize-count-for-test is outside the target group")
    if minimize_count == 0:
        return ()
    windows = _exact_flash_windows(
        expected_count=expected_count,
        keywords=keywords,
        resolve_fingerprints=True,
    )
    candidates = [
        window
        for window in sorted(windows, key=lambda item: item.handle, reverse=True)
        if not window.minimized
    ]
    if len(candidates) < minimize_count:
        raise ValueError("Not enough restored Flash windows for the test")

    user32 = ctypes.windll.user32
    user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.ShowWindow.restype = wintypes.BOOL
    changed: list[int] = []
    try:
        for window in candidates[:minimize_count]:
            user32.ShowWindow(wintypes.HWND(window.handle), 6)  # SW_MINIMIZE
            changed.append(window.handle)
        time.sleep(0.25)
        return tuple(changed)
    except OSError:
        _restore_flash_windows(changed)
        raise


def _restore_flash_windows(handles) -> None:
    """Restore only windows minimized by this verifier."""
    if os.name != "nt":
        return
    user32 = ctypes.windll.user32
    user32.IsWindow.argtypes = (wintypes.HWND,)
    user32.IsWindow.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.ShowWindow.restype = wintypes.BOOL
    for handle in handles:
        hwnd = wintypes.HWND(handle)
        if user32.IsWindow(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify approved B/C input delivery after exact 14-window identity "
            "and responsiveness preflight."
        )
    )
    parser.add_argument("--key", choices=("B", "C"), required=True)
    parser.add_argument(
        "--policy",
        choices=tuple(policy.value for policy in WindowInputPolicy),
        required=True,
    )
    parser.add_argument("--expected-count", type=int, default=14)
    parser.add_argument(
        "--keywords",
        nargs="+",
        default=["Adobe Flash Player"],
    )
    parser.add_argument(
        "--execute-approved-input",
        action="store_true",
        help="Actually send the approved key after the complete preflight passes.",
    )
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=0.0,
        help=(
            "Wait before the live preflight so a foreground-only test can "
            "place exactly one Flash window in front."
        ),
    )
    parser.add_argument(
        "--activate-one-for-foreground-test",
        action="store_true",
        help=(
            "For a foreground_only live test, restore and verify exactly one "
            "ShockwaveFlash window as foreground immediately before preflight."
        ),
    )
    parser.add_argument(
        "--minimize-count-for-test",
        type=int,
        default=0,
        help=(
            "Temporarily minimize an exact anonymous subset immediately before "
            "the input test, then always restore only that subset."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    restore_handles: tuple[int, ...] = ()
    try:
        if args.delay_seconds < 0 or args.delay_seconds > 30:
            raise ValueError("--delay-seconds must be between 0 and 30")
        if (
            args.minimize_count_for_test < 0
            or args.minimize_count_for_test > args.expected_count
        ):
            raise ValueError(
                "--minimize-count-for-test must be inside the expected group"
            )
        if args.minimize_count_for_test and not args.execute_approved_input:
            raise ValueError(
                "Minimized preparation requires approved live execution"
            )
        if (
            args.policy == WindowInputPolicy.FOREGROUND_ONLY.value
            and args.minimize_count_for_test
        ):
            raise ValueError(
                "foreground_only cannot prepare minimized target windows"
            )
        if (
            args.activate_one_for_foreground_test
            and args.minimize_count_for_test == args.expected_count
        ):
            raise ValueError("Cannot foreground a fully minimized target group")
        if args.delay_seconds:
            time.sleep(args.delay_seconds)
        restore_handles = _minimize_flash_windows_for_test(
            expected_count=args.expected_count,
            keywords=args.keywords,
            minimize_count=args.minimize_count_for_test,
        )
        controller = None
        if args.activate_one_for_foreground_test:
            if (
                args.policy
                not in {
                    WindowInputPolicy.FOREGROUND_ONLY.value,
                    WindowInputPolicy.FOREGROUND_BACKGROUND.value,
                }
                or not args.execute_approved_input
            ):
                raise ValueError(
                    "Foreground activation requires a foreground-enabled policy"
                )
            validated_windows = _exact_flash_windows(
                expected_count=args.expected_count,
                keywords=args.keywords,
                resolve_fingerprints=True,
            )
            controller = WindowsInputSyncController(
                expected_windows=args.expected_count,
                title_keywords=args.keywords,
                window_backend=_SnapshotWindowBackend(validated_windows),
                message_backend=Win32KeyMessageBackend(),
            )
            _activate_one_flash_window_for_test(
                expected_count=args.expected_count,
                keywords=args.keywords,
                windows=validated_windows,
            )
        if controller is None:
            controller = WindowsInputSyncController.for_real_windows(
                expected_windows=args.expected_count,
                title_keywords=args.keywords,
            )
        result = controller.send_approved_key(
            args.key,
            policy=args.policy,
            execute=args.execute_approved_input,
        )
        if (
            args.execute_approved_input
            and restore_handles
            and result.sent_windows > 0
        ):
            time.sleep(MINIMIZED_INPUT_SETTLE_SECONDS)
        payload = result.to_dict()
        payload["minimized_hold_seconds"] = (
            MINIMIZED_INPUT_SETTLE_SECONDS if restore_handles else 0.0
        )
        exit_code = 0 if (result.passed or result.ready) else 3
    except (OSError, ValueError):
        payload = {
            "passed": False,
            "ready": False,
            "failure_codes": ["input_verifier_execution_failed"],
            "raw_arguments_emitted": False,
            "fingerprints_emitted": False,
            "input_sent": False,
        }
        exit_code = 4
    finally:
        _restore_flash_windows(restore_handles)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
