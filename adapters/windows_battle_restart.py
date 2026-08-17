"""Close exactly one verified battle-disconnected window and reopen its shortcut."""

from __future__ import annotations

import ctypes
import base64
import json
import os
import secrets
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, replace
from ctypes import wintypes
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol

from adapters.windows_launch_fingerprint import (
    PowerShellShortcutFingerprintResolver,
    ShortcutFingerprintResolver,
    normalize_launch_fingerprint,
)
from adapters.windows_window import (
    WindowBackend,
    WindowInfo,
    complete_window_instance_identity,
)
from services.group_launch_service import GroupLaunchTarget


WindowInstanceIdentity = tuple[object, ...]
WindowInstanceIdentityProvider = Callable[
    [int],
    WindowInstanceIdentity | None,
]
TargetAbsenceCheck = Callable[[], str | None]


class BattleReopenStage(str, Enum):
    FIRST_ABSENCE_STARTED = "first_absence_check_started"
    FIRST_ABSENCE_COMPLETED = "first_absence_check_completed"
    SHORTCUT_FINGERPRINT_STARTED = "shortcut_fingerprint_check_started"
    SHORTCUT_FINGERPRINT_COMPLETED = "shortcut_fingerprint_check_completed"
    SECOND_ABSENCE_STARTED = "second_absence_check_started"
    SECOND_ABSENCE_COMPLETED = "second_absence_check_completed"
    SHORTCUT_LAUNCH_PREPARED = "shortcut_launch_prepared"
    SHORTCUT_LAUNCH_ENTERED = "shortcut_launch_entered"
    SHORTCUT_LAUNCH_RETURNED = "shortcut_launch_returned"
    WAITING_NEW_INSTANCE = "waiting_new_instance"
    NEW_INSTANCE_APPEARED = "new_instance_appeared"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class BattleReopenStageEvidence:
    owner: str
    entry_id: str
    fingerprint: str
    original_instance: tuple[object, ...]
    original_shortcut: str
    stage: str
    stage_started_at: float
    stage_ended_at: float | None
    delivery_boundary_crossed: bool
    retry_allowed: bool
    wait_new_instance_only: bool
    failure_reason: str | None = None
    hard_timeout: bool = False


class BattleReopenWorker(Protocol):
    """One killable, short-lived reopen worker owned by one recovery event."""

    @property
    def job_id(self) -> str:
        """Return the unpredictable identifier bound to this worker."""

    def poll_events(self) -> tuple[Mapping[str, object], ...]:
        """Return newly emitted, monotonically sequenced stage events."""

    def is_running(self) -> bool:
        """Return whether the isolated process is still alive."""

    def authorize_launch(self) -> bool:
        """Acknowledge launch only after consumed state is durable."""

    def terminate_and_wait(self) -> bool:
        """Terminate, kill if needed, wait, and prove the worker is gone."""

    def cleanup(self) -> None:
        """Remove this worker's non-sensitive temporary stage files."""


class WindowCloseBackend(Protocol):
    def is_window(self, handle: int) -> bool:
        """Return whether the exact top-level HWND still exists."""

    def close_window(self, handle: int) -> bool:
        """Legacy exact-HWND close used outside battle reconnect."""

    def close_window_if_instance_matches(
        self,
        handle: int,
        expected_identity: WindowInstanceIdentity,
        current_identity: WindowInstanceIdentityProvider,
    ) -> tuple[bool, str | None]:
        """Close only when the complete identity still matches at delivery."""


class ShortcutOpenBackend(Protocol):
    def open_shortcut(self, target: GroupLaunchTarget) -> bool:
        """Legacy validated shortcut open used outside battle reconnect."""

    def open_shortcut_if_target_absent(
        self,
        target: GroupLaunchTarget,
        absence_check: TargetAbsenceCheck,
    ) -> tuple[bool, str | None]:
        """Open only when target absence is rechecked at delivery."""


@dataclass(frozen=True, slots=True)
class BattleRestartResult:
    success: bool
    failure_code: str | None = None
    window_closed: bool = False
    shortcut_open_requested: bool = False
    pending: bool = False
    stage: str | None = None
    delivery_boundary_crossed: bool = False
    retry_allowed: bool = False
    wait_new_instance_only: bool = False
    hard_timeout: bool = False
    stage_evidence: tuple[BattleReopenStageEvidence, ...] = ()


class Win32WindowCloseBackend:
    WM_CLOSE = 0x0010

    @staticmethod
    def _user32():
        if os.name != "nt":
            return None
        return ctypes.windll.user32

    @staticmethod
    def _configure(user32) -> None:
        user32.IsWindow.argtypes = (wintypes.HWND,)
        user32.IsWindow.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.PostMessageW.restype = wintypes.BOOL

    def is_window(self, handle: int) -> bool:
        user32 = self._user32()
        if user32 is None:
            return False
        self._configure(user32)
        return bool(user32.IsWindow(wintypes.HWND(handle)))

    def close_window(self, handle: int) -> bool:
        user32 = self._user32()
        if user32 is None:
            return False
        self._configure(user32)
        hwnd = wintypes.HWND(handle)
        if not user32.IsWindow(hwnd):
            return False
        return bool(user32.PostMessageW(hwnd, self.WM_CLOSE, 0, 0))

    def close_window_if_instance_matches(
        self,
        handle: int,
        expected_identity: WindowInstanceIdentity,
        current_identity: WindowInstanceIdentityProvider,
    ) -> tuple[bool, str | None]:
        user32 = self._user32()
        if user32 is None:
            return False, "battle_window_close_failed"
        self._configure(user32)
        hwnd = wintypes.HWND(handle)
        if not user32.IsWindow(hwnd):
            return False, "battle_window_missing"
        # The full identity check belongs inside the close backend so there is
        # no caller/backend gap in which a reused HWND can silently inherit the
        # already-authorized WM_CLOSE.
        if current_identity(handle) != expected_identity:
            return False, "battle_window_identity_changed"
        if not user32.PostMessageW(hwnd, self.WM_CLOSE, 0, 0):
            return False, "battle_window_close_failed"
        return True, None


class WindowsShortcutOpenBackend:
    def __init__(
        self,
        shortcut_fingerprint_resolver: ShortcutFingerprintResolver | None = None,
    ) -> None:
        self._shortcut_fingerprint_resolver = (
            shortcut_fingerprint_resolver
            or PowerShellShortcutFingerprintResolver()
        )

    def _target_fingerprint_failure(
        self,
        target: GroupLaunchTarget,
    ) -> str | None:
        expected = normalize_launch_fingerprint(target.fingerprint)
        if expected is None or not target.shortcut_path.is_file():
            return "battle_shortcut_identity_unresolved"
        try:
            resolved = self._shortcut_fingerprint_resolver.resolve(
                (target.shortcut_path,)
            )
        except Exception:
            return "battle_shortcut_identity_unresolved"
        if set(resolved) != {target.shortcut_path}:
            return "battle_shortcut_identity_unresolved"
        actual = normalize_launch_fingerprint(
            resolved.get(target.shortcut_path)
        )
        if actual != expected:
            return "battle_shortcut_identity_changed"
        return None

    def open_shortcut(self, target: GroupLaunchTarget) -> bool:
        if (
            os.name != "nt"
            or self._target_fingerprint_failure(target) is not None
        ):
            return False
        try:
            os.startfile(str(target.shortcut_path))  # type: ignore[attr-defined]
            return True
        except OSError:
            return False

    def open_shortcut_if_target_absent(
        self,
        target: GroupLaunchTarget,
        absence_check: TargetAbsenceCheck,
    ) -> tuple[bool, str | None]:
        # Keep the last live-window check inside the same backend boundary as
        # os.startfile. The outer stability check alone cannot cover a
        # self-reopen that occurs immediately before shortcut delivery.
        failure_code = absence_check()
        if failure_code is not None:
            return False, failure_code
        failure_code = self._target_fingerprint_failure(target)
        if failure_code is not None:
            return False, failure_code
        # Resolving the shortcut can take long enough for the game target to
        # reopen.  This second check is deliberately after the final identity
        # resolution and immediately before delivery; do not call
        # ``open_shortcut`` here because it would resolve again and create a
        # new check-to-open gap.
        failure_code = absence_check()
        if failure_code is not None:
            return False, failure_code
        if os.name != "nt":
            return False, "battle_shortcut_open_failed"
        try:
            os.startfile(str(target.shortcut_path))  # type: ignore[attr-defined]
        except OSError:
            return False, "battle_shortcut_open_failed"
        return True, None


_POWERSHELL_REOPEN_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$script:encoding = New-Object System.Text.UTF8Encoding($false)
$script:sequence = 0
$script:jobId = [string]$env:FLASH_REOPEN_JOB_ID
$script:token = [string]$env:FLASH_REOPEN_JOB_TOKEN
$script:eventPath = [string]$env:FLASH_REOPEN_EVENT_PATH
$script:startPath = [string]$env:FLASH_REOPEN_START_PATH
$script:ackPath = [string]$env:FLASH_REOPEN_ACK_PATH

function Emit-Stage([string]$stage, [string]$failureCode = '') {
    $script:sequence += 1
    $record = [ordered]@{
        job_id = $script:jobId
        token = $script:token
        sequence = $script:sequence
        stage = $stage
        timestamp_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        failure_code = $failureCode
    }
    $line = $record | ConvertTo-Json -Compress
    [IO.File]::AppendAllText(
        $script:eventPath,
        $line + [Environment]::NewLine,
        $script:encoding
    )
}

function Wait-TokenFile([string]$path, [int]$maximumSeconds) {
    $deadline = [DateTime]::UtcNow.AddSeconds($maximumSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            if (
                [IO.File]::Exists($path) -and
                [string]::Equals(
                    [IO.File]::ReadAllText($path, $script:encoding),
                    $script:token,
                    [StringComparison]::Ordinal
                )
            ) {
                return $true
            }
        } catch {
        }
        Start-Sleep -Milliseconds 20
    }
    return $false
}

if (-not (Wait-TokenFile $script:startPath 15)) {
    exit 91
}

$payloadJson = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String([string]$env:FLASH_REOPEN_PAYLOAD_B64)
)
$payload = $payloadJson | ConvertFrom-Json

Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public static class FlashReopenNative {
    public sealed class WindowRow {
        public long Handle { get; set; }
        public string Title { get; set; }
        public bool Minimized { get; set; }
        public int Left { get; set; }
        public int Top { get; set; }
        public int Right { get; set; }
        public int Bottom { get; set; }
        public uint ProcessId { get; set; }
        public uint ThreadId { get; set; }
        public string WindowClass { get; set; }
        public ulong Lifecycle { get; set; }
    }

    private delegate bool EnumProc(IntPtr hwnd, IntPtr lparam);
    [StructLayout(LayoutKind.Sequential)]
    private struct RECT { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)]
    private struct FILETIME { public uint Low, High; }
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    private struct SHELLEXECUTEINFOW {
        public int cbSize;
        public uint fMask;
        public IntPtr hwnd;
        [MarshalAs(UnmanagedType.LPWStr)] public string lpVerb;
        [MarshalAs(UnmanagedType.LPWStr)] public string lpFile;
        [MarshalAs(UnmanagedType.LPWStr)] public string lpParameters;
        [MarshalAs(UnmanagedType.LPWStr)] public string lpDirectory;
        public int nShow;
        public IntPtr hInstApp;
        public IntPtr lpIDList;
        [MarshalAs(UnmanagedType.LPWStr)] public string lpClass;
        public IntPtr hkeyClass;
        public uint dwHotKey;
        public IntPtr hIconOrMonitor;
        public IntPtr hProcess;
    }

    private const uint SEE_MASK_FLAG_NO_UI = 0x00000400;
    private const uint SEE_MASK_NOASYNC = 0x00000100;
    private const int SW_SHOWNOACTIVATE = 4;

    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumProc callback, IntPtr value);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] private static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetWindowTextLengthW(IntPtr hwnd);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetWindowTextW(IntPtr hwnd, StringBuilder value, int maximum);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetClassNameW(IntPtr hwnd, StringBuilder value, int maximum);
    [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);
    [DllImport("kernel32.dll")] private static extern IntPtr OpenProcess(uint access, bool inherit, uint processId);
    [DllImport("kernel32.dll")] private static extern bool GetProcessTimes(IntPtr process, out FILETIME created, out FILETIME exited, out FILETIME kernel, out FILETIME user);
    [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
    [DllImport("shell32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool ShellExecuteExW(ref SHELLEXECUTEINFOW info);

    public static void LaunchWithoutActivation(string path) {
        if (String.IsNullOrWhiteSpace(path)) {
            throw new ArgumentException("Shortcut path is required", "path");
        }
        var info = new SHELLEXECUTEINFOW {
            cbSize = Marshal.SizeOf(typeof(SHELLEXECUTEINFOW)),
            fMask = SEE_MASK_FLAG_NO_UI | SEE_MASK_NOASYNC,
            lpVerb = "open",
            lpFile = path,
            nShow = SW_SHOWNOACTIVATE
        };
        if (!ShellExecuteExW(ref info)) {
            throw new System.ComponentModel.Win32Exception(
                Marshal.GetLastWin32Error()
            );
        }
    }

    private static ulong Lifecycle(uint processId) {
        IntPtr process = OpenProcess(0x1000, false, processId);
        if (process == IntPtr.Zero) return 0;
        try {
            FILETIME created, exited, kernel, user;
            if (!GetProcessTimes(process, out created, out exited, out kernel, out user)) return 0;
            return ((ulong)created.High << 32) | created.Low;
        } finally {
            CloseHandle(process);
        }
    }

    public static WindowRow[] Snapshot() {
        var rows = new List<WindowRow>();
        EnumProc callback = delegate(IntPtr hwnd, IntPtr ignored) {
            if (!IsWindowVisible(hwnd)) return true;
            int length = GetWindowTextLengthW(hwnd);
            if (length <= 0) return true;
            var title = new StringBuilder(length + 1);
            if (GetWindowTextW(hwnd, title, title.Capacity) <= 0) return true;
            RECT rect;
            if (!GetWindowRect(hwnd, out rect)) return true;
            uint processId;
            uint threadId = GetWindowThreadProcessId(hwnd, out processId);
            var windowClass = new StringBuilder(512);
            if (GetClassNameW(hwnd, windowClass, windowClass.Capacity) <= 0) return true;
            rows.Add(new WindowRow {
                Handle = hwnd.ToInt64(), Title = title.ToString(), Minimized = IsIconic(hwnd),
                Left = rect.Left, Top = rect.Top, Right = rect.Right, Bottom = rect.Bottom,
                ProcessId = processId, ThreadId = threadId, WindowClass = windowClass.ToString(),
                Lifecycle = Lifecycle(processId)
            });
            return true;
        };
        if (!EnumWindows(callback, IntPtr.Zero)) throw new InvalidOperationException("EnumWindows failed");
        return rows.ToArray();
    }
}
'@

function Get-ShortcutCandidates {
    $shell = New-Object -ComObject WScript.Shell
    $items = @()
    $roots = @(
        [Environment]::GetFolderPath('Desktop'),
        [Environment]::GetFolderPath('CommonDesktopDirectory')
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Sort-Object -Unique
    foreach ($root in $roots) {
        foreach ($item in Get-ChildItem -LiteralPath $root -Filter '*.lnk' -File -Recurse -ErrorAction SilentlyContinue) {
            try {
                $shortcut = $shell.CreateShortcut($item.FullName)
                $arguments = [string]$shortcut.Arguments
                if (-not [string]::IsNullOrWhiteSpace($arguments)) {
                    $items += [pscustomobject]@{
                        Arguments = $arguments
                        TargetPath = [string]$shortcut.TargetPath
                    }
                }
            } catch {
            }
        }
    }
    return @($items)
}

function Get-ProcessFingerprint([uint32]$processId, [object[]]$shortcuts) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
    if ($null -eq $process -or [string]::IsNullOrEmpty([string]$process.CommandLine)) {
        return $null
    }
    $commandLine = [string]$process.CommandLine
    $executablePath = [string]$process.ExecutablePath
    $arguments = @(
        $shortcuts | Where-Object {
            $expected = ([string]$_.Arguments).Trim()
            $trimmed = $commandLine.TrimEnd()
            $tail = -not [string]::IsNullOrEmpty($expected) -and $trimmed.EndsWith($expected, [StringComparison]::Ordinal)
            $prefix = $trimmed.Length - $expected.Length
            $boundary = $prefix -gt 0 -and [char]::IsWhiteSpace($trimmed[$prefix - 1])
            $target = (
                [string]::IsNullOrWhiteSpace($_.TargetPath) -or
                [string]::IsNullOrWhiteSpace($executablePath) -or
                [string]::Equals(
                    [IO.Path]::GetFullPath($_.TargetPath),
                    [IO.Path]::GetFullPath($executablePath),
                    [StringComparison]::OrdinalIgnoreCase
                )
            )
            $tail -and $boundary -and $target
        } | Select-Object -ExpandProperty Arguments -Unique
    )
    if ($arguments.Count -ne 1) { return $null }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes([string]$arguments[0])
        return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Get-LiveIdentitySnapshot {
    $shortcuts = @(Get-ShortcutCandidates)
    $keys = @()
    $fingerprints = @()
    foreach ($row in [FlashReopenNative]::Snapshot()) {
        $matches = $true
        foreach ($keyword in @($payload.title_keywords)) {
            if ($row.Title.IndexOf([string]$keyword, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
                $matches = $false
                break
            }
        }
        if (-not $matches) { continue }
        $fingerprint = Get-ProcessFingerprint $row.ProcessId $shortcuts
        if ([string]::IsNullOrWhiteSpace([string]$fingerprint) -or $row.Lifecycle -eq 0) {
            return [pscustomobject]@{ Failure = 'battle_window_existing_state_unknown'; Keys = @(); Fingerprints = @() }
        }
        $classB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([string]$row.WindowClass))
        $key = @(
            [string]$fingerprint,
            [string]$row.Handle,
            [string]$row.ProcessId,
            [string]$row.ThreadId,
            $classB64,
            [string]$row.Lifecycle
        ) -join '|'
        $keys += $key
        $fingerprints += [string]$fingerprint
    }
    return [pscustomobject]@{ Failure = ''; Keys = @($keys); Fingerprints = @($fingerprints) }
}

function Test-TargetAbsent {
    try {
        $snapshot = Get-LiveIdentitySnapshot
    } catch {
        return 'battle_window_enumeration_failed'
    }
    if (-not [string]::IsNullOrWhiteSpace([string]$snapshot.Failure)) {
        return [string]$snapshot.Failure
    }
    $actual = @($snapshot.Keys | Sort-Object)
    $expected = @($payload.expected_identity_keys | Sort-Object)
    if ($actual.Count -ne $expected.Count) { return 'battle_contract_identity_changed' }
    for ($index = 0; $index -lt $expected.Count; $index++) {
        if (-not [string]::Equals([string]$actual[$index], [string]$expected[$index], [StringComparison]::Ordinal)) {
            return 'battle_contract_identity_changed'
        }
    }
    if (@($snapshot.Fingerprints) -contains [string]$payload.fingerprint) {
        return 'battle_window_already_exists'
    }
    return ''
}

function Get-TargetShortcutFingerprint {
    $path = [string]$payload.shortcut_path
    if (
        [string]::IsNullOrWhiteSpace($path) -or
        -not [string]::Equals([IO.Path]::GetExtension($path), '.lnk', [StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath $path -PathType Leaf)
    ) { return '' }
    try {
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($path)
        $arguments = [string]$shortcut.Arguments
        if ([string]::IsNullOrWhiteSpace($arguments)) { return '' }
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            $bytes = [Text.Encoding]::UTF8.GetBytes($arguments)
            return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
        } finally {
            $sha.Dispose()
        }
    } catch {
        return ''
    }
}

function Fail-Reopen([string]$code) {
    Emit-Stage 'failed' $code
    exit 2
}

Emit-Stage 'first_absence_check_started'
$stabilityDeadline = [DateTime]::UtcNow.AddMilliseconds(
    [Math]::Max(1, [double]$payload.absence_stability_seconds * 1000.0)
)
$maximumChecks = [Math]::Max(
    2,
    [int]([double]$payload.absence_stability_seconds / [double]$payload.poll_seconds) + 3
)
$confirmed = $false
for ($check = 0; $check -lt $maximumChecks; $check++) {
    $failure = Test-TargetAbsent
    if (-not [string]::IsNullOrWhiteSpace($failure)) { Fail-Reopen $failure }
    $remaining = $stabilityDeadline - [DateTime]::UtcNow
    if ($remaining.TotalMilliseconds -le 0) {
        $confirmed = $true
        break
    }
    Start-Sleep -Milliseconds ([int][Math]::Max(
        1,
        [Math]::Min(
            [double]$payload.poll_seconds * 1000.0,
            $remaining.TotalMilliseconds
        )
    ))
}
if (-not $confirmed) { Fail-Reopen 'battle_window_absence_unconfirmed' }
Emit-Stage 'first_absence_check_completed'

Emit-Stage 'shortcut_fingerprint_check_started'
$actualFingerprint = Get-TargetShortcutFingerprint
if ([string]::IsNullOrWhiteSpace($actualFingerprint)) { Fail-Reopen 'battle_shortcut_identity_unresolved' }
if (-not [string]::Equals($actualFingerprint, [string]$payload.fingerprint, [StringComparison]::Ordinal)) {
    Fail-Reopen 'battle_shortcut_identity_changed'
}
Emit-Stage 'shortcut_fingerprint_check_completed'

Emit-Stage 'second_absence_check_started'
$failure = Test-TargetAbsent
if (-not [string]::IsNullOrWhiteSpace($failure)) { Fail-Reopen $failure }
Emit-Stage 'second_absence_check_completed'
Emit-Stage 'shortcut_launch_prepared'

if (-not (Wait-TokenFile $script:ackPath 65)) { exit 92 }
$failure = Test-TargetAbsent
if (-not [string]::IsNullOrWhiteSpace($failure)) { Fail-Reopen $failure }
Emit-Stage 'shortcut_launch_entered'
try {
    [FlashReopenNative]::LaunchWithoutActivation(
        [string]$payload.shortcut_path
    )
} catch {
    Fail-Reopen 'battle_shortcut_open_failed'
}
Emit-Stage 'shortcut_launch_returned'
exit 0
"""


class _Win32KillOnCloseJob:
    """Kill only the PowerShell worker when its owning process disappears."""

    _KILL_ON_CLOSE = 0x00002000
    _SILENT_BREAKAWAY_OK = 0x00001000
    _EXTENDED_LIMIT_INFORMATION = 9

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = (
            ("per_process_user_time_limit", ctypes.c_longlong),
            ("per_job_user_time_limit", ctypes.c_longlong),
            ("limit_flags", wintypes.DWORD),
            ("minimum_working_set_size", ctypes.c_size_t),
            ("maximum_working_set_size", ctypes.c_size_t),
            ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority_class", wintypes.DWORD),
            ("scheduling_class", wintypes.DWORD),
        )

    class _IoCounters(ctypes.Structure):
        _fields_ = tuple(
            (name, ctypes.c_ulonglong)
            for name in (
                "read_operation_count",
                "write_operation_count",
                "other_operation_count",
                "read_transfer_count",
                "write_transfer_count",
                "other_transfer_count",
            )
        )

    class _ExtendedLimitInformation(ctypes.Structure):
        pass

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows job objects are unavailable")
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = (
            wintypes.HANDLE,
            wintypes.HANDLE,
        )
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        self._kernel32 = kernel32
        self._handle = handle
        limits = self._ExtendedLimitInformation()
        limits.basic_limit_information.limit_flags = (
            self._KILL_ON_CLOSE | self._SILENT_BREAKAWAY_OK
        )
        if not kernel32.SetInformationJobObject(
            handle,
            self._EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.get_last_error()
            self.close()
            raise OSError(error, "SetInformationJobObject failed")

    def assign(self, process_handle: int) -> None:
        if not self._handle or not self._kernel32.AssignProcessToJobObject(
            self._handle,
            wintypes.HANDLE(process_handle),
        ):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")

    def close(self) -> None:
        handle = getattr(self, "_handle", None)
        if handle:
            self._handle = None
            self._kernel32.CloseHandle(handle)


_Win32KillOnCloseJob._ExtendedLimitInformation._fields_ = (
    ("basic_limit_information", _Win32KillOnCloseJob._BasicLimitInformation),
    ("io_info", _Win32KillOnCloseJob._IoCounters),
    ("process_memory_limit", ctypes.c_size_t),
    ("job_memory_limit", ctypes.c_size_t),
    ("peak_process_memory_used", ctypes.c_size_t),
    ("peak_job_memory_used", ctypes.c_size_t),
)


class _PowerShellBattleReopenWorker:
    """Packaged-safe worker: PowerShell is the only executable dependency."""

    def __init__(self, payload: Mapping[str, object]) -> None:
        if os.name != "nt":
            raise OSError("battle reopen is Windows-only")
        self._job_id = secrets.token_hex(16)
        self._token = secrets.token_hex(32)
        self._temporary_dir = Path(
            tempfile.mkdtemp(prefix="flash-battle-reopen-")
        )
        self._event_path = self._temporary_dir / "stages.jsonl"
        self._start_path = self._temporary_dir / "start.signal"
        self._ack_path = self._temporary_dir / "launch.signal"
        self._script_path = self._temporary_dir / "worker.ps1"
        self._seen_lines = 0
        self._next_sequence = 1
        self._process: subprocess.Popen[bytes] | None = None
        self._job: _Win32KillOnCloseJob | None = None
        environment = os.environ.copy()
        environment.update(
            {
                "FLASH_REOPEN_JOB_ID": self._job_id,
                "FLASH_REOPEN_JOB_TOKEN": self._token,
                "FLASH_REOPEN_EVENT_PATH": str(self._event_path),
                "FLASH_REOPEN_START_PATH": str(self._start_path),
                "FLASH_REOPEN_ACK_PATH": str(self._ack_path),
                "FLASH_REOPEN_PAYLOAD_B64": base64.b64encode(
                    json.dumps(
                        dict(payload),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).decode("ascii"),
            }
        )
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._script_path.write_text(
                _POWERSHELL_REOPEN_SCRIPT,
                encoding="ascii",
            )
            self._job = _Win32KillOnCloseJob()
            self._process = subprocess.Popen(
                (
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(self._script_path),
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                creationflags=creation_flags,
            )
            self._job.assign(int(self._process._handle))  # type: ignore[attr-defined]
            self._start_path.write_text(self._token, encoding="utf-8")
        except Exception:
            self.terminate_and_wait()
            self.cleanup()
            raise

    @property
    def job_id(self) -> str:
        return self._job_id

    def poll_events(self) -> tuple[Mapping[str, object], ...]:
        try:
            raw_text = self._event_path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return ()
        raw_lines = raw_text.splitlines()
        # AppendAllText is one record write, but a concurrent reader can still
        # observe the final record before its newline becomes visible.  Keep
        # that tail pending; only a newline-terminated record can advance the
        # trusted monotonic sequence or the launch boundary.
        if raw_text and not raw_text.endswith("\n"):
            raw_lines = raw_lines[:-1]
        if len(raw_lines) < self._seen_lines:
            raise ValueError("reopen stage stream was truncated")
        events: list[Mapping[str, object]] = []
        for raw_line in raw_lines[self._seen_lines :]:
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError as error:
                raise ValueError("invalid reopen stage event") from error
            if (
                not isinstance(event, dict)
                or event.get("job_id") != self._job_id
                or not secrets.compare_digest(
                    str(event.get("token", "")),
                    self._token,
                )
                or event.get("sequence") != self._next_sequence
            ):
                raise ValueError("untrusted reopen stage event")
            self._next_sequence += 1
            events.append(event)
        self._seen_lines = len(raw_lines)
        return tuple(events)

    def is_running(self) -> bool:
        return bool(self._process is not None and self._process.poll() is None)

    def authorize_launch(self) -> bool:
        if not self.is_running() or self._ack_path.exists():
            return False
        try:
            self._ack_path.write_text(self._token, encoding="utf-8")
        except OSError:
            return False
        return True

    def terminate_and_wait(self) -> bool:
        process = self._process
        if process is None:
            return True
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=0.5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=0.5)
                except (OSError, subprocess.TimeoutExpired):
                    job = self._job
                    if job is not None:
                        job.close()
                    try:
                        process.wait(timeout=1.0)
                    except (OSError, subprocess.TimeoutExpired):
                        return False
        job = self._job
        if job is not None:
            job.close()
            self._job = None
        return process.poll() is not None

    def cleanup(self) -> None:
        if self.is_running():
            return
        job = self._job
        if job is not None:
            job.close()
            self._job = None
        for path in (
            self._ack_path,
            self._start_path,
            self._event_path,
            self._script_path,
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                return
        try:
            self._temporary_dir.rmdir()
        except OSError:
            pass


def _reopen_identity_key(identity: WindowInstanceIdentity) -> str:
    fingerprint, handle, process_id, thread_id, window_class, lifecycle, _rect, _minimized = identity
    encoded_class = base64.b64encode(
        str(window_class).encode("utf-8")
    ).decode("ascii")
    return "|".join(
        (
            str(fingerprint),
            str(handle),
            str(process_id),
            str(thread_id),
            encoded_class,
            str(lifecycle),
        )
    )


def _normalized_shortcut_path(path: os.PathLike[str] | str) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(path))))


@dataclass(slots=True)
class _BoundedBattleReopenJob:
    owner: str
    entry_id: str
    fingerprint: str
    original_instance: tuple[object, ...]
    original_shortcut: str
    worker: BattleReopenWorker
    deadline: float | None
    stage_started_monotonic: float
    wall_clock: Callable[[], float]
    current_stage: str = BattleReopenStage.FIRST_ABSENCE_STARTED.value
    delivery_boundary_crossed: bool = False
    launch_authorized: bool = False
    evidence: tuple[BattleReopenStageEvidence, ...] = ()
    terminal_result: BattleRestartResult | None = None

    _ORDER = (
        BattleReopenStage.FIRST_ABSENCE_STARTED.value,
        BattleReopenStage.FIRST_ABSENCE_COMPLETED.value,
        BattleReopenStage.SHORTCUT_FINGERPRINT_STARTED.value,
        BattleReopenStage.SHORTCUT_FINGERPRINT_COMPLETED.value,
        BattleReopenStage.SECOND_ABSENCE_STARTED.value,
        BattleReopenStage.SECOND_ABSENCE_COMPLETED.value,
        BattleReopenStage.SHORTCUT_LAUNCH_PREPARED.value,
        BattleReopenStage.SHORTCUT_LAUNCH_ENTERED.value,
        BattleReopenStage.SHORTCUT_LAUNCH_RETURNED.value,
    )

    def matches(
        self,
        owner: str,
        entry_id: str,
        original_instance: tuple[object, ...],
        shortcut_path: os.PathLike[str] | str,
    ) -> bool:
        return bool(
            self.owner == owner
            and self.entry_id == entry_id
            and self.original_instance == tuple(original_instance)
            and _normalized_shortcut_path(self.original_shortcut)
            == _normalized_shortcut_path(shortcut_path)
        )

    def owned_by(self, owner: str, entry_id: str) -> bool:
        return self.owner == owner and self.entry_id == entry_id

    def _append_evidence(
        self,
        stage: str,
        timestamp: float,
        *,
        stage_started_at: float | None = None,
        stage_ended_at: float | None = None,
        failure_reason: str | None = None,
        hard_timeout: bool = False,
        retry_allowed: bool = False,
        wait_new_instance_only: bool | None = None,
    ) -> None:
        wait_only = (
            self.delivery_boundary_crossed
            if wait_new_instance_only is None
            else wait_new_instance_only
        )
        self.evidence += (
            BattleReopenStageEvidence(
                owner=self.owner,
                entry_id=self.entry_id,
                fingerprint=self.fingerprint,
                original_instance=self.original_instance,
                original_shortcut=self.original_shortcut,
                stage=stage,
                stage_started_at=(
                    timestamp
                    if stage_started_at is None
                    else stage_started_at
                ),
                stage_ended_at=stage_ended_at,
                delivery_boundary_crossed=self.delivery_boundary_crossed,
                retry_allowed=retry_allowed,
                wait_new_instance_only=wait_only,
                failure_reason=failure_reason,
                hard_timeout=hard_timeout,
            ),
        )

    def _complete_evidence_stage(
        self,
        started_stage: str,
        completed_stage: str,
        timestamp: float,
    ) -> None:
        records = list(self.evidence)
        started_at = timestamp
        for index in range(len(records) - 1, -1, -1):
            record = records[index]
            if record.stage == started_stage and record.stage_ended_at is None:
                started_at = record.stage_started_at
                records[index] = replace(record, stage_ended_at=timestamp)
                break
        self.evidence = tuple(records)
        self._append_evidence(
            completed_stage,
            timestamp,
            stage_started_at=started_at,
            stage_ended_at=timestamp,
        )

    def _close_last_open_evidence(
        self,
        timestamp: float,
        *,
        failure_reason: str | None = None,
        hard_timeout: bool = False,
    ) -> None:
        records = list(self.evidence)
        for index in range(len(records) - 1, -1, -1):
            record = records[index]
            if record.stage_ended_at is None:
                records[index] = replace(
                    record,
                    stage_ended_at=timestamp,
                    failure_reason=failure_reason,
                    hard_timeout=hard_timeout,
                )
                break
        self.evidence = tuple(records)

    def _result(
        self,
        *,
        success: bool = False,
        failure_code: str | None = None,
        pending: bool = False,
        retry_allowed: bool = False,
        hard_timeout: bool = False,
    ) -> BattleRestartResult:
        wait_only = self.delivery_boundary_crossed or (
            self.launch_authorized and not retry_allowed
        )
        return BattleRestartResult(
            success,
            failure_code,
            shortcut_open_requested=self.delivery_boundary_crossed,
            pending=pending,
            stage=self.current_stage,
            delivery_boundary_crossed=self.delivery_boundary_crossed,
            retry_allowed=retry_allowed,
            wait_new_instance_only=wait_only,
            hard_timeout=hard_timeout,
            stage_evidence=self.evidence,
        )

    def authorize_launch(self) -> BattleRestartResult:
        if self.current_stage != BattleReopenStage.SHORTCUT_LAUNCH_PREPARED.value:
            return self._result(
                failure_code="battle_reopen_launch_not_ready",
                pending=True,
            )
        if self.launch_authorized:
            return self._result(pending=True)
        if not self.worker.authorize_launch():
            stopped = self.worker.terminate_and_wait()
            if stopped:
                self.worker.cleanup()
            return self._result(
                failure_code="battle_reopen_launch_authorization_failed",
                retry_allowed=False,
            )
        self.launch_authorized = True
        return self._result(pending=True)

    def _failure(
        self,
        code: str,
        now: float,
        *,
        hard_timeout: bool,
        deadline_expired: bool = False,
    ) -> BattleRestartResult:
        stopped = self.worker.terminate_and_wait()
        if stopped:
            self.worker.cleanup()
        retry_allowed = bool(
            stopped
            and not self.delivery_boundary_crossed
            and not self.launch_authorized
            and not deadline_expired
        )
        wait_only = bool(
            self.delivery_boundary_crossed
            or self.launch_authorized
            or not stopped
        )
        if not stopped:
            code = "battle_reopen_worker_unreaped"
        self.current_stage = BattleReopenStage.FAILED.value
        failed_at = self.wall_clock()
        self._close_last_open_evidence(
            failed_at,
            failure_reason=code,
            hard_timeout=hard_timeout,
        )
        self._append_evidence(
            self.current_stage,
            failed_at,
            stage_ended_at=failed_at,
            failure_reason=code,
            hard_timeout=hard_timeout,
            retry_allowed=retry_allowed,
            wait_new_instance_only=wait_only,
        )
        result = self._result(
            failure_code=code,
            retry_allowed=retry_allowed,
            hard_timeout=hard_timeout,
        )
        self.terminal_result = result
        return result

    def poll(
        self,
        now: float,
        *,
        phase_timeouts: Mapping[str, float],
        enforce_limits: bool = True,
    ) -> BattleRestartResult:
        if self.terminal_result is not None:
            return self.terminal_result
        if (
            enforce_limits
            and self.deadline is not None
            and now >= self.deadline
        ):
            return self._failure(
                "tcp_reconnect_timeout",
                now,
                hard_timeout=True,
                deadline_expired=True,
            )
        if self.current_stage == BattleReopenStage.WAITING_NEW_INSTANCE.value:
            return self._result(pending=True)
        try:
            events = self.worker.poll_events()
        except (OSError, TypeError, ValueError):
            return self._failure(
                "battle_reopen_stage_evidence_invalid",
                now,
                hard_timeout=False,
            )
        for event in events:
            stage = event.get("stage")
            timestamp_ms = event.get("timestamp_ms")
            if (
                not isinstance(stage, str)
                or not isinstance(timestamp_ms, int)
                or isinstance(timestamp_ms, bool)
            ):
                return self._failure(
                    "battle_reopen_stage_evidence_invalid",
                    now,
                    hard_timeout=False,
                )
            if stage == BattleReopenStage.FAILED.value:
                failure = event.get("failure_code")
                code = (
                    failure
                    if isinstance(failure, str) and failure
                    else "battle_reopen_worker_failed"
                )
                return self._failure(code, now, hard_timeout=False)
            try:
                expected_index = self._ORDER.index(self.current_stage) + 1
            except ValueError:
                expected_index = 0
            initial_stage_marker = bool(
                stage == self.current_stage
                and len(self.evidence) == 1
                and self.evidence[0].stage == stage
                and self.evidence[0].stage_ended_at is None
            )
            if stage == self.current_stage and (
                not self.evidence or initial_stage_marker
            ):
                expected = stage
            else:
                expected = (
                    self._ORDER[expected_index]
                    if expected_index < len(self._ORDER)
                    else None
                )
            if stage != expected:
                return self._failure(
                    "battle_reopen_stage_order_invalid",
                    now,
                    hard_timeout=False,
                )
            self.current_stage = stage
            self.stage_started_monotonic = now
            if stage == BattleReopenStage.SHORTCUT_LAUNCH_ENTERED.value:
                self.delivery_boundary_crossed = True
            timestamp = timestamp_ms / 1000.0
            completion_pairs = {
                BattleReopenStage.FIRST_ABSENCE_COMPLETED.value: (
                    BattleReopenStage.FIRST_ABSENCE_STARTED.value
                ),
                BattleReopenStage.SHORTCUT_FINGERPRINT_COMPLETED.value: (
                    BattleReopenStage.SHORTCUT_FINGERPRINT_STARTED.value
                ),
                BattleReopenStage.SECOND_ABSENCE_COMPLETED.value: (
                    BattleReopenStage.SECOND_ABSENCE_STARTED.value
                ),
                BattleReopenStage.SHORTCUT_LAUNCH_RETURNED.value: (
                    BattleReopenStage.SHORTCUT_LAUNCH_ENTERED.value
                ),
            }
            started_stage = completion_pairs.get(stage)
            if started_stage is not None:
                self._complete_evidence_stage(
                    started_stage,
                    stage,
                    timestamp,
                )
            elif not initial_stage_marker:
                self._append_evidence(
                    stage,
                    timestamp,
                    stage_ended_at=(
                        timestamp
                        if stage
                        == BattleReopenStage.SHORTCUT_LAUNCH_PREPARED.value
                        else None
                    ),
                )

        if self.current_stage == BattleReopenStage.SHORTCUT_LAUNCH_RETURNED.value:
            stopped = self.worker.terminate_and_wait()
            if not stopped:
                return self._failure(
                    "battle_reopen_worker_unreaped",
                    now,
                    hard_timeout=False,
                )
            self.worker.cleanup()
            self.current_stage = BattleReopenStage.WAITING_NEW_INSTANCE.value
            self._append_evidence(self.current_stage, self.wall_clock())
            return self._result(success=True)

        if not enforce_limits:
            return self._result(pending=True)
        if not self.worker.is_running():
            return self._failure(
                "battle_reopen_worker_exited",
                now,
                hard_timeout=False,
            )
        timeout = phase_timeouts.get(self.current_stage)
        if timeout is not None and now - self.stage_started_monotonic >= timeout:
            timeout_codes = {
                BattleReopenStage.FIRST_ABSENCE_STARTED.value: "battle_first_absence_hard_timeout",
                BattleReopenStage.SHORTCUT_FINGERPRINT_STARTED.value: "battle_shortcut_fingerprint_hard_timeout",
                BattleReopenStage.SECOND_ABSENCE_STARTED.value: "battle_second_absence_hard_timeout",
                BattleReopenStage.SHORTCUT_LAUNCH_PREPARED.value: "battle_final_absence_hard_timeout",
                BattleReopenStage.SHORTCUT_LAUNCH_ENTERED.value: "battle_shortcut_launch_hard_timeout",
            }
            return self._failure(
                timeout_codes.get(
                    self.current_stage,
                    "battle_reopen_stage_hard_timeout",
                ),
                now,
                hard_timeout=True,
            )
        return self._result(pending=True)

    def mark_new_instance(
        self,
        now: float,
        *,
        phase_timeouts: Mapping[str, float],
    ) -> BattleRestartResult:
        # A unique replacement can race the controller's preceding poll.  Take
        # one final non-blocking read of the authenticated stream before the
        # worker is stopped so an already-flushed launch-entered/returned event
        # is retained.  The replacement itself proves Windows received a
        # launch, even when launch-entered was the unread final record; it does
        # not prove (and must not synthesize) launch-returned.
        retrying_unreaped = bool(
            self.terminal_result is not None
            and self.terminal_result.failure_code
            == "battle_reopen_worker_unreaped"
        )
        if (
            not retrying_unreaped
            and self.terminal_result is None
            and self.current_stage
            != BattleReopenStage.WAITING_NEW_INSTANCE.value
        ):
            self.poll(
                now,
                phase_timeouts=phase_timeouts,
                enforce_limits=False,
            )
        self.delivery_boundary_crossed = True
        stopped = self.worker.terminate_and_wait()
        if not stopped:
            if retrying_unreaped:
                return self.terminal_result  # type: ignore[return-value]
            failed_at = self.wall_clock()
            self._close_last_open_evidence(
                failed_at,
                failure_reason="battle_reopen_worker_unreaped",
            )
            self.current_stage = BattleReopenStage.FAILED.value
            self._append_evidence(
                self.current_stage,
                failed_at,
                stage_ended_at=failed_at,
                failure_reason="battle_reopen_worker_unreaped",
                retry_allowed=False,
                wait_new_instance_only=True,
            )
            result = self._result(
                failure_code="battle_reopen_worker_unreaped",
                retry_allowed=False,
            )
            self.terminal_result = result
            return result
        self.worker.cleanup()
        self.terminal_result = None
        observed_at = self.wall_clock()
        self._close_last_open_evidence(observed_at)
        self.current_stage = BattleReopenStage.NEW_INSTANCE_APPEARED.value
        self._append_evidence(
            self.current_stage,
            observed_at,
            stage_ended_at=observed_at,
        )
        return self._result(success=True)

    def cancel(self) -> bool:
        stopped = self.worker.terminate_and_wait()
        if stopped:
            self.worker.cleanup()
        return stopped


class WindowsBattleWindowRestarter:
    """Fail closed unless the complete window instance and shortcut agree."""

    def __init__(
        self,
        window_backend: WindowBackend,
        close_backend: WindowCloseBackend,
        open_backend: ShortcutOpenBackend,
        *,
        title_keywords: Iterable[str] = ("Adobe Flash Player",),
        close_timeout_seconds: float = 10.0,
        poll_seconds: float = 0.1,
        absence_stability_seconds: float = 1.0,
        reopen_enumeration_timeout_seconds: float = 5.0,
        reopen_fingerprint_timeout_seconds: float = 12.0,
        reopen_launch_timeout_seconds: float = 8.0,
        monotonic_clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], float] = time.time,
        reopen_worker_factory: (
            Callable[[Mapping[str, object]], BattleReopenWorker] | None
        ) = None,
    ) -> None:
        if (
            close_timeout_seconds <= 0
            or poll_seconds <= 0
            or absence_stability_seconds <= 0
            or reopen_enumeration_timeout_seconds <= 0
            or reopen_fingerprint_timeout_seconds <= 0
            or reopen_launch_timeout_seconds <= 0
        ):
            raise ValueError("timeouts must be positive.")
        self._title_keywords = tuple(
            keyword.strip().casefold()
            for keyword in title_keywords
            if isinstance(keyword, str) and keyword.strip()
        )
        if not self._title_keywords:
            raise ValueError("title_keywords must not be empty.")
        self._window_backend = window_backend
        self._close_backend = close_backend
        self._open_backend = open_backend
        self._close_timeout_seconds = float(close_timeout_seconds)
        self._poll_seconds = float(poll_seconds)
        self._absence_stability_seconds = float(
            absence_stability_seconds
        )
        self._reopen_phase_timeouts = {
            BattleReopenStage.FIRST_ABSENCE_STARTED.value: float(
                reopen_enumeration_timeout_seconds
            ),
            BattleReopenStage.SHORTCUT_FINGERPRINT_STARTED.value: float(
                reopen_fingerprint_timeout_seconds
            ),
            BattleReopenStage.SECOND_ABSENCE_STARTED.value: float(
                reopen_enumeration_timeout_seconds
            ),
            BattleReopenStage.SHORTCUT_LAUNCH_PREPARED.value: float(
                reopen_enumeration_timeout_seconds
            ),
            BattleReopenStage.SHORTCUT_LAUNCH_ENTERED.value: float(
                reopen_launch_timeout_seconds
            ),
        }
        self._monotonic_clock = monotonic_clock
        self._sleeper = sleeper
        self._wall_clock = wall_clock
        self._reopen_worker_factory = (
            reopen_worker_factory or _PowerShellBattleReopenWorker
        )
        self._active_reopen_job: _BoundedBattleReopenJob | None = None
        self._reopen_evidence_history: tuple[
            BattleReopenStageEvidence,
            ...,
        ] = ()
        self._reopen_evidence_event_key: (
            tuple[str, str, tuple[object, ...], str] | None
        ) = None

    @property
    def reopen_stage_evidence(self) -> tuple[BattleReopenStageEvidence, ...]:
        active = self._active_reopen_job
        return self._reopen_evidence_history + (
            active.evidence if active is not None else ()
        )

    def _remember_and_clear_reopen_job(
        self,
        job: _BoundedBattleReopenJob,
    ) -> None:
        if self._active_reopen_job is not job:
            return
        self._reopen_evidence_history += job.evidence
        self._active_reopen_job = None

    def _bounded_reopen_result(
        self,
        job: _BoundedBattleReopenJob,
        result: BattleRestartResult,
    ) -> BattleRestartResult:
        if (
            not result.pending
            and result.failure_code is not None
            and result.retry_allowed
        ):
            self._remember_and_clear_reopen_job(job)
        evidence = self.reopen_stage_evidence
        return (
            result
            if result.stage_evidence == evidence
            else replace(result, stage_evidence=evidence)
        )

    def begin_bounded_reopen(
        self,
        *,
        owner: str,
        entry_id: str,
        original_instance: tuple[object, ...],
        target: GroupLaunchTarget,
        candidate_windows: Iterable[WindowInfo],
        deadline: float | None,
    ) -> BattleRestartResult:
        """Start or poll the only non-blocking reopen job for one owner."""

        if (
            normalize_launch_fingerprint(owner) != target.fingerprint
            or entry_id != target.entry_id
            or not original_instance
        ):
            return BattleRestartResult(
                False,
                "battle_reopen_owner_identity_invalid",
            )
        active = self._active_reopen_job
        if active is not None:
            if not active.matches(
                owner,
                entry_id,
                tuple(original_instance),
                target.shortcut_path,
            ):
                return BattleRestartResult(
                    False,
                    "battle_reopen_job_conflict",
                    wait_new_instance_only=True,
                )
            return self.poll_bounded_reopen(
                owner=owner,
                entry_id=entry_id,
                deadline=deadline,
            )
        event_key = (
            owner,
            entry_id,
            tuple(original_instance),
            _normalized_shortcut_path(target.shortcut_path),
        )
        if self._reopen_evidence_event_key != event_key:
            self._reopen_evidence_history = ()
            self._reopen_evidence_event_key = event_key
        try:
            candidates = tuple(candidate_windows)
        except Exception:
            return BattleRestartResult(
                False,
                "battle_window_enumeration_failed",
                retry_allowed=True,
            )
        failure_code = self._missing_target_failure(
            target,
            candidates,
            candidates,
        )
        if failure_code is not None:
            return BattleRestartResult(
                False,
                failure_code,
                retry_allowed=True,
            )
        identities = tuple(
            self._window_instance_identity(window) for window in candidates
        )
        if any(identity is None for identity in identities):
            return BattleRestartResult(
                False,
                "battle_window_existing_state_unknown",
                retry_allowed=True,
            )
        now = self._monotonic_clock()
        if deadline is not None and now >= deadline:
            return BattleRestartResult(
                False,
                "tcp_reconnect_timeout",
                hard_timeout=True,
            )
        payload: Mapping[str, object] = {
            "fingerprint": target.fingerprint,
            "shortcut_path": str(target.shortcut_path),
            "title_keywords": self._title_keywords,
            "absence_stability_seconds": self._absence_stability_seconds,
            "poll_seconds": self._poll_seconds,
            "expected_identity_keys": tuple(
                sorted(
                    _reopen_identity_key(identity)
                    for identity in identities
                    if identity is not None
                )
            ),
        }
        try:
            worker = self._reopen_worker_factory(payload)
        except Exception:
            return BattleRestartResult(
                False,
                "battle_reopen_worker_start_failed",
                retry_allowed=True,
            )
        job = _BoundedBattleReopenJob(
            owner=owner,
            entry_id=entry_id,
            fingerprint=target.fingerprint,
            original_instance=tuple(original_instance),
            original_shortcut=str(target.shortcut_path),
            worker=worker,
            deadline=deadline,
            stage_started_monotonic=now,
            wall_clock=self._wall_clock,
        )
        job._append_evidence(
            BattleReopenStage.FIRST_ABSENCE_STARTED.value,
            self._wall_clock(),
        )
        self._active_reopen_job = job
        result = job.poll(
            now,
            phase_timeouts=self._reopen_phase_timeouts,
        )
        return self._bounded_reopen_result(job, result)

    def poll_bounded_reopen(
        self,
        *,
        owner: str,
        entry_id: str,
        deadline: float | None,
    ) -> BattleRestartResult:
        job = self._active_reopen_job
        if job is None or not job.owned_by(owner, entry_id):
            return BattleRestartResult(
                False,
                "battle_reopen_job_missing",
            )
        if deadline != job.deadline:
            return BattleRestartResult(
                False,
                "battle_reopen_deadline_changed",
                stage=job.current_stage,
                wait_new_instance_only=True,
                stage_evidence=job.evidence,
            )
        result = job.poll(
            self._monotonic_clock(),
            phase_timeouts=self._reopen_phase_timeouts,
        )
        return self._bounded_reopen_result(job, result)

    def authorize_bounded_reopen(
        self,
        *,
        owner: str,
        entry_id: str,
    ) -> BattleRestartResult:
        job = self._active_reopen_job
        if job is None or not job.owned_by(owner, entry_id):
            return BattleRestartResult(
                False,
                "battle_reopen_job_missing",
            )
        now = self._monotonic_clock()
        if job.deadline is not None and now >= job.deadline:
            return self._bounded_reopen_result(
                job,
                job._failure(
                    "tcp_reconnect_timeout",
                    now,
                    hard_timeout=True,
                    deadline_expired=True,
                ),
            )
        return self._bounded_reopen_result(
            job,
            job.authorize_launch(),
        )

    def complete_bounded_reopen(
        self,
        *,
        owner: str,
        entry_id: str,
    ) -> BattleRestartResult:
        job = self._active_reopen_job
        if job is None or not job.owned_by(owner, entry_id):
            return BattleRestartResult(
                False,
                "battle_reopen_job_missing",
            )
        result = job.mark_new_instance(
            self._monotonic_clock(),
            phase_timeouts=self._reopen_phase_timeouts,
        )
        if result.failure_code == "battle_reopen_worker_unreaped":
            return self._bounded_reopen_result(job, result)
        self._remember_and_clear_reopen_job(job)
        return replace(result, stage_evidence=self.reopen_stage_evidence)

    def cancel_bounded_reopen(
        self,
        *,
        owner: str,
        entry_id: str,
    ) -> bool:
        job = self._active_reopen_job
        if job is None:
            return True
        if not job.owned_by(owner, entry_id):
            return False
        stopped = job.cancel()
        if stopped:
            self._remember_and_clear_reopen_job(job)
        return stopped

    def close_verified(
        self,
        window: WindowInfo,
        candidate_windows: Iterable[WindowInfo],
        *,
        deadline: float | None = None,
    ) -> BattleRestartResult:
        """Close one exact member of a static, current contract collection.

        The controller owns the semantic before/after contract transition.  At
        this native boundary we only accept the exact complete collection it
        already resolved, then re-enumerate immediately before WM_CLOSE.
        """

        expected_identity = self._window_instance_identity(window)
        if expected_identity is None:
            return BattleRestartResult(False, "battle_window_identity_invalid")
        try:
            expected_candidates = tuple(candidate_windows)
        except Exception:
            return BattleRestartResult(False, "battle_window_enumeration_failed")
        if self._candidate_collection_failure(
            expected_candidates,
            expected_candidates,
        ) is not None:
            return BattleRestartResult(False, "battle_contract_identity_changed")
        if not self._deadline_current(deadline):
            return BattleRestartResult(False, "tcp_reconnect_timeout")
        try:
            candidates = self._live_candidate_windows()
        except Exception:
            return BattleRestartResult(False, "battle_window_enumeration_failed")
        failure_code = self._candidate_collection_failure(
            candidates,
            expected_candidates,
        )
        if failure_code is not None:
            return BattleRestartResult(False, failure_code)
        if not self._identity_occurs_once(expected_identity, candidates):
            return BattleRestartResult(False, "battle_window_identity_changed")
        if not self._close_backend.is_window(window.handle):
            return BattleRestartResult(False, "battle_window_missing")
        try:
            final_candidates = self._live_candidate_windows()
        except Exception:
            return BattleRestartResult(False, "battle_window_enumeration_failed")
        failure_code = self._candidate_collection_failure(
            final_candidates,
            expected_candidates,
        )
        if failure_code is not None:
            return BattleRestartResult(False, failure_code)
        if not self._identity_occurs_once(expected_identity, final_candidates):
            return BattleRestartResult(False, "battle_window_identity_changed")
        if not self._deadline_current(deadline):
            return BattleRestartResult(False, "tcp_reconnect_timeout")
        try:
            closed, close_failure = (
                self._close_backend.close_window_if_instance_matches(
                    window.handle,
                    expected_identity,
                    lambda handle: self._current_window_instance_identity(
                        handle,
                        expected_candidates,
                    ),
                )
            )
        except Exception:
            closed = False
            close_failure = "battle_window_close_failed"
        if not closed:
            return BattleRestartResult(
                False,
                close_failure or "battle_window_close_failed",
            )

        close_deadline = self._monotonic_clock() + self._close_timeout_seconds
        while self._close_backend.is_window(window.handle):
            if not self._deadline_current(deadline):
                return BattleRestartResult(
                    False,
                    "tcp_reconnect_timeout",
                    window_closed=True,
                )
            if self._monotonic_clock() >= close_deadline:
                return BattleRestartResult(
                    False,
                    "battle_window_close_timeout",
                    window_closed=True,
                )
            self._sleeper(self._poll_seconds)
        return BattleRestartResult(True, window_closed=True)

    @staticmethod
    def _window_instance_identity(
        window: WindowInfo,
    ) -> WindowInstanceIdentity | None:
        return complete_window_instance_identity(window)

    def _deadline_current(self, deadline: float | None) -> bool:
        return deadline is None or self._monotonic_clock() < deadline

    @classmethod
    def _identity_occurs_once(
        cls,
        identity: WindowInstanceIdentity,
        candidates: Iterable[WindowInfo],
    ) -> bool:
        return sum(
            cls._window_instance_identity(candidate) == identity
            for candidate in candidates
        ) == 1

    @classmethod
    def _candidate_collection_failure(
        cls,
        candidates: tuple[WindowInfo, ...],
        expected_candidates: tuple[WindowInfo, ...],
    ) -> str | None:
        """Require a full immutable collection, not a fingerprint allowlist."""

        actual_identities = tuple(
            cls._window_instance_identity(candidate) for candidate in candidates
        )
        expected_identities = tuple(
            cls._window_instance_identity(candidate)
            for candidate in expected_candidates
        )
        if (
            any(identity is None for identity in actual_identities)
            or any(identity is None for identity in expected_identities)
        ):
            return "battle_window_existing_state_unknown"
        actual = tuple(
            identity for identity in actual_identities if identity is not None
        )
        expected = tuple(
            identity
            for identity in expected_identities
            if identity is not None
        )
        for identities in (actual, expected):
            handles = tuple(identity[1] for identity in identities)
            process_ids = tuple(identity[2] for identity in identities)
            stable_tokens = tuple(identity[:6] for identity in identities)
            if (
                len(handles) != len(set(handles))
                or len(process_ids) != len(set(process_ids))
                or len(stable_tokens) != len(set(stable_tokens))
            ):
                return "battle_window_identity_duplicate"
        if Counter(actual) != Counter(expected):
            return "battle_contract_identity_changed"
        return None

    def _current_window_instance_identity(
        self,
        handle: int,
        expected_candidates: tuple[WindowInfo, ...],
    ) -> WindowInstanceIdentity | None:
        try:
            candidates = self._live_candidate_windows()
        except Exception:
            return None
        if self._candidate_collection_failure(
            candidates,
            expected_candidates,
        ) is not None:
            return None
        exact = tuple(
            candidate
            for candidate in candidates
            if candidate.handle == handle
        )
        if len(exact) != 1:
            return None
        return self._window_instance_identity(exact[0])

    @staticmethod
    def _missing_target_failure(
        target: GroupLaunchTarget,
        candidates: tuple[WindowInfo, ...],
        expected_candidates: tuple[WindowInfo, ...],
    ) -> str | None:
        collection_failure = (
            WindowsBattleWindowRestarter._candidate_collection_failure(
                candidates,
                expected_candidates,
            )
        )
        if collection_failure is not None:
            return collection_failure
        fingerprints = tuple(
            WindowsBattleWindowRestarter._window_instance_identity(window)[0]
            for window in candidates
        )
        if target.fingerprint in fingerprints:
            return "battle_window_already_exists"
        return None

    def _live_candidate_windows(self) -> tuple[WindowInfo, ...]:
        return tuple(
            window
            for window in self._window_backend.list_windows()
            if all(
                keyword in window.title.casefold()
                for keyword in self._title_keywords
            )
        )

    def _live_target_failure(
        self,
        target: GroupLaunchTarget,
        expected_candidates: tuple[WindowInfo, ...],
        deadline: float | None,
    ) -> str | None:
        if not self._deadline_current(deadline):
            return "tcp_reconnect_timeout"
        try:
            candidates = self._live_candidate_windows()
        except Exception:
            return "battle_window_enumeration_failed"
        return self._missing_target_failure(
            target,
            candidates,
            expected_candidates,
        )

    def _stable_target_absence_failure(
        self,
        target: GroupLaunchTarget,
        expected_candidates: tuple[WindowInfo, ...],
        owner_deadline: float | None,
    ) -> str | None:
        stability_deadline = (
            self._monotonic_clock() + self._absence_stability_seconds
        )
        maximum_checks = max(
            2,
            int(
                self._absence_stability_seconds
                / self._poll_seconds
            )
            + 3,
        )
        for _check in range(maximum_checks):
            failure_code = self._live_target_failure(
                target,
                expected_candidates,
                owner_deadline,
            )
            if failure_code is not None:
                return failure_code
            remaining = stability_deadline - self._monotonic_clock()
            if remaining <= 0:
                return None
            self._sleeper(min(self._poll_seconds, remaining))
        # A stalled or invalid monotonic clock must fail closed rather than
        # turning the stability confirmation into an unbounded loop.
        return "battle_window_absence_unconfirmed"

    def reopen_missing(
        self,
        target: GroupLaunchTarget,
        candidate_windows: Iterable[WindowInfo],
        *,
        deadline: float | None = None,
    ) -> BattleRestartResult:
        """Retry one shortcut only after a fresh fail-closed enumeration."""
        try:
            candidates = tuple(candidate_windows)
        except Exception:
            return BattleRestartResult(
                False,
                "battle_window_enumeration_failed",
            )
        if not self._deadline_current(deadline):
            return BattleRestartResult(False, "tcp_reconnect_timeout")
        failure_code = self._missing_target_failure(target, candidates, candidates)
        if failure_code is not None:
            return BattleRestartResult(
                False,
                failure_code,
            )

        # The caller holds the shared exclusive game-operation lease for this
        # whole method. A bounded stable-absence window catches delayed
        # self-reopens; the opener performs one more check at delivery.
        failure_code = self._stable_target_absence_failure(
            target,
            candidates,
            deadline,
        )
        if failure_code is not None:
            return BattleRestartResult(False, failure_code)

        try:
            opened, open_failure = (
                self._open_backend.open_shortcut_if_target_absent(
                    target,
                    lambda: self._live_target_failure(
                        target,
                        candidates,
                        deadline,
                    ),
                )
            )
        except Exception:
            opened = False
            open_failure = "battle_shortcut_open_failed"
        if not opened:
            return BattleRestartResult(
                False,
                open_failure or "battle_shortcut_open_failed",
            )
        return BattleRestartResult(
            True,
            shortcut_open_requested=True,
        )
