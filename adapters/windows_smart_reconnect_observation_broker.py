"""Process-isolated, non-mutating observations for smart reconnect."""

from __future__ import annotations

import ctypes
import base64
import hashlib
import json
import math
import multiprocessing
import os
import socket
import subprocess
import time
from dataclasses import dataclass, field, replace
from ctypes import wintypes
from multiprocessing.connection import Connection, wait
from pathlib import Path
from threading import Lock, RLock
from typing import Callable, Iterable, TypeVar

from adapters.game_screen_recognizer import (
    ReferenceScreenRecognizer,
    ScreenRecognition,
)
from adapters.windows_background_capture import (
    CaptureSample,
    Win32PrintWindowProvider,
    Win32VisibleRegionCaptureProvider,
)
from adapters.windows_launch_fingerprint import (
    PowerShellShortcutFingerprintResolver,
    normalize_launch_fingerprint,
)
from adapters.windows_shortcut_seal import Win32ShortcutFileIdentityProvider
from adapters.windows_window import WindowInfo, Win32WindowBackend
from core.reconnect_policy import ReconnectScreenState
from core.smart_reconnect_authorization import ShortcutSeal
from core.target_window_contract import (
    ObservationActionLease,
    ObservationFreshness,
    ProcessObservationCacheKey,
    RoleObservationCacheKey,
    ShortcutObservationCacheKey,
)
from core.window_instance import WindowInstanceToken
from adapters.windows_role_id_ocr import WindowsRoleIdOcrReader
from services.role_id_template_service import (
    clean_role_id_text,
    role_id_region_sample,
)


ENUMERATION_TIMEOUT_SECONDS = 3.0
WINDOW_TIMEOUT_SECONDS = 3.0
MAX_PARALLEL_WINDOWS = 4
ACTION_LEASE_SECONDS = 30.0
WORKER_STOP_SECONDS = 0.5
_SHORTCUT_ARGUMENTS_MISSING = "shortcut_launch_arguments_missing"
_AF_INET = 2
_TCP_TABLE_OWNER_PID_ALL = 5
_MIB_TCP_STATE_ESTAB = 5
_ERROR_INSUFFICIENT_BUFFER = 122

_RESULT = TypeVar("_RESULT")


def _unknown_recognition() -> ScreenRecognition:
    return ScreenRecognition(
        state=ReconnectScreenState.UNKNOWN,
        score=None,
        click_point=None,
        reference_name=None,
    )


def _capture_sha256(sample: CaptureSample | None) -> str | None:
    if sample is None or not sample.api_succeeded:
        return None
    digest = hashlib.sha256()
    digest.update(sample.width.to_bytes(8, "little", signed=True))
    digest.update(sample.height.to_bytes(8, "little", signed=True))
    digest.update(sample.pixels)
    return digest.hexdigest()


class _MibTcpRowOwnerPid(ctypes.Structure):
    _fields_ = (
        ("state", wintypes.DWORD),
        ("local_address", wintypes.DWORD),
        ("local_port", wintypes.DWORD),
        ("remote_address", wintypes.DWORD),
        ("remote_port", wintypes.DWORD),
        ("owning_process_id", wintypes.DWORD),
    )


def _is_external_ipv4_remote_address(remote_address: int) -> bool:
    host_order = socket.ntohl(int(remote_address))
    return host_order != 0 and (host_order >> 24) != 127


def _ipv4_established_counts_by_pid(
    process_ids: Iterable[int],
) -> dict[int, int]:
    """Read one IPv4 owner-PID table and return only aggregate PID counts."""

    requested = frozenset(
        value
        for value in process_ids
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    )
    if not requested:
        return {}
    if os.name != "nt":
        raise OSError("IPv4 TCP owner table is available only on Windows")
    system_root = os.environ.get("SystemRoot")
    if not system_root:
        raise OSError("Windows system root is unavailable")
    dll_path = os.path.join(system_root, "System32", "iphlpapi.dll")
    if not os.path.isabs(dll_path):
        raise OSError("Windows system directory is not absolute")
    iphlpapi = ctypes.WinDLL(
        dll_path,
        use_last_error=True,
    )
    get_table = iphlpapi.GetExtendedTcpTable
    get_table.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.ULONG),
        wintypes.BOOL,
        wintypes.ULONG,
        ctypes.c_int,
        wintypes.ULONG,
    )
    get_table.restype = wintypes.DWORD
    size = wintypes.ULONG(0)
    status = get_table(
        None,
        ctypes.byref(size),
        False,
        _AF_INET,
        _TCP_TABLE_OWNER_PID_ALL,
        0,
    )
    if status not in (0, _ERROR_INSUFFICIENT_BUFFER) or size.value < 4:
        raise OSError(int(status), "GetExtendedTcpTable size query failed")
    for _attempt in range(3):
        buffer = (ctypes.c_ubyte * size.value)()
        status = get_table(
            buffer,
            ctypes.byref(size),
            False,
            _AF_INET,
            _TCP_TABLE_OWNER_PID_ALL,
            0,
        )
        if status == _ERROR_INSUFFICIENT_BUFFER:
            continue
        if status != 0:
            raise OSError(int(status), "GetExtendedTcpTable failed")
        entry_count = ctypes.cast(
            buffer,
            ctypes.POINTER(wintypes.DWORD),
        ).contents.value
        row_size = ctypes.sizeof(_MibTcpRowOwnerPid)
        first_row = ctypes.sizeof(wintypes.DWORD)
        if first_row + entry_count * row_size > len(buffer):
            raise OSError("GetExtendedTcpTable returned a truncated table")
        counts = {process_id: 0 for process_id in requested}
        for index in range(entry_count):
            row = _MibTcpRowOwnerPid.from_buffer_copy(
                buffer,
                first_row + index * row_size,
            )
            process_id = int(row.owning_process_id)
            if (
                process_id in requested
                and int(row.state) == _MIB_TCP_STATE_ESTAB
                and _is_external_ipv4_remote_address(row.remote_address)
            ):
                counts[process_id] += 1
        return counts
    raise OSError(_ERROR_INSUFFICIENT_BUFFER, "TCP table kept changing size")


@dataclass(frozen=True, slots=True)
class SmartReconnectShortcutObservation:
    path: str
    fingerprint: str | None
    seal: ShortcutSeal | None
    failure_codes: tuple[str, ...] = ()
    cache_key: ShortcutObservationCacheKey | None = None


@dataclass(frozen=True, slots=True)
class SmartReconnectWindowObservation:
    window: WindowInfo
    instance: WindowInstanceToken | None
    sample: CaptureSample | None
    recognition: ScreenRecognition
    fresh_capture: bool
    capture_route: str | None
    role_id: str | None
    failure_codes: tuple[str, ...] = ()
    freshness: ObservationFreshness | None = None
    process_cache_key: ProcessObservationCacheKey | None = None
    role_cache_key: RoleObservationCacheKey | None = None
    role_region_sha256: str | None = None
    tcp_established_connections: int | None = None
    tcp_failure_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        freshness = self.freshness
        if freshness is None:
            freshness = (
                ObservationFreshness.PROVEN_CURRENT
                if self.fresh_capture
                else ObservationFreshness.UNPROVEN
            )
            object.__setattr__(self, "freshness", freshness)
        if not isinstance(freshness, ObservationFreshness):
            raise TypeError("window observation freshness is invalid")
        if self.fresh_capture is not (
            freshness is ObservationFreshness.PROVEN_CURRENT
        ):
            raise ValueError("window freshness flags disagree")
        if self.role_region_sha256 is not None:
            role_region_sha256 = normalize_launch_fingerprint(
                self.role_region_sha256
            )
            if role_region_sha256 is None:
                raise ValueError("window role-region SHA-256 is invalid")
            object.__setattr__(
                self,
                "role_region_sha256",
                role_region_sha256,
            )
        if (
            self.tcp_established_connections is not None
            and (
                isinstance(self.tcp_established_connections, bool)
                or not isinstance(self.tcp_established_connections, int)
                or self.tcp_established_connections < 0
            )
        ):
            raise ValueError("TCP established connection count is invalid")


@dataclass(frozen=True, slots=True)
class SmartReconnectTcpWindowObservation:
    instance: WindowInstanceToken
    established_connections: int | None
    failure_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.instance, WindowInstanceToken):
            raise TypeError("TCP observation requires a complete window instance")
        if (
            self.established_connections is not None
            and (
                isinstance(self.established_connections, bool)
                or not isinstance(self.established_connections, int)
                or self.established_connections < 0
            )
        ):
            raise ValueError("TCP established connection count is invalid")


@dataclass(frozen=True, slots=True)
class SmartReconnectTcpBatchResult:
    observations: tuple[SmartReconnectTcpWindowObservation, ...]
    failure_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        observations = tuple(self.observations)
        if any(
            not isinstance(item, SmartReconnectTcpWindowObservation)
            for item in observations
        ):
            raise TypeError("TCP batch contains an invalid observation")
        instances = tuple(item.instance for item in observations)
        if len(instances) != len(set(instances)):
            raise ValueError("TCP batch contains a duplicate window instance")
        object.__setattr__(self, "observations", observations)


@dataclass(frozen=True, slots=True)
class SmartReconnectEnumerationResult:
    windows: tuple[WindowInfo, ...]
    shortcuts: tuple[SmartReconnectShortcutObservation, ...]
    failure_codes: tuple[str, ...] = ()
    foreground_handle: int | None = None


@dataclass(frozen=True, slots=True)
class SmartReconnectObservationSnapshot:
    generation: int
    request_serial: int = 0
    published_at_monotonic: float = 0.0
    action_deadline_monotonic: float = 0.0
    static_generation: int = 0
    changed_fingerprints: frozenset[str] = frozenset()
    windows: tuple[SmartReconnectWindowObservation, ...] = ()
    shortcuts: tuple[SmartReconnectShortcutObservation, ...] = ()
    blocked_fingerprints: frozenset[str] = frozenset()
    isolated_window_count: int = 0
    anonymous_isolated_window_count: int = 0
    failure_codes: tuple[str, ...] = ()
    foreground_handle: int | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise ValueError("observation generation must be non-negative")
        if self.isolated_window_count < 0:
            raise ValueError("isolated window count must be non-negative")
        if (
            self.anonymous_isolated_window_count < 0
            or self.anonymous_isolated_window_count
            > self.isolated_window_count
        ):
            raise ValueError("anonymous isolation count is invalid")
        for value, field_name in (
            (self.request_serial, "request serial"),
            (self.static_generation, "static generation"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"observation {field_name} must be non-negative")
        for value, field_name in (
            (self.published_at_monotonic, "published time"),
            (self.action_deadline_monotonic, "action deadline"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value < 0
            ):
                raise ValueError(f"observation {field_name} must be non-negative")
        object.__setattr__(
            self,
            "changed_fingerprints",
            frozenset(
                fingerprint
                for value in self.changed_fingerprints
                if (fingerprint := normalize_launch_fingerprint(value)) is not None
            ),
        )

    def window_for(
        self,
        fingerprint: str,
    ) -> SmartReconnectWindowObservation | None:
        normalized = normalize_launch_fingerprint(fingerprint)
        matches = tuple(
            item
            for item in self.windows
            if normalize_launch_fingerprint(item.window.launch_fingerprint)
            == normalized
        )
        return matches[0] if normalized is not None and len(matches) == 1 else None

    def shortcut_for(
        self,
        fingerprint: str,
    ) -> SmartReconnectShortcutObservation | None:
        normalized = normalize_launch_fingerprint(fingerprint)
        matches = tuple(
            item for item in self.shortcuts if item.fingerprint == normalized
        )
        return matches[0] if normalized is not None and len(matches) == 1 else None


@dataclass(frozen=True, slots=True)
class SmartReconnectObservationRequest:
    stage: str
    reference_dir: str
    title_keywords: tuple[str, ...]
    shortcut_paths: tuple[str, ...] = ()
    shortcut_roots: tuple[str, ...] = ()
    window: WindowInfo | None = None
    windows: tuple[WindowInfo, ...] = ()
    cached_role_id: str | None = None
    role_cache_hit: bool = False
    role_cache_key: RoleObservationCacheKey | None = None
    visible_capture_enabled: bool = True
    obscured_capture_enabled: bool = True
    minimized_capture_enabled: bool = True
    expected_seal: ShortcutSeal | None = None
    shortcut_static_observations: tuple[
        SmartReconnectShortcutObservation,
        ...,
    ] = ()


@dataclass(frozen=True, slots=True)
class SmartReconnectSealWitness:
    generation: int
    request_serial: int
    expected_seal: ShortcutSeal


@dataclass(frozen=True, slots=True)
class SmartReconnectObservationJob:
    request_serial: int
    job_serial: int
    worker_epoch: int
    kind: str
    deadline_monotonic: float
    request: SmartReconnectObservationRequest


@dataclass(frozen=True, slots=True)
class _WorkerReply:
    request_serial: int
    job_serial: int
    worker_epoch: int
    succeeded: bool
    payload: object | None


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _shortcut_cache_key(path: Path) -> ShortcutObservationCacheKey | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    try:
        identity = Win32ShortcutFileIdentityProvider().identity_for(path)
        return ShortcutObservationCacheKey(
            normalized_path=_normalized_path(path),
            file_identity=identity,
            modified_ns=stat.st_mtime_ns,
            size=stat.st_size,
            content_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    except (OSError, TypeError, ValueError):
        return None


def _system_powershell_path() -> str:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    return os.path.join(
        system_root,
        "System32",
        "WindowsPowerShell",
        "v1.0",
        "powershell.exe",
    )


def _standard_powershell_environment(
    source: dict[str, str] | None = None,
) -> dict[str, str]:
    environment = dict(source or os.environ)
    system_root = environment.get("SystemRoot", r"C:\Windows")
    program_files = environment.get("ProgramFiles", r"C:\Program Files")
    environment["PSModulePath"] = os.pathsep.join(
        (
            os.path.join(
                system_root,
                "System32",
                "WindowsPowerShell",
                "v1.0",
                "Modules",
            ),
            os.path.join(program_files, "WindowsPowerShell", "Modules"),
        )
    )
    return environment


def _run_system_powershell(command, *args, **kwargs):
    command = list(command)
    if command:
        command[0] = _system_powershell_path()
    kwargs["env"] = _standard_powershell_environment(kwargs.get("env"))
    previous = _worker_dll_directory_reset()
    try:
        return subprocess.run(command, *args, **kwargs)
    finally:
        _worker_dll_directory_restore(previous)


def _discover_shortcut_paths(
    requested: Iterable[str],
    roots: Iterable[str],
) -> tuple[Path, ...]:
    candidates: dict[str, Path] = {}
    for value in requested:
        try:
            path = Path(value).resolve(strict=False)
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        candidates[_normalized_path(path)] = path
    for value in roots:
        try:
            root = Path(value).resolve(strict=False)
            if not root.is_dir():
                continue
            for path in root.glob("*.lnk"):
                candidates[_normalized_path(path)] = path
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
    return tuple(candidates[key] for key in sorted(candidates))


class _BoundedPowerShellShortcutFingerprintResolver(
    PowerShellShortcutFingerprintResolver
):
    """Resolve all shortcuts in one process with a deadline per item."""

    _SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$resolved = @{}
$abandoned = New-Object System.Collections.ArrayList
try {
    $encoded = [string]$env:FLASH_SHORTCUT_PATHS_B64
    $paths = @()
    if (-not [string]::IsNullOrWhiteSpace($encoded)) {
        $decodedPaths = (
            [Text.Encoding]::UTF8.GetString(
                [Convert]::FromBase64String($encoded)
            ) | ConvertFrom-Json
        )
        $paths = @($decodedPaths)
    }
    $itemScript = @'
param([int]$Index, [string]$Path)
$ErrorActionPreference = 'Stop'
if (
    [string]::IsNullOrWhiteSpace($Path) -or
    [IO.Path]::GetExtension($Path) -ine '.lnk' -or
    -not (Test-Path -LiteralPath $Path -PathType Leaf)
) { return }
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($Path)
$arguments = [string]$shortcut.Arguments
if ([string]::IsNullOrWhiteSpace($arguments)) {
    [pscustomobject]@{
        Key = [string]$Index
        EmptyArguments = $true
    }
    return
}
$sha256 = [Security.Cryptography.SHA256]::Create()
try {
    $digest = $sha256.ComputeHash(
        [Text.Encoding]::UTF8.GetBytes($arguments)
    )
    [pscustomobject]@{
        Key = [string]$Index
        Value = [BitConverter]::ToString($digest).Replace('-', '').ToLowerInvariant()
        EmptyArguments = $false
    }
} finally {
    $sha256.Dispose()
    }
'@
    $active = New-Object System.Collections.ArrayList
    $next = 0
    while ($next -lt $paths.Count -or $active.Count -gt 0) {
        # WScript.Shell is process-wide on Windows PowerShell 5.1. One
        # isolated runspace at a time prevents one item from releasing the
        # COM object while a sibling is still reading it.
        while ($next -lt $paths.Count -and $active.Count -lt 1) {
            $runspace = [RunspaceFactory]::CreateRunspace()
            $runspace.ApartmentState = [Threading.ApartmentState]::STA
            $runspace.ThreadOptions = [Management.Automation.Runspaces.PSThreadOptions]::UseNewThread
            $runspace.Open()
            $powerShell = [PowerShell]::Create()
            $powerShell.Runspace = $runspace
            [void]$powerShell.AddScript($itemScript).AddArgument($next).AddArgument($paths[$next])
            $async = $powerShell.BeginInvoke()
            [void]$active.Add([pscustomobject]@{
                PowerShell = $powerShell
                Runspace = $runspace
                Async = $async
                Deadline = [DateTime]::UtcNow.AddSeconds(3)
            })
            $next++
        }
        foreach ($task in @($active)) {
            if ($task.Async.IsCompleted) {
                try {
                    foreach ($item in @($task.PowerShell.EndInvoke($task.Async))) {
                        if ($null -eq $item -or $null -eq $item.Key) { continue }
                        $key = [string]$item.Key
                        if ([string]::IsNullOrWhiteSpace($key)) { continue }
                        if ($item.EmptyArguments -eq $true) {
                            $resolved[$key] = $null
                        } elseif ($item.Value) {
                            $resolved[$key] = [string]$item.Value
                        }
                    }
                } catch {
                    # Per-item failure stays absent without exposing arguments.
                } finally {
                    $task.PowerShell.Dispose()
                    $task.Runspace.Dispose()
                    [void]$active.Remove($task)
                }
            } elseif ([DateTime]::UtcNow -ge $task.Deadline) {
                try { [void]$task.PowerShell.BeginStop($null, $null) } catch {}
                [void]$abandoned.Add($task)
                [void]$active.Remove($task)
            }
        }
        if ($next -lt $paths.Count -or $active.Count -gt 0) {
            Start-Sleep -Milliseconds 10
        }
    }
} catch {
    $resolved = @{}
}
[Console]::Out.WriteLine(($resolved | ConvertTo-Json -Compress))
[Console]::Out.Flush()
if ($abandoned.Count -gt 0) { [Environment]::Exit(0) }
"""

    def resolve_with_terminal_negatives(
        self,
        shortcut_paths: Iterable[Path],
    ) -> tuple[dict[Path, str], frozenset[Path]]:
        """Return hashes plus paths proven to have no launch arguments."""

        paths = tuple(Path(path) for path in shortcut_paths)
        if os.name != "nt" or not paths:
            return {}, frozenset()
        encoded_paths = base64.b64encode(
            json.dumps(
                [str(path) for path in paths],
                ensure_ascii=False,
            ).encode("utf-8")
        ).decode("ascii")
        environment = os.environ.copy()
        environment["FLASH_SHORTCUT_PATHS_B64"] = encoded_paths
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = self._runner(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-EncodedCommand",
                    self._encoded_script(),
                ],
                capture_output=True,
                text=False,
                timeout=self._timeout_seconds,
                env=environment,
                creationflags=creation_flags,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return {}, frozenset()
        if completed.returncode != 0:
            return {}, frozenset()
        try:
            output = completed.stdout
            if isinstance(output, bytes):
                output = output.decode("utf-8-sig")
            raw = json.loads(output.strip() or "{}")
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
            return {}, frozenset()
        if not isinstance(raw, dict):
            return {}, frozenset()

        resolved: dict[Path, str] = {}
        terminal_negatives: set[Path] = set()
        for raw_index, value in raw.items():
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                continue
            if not 0 <= index < len(paths):
                continue
            if value is None:
                terminal_negatives.add(paths[index])
                continue
            fingerprint = normalize_launch_fingerprint(value)
            if fingerprint is not None:
                resolved[paths[index]] = fingerprint
        return resolved, frozenset(terminal_negatives)


def _shortcut_observations(
    paths: tuple[Path, ...],
) -> tuple[SmartReconnectShortcutObservation, ...]:
    if not paths:
        return ()
    resolver = _BoundedPowerShellShortcutFingerprintResolver(
        runner=_run_system_powershell,
        timeout_seconds=(
            WINDOW_TIMEOUT_SECONDS
            * (1 + math.ceil(len(paths) / MAX_PARALLEL_WINDOWS))
        ),
    )
    try:
        raw, terminal_negative_paths = (
            resolver.resolve_with_terminal_negatives(paths)
        )
    except _WorkerEnvironmentError:
        raise
    except Exception:
        raw = {}
        terminal_negative_paths = frozenset()
    normalized_raw: dict[str, str] = {}
    for path, fingerprint in raw.items():
        normalized = normalize_launch_fingerprint(fingerprint)
        if normalized is not None:
            normalized_raw[_normalized_path(Path(path))] = normalized
    normalized_terminal_negatives = frozenset(
        _normalized_path(Path(path)) for path in terminal_negative_paths
    )
    identity_provider = Win32ShortcutFileIdentityProvider()
    observations: list[SmartReconnectShortcutObservation] = []
    for path in paths:
        normalized_path = _normalized_path(path)
        fingerprint = normalized_raw.get(normalized_path)
        if normalized_path in normalized_terminal_negatives:
            observations.append(
                SmartReconnectShortcutObservation(
                    path=normalized_path,
                    fingerprint=None,
                    seal=None,
                    failure_codes=(_SHORTCUT_ARGUMENTS_MISSING,),
                    cache_key=_shortcut_cache_key(path),
                )
            )
            continue
        if fingerprint is None:
            observations.append(
                SmartReconnectShortcutObservation(
                    path=normalized_path,
                    fingerprint=None,
                    seal=None,
                    failure_codes=("shortcut_identity_unresolved",),
                    cache_key=_shortcut_cache_key(path),
                )
            )
            continue
        try:
            identity = identity_provider.identity_for(path)
            content_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            seal = ShortcutSeal(
                file_identity=identity,
                content_sha256=content_sha256,
                launch_fingerprint=fingerprint,
            )
        except Exception:
            observations.append(
                SmartReconnectShortcutObservation(
                    path=normalized_path,
                    fingerprint=fingerprint,
                    seal=None,
                    failure_codes=("shortcut_seal_unresolved",),
                    cache_key=_shortcut_cache_key(path),
                )
            )
            continue
        observations.append(
            SmartReconnectShortcutObservation(
                path=normalized_path,
                fingerprint=fingerprint,
                seal=seal,
                cache_key=_shortcut_cache_key(path),
            )
        )
    return tuple(observations)


def _shortcut_observations_from_static(
    static_observations: tuple[SmartReconnectShortcutObservation, ...],
) -> tuple[SmartReconnectShortcutObservation, ...]:
    """Resolve arguments only; reuse bounded per-path file evidence."""
    if not static_observations:
        return ()
    paths = tuple(Path(item.path) for item in static_observations)
    resolver = _BoundedPowerShellShortcutFingerprintResolver(
        runner=_run_system_powershell,
        timeout_seconds=(
            WINDOW_TIMEOUT_SECONDS
            * (1 + math.ceil(len(paths) / MAX_PARALLEL_WINDOWS))
        ),
    )
    try:
        raw, terminal_negative_paths = (
            resolver.resolve_with_terminal_negatives(paths)
        )
    except _WorkerEnvironmentError:
        raise
    except Exception:
        raw = {}
        terminal_negative_paths = frozenset()
    normalized_raw = {
        _normalized_path(Path(path)): normalized
        for path, fingerprint in raw.items()
        if (normalized := normalize_launch_fingerprint(fingerprint)) is not None
    }
    normalized_terminal_negatives = frozenset(
        _normalized_path(Path(path)) for path in terminal_negative_paths
    )
    results: list[SmartReconnectShortcutObservation] = []
    for static in static_observations:
        cache_key = static.cache_key
        fingerprint = normalized_raw.get(static.path)
        if cache_key is None:
            results.append(replace(
                static,
                fingerprint=None,
                seal=None,
                failure_codes=("shortcut_static_unresolved",),
            ))
            continue
        if static.path in normalized_terminal_negatives:
            results.append(replace(
                static,
                fingerprint=None,
                seal=None,
                failure_codes=(_SHORTCUT_ARGUMENTS_MISSING,),
            ))
            continue
        if fingerprint is None:
            results.append(replace(
                static,
                fingerprint=None,
                seal=None,
                failure_codes=("shortcut_identity_unresolved",),
            ))
            continue
        results.append(SmartReconnectShortcutObservation(
            path=static.path,
            fingerprint=fingerprint,
            seal=ShortcutSeal(
                file_identity=cache_key.file_identity,
                content_sha256=cache_key.content_sha256,
                launch_fingerprint=fingerprint,
            ),
            cache_key=cache_key,
        ))
    return tuple(results)


def _shortcut_observation_is_resolved(
    item: SmartReconnectShortcutObservation,
) -> bool:
    return (
        normalize_launch_fingerprint(item.fingerprint) is not None
        and item.seal is not None
        and not item.failure_codes
    )


def _shortcut_observation_is_terminal_negative(
    item: SmartReconnectShortcutObservation,
) -> bool:
    return (
        item.cache_key is not None
        and item.fingerprint is None
        and item.seal is None
        and item.failure_codes == (_SHORTCUT_ARGUMENTS_MISSING,)
    )


def _shortcut_observation_is_cacheable(
    item: SmartReconnectShortcutObservation,
) -> bool:
    return (
        _shortcut_observation_is_resolved(item)
        or _shortcut_observation_is_terminal_negative(item)
    )


class _ScopedPowerShellLaunchFingerprintResolver:
    """Resolve process identities from only the explicitly scoped shortcuts."""

    _SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$resolved = @{}
$abandoned = New-Object System.Collections.ArrayList
try {
    $pids = @(
        ([string]$env:FLASH_WINDOW_PIDS).Split(',') |
        ForEach-Object {
            $parsed = 0
            if ([int]::TryParse($_, [ref]$parsed) -and $parsed -gt 0) {
                $parsed
            }
        } | Sort-Object -Unique
    )
    $encoded = [string]$env:FLASH_SCOPED_SHORTCUT_PATHS_B64
    $itemScript = @'
param([int]$ProcessId, [string]$EncodedPaths)
$ErrorActionPreference = 'Stop'
$decodedPaths = (
    [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String($EncodedPaths)
    ) | ConvertFrom-Json
)
$paths = @($decodedPaths)
$shell = New-Object -ComObject WScript.Shell
$shortcuts = @()
foreach ($pathValue in $paths) {
    $path = [string]$pathValue
    if (
        [IO.Path]::GetExtension($path) -ine '.lnk' -or
        -not (Test-Path -LiteralPath $path -PathType Leaf)
    ) { continue }
    try {
        $item = $shell.CreateShortcut($path)
        $arguments = ([string]$item.Arguments).Trim()
        if (-not [string]::IsNullOrWhiteSpace($arguments)) {
            $shortcuts += [pscustomobject]@{
                Arguments = $arguments
                TargetPath = [string]$item.TargetPath
            }
        }
    } catch {}
}
$process = Get-CimInstance Win32_Process -Filter (
    "ProcessId = $ProcessId"
) -ErrorAction Stop
if ($null -eq $process) { return }
$commandLine = ([string]$process.CommandLine).TrimEnd()
$executablePath = [string]$process.ExecutablePath
$matches = @(
    $shortcuts | Where-Object {
        $arguments = [string]$_.Arguments
        $prefixLength = $commandLine.Length - $arguments.Length
        $commandLine.EndsWith($arguments, [StringComparison]::Ordinal) -and
        $prefixLength -gt 0 -and
        [char]::IsWhiteSpace($commandLine[$prefixLength - 1]) -and
        (
            [string]::IsNullOrWhiteSpace($_.TargetPath) -or
            [string]::IsNullOrWhiteSpace($executablePath) -or
            [string]::Equals(
                [IO.Path]::GetFullPath($_.TargetPath),
                [IO.Path]::GetFullPath($executablePath),
                [StringComparison]::OrdinalIgnoreCase
            )
        )
    } | Select-Object -ExpandProperty Arguments -Unique
)
if ($matches.Count -ne 1) { return }
$sha256 = [Security.Cryptography.SHA256]::Create()
try {
    $digest = $sha256.ComputeHash(
        [Text.Encoding]::UTF8.GetBytes([string]$matches[0])
    )
    [pscustomobject]@{
        Key = [string]$ProcessId
        Value = [BitConverter]::ToString($digest).Replace('-', '').ToLowerInvariant()
    }
} finally {
    $sha256.Dispose()
}
'@
    $active = New-Object System.Collections.ArrayList
    $next = 0
    while ($next -lt $pids.Count -or $active.Count -gt 0) {
        # Every PID remains independently time-bounded, but WScript.Shell is
        # process-wide on Windows PowerShell 5.1. Do not let one PID release
        # the shared COM object while a sibling PID is still using it.
        while ($next -lt $pids.Count -and $active.Count -lt 1) {
            $runspace = [RunspaceFactory]::CreateRunspace()
            $runspace.ApartmentState = [Threading.ApartmentState]::STA
            $runspace.ThreadOptions = [Management.Automation.Runspaces.PSThreadOptions]::UseNewThread
            $runspace.Open()
            $powerShell = [PowerShell]::Create()
            $powerShell.Runspace = $runspace
            [void]$powerShell.AddScript($itemScript).AddArgument($pids[$next]).AddArgument([string]$encoded)
            $async = $powerShell.BeginInvoke()
            [void]$active.Add([pscustomobject]@{
                PowerShell = $powerShell
                Runspace = $runspace
                Async = $async
                Deadline = [DateTime]::UtcNow.AddSeconds(3)
            })
            $next++
        }
        foreach ($task in @($active)) {
            if ($task.Async.IsCompleted) {
                try {
                    foreach ($item in @($task.PowerShell.EndInvoke($task.Async))) {
                        if ($null -ne $item -and $item.Key -and $item.Value) {
                            $resolved[[string]$item.Key] = [string]$item.Value
                        }
                    }
                } catch {
                    # Per-process failure remains absent and affects only it.
                } finally {
                    $task.PowerShell.Dispose()
                    $task.Runspace.Dispose()
                    [void]$active.Remove($task)
                }
            } elseif ([DateTime]::UtcNow -ge $task.Deadline) {
                try { [void]$task.PowerShell.BeginStop($null, $null) } catch {}
                [void]$abandoned.Add($task)
                [void]$active.Remove($task)
            }
        }
        if ($next -lt $pids.Count -or $active.Count -gt 0) {
            Start-Sleep -Milliseconds 10
        }
    }
} catch {
    $resolved = @{}
}
[Console]::Out.WriteLine(($resolved | ConvertTo-Json -Compress))
[Console]::Out.Flush()
if ($abandoned.Count -gt 0) { [Environment]::Exit(0) }
"""

    def __init__(self, paths: tuple[Path, ...]) -> None:
        self._paths = paths

    def resolve(self, process_ids: Iterable[int]) -> dict[int, str]:
        normalized_ids = sorted(
            {
                value
                for value in process_ids
                if isinstance(value, int)
                and not isinstance(value, bool)
                and value > 0
            }
        )
        if os.name != "nt" or not normalized_ids or not self._paths:
            return {}
        environment = _standard_powershell_environment()
        environment["FLASH_WINDOW_PIDS"] = ",".join(
            str(value) for value in normalized_ids
        )
        environment["FLASH_SCOPED_SHORTCUT_PATHS_B64"] = base64.b64encode(
            json.dumps(
                [os.fspath(path) for path in self._paths],
                ensure_ascii=False,
            ).encode("utf-8")
        ).decode("ascii")
        try:
            completed = _run_system_powershell(
                (
                    _system_powershell_path(),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-EncodedCommand",
                    base64.b64encode(
                        self._SCRIPT.encode("utf-16-le")
                    ).decode("ascii"),
                ),
                capture_output=True,
                text=False,
                timeout=(
                    WINDOW_TIMEOUT_SECONDS
                    * (
                        1
                        + math.ceil(
                            len(normalized_ids) / MAX_PARALLEL_WINDOWS
                        )
                    )
                ),
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
            if completed.returncode != 0:
                return {}
            output = completed.stdout.decode("utf-8-sig")
            raw = json.loads(output.strip() or "{}")
        except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        resolved: dict[int, str] = {}
        for raw_process_id, raw_fingerprint in raw.items():
            try:
                process_id = int(raw_process_id)
            except (TypeError, ValueError):
                continue
            fingerprint = normalize_launch_fingerprint(raw_fingerprint)
            if process_id in normalized_ids and fingerprint is not None:
                resolved[process_id] = fingerprint
        return resolved


class _EmptyLaunchFingerprintResolver:
    """Enumerate raw windows without performing process or shortcut I/O."""

    def resolve(self, _process_ids: Iterable[int]) -> dict[int, str]:
        return {}


def _enumerate_observation_source(
    request: SmartReconnectObservationRequest,
) -> SmartReconnectEnumerationResult:
    paths = _discover_shortcut_paths(
        request.shortcut_paths,
        request.shortcut_roots,
    )
    backend = Win32WindowBackend(_EmptyLaunchFingerprintResolver())
    keywords = tuple(value.casefold() for value in request.title_keywords)
    try:
        windows = tuple(
            window
            for window in backend.list_windows()
            if all(keyword in window.title.casefold() for keyword in keywords)
        )
        foreground_handle = backend.foreground_handle()
    except Exception:
        return SmartReconnectEnumerationResult(
            windows=(),
            shortcuts=(),
            failure_codes=("window_enumeration_failed",),
        )
    return SmartReconnectEnumerationResult(
        windows=windows,
        shortcuts=tuple(
            SmartReconnectShortcutObservation(
                path=_normalized_path(path),
                fingerprint=None,
                seal=None,
                failure_codes=("shortcut_observation_pending",),
                cache_key=None,
            )
            for path in paths
        ),
        foreground_handle=foreground_handle,
    )


def _resolve_shortcut_observation(
    request: SmartReconnectObservationRequest,
) -> tuple[SmartReconnectShortcutObservation, ...]:
    paths = tuple(Path(value) for value in request.shortcut_paths)
    if not paths:
        return ()
    static_by_path = {
        item.path: item
        for item in request.shortcut_static_observations
        if item.cache_key is not None
    }
    normalized_paths = tuple(_normalized_path(path) for path in paths)
    if any(path not in static_by_path for path in normalized_paths):
        return tuple(
            SmartReconnectShortcutObservation(
                path=path,
                fingerprint=None,
                seal=None,
                failure_codes=("shortcut_static_missing",),
            )
            for path in normalized_paths
        )
    return _shortcut_observations_from_static(
        tuple(static_by_path[path] for path in normalized_paths)
    )


def _resolve_shortcut_static_observation(
    request: SmartReconnectObservationRequest,
) -> SmartReconnectShortcutObservation:
    if len(request.shortcut_paths) != 1:
        raise ValueError("shortcut static observation requires one path")
    path = Path(request.shortcut_paths[0])
    normalized_path = _normalized_path(path)
    cache_key = _shortcut_cache_key(path)
    return SmartReconnectShortcutObservation(
        path=normalized_path,
        fingerprint=None,
        seal=None,
        failure_codes=(
            ("shortcut_observation_pending",)
            if cache_key is not None
            else ("shortcut_static_unresolved",)
        ),
        cache_key=cache_key,
    )


def _resolve_window_identity(
    request: SmartReconnectObservationRequest,
) -> WindowInfo:
    window = request.window
    if not isinstance(window, WindowInfo):
        raise ValueError("window identity requires a WindowInfo")
    process_id = window.process_id
    if (
        not isinstance(process_id, int)
        or isinstance(process_id, bool)
        or process_id <= 0
    ):
        return replace(window, launch_fingerprint=None)
    paths = tuple(Path(value) for value in request.shortcut_paths)
    resolved = _ScopedPowerShellLaunchFingerprintResolver(paths).resolve(
        (process_id,)
    )
    return replace(
        window,
        launch_fingerprint=resolved.get(process_id),
    )


def _resolve_window_identities(
    request: SmartReconnectObservationRequest,
) -> tuple[WindowInfo, ...]:
    windows = tuple(request.windows)
    if not windows and isinstance(request.window, WindowInfo):
        windows = (request.window,)
    if not windows:
        return ()
    process_ids = tuple(
        window.process_id
        for window in windows
        if isinstance(window.process_id, int)
        and not isinstance(window.process_id, bool)
        and window.process_id > 0
    )
    paths = tuple(Path(value) for value in request.shortcut_paths)
    resolved = _ScopedPowerShellLaunchFingerprintResolver(paths).resolve(
        process_ids
    )
    return tuple(
        replace(
            window,
            launch_fingerprint=(
                resolved.get(window.process_id)
                if isinstance(window.process_id, int)
                else None
            ),
        )
        for window in windows
    )


def _observe_ipv4_tcp_table(
    request: SmartReconnectObservationRequest,
) -> SmartReconnectTcpBatchResult:
    instances = tuple(dict.fromkeys(
        instance
        for window in request.windows
        if (instance := WindowInstanceToken.from_window(window)) is not None
    ))
    if not instances:
        return SmartReconnectTcpBatchResult(())
    try:
        counts = _ipv4_established_counts_by_pid(
            tuple(instance.process_id for instance in instances)
        )
    except Exception:
        failure = ("tcp_table_unavailable",)
        return SmartReconnectTcpBatchResult(
            tuple(
                SmartReconnectTcpWindowObservation(instance, None, failure)
                for instance in instances
            ),
            failure,
        )
    return SmartReconnectTcpBatchResult(tuple(
        SmartReconnectTcpWindowObservation(
            instance,
            counts.get(instance.process_id, 0),
        )
        for instance in instances
    ))


def _observe_window(
    request: SmartReconnectObservationRequest,
) -> SmartReconnectWindowObservation:
    window = request.window
    if not isinstance(window, WindowInfo):
        raise ValueError("window observation requires a WindowInfo")
    instance = WindowInstanceToken.from_window(window)
    role_id: str | None = None
    role_failure: tuple[str, ...] = ()
    route = "minimized" if window.minimized else None
    sample: CaptureSample | None = None
    recognition = _unknown_recognition()
    fresh = False
    capture_failure: tuple[str, ...] = ()
    background_sample: CaptureSample | None = None
    try:
        background_sample = Win32PrintWindowProvider().capture(window.handle)
    except Exception:
        background_sample = None
    if window.minimized:
        if not request.minimized_capture_enabled:
            recognition = ScreenRecognition(
                state=ReconnectScreenState.CHECK_DISABLED,
                score=None,
                click_point=None,
                reference_name=None,
            )
        elif background_sample is not None and background_sample.api_succeeded:
            sample = background_sample
            capture_failure = ("desktop_pixels_unproven",)
        else:
            capture_failure = ("background_capture_unknown",)
    else:
        visible_sample: CaptureSample | None = None
        try:
            visible_sample = Win32VisibleRegionCaptureProvider().capture(
                window.handle
            )
        except Exception:
            visible_sample = None
        route = "visible" if (
            visible_sample is not None and visible_sample.api_succeeded
        ) else "obscured"
        route_enabled = (
            request.visible_capture_enabled
            if route == "visible"
            else request.obscured_capture_enabled
        )
        if not route_enabled:
            recognition = ScreenRecognition(
                state=ReconnectScreenState.CHECK_DISABLED,
                score=None,
                click_point=None,
                reference_name=None,
            )
        else:
            sample = (
                visible_sample
                if route == "visible"
                else background_sample
                if background_sample is not None
                and background_sample.api_succeeded
                else None
            )
            if route == "visible" and sample is not None and sample.api_succeeded:
                recognition = ReferenceScreenRecognizer(
                    Path(request.reference_dir)
                ).recognize_capture(sample)
                fresh = True
            elif sample is not None and sample.api_succeeded:
                capture_failure = ("desktop_pixels_unproven",)
            else:
                capture_failure = ("background_capture_unknown",)
    current_visible_sample = bool(
        instance is not None
        and route == "visible"
        and fresh
        and sample is not None
        and sample.api_succeeded
    )
    role_sample: CaptureSample | None = None
    role_region_sha256: str | None = None
    if current_visible_sample:
        try:
            role_sample = role_id_region_sample(sample)
            role_region_sha256 = _capture_sha256(role_sample)
        except Exception:
            role_sample = None
            role_region_sha256 = None
    cache_matches_current_region = bool(
        current_visible_sample
        and role_region_sha256 is not None
        and request.role_cache_hit
        and request.role_cache_key is not None
        and request.role_cache_key.instance == instance
        and request.role_cache_key.role_region_sha256
        == role_region_sha256
    )
    if cache_matches_current_region:
        role_id = clean_role_id_text(request.cached_role_id or "") or None
        if role_id is None:
            role_failure = ("role_identity_unresolved",)
    elif current_visible_sample and role_region_sha256 is not None:
        try:
            role_id = clean_role_id_text(
                WindowsRoleIdOcrReader().read(role_sample)
                if role_sample is not None and role_sample.api_succeeded
                else ""
            ) or None
            if not role_id:
                role_failure = ("role_identity_unresolved",)
        except Exception:
            role_failure = ("role_identity_unresolved",)
    elif current_visible_sample:
        role_failure = ("role_identity_unresolved",)
    elif instance is None:
        role_failure = ("window_instance_incomplete",)
    else:
        role_failure = ("role_visible_evidence_unavailable",)
    process_cache_key: ProcessObservationCacheKey | None = None
    if (
        isinstance(window.process_id, int)
        and not isinstance(window.process_id, bool)
        and window.process_id > 0
        and isinstance(window.process_lifecycle_token, int)
        and not isinstance(window.process_lifecycle_token, bool)
        and window.process_lifecycle_token > 0
    ):
        process_cache_key = ProcessObservationCacheKey(
            window.process_id,
            window.process_lifecycle_token,
        )
    role_cache_key: RoleObservationCacheKey | None = None
    if (
        current_visible_sample
        and role_region_sha256 is not None
        and request.role_cache_key is not None
        and request.role_cache_key.instance == instance
    ):
        try:
            role_cache_key = replace(
                request.role_cache_key,
                role_region_sha256=role_region_sha256,
            )
        except (TypeError, ValueError):
            role_cache_key = None
    return SmartReconnectWindowObservation(
        window=window,
        instance=instance,
        sample=sample,
        recognition=recognition,
        fresh_capture=fresh,
        capture_route=route,
        role_id=role_id,
        failure_codes=tuple(dict.fromkeys((*role_failure, *capture_failure))),
        freshness=(
            ObservationFreshness.PROVEN_CURRENT
            if fresh
            else ObservationFreshness.UNPROVEN
        ),
        process_cache_key=process_cache_key,
        role_cache_key=role_cache_key,
        role_region_sha256=role_region_sha256,
    )


def _revalidate_seal(
    request: SmartReconnectObservationRequest,
) -> ShortcutSeal | None:
    expected = request.expected_seal
    if not isinstance(expected, ShortcutSeal):
        return None
    observations = _shortcut_observations(
        (Path(expected.file_identity.normalized_path),)
    )
    if len(observations) != 1:
        return None
    return observations[0].seal


def _execute_observation_request(
    request: SmartReconnectObservationRequest,
) -> object:
    if request.stage == "enumerate":
        return _enumerate_observation_source(request)
    if request.stage == "shortcut":
        return _resolve_shortcut_observation(request)
    if request.stage == "shortcut_static":
        return _resolve_shortcut_static_observation(request)
    if request.stage == "identity":
        return _resolve_window_identities(request)
    if request.stage == "tcp":
        return _observe_ipv4_tcp_table(request)
    if request.stage == "window":
        return _observe_window(request)
    if request.stage == "seal":
        return _revalidate_seal(request)
    raise ValueError("unsupported observation stage")


class _WorkerEnvironmentError(RuntimeError):
    """The worker cannot safely create another child process."""


def _worker_dll_directory_reset() -> str | None:
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetDllDirectoryW.argtypes = (
            ctypes.c_uint32,
            ctypes.c_wchar_p,
        )
        kernel32.GetDllDirectoryW.restype = ctypes.c_uint32
        kernel32.SetDllDirectoryW.argtypes = (ctypes.c_wchar_p,)
        kernel32.SetDllDirectoryW.restype = ctypes.c_int
        size = kernel32.GetDllDirectoryW(0, None)
        previous = None
        if size:
            buffer = ctypes.create_unicode_buffer(size + 1)
            if kernel32.GetDllDirectoryW(len(buffer), buffer):
                previous = buffer.value
        if not kernel32.SetDllDirectoryW(None):
            raise OSError(ctypes.get_last_error())
        return previous
    except Exception as error:
        raise _WorkerEnvironmentError(
            "failed to clear the worker DLL directory"
        ) from error


def _worker_dll_directory_restore(previous: str | None) -> None:
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetDllDirectoryW.argtypes = (ctypes.c_wchar_p,)
        kernel32.SetDllDirectoryW.restype = ctypes.c_int
        if not kernel32.SetDllDirectoryW(previous):
            raise OSError(ctypes.get_last_error())
    except Exception as error:
        raise _WorkerEnvironmentError(
            "failed to restore the worker DLL directory"
        ) from error


def _worker_bootstrap(
    gate,
    connection: Connection,
    worker_epoch: int,
    operation: Callable[[SmartReconnectObservationRequest], object],
) -> None:
    try:
        if not gate.wait(5.0):
            return
        while True:
            try:
                job = connection.recv()
            except (EOFError, OSError):
                return
            if job is None:
                return
            if (
                not isinstance(job, SmartReconnectObservationJob)
                or job.worker_epoch != worker_epoch
            ):
                continue
            fatal_worker_environment = False
            try:
                payload = operation(job.request)
                reply = _WorkerReply(
                    job.request_serial,
                    job.job_serial,
                    worker_epoch,
                    True,
                    payload,
                )
            except BaseException as error:
                fatal_worker_environment = isinstance(
                    error,
                    _WorkerEnvironmentError,
                )
                reply = _WorkerReply(
                    job.request_serial,
                    job.job_serial,
                    worker_epoch,
                    False,
                    f"{type(error).__name__}: {error}",
                )
            try:
                connection.send(reply)
            except (BrokenPipeError, EOFError, OSError):
                return
            if fatal_worker_environment:
                return
    finally:
        connection.close()


class _WindowsJob:
    """One kill-on-close Job Object assigned before worker I/O starts."""

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = tuple((name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        ))

    class _BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = (
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        )

    class _EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        pass

    _EXTENDED_LIMIT_INFORMATION._fields_ = (
        ("BasicLimitInformation", _BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )

    def __init__(self, process_id: int) -> None:
        self._handle = None
        if os.name != "nt":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.OpenProcess.argtypes = (
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint32,
        )
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.AssignProcessToJobObject.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        information = self._EXTENDED_LIMIT_INFORMATION()
        information.BasicLimitInformation.LimitFlags = (
            self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not kernel32.SetInformationJobObject(
            handle,
            self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            kernel32.CloseHandle(handle)
            raise OSError(
                ctypes.get_last_error(),
                "SetInformationJobObject failed",
            )
        process_handle = kernel32.OpenProcess(
            self._PROCESS_TERMINATE | self._PROCESS_SET_QUOTA,
            False,
            int(process_id),
        )
        if not process_handle:
            kernel32.CloseHandle(handle)
            raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        try:
            if not kernel32.AssignProcessToJobObject(handle, process_handle):
                raise OSError(
                    ctypes.get_last_error(),
                    "AssignProcessToJobObject failed",
                )
        except BaseException:
            kernel32.CloseHandle(handle)
            raise
        finally:
            kernel32.CloseHandle(process_handle)
        self._handle = handle

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
            kernel32.CloseHandle.restype = ctypes.c_int
            kernel32.CloseHandle(handle)


@dataclass(slots=True)
class _ActiveWorker:
    process: multiprocessing.Process
    connection: Connection
    job: _WindowsJob
    slot_index: int
    epoch: int
    finish_lock: Lock = field(default_factory=Lock)


class WindowsSmartReconnectObservationBroker:
    """Publish one immutable snapshot after bounded, process-only I/O."""

    def __init__(
        self,
        *,
        reference_dir: Path,
        title_keywords: Iterable[str] = ("Adobe Flash Player",),
        shortcut_roots: Iterable[Path] = (),
        visible_capture_enabled: bool = True,
        obscured_capture_enabled: bool = True,
        minimized_capture_enabled: bool = True,
        _worker_operation: (
            Callable[[SmartReconnectObservationRequest], object] | None
        ) = None,
    ) -> None:
        keywords = tuple(
            value.strip().casefold()
            for value in title_keywords
            if isinstance(value, str) and value.strip()
        )
        if not keywords:
            raise ValueError("at least one window title keyword is required")
        self._reference_dir = _normalized_path(Path(reference_dir))
        self._title_keywords = keywords
        self._shortcut_roots = tuple(
            _normalized_path(Path(path)) for path in shortcut_roots
        )
        self._visible_capture_enabled = bool(visible_capture_enabled)
        self._obscured_capture_enabled = bool(obscured_capture_enabled)
        self._minimized_capture_enabled = bool(minimized_capture_enabled)
        self._worker_operation = _worker_operation or _execute_observation_request
        self._context = multiprocessing.get_context("spawn")
        self._refresh_lock = Lock()
        self._request_lock = Lock()
        self._pool_lock = RLock()
        self._state_lock = RLock()
        self._action_gate = RLock()
        self._active_lock = RLock()
        self._active: dict[int, _ActiveWorker] = {}
        self._slots: dict[int, _ActiveWorker] = {}
        self._slot_epochs = [0] * MAX_PARALLEL_WINDOWS
        self._job_serial = 0
        self._started = False
        self._request_serial = 0
        self._published_request_serial = 0
        self._generation = 0
        self._static_generation = 0
        self._closed = False
        self._latest = SmartReconnectObservationSnapshot(generation=0)
        self._stable_snapshot: SmartReconnectObservationSnapshot | None = None
        self._action_snapshot: SmartReconnectObservationSnapshot | None = None
        self._action_lease: ObservationActionLease | None = None
        self._refresh_inflight = False
        self._published_snapshot_without_wait: (
            SmartReconnectObservationSnapshot | None
        ) = None
        self._witness_serial = 0
        self._current_witnesses: dict[str, SmartReconnectSealWitness] = {}
        self._published_witnesses_without_wait: tuple[
            SmartReconnectSealWitness, ...
        ] = ()
        self._shortcut_cache: dict[
            str,
            SmartReconnectShortcutObservation,
        ] = {}
        self._process_cache: dict[
            ProcessObservationCacheKey,
            str,
        ] = {}
        self._role_cache: dict[
            RoleObservationCacheKey,
            str,
        ] = {}
        self._role_cache_by_instance: dict[
            WindowInstanceToken,
            tuple[RoleObservationCacheKey, str],
        ] = {}
        self._identity_source = (0, 0)
        self._role_source_generation = 1
        self._shortcut_catalog_paths: tuple[str, ...] = ()
        self._shortcut_refresh_required = True
        self._last_static_by_fingerprint: dict[
            str,
            tuple[WindowInstanceToken, ShortcutSeal | None, str | None],
        ] = {}

    @staticmethod
    def batch_timeout_seconds(window_count: int) -> float:
        count = max(0, int(window_count))
        return (
            2.0 * ENUMERATION_TIMEOUT_SECONDS
            + (4 + math.ceil(count / MAX_PARALLEL_WINDOWS))
            * WINDOW_TIMEOUT_SECONDS
        )

    def latest_snapshot(self) -> SmartReconnectObservationSnapshot:
        with self._state_lock:
            return self._latest

    def stable_snapshot(self) -> SmartReconnectObservationSnapshot | None:
        """Read the immutable stable pointer without waiting on broker work."""

        return self._published_snapshot_without_wait

    def refresh_inflight(self) -> bool:
        with self._state_lock:
            return self._refresh_inflight

    def current_snapshot(self) -> SmartReconnectObservationSnapshot | None:
        """Return only the action-capable, unexpired current publication."""

        with self._state_lock:
            return (
                self._action_snapshot
                if self._generation_is_current_unlocked(
                    (
                        self._action_snapshot.generation
                        if self._action_snapshot is not None
                        else 0
                    )
                )
                else None
            )

    def action_snapshot(
        self,
    ) -> tuple[
        SmartReconnectObservationSnapshot,
        ObservationActionLease,
    ] | None:
        with self._state_lock:
            snapshot = self._action_snapshot
            lease = self._action_lease
            if (
                snapshot is None
                or lease is None
                or not self._action_lease_is_current_unlocked(lease)
            ):
                return None
            return snapshot, lease

    def published_snapshot_without_wait(
        self,
    ) -> SmartReconnectObservationSnapshot | None:
        """Read the immutable publication pointer without taking a lock."""

        return self._published_snapshot_without_wait

    def is_generation_current(self, generation: int) -> bool:
        with self._state_lock:
            return self._generation_is_current_unlocked(generation)

    def _generation_is_current_unlocked(self, generation: int) -> bool:
        return bool(
            not self._closed
            and isinstance(generation, int)
            and not isinstance(generation, bool)
            and generation > 0
            and self._action_snapshot is not None
            and self._action_snapshot.generation == generation
            and self._published_request_serial == self._request_serial
            and self._action_lease is not None
            and self._action_lease.deadline_monotonic > time.monotonic()
        )

    def _action_lease_is_current_unlocked(
        self,
        lease: ObservationActionLease,
    ) -> bool:
        return bool(
            lease is self._action_lease
            and self._action_snapshot is not None
            and lease.request_serial == self._request_serial
            and lease.observation_generation == self._action_snapshot.generation
            and lease.deadline_monotonic > time.monotonic()
            and self._published_request_serial == self._request_serial
            and not self._closed
        )

    def run_if_generation_current(
        self,
        generation: int,
        callback: Callable[[], _RESULT],
    ) -> tuple[bool, _RESULT | None]:
        """Run one memory-only commit while the published generation is fixed."""

        if not callable(callback):
            raise TypeError("callback must be callable")
        with self._action_gate:
            with self._state_lock:
                if not self._generation_is_current_unlocked(generation):
                    return False, None
            return True, callback()

    def run_if_action_current(
        self,
        lease: ObservationActionLease,
        callback: Callable[[], _RESULT],
    ) -> tuple[bool, _RESULT | None]:
        """Linearize a pure-memory action against one unforgeable lease."""

        if not isinstance(lease, ObservationActionLease):
            return False, None
        if not callable(callback):
            raise TypeError("callback must be callable")
        with self._action_gate:
            with self._state_lock:
                if not self._action_lease_is_current_unlocked(lease):
                    return False, None
            return True, callback()

    def set_visible_capture_enabled(self, enabled: bool) -> None:
        self.set_capture_modes(
            visible=enabled,
            obscured=self._obscured_capture_enabled,
            minimized=self._minimized_capture_enabled,
        )

    def set_identity_source(
        self,
        identity_generation: int,
        config_revision: int,
    ) -> None:
        """Invalidate only role evidence when its immutable source changes."""

        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for value in (identity_generation, config_revision)
        ):
            raise ValueError("identity source values must be non-negative")
        source = (identity_generation, config_revision)
        with self._action_gate:
            with self._state_lock:
                if self._closed or source == self._identity_source:
                    return
                if source[1] != self._identity_source[1]:
                    self._shortcut_refresh_required = True
                self._identity_source = source
                self._role_source_generation += 1
                self._role_cache.clear()
                self._role_cache_by_instance.clear()
                self._request_serial += 1
                self._current_witnesses.clear()
                self._published_witnesses_without_wait = ()
                self._action_snapshot = None
                self._action_lease = None
                self._refresh_inflight = False

    def set_capture_modes(
        self,
        *,
        visible: bool,
        obscured: bool,
        minimized: bool,
    ) -> None:
        with self._action_gate:
            with self._state_lock:
                values = (bool(visible), bool(obscured), bool(minimized))
                if values == (
                    self._visible_capture_enabled,
                    self._obscured_capture_enabled,
                    self._minimized_capture_enabled,
                ):
                    return
                (
                    self._visible_capture_enabled,
                    self._obscured_capture_enabled,
                    self._minimized_capture_enabled,
                ) = values
                self._request_serial += 1
                self._current_witnesses.clear()
                self._published_witnesses_without_wait = ()
                self._action_snapshot = None
                self._action_lease = None
                self._refresh_inflight = False

    def _next_request(
        self,
        shortcut_paths: tuple[str, ...] | None = None,
    ) -> tuple[
        int,
        SmartReconnectObservationSnapshot,
        int,
        dict[str, SmartReconnectShortcutObservation],
        dict[ProcessObservationCacheKey, str],
        dict[RoleObservationCacheKey, str],
        dict[
            WindowInstanceToken,
            tuple[RoleObservationCacheKey, str],
        ],
        bool,
    ] | int | None:
        with self._action_gate:
            with self._state_lock:
                if self._closed:
                    return None
                self._request_serial += 1
                self._current_witnesses.clear()
                self._published_witnesses_without_wait = ()
                self._action_snapshot = None
                self._action_lease = None
                self._refresh_inflight = True
                context = (
                    self._request_serial,
                    self._stable_snapshot or self._latest,
                    self._role_source_generation,
                    dict(self._shortcut_cache),
                    dict(self._process_cache),
                    dict(self._role_cache),
                    dict(self._role_cache_by_instance),
                    bool(
                        self._shortcut_refresh_required
                        or shortcut_paths != self._shortcut_catalog_paths
                        or not self._shortcut_cache
                    ),
                )
                return self._request_serial if shortcut_paths is None else context

    def invalidate_action(self) -> bool:
        """Revoke every action lease and supersede an in-flight refresh."""

        with self._action_gate:
            with self._state_lock:
                if self._closed:
                    return False
                self._request_serial += 1
                self._current_witnesses.clear()
                self._published_witnesses_without_wait = ()
                self._action_snapshot = None
                self._action_lease = None
                self._refresh_inflight = False
                return True

    def _request_is_current(self, serial: int) -> bool:
        with self._state_lock:
            return not self._closed and self._request_serial == serial

    def _request(
        self,
        request: SmartReconnectObservationRequest,
        timeout_seconds: float,
    ) -> object:
        results = self._request_many((request,), timeout_seconds)
        return results[0] if results else None

    def start(self) -> bool:
        """Start exactly four persistent spawn workers once."""

        with self._pool_lock:
            with self._state_lock:
                if self._closed:
                    return False
                if self._started:
                    return len(self._slots) == MAX_PARALLEL_WINDOWS
            started: list[_ActiveWorker] = []
            try:
                for slot_index in range(MAX_PARALLEL_WINDOWS):
                    worker = self._start_worker(slot_index)
                    started.append(worker)
            except BaseException:
                for worker in started:
                    self._finish_worker(worker, kill=True)
                with self._state_lock:
                    self._started = False
                return False
            with self._state_lock:
                closed = self._closed
                if not closed:
                    self._started = True
            if closed:
                for worker in tuple(started):
                    self._finish_worker(worker, kill=True)
                return False
            return True

    def _start_worker(self, slot_index: int) -> _ActiveWorker:
        if (
            isinstance(slot_index, bool)
            or not isinstance(slot_index, int)
            or slot_index < 0
            or slot_index >= MAX_PARALLEL_WINDOWS
        ):
            raise ValueError("worker slot index is invalid")
        parent, child = self._context.Pipe(duplex=True)
        gate = self._context.Event()
        self._slot_epochs[slot_index] += 1
        epoch = self._slot_epochs[slot_index]
        process = self._context.Process(
            target=_worker_bootstrap,
            args=(gate, child, epoch, self._worker_operation),
            daemon=False,
        )
        started = False
        child_closed = False
        job: _WindowsJob | None = None
        try:
            with self._state_lock:
                if self._closed:
                    raise RuntimeError("observation broker is closed")
            process.start()
            started = True
            child.close()
            child_closed = True
            job = _WindowsJob(process.pid)
            worker = _ActiveWorker(
                process=process,
                connection=parent,
                job=job,
                slot_index=slot_index,
                epoch=epoch,
            )
            with self._state_lock:
                if self._closed:
                    raise RuntimeError("observation broker closed during start")
                with self._active_lock:
                    self._active[id(worker)] = worker
                    self._slots[slot_index] = worker
            gate.set()
            return worker
        except BaseException:
            if not child_closed:
                child.close()
            if job is not None:
                job.close()
            if started:
                process.join(0.1)
                if process.is_alive():
                    process.terminate()
                    process.join(0.5)
                if process.is_alive():
                    kill_process = getattr(process, "kill", None)
                    if callable(kill_process):
                        kill_process()
                        process.join(0.5)
            parent.close()
            if not started or not process.is_alive():
                try:
                    process.close()
                except (AttributeError, ValueError):
                    pass
            raise

    def _finish_worker(self, worker: _ActiveWorker, *, kill: bool) -> bool:
        with worker.finish_lock:
            with self._active_lock:
                if self._active.get(id(worker)) is not worker:
                    return True
            if not kill:
                try:
                    worker.connection.send(None)
                except (BrokenPipeError, EOFError, OSError):
                    pass
                worker.process.join(WORKER_STOP_SECONDS)
            if kill:
                worker.job.close()
            worker.process.join(WORKER_STOP_SECONDS)
            if worker.process.is_alive():
                worker.job.close()
                worker.process.terminate()
                worker.process.join(WORKER_STOP_SECONDS)
            if worker.process.is_alive():
                kill_process = getattr(worker.process, "kill", None)
                if callable(kill_process):
                    kill_process()
                    worker.process.join(WORKER_STOP_SECONDS)
            if worker.process.is_alive():
                return False
            worker.connection.close()
            worker.job.close()
            try:
                worker.process.close()
            except (AttributeError, ValueError):
                pass
            with self._active_lock:
                if self._active.get(id(worker)) is worker:
                    self._active.pop(id(worker), None)
                if self._slots.get(worker.slot_index) is worker:
                    self._slots.pop(worker.slot_index, None)
            return True

    def _restart_worker(self, worker: _ActiveWorker) -> _ActiveWorker | None:
        with self._pool_lock:
            slot_index = worker.slot_index
            if not self._finish_worker(worker, kill=True):
                return None
            with self._state_lock:
                if self._closed:
                    return None
            try:
                return self._start_worker(slot_index)
            except BaseException:
                return None

    def _request_many(
        self,
        requests: tuple[SmartReconnectObservationRequest, ...],
        timeout_seconds: float,
    ) -> tuple[object | None, ...]:
        if not requests:
            return ()
        with self._request_lock:
            if not self.start():
                return tuple(None for _request in requests)
            results: list[object | None] = [None] * len(requests)
            for offset in range(0, len(requests), MAX_PARALLEL_WINDOWS):
                chunk = requests[offset : offset + MAX_PARALLEL_WINDOWS]
                with self._active_lock:
                    workers = tuple(
                        self._slots.get(index) for index in range(len(chunk))
                    )
                if any(worker is None for worker in workers):
                    continue
                pending: dict[Connection, tuple[int, _ActiveWorker, SmartReconnectObservationJob]] = {}
                now = time.monotonic()
                for index, (worker, request) in enumerate(zip(workers, chunk)):
                    assert worker is not None
                    self._job_serial += 1
                    job = SmartReconnectObservationJob(
                        request_serial=self._request_serial,
                        job_serial=self._job_serial,
                        worker_epoch=worker.epoch,
                        kind=request.stage,
                        deadline_monotonic=now + timeout_seconds,
                        request=request,
                    )
                    try:
                        worker.connection.send(job)
                    except (BrokenPipeError, EOFError, OSError):
                        self._restart_worker(worker)
                        continue
                    pending[worker.connection] = (offset + index, worker, job)
                while pending:
                    now = time.monotonic()
                    expired = tuple(
                        connection
                        for connection, (_index, _worker, job) in pending.items()
                        if job.deadline_monotonic <= now
                    )
                    for connection in expired:
                        _index, worker, _job = pending.pop(connection)
                        self._restart_worker(worker)
                    if not pending:
                        break
                    nearest = min(
                        job.deadline_monotonic
                        for _index, _worker, job in pending.values()
                    )
                    try:
                        ready = wait(tuple(pending), max(0.0, nearest - now))
                    except (OSError, ValueError):
                        ready = ()
                    for connection in ready:
                        entry = pending.pop(connection, None)
                        if entry is None:
                            continue
                        index, worker, job = entry
                        try:
                            reply = connection.recv()
                        except (EOFError, OSError):
                            reply = None
                        if (
                            isinstance(reply, _WorkerReply)
                            and reply.request_serial == job.request_serial
                            and reply.job_serial == job.job_serial
                            and reply.worker_epoch == worker.epoch
                            and reply.succeeded
                        ):
                            results[index] = reply.payload
                        else:
                            self._restart_worker(worker)
            return tuple(results)

    def _request_bounded(
        self,
        requests: tuple[SmartReconnectObservationRequest, ...],
        timeout_seconds: float,
    ) -> tuple[object | None, ...]:
        results: list[object | None] = []
        for offset in range(0, len(requests), MAX_PARALLEL_WINDOWS):
            results.extend(
                self._request_many(
                    requests[offset : offset + MAX_PARALLEL_WINDOWS],
                    timeout_seconds,
                )
            )
        return tuple(results)

    def _complete_enumeration(
        self,
        raw: SmartReconnectEnumerationResult,
        *,
        shortcut_cache: dict[
            str,
            SmartReconnectShortcutObservation,
        ] | None = None,
        process_cache: dict[ProcessObservationCacheKey, str] | None = None,
        role_cache: dict[RoleObservationCacheKey, str] | None = None,
        role_cache_by_instance: dict[
            WindowInstanceToken,
            tuple[RoleObservationCacheKey, str],
        ] | None = None,
        shortcut_batch_available: list[bool] | None = None,
        refresh_shortcuts: bool = True,
        expected_shortcut_cache_keys: dict[
            str,
            ShortcutObservationCacheKey,
        ] | None = None,
        shortcut_static_failures: dict[
            str,
            SmartReconnectShortcutObservation,
        ] | None = None,
    ) -> SmartReconnectEnumerationResult:
        shortcut_cache = (
            self._shortcut_cache if shortcut_cache is None else shortcut_cache
        )
        process_cache = (
            self._process_cache if process_cache is None else process_cache
        )
        role_cache = self._role_cache if role_cache is None else role_cache
        role_cache_by_instance = (
            self._role_cache_by_instance
            if role_cache_by_instance is None
            else role_cache_by_instance
        )
        shortcut_batch_available = (
            [True]
            if shortcut_batch_available is None
            else shortcut_batch_available
        )
        shortcut_static_failures = shortcut_static_failures or {}
        prior_shortcuts = tuple(shortcut_cache.values())
        shortcuts = list(raw.shortcuts)
        for index, item in enumerate(shortcuts):
            failed = shortcut_static_failures.get(item.path)
            if failed is not None:
                shortcuts[index] = failed
        static_indexes = tuple(
            index
            for index, item in enumerate(shortcuts)
            if item.path not in shortcut_static_failures
            and not _shortcut_observation_is_cacheable(item)
        )
        static_results = self._request_bounded(
            tuple(
                SmartReconnectObservationRequest(
                    stage="shortcut_static",
                    reference_dir=self._reference_dir,
                    title_keywords=self._title_keywords,
                    shortcut_paths=(shortcuts[index].path,),
                )
                for index in static_indexes
            ),
            WINDOW_TIMEOUT_SECONDS,
        )
        for result_index, shortcut_index in enumerate(static_indexes):
            result = (
                static_results[result_index]
                if result_index < len(static_results)
                else None
            )
            if (
                isinstance(result, SmartReconnectShortcutObservation)
                and result.path == shortcuts[shortcut_index].path
                and result.cache_key is not None
                and not (
                    set(result.failure_codes)
                    - {"shortcut_observation_pending"}
                )
            ):
                expected_cache_key = (
                    expected_shortcut_cache_keys.get(result.path)
                    if expected_shortcut_cache_keys is not None
                    else result.cache_key
                )
                if expected_cache_key == result.cache_key:
                    shortcuts[shortcut_index] = result
                else:
                    shortcut_cache.pop(result.path, None)
                    shortcuts[shortcut_index] = (
                        SmartReconnectShortcutObservation(
                            path=result.path,
                            fingerprint=None,
                            seal=None,
                            failure_codes=("shortcut_static_unresolved",),
                            cache_key=None,
                        )
                    )
            else:
                shortcut_cache.pop(shortcuts[shortcut_index].path, None)
                shortcuts[shortcut_index] = SmartReconnectShortcutObservation(
                    path=shortcuts[shortcut_index].path,
                    fingerprint=None,
                    seal=None,
                    failure_codes=("shortcut_static_timeout",),
                )
        shortcut_indexes: list[int] = []
        for index, item in enumerate(shortcuts):
            if (
                normalize_launch_fingerprint(item.fingerprint) is not None
                and item.seal is not None
                and not item.failure_codes
            ):
                shortcut_cache[item.path] = item
                continue
            if item.cache_key is None:
                continue
            cached = shortcut_cache.get(item.path)
            if (
                cached is not None
                and item.cache_key is not None
                and cached.cache_key == item.cache_key
                and _shortcut_observation_is_cacheable(cached)
            ):
                shortcuts[index] = cached
                continue
            shortcut_indexes.append(index)
        if shortcut_indexes and shortcut_batch_available[0]:
            shortcut_batch_available[0] = False
            raw_shortcut_result = self._request(
                SmartReconnectObservationRequest(
                    stage="shortcut",
                    reference_dir=self._reference_dir,
                    title_keywords=self._title_keywords,
                    shortcut_paths=tuple(
                        shortcuts[index].path for index in shortcut_indexes
                    ),
                    shortcut_static_observations=tuple(
                        shortcuts[index] for index in shortcut_indexes
                    ),
                ),
                WINDOW_TIMEOUT_SECONDS
                * (
                    1
                    + math.ceil(
                        len(shortcut_indexes) / MAX_PARALLEL_WINDOWS
                    )
                ),
            )
            by_path = {
                item.path: item
                for item in raw_shortcut_result
                if isinstance(item, SmartReconnectShortcutObservation)
            } if isinstance(raw_shortcut_result, tuple) else {}
            for index in shortcut_indexes:
                result = by_path.get(shortcuts[index].path)
                if (
                    isinstance(result, SmartReconnectShortcutObservation)
                    and result.cache_key == shortcuts[index].cache_key
                ):
                    shortcuts[index] = result
                    if _shortcut_observation_is_cacheable(result):
                        shortcut_cache[result.path] = result
                    continue
                shortcuts[index] = SmartReconnectShortcutObservation(
                    path=shortcuts[index].path,
                    fingerprint=None,
                    seal=None,
                    failure_codes=("shortcut_observation_timeout",),
                    cache_key=shortcuts[index].cache_key,
                )
        elif shortcut_indexes:
            for index in shortcut_indexes:
                shortcuts[index] = SmartReconnectShortcutObservation(
                    path=shortcuts[index].path,
                    fingerprint=None,
                    seal=None,
                    failure_codes=("shortcut_batch_deferred",),
                    cache_key=shortcuts[index].cache_key,
                )

        changed_shortcuts = self._changed_shortcut_fingerprints(
            prior_shortcuts,
            tuple(shortcuts),
        )
        if changed_shortcuts:
            for key, fingerprint in tuple(process_cache.items()):
                if fingerprint in changed_shortcuts:
                    process_cache.pop(key, None)
            for key in tuple(role_cache):
                if key.fingerprint in changed_shortcuts:
                    role_cache.pop(key, None)
            for instance, cached in tuple(role_cache_by_instance.items()):
                if cached[0].fingerprint in changed_shortcuts:
                    role_cache_by_instance.pop(instance, None)
        usable_paths = tuple(
            item.path
            for item in shortcuts
            if normalize_launch_fingerprint(item.fingerprint) is not None
            and item.seal is not None
            and not item.failure_codes
        )
        identity_indexes: list[int] = []
        windows = list(raw.windows)
        for index, window in enumerate(windows):
            if normalize_launch_fingerprint(window.launch_fingerprint) is not None:
                continue
            process_key: ProcessObservationCacheKey | None = None
            if (
                isinstance(window.process_id, int)
                and not isinstance(window.process_id, bool)
                and window.process_id > 0
                and isinstance(window.process_lifecycle_token, int)
                and not isinstance(window.process_lifecycle_token, bool)
                and window.process_lifecycle_token > 0
            ):
                process_key = ProcessObservationCacheKey(
                    window.process_id,
                    window.process_lifecycle_token,
                )
            cached_fingerprint = (
                process_cache.get(process_key)
                if process_key is not None
                else None
            )
            if cached_fingerprint is not None:
                windows[index] = replace(
                    window,
                    launch_fingerprint=cached_fingerprint,
                )
                continue
            identity_indexes.append(index)
        if identity_indexes:
            raw_identity_result = self._request(
                SmartReconnectObservationRequest(
                    stage="identity",
                    reference_dir=self._reference_dir,
                    title_keywords=self._title_keywords,
                    shortcut_paths=usable_paths,
                    windows=tuple(windows[index] for index in identity_indexes),
                ),
                WINDOW_TIMEOUT_SECONDS
                * (
                    1
                    + math.ceil(
                        len(identity_indexes) / MAX_PARALLEL_WINDOWS
                    )
                ),
            )
            identity_results = (
                raw_identity_result
                if isinstance(raw_identity_result, tuple)
                else ()
            )
            for result_index, window_index in enumerate(identity_indexes):
                result = (
                    identity_results[result_index]
                    if result_index < len(identity_results)
                    else None
                )
                if isinstance(result, WindowInfo):
                    windows[window_index] = result
                    fingerprint = normalize_launch_fingerprint(
                        result.launch_fingerprint
                    )
                    if (
                        fingerprint is not None
                        and isinstance(result.process_id, int)
                        and isinstance(result.process_lifecycle_token, int)
                    ):
                        try:
                            process_cache[
                                ProcessObservationCacheKey(
                                    result.process_id,
                                    result.process_lifecycle_token,
                                )
                            ] = fingerprint
                        except (TypeError, ValueError):
                            pass
                else:
                    windows[window_index] = replace(
                        windows[window_index],
                        launch_fingerprint=None,
                    )
        cached_shortcuts = {
            item.path: item
            for item in shortcuts
            if _shortcut_observation_is_cacheable(item)
        }
        shortcut_cache.clear()
        shortcut_cache.update(cached_shortcuts)
        return SmartReconnectEnumerationResult(
            windows=tuple(windows),
            shortcuts=tuple(shortcuts),
            failure_codes=raw.failure_codes,
            foreground_handle=raw.foreground_handle,
        )

    def _enumeration_request(
        self,
        shortcut_paths: tuple[str, ...],
    ) -> SmartReconnectObservationRequest:
        return SmartReconnectObservationRequest(
            stage="enumerate",
            reference_dir=self._reference_dir,
            title_keywords=self._title_keywords,
            shortcut_paths=shortcut_paths,
            shortcut_roots=self._shortcut_roots,
            visible_capture_enabled=self._visible_capture_enabled,
            obscured_capture_enabled=self._obscured_capture_enabled,
            minimized_capture_enabled=self._minimized_capture_enabled,
        )

    @staticmethod
    def _unique_windows(
        windows: tuple[WindowInfo, ...],
    ) -> tuple[
        dict[str, tuple[WindowInfo, WindowInstanceToken]],
        frozenset[str],
        int,
    ]:
        handle_counts: dict[int, int] = {}
        process_counts: dict[int, int] = {}
        for window in windows:
            handle_counts[window.handle] = handle_counts.get(window.handle, 0) + 1
            if isinstance(window.process_id, int) and window.process_id > 0:
                process_counts[window.process_id] = (
                    process_counts.get(window.process_id, 0) + 1
                )
        grouped: dict[str, list[tuple[WindowInfo, WindowInstanceToken]]] = {}
        blocked: set[str] = set()
        anonymous = 0
        for window in windows:
            fingerprint = normalize_launch_fingerprint(
                window.launch_fingerprint
            )
            if fingerprint is None:
                anonymous += 1
                continue
            instance = WindowInstanceToken.from_window(window)
            collision = bool(
                handle_counts.get(window.handle, 0) != 1
                or not isinstance(window.process_id, int)
                or process_counts.get(window.process_id, 0) != 1
            )
            if instance is None or collision:
                blocked.add(fingerprint)
                continue
            grouped.setdefault(fingerprint, []).append((window, instance))
        unique: dict[str, tuple[WindowInfo, WindowInstanceToken]] = {}
        for fingerprint, values in grouped.items():
            if len(values) != 1:
                blocked.add(fingerprint)
            else:
                unique[fingerprint] = values[0]
        for fingerprint in blocked:
            unique.pop(fingerprint, None)
        return unique, frozenset(blocked), anonymous

    @staticmethod
    def _shortcut_catalog_matches(
        before: tuple[SmartReconnectShortcutObservation, ...],
        after: tuple[SmartReconnectShortcutObservation, ...],
    ) -> bool:
        return before == after

    @staticmethod
    def _changed_shortcut_fingerprints(
        before: tuple[SmartReconnectShortcutObservation, ...],
        after: tuple[SmartReconnectShortcutObservation, ...],
    ) -> frozenset[str]:
        before_by_path = {item.path: item for item in before}
        after_by_path = {item.path: item for item in after}
        changed: set[str] = set()
        for path in before_by_path.keys() | after_by_path.keys():
            old = before_by_path.get(path)
            new = after_by_path.get(path)
            if old == new:
                continue
            for item in (old, new):
                if item is not None and item.fingerprint is not None:
                    changed.add(item.fingerprint)
        return frozenset(changed)

    @staticmethod
    def _previously_attributable_failures(
        previous: SmartReconnectObservationSnapshot,
        *observations: SmartReconnectEnumerationResult,
    ) -> frozenset[str]:
        """Attribute a timed-out item only to its prior immutable identity."""

        prior_shortcuts = {
            item.path: item.fingerprint
            for item in previous.shortcuts
            if normalize_launch_fingerprint(item.fingerprint) is not None
        }
        prior_instances = {
            item.instance: normalize_launch_fingerprint(
                item.window.launch_fingerprint
            )
            for item in previous.windows
            if item.instance is not None
        }
        blocked: set[str] = set()
        for observation in observations:
            for item in observation.shortcuts:
                if item.failure_codes:
                    fingerprint = normalize_launch_fingerprint(
                        prior_shortcuts.get(item.path)
                    )
                    if fingerprint is not None:
                        blocked.add(fingerprint)
            for window in observation.windows:
                if normalize_launch_fingerprint(
                    window.launch_fingerprint
                ) is not None:
                    continue
                instance = WindowInstanceToken.from_window(window)
                fingerprint = normalize_launch_fingerprint(
                    prior_instances.get(instance)
                )
                if fingerprint is not None:
                    blocked.add(fingerprint)
        return frozenset(blocked)

    @staticmethod
    def _invalid_snapshot(*failure_codes: str) -> SmartReconnectObservationSnapshot:
        return SmartReconnectObservationSnapshot(
            generation=0,
            failure_codes=tuple(dict.fromkeys(failure_codes)),
        )

    def _role_cache_key_for(
        self,
        instance: WindowInstanceToken,
        fingerprint: str,
        shortcuts: tuple[SmartReconnectShortcutObservation, ...],
        source_generation: int,
        role_region_sha256: str,
    ) -> RoleObservationCacheKey | None:
        matches = tuple(
            item
            for item in shortcuts
            if normalize_launch_fingerprint(item.fingerprint) == fingerprint
            and item.seal is not None
            and not item.failure_codes
        )
        if len(matches) != 1:
            return None
        try:
            return RoleObservationCacheKey(
                instance=instance,
                fingerprint=fingerprint,
                shortcut_seal=matches[0].seal,
                source_generation=source_generation,
                role_region_sha256=role_region_sha256,
            )
        except (TypeError, ValueError):
            return None

    def refresh(
        self,
        shortcut_paths: Iterable[Path] = (),
    ) -> SmartReconnectObservationSnapshot:
        normalized_paths = tuple(
            dict.fromkeys(
                _normalized_path(Path(path))
                for path in shortcut_paths
            )
        )
        with self._refresh_lock:
            refresh_state = self._next_request(normalized_paths)
            if refresh_state is None:
                return self._invalid_snapshot("observation_broker_closed")
            (
                serial,
                previous,
                role_source_generation,
                shortcut_cache,
                process_cache,
                role_cache,
                role_cache_by_instance,
                refresh_shortcuts,
            ) = refresh_state
            shortcut_batch_available = [True]
            before = self._request(
                self._enumeration_request(normalized_paths),
                ENUMERATION_TIMEOUT_SECONDS,
            )
            if not isinstance(before, SmartReconnectEnumerationResult):
                return self._publish_global_failure(
                    serial,
                    "window_enumeration_timeout",
                )
            if before.failure_codes:
                return self._publish_global_failure(
                    serial,
                    *before.failure_codes,
                )
            before = self._complete_enumeration(
                before,
                shortcut_cache=shortcut_cache,
                process_cache=process_cache,
                role_cache=role_cache,
                role_cache_by_instance=role_cache_by_instance,
                shortcut_batch_available=shortcut_batch_available,
                refresh_shortcuts=refresh_shortcuts,
            )
            before_static_failures = {
                item.path: item
                for item in before.shortcuts
                if item.cache_key is None
                and "shortcut_static_timeout" in item.failure_codes
            }
            before_shortcut_cache_keys = {
                item.path: item.cache_key
                for item in before.shortcuts
                if item.cache_key is not None
            }
            unique_before, blocked, anonymous = self._unique_windows(
                before.windows
            )
            tcp_batch = self._request(
                SmartReconnectObservationRequest(
                    stage="tcp",
                    reference_dir=self._reference_dir,
                    title_keywords=self._title_keywords,
                    windows=tuple(
                        window for window, _instance in unique_before.values()
                    ),
                ),
                WINDOW_TIMEOUT_SECONDS,
            )
            if not isinstance(tcp_batch, SmartReconnectTcpBatchResult):
                failure = ("tcp_table_timeout",)
                tcp_batch = SmartReconnectTcpBatchResult(
                    tuple(
                        SmartReconnectTcpWindowObservation(
                            instance,
                            None,
                            failure,
                        )
                        for _window, instance in unique_before.values()
                    ),
                    failure,
                )
            tcp_by_instance = {
                item.instance: item for item in tcp_batch.observations
            }
            requests_list: list[SmartReconnectObservationRequest] = []
            for fingerprint, (window, instance) in unique_before.items():
                role_cache_key: RoleObservationCacheKey | None = None
                cached_role_id: str | None = None
                prior_role_cache = role_cache_by_instance.get(instance)
                if prior_role_cache is not None:
                    prior_key = prior_role_cache[0]
                    current_key = self._role_cache_key_for(
                        instance,
                        fingerprint,
                        before.shortcuts,
                        role_source_generation,
                        prior_key.role_region_sha256,
                    )
                    if current_key == prior_key:
                        cached_role_id = role_cache.get(prior_key)
                        if cached_role_id is not None:
                            role_cache_key = prior_key
                requests_list.append(SmartReconnectObservationRequest(
                    stage="window",
                    reference_dir=self._reference_dir,
                    title_keywords=self._title_keywords,
                    shortcut_paths=normalized_paths,
                    shortcut_roots=self._shortcut_roots,
                    window=window,
                    cached_role_id=cached_role_id,
                    role_cache_hit=cached_role_id is not None,
                    role_cache_key=role_cache_key,
                    visible_capture_enabled=self._visible_capture_enabled,
                    obscured_capture_enabled=(
                        self._obscured_capture_enabled
                    ),
                    minimized_capture_enabled=(
                        self._minimized_capture_enabled
                    ),
                ))
            requests = tuple(requests_list)
            observed = self._request_bounded(
                requests,
                WINDOW_TIMEOUT_SECONDS,
            )
            after = self._request(
                self._enumeration_request(normalized_paths),
                ENUMERATION_TIMEOUT_SECONDS,
            )
            if not isinstance(after, SmartReconnectEnumerationResult):
                return self._publish_global_failure(
                    serial,
                    "window_revalidation_timeout",
                )
            if after.failure_codes:
                return self._publish_global_failure(serial, *after.failure_codes)
            after = self._complete_enumeration(
                after,
                shortcut_cache=shortcut_cache,
                process_cache=process_cache,
                role_cache=role_cache,
                role_cache_by_instance=role_cache_by_instance,
                shortcut_batch_available=shortcut_batch_available,
                refresh_shortcuts=False,
                expected_shortcut_cache_keys=before_shortcut_cache_keys,
                shortcut_static_failures=before_static_failures,
            )
            unique_after, after_blocked, after_anonymous = self._unique_windows(
                after.windows
            )
            blocked_set = set(blocked | after_blocked)
            blocked_set.update(
                self._previously_attributable_failures(
                    previous,
                    before,
                    after,
                )
            )
            if not self._shortcut_catalog_matches(
                before.shortcuts,
                after.shortcuts,
            ):
                blocked_set.update(
                    self._changed_shortcut_fingerprints(
                        before.shortcuts,
                        after.shortcuts,
                    )
                )
            results: list[SmartReconnectWindowObservation] = []
            for fingerprint, (window, instance) in unique_before.items():
                current = unique_after.get(fingerprint)
                if current is None or current[1] != instance:
                    blocked_set.add(fingerprint)
                    continue
                index = tuple(unique_before).index(fingerprint)
                item = observed[index] if index < len(observed) else None
                if (
                    not isinstance(item, SmartReconnectWindowObservation)
                    or item.instance != instance
                    or normalize_launch_fingerprint(
                        item.window.launch_fingerprint
                    )
                    != fingerprint
                ):
                    blocked_set.add(fingerprint)
                    continue
                if "window_instance_incomplete" in item.failure_codes:
                    blocked_set.add(fingerprint)
                    continue
                tcp_observation = tcp_by_instance.get(instance)
                item = replace(
                    item,
                    tcp_established_connections=(
                        tcp_observation.established_connections
                        if tcp_observation is not None
                        else None
                    ),
                    tcp_failure_codes=(
                        tcp_observation.failure_codes
                        if tcp_observation is not None
                        else ("tcp_observation_missing",)
                    ),
                )
                sample_sha256 = _capture_sha256(item.sample)
                role_evidence_is_current = bool(
                    item.freshness is ObservationFreshness.PROVEN_CURRENT
                    and item.fresh_capture is True
                    and item.capture_route == "visible"
                    and sample_sha256 is not None
                    and item.role_region_sha256 is not None
                )
                if not role_evidence_is_current:
                    item = replace(
                        item,
                        recognition=_unknown_recognition(),
                        fresh_capture=False,
                        freshness=ObservationFreshness.UNPROVEN,
                        role_id=None,
                        role_cache_key=None,
                        role_region_sha256=None,
                        failure_codes=tuple(dict.fromkeys((
                            *item.failure_codes,
                            "desktop_pixels_unproven",
                        ))),
                    )
                prior_role_cache = role_cache_by_instance.pop(
                    instance,
                    None,
                )
                if prior_role_cache is not None:
                    role_cache.pop(prior_role_cache[0], None)
                role_cache_key = (
                    self._role_cache_key_for(
                        instance,
                        fingerprint,
                        after.shortcuts,
                        role_source_generation,
                        item.role_region_sha256,
                    )
                    if role_evidence_is_current
                    else None
                )
                process_cache_key = (
                    ProcessObservationCacheKey(
                        window.process_id,
                        window.process_lifecycle_token,
                    )
                    if (
                        isinstance(window.process_id, int)
                        and not isinstance(window.process_id, bool)
                        and window.process_id > 0
                        and isinstance(window.process_lifecycle_token, int)
                        and not isinstance(window.process_lifecycle_token, bool)
                        and window.process_lifecycle_token > 0
                    )
                    else None
                )
                item = replace(
                    item,
                    role_cache_key=role_cache_key,
                    process_cache_key=process_cache_key,
                )
                if (
                    role_cache_key is not None
                    and isinstance(item.role_id, str)
                    and item.role_id.strip()
                ):
                    role_cache[role_cache_key] = item.role_id
                    role_cache_by_instance[instance] = (
                        role_cache_key,
                        item.role_id,
                    )
                else:
                    if role_cache_key is not None:
                        role_cache.pop(role_cache_key, None)
                results.append(item)
            results = [
                item
                for item in results
                if normalize_launch_fingerprint(item.window.launch_fingerprint)
                not in blocked_set
            ]
            current_isolated = sum(
                1
                for window in after.windows
                if (
                    (fingerprint := normalize_launch_fingerprint(
                        window.launch_fingerprint
                    ))
                    is None
                    or fingerprint in blocked_set
                )
            )
            current_fingerprints = frozenset(
                fingerprint
                for fingerprint in (
                    normalize_launch_fingerprint(window.launch_fingerprint)
                    for window in after.windows
                )
                if fingerprint is not None
            )
            prior_fingerprint_by_instance = {
                item.instance: normalize_launch_fingerprint(
                    item.window.launch_fingerprint
                )
                for item in previous.windows
                if item.instance is not None
            }
            unidentified_current_prior_fingerprints = frozenset(
                fingerprint
                for window in after.windows
                if normalize_launch_fingerprint(
                    window.launch_fingerprint
                ) is None
                and (
                    fingerprint := normalize_launch_fingerprint(
                        prior_fingerprint_by_instance.get(
                            WindowInstanceToken.from_window(window)
                        )
                    )
                ) is not None
            )
            absent_blocked = (
                blocked_set
                - current_fingerprints
                - unidentified_current_prior_fingerprints
            )
            isolated = current_isolated + len(absent_blocked)
            snapshot = SmartReconnectObservationSnapshot(
                generation=0,
                request_serial=serial,
                windows=tuple(results),
                shortcuts=after.shortcuts,
                blocked_fingerprints=frozenset(blocked_set),
                isolated_window_count=isolated,
                anonymous_isolated_window_count=after_anonymous,
                failure_codes=(),
                foreground_handle=after.foreground_handle,
            )
            published = self._publish(
                serial,
                snapshot,
                expected_role_source_generation=role_source_generation,
                shortcut_cache=shortcut_cache,
                process_cache=process_cache,
                role_cache=role_cache,
                role_cache_by_instance=role_cache_by_instance,
                shortcut_catalog_paths=normalized_paths,
            )
            return (
                published
                if published is not None
                else self._invalid_snapshot("observation_request_superseded")
            )

    def _publish_global_failure(
        self,
        serial: int,
        *failure_codes: str,
    ) -> SmartReconnectObservationSnapshot:
        with self._action_gate:
            with self._state_lock:
                if self._closed or self._request_serial != serial:
                    return self._invalid_snapshot("observation_request_superseded")
                self._refresh_inflight = False
                self._action_snapshot = None
                self._action_lease = None
                return self._invalid_snapshot(*failure_codes)

    @staticmethod
    def _static_state_for_snapshot(
        snapshot: SmartReconnectObservationSnapshot,
    ) -> dict[
        str,
        tuple[WindowInstanceToken, ShortcutSeal | None, str | None],
    ]:
        result: dict[
            str,
            tuple[WindowInstanceToken, ShortcutSeal | None, str | None],
        ] = {}
        for item in snapshot.windows:
            fingerprint = normalize_launch_fingerprint(
                item.window.launch_fingerprint
            )
            if fingerprint is None or item.instance is None:
                continue
            shortcut = snapshot.shortcut_for(fingerprint)
            result[fingerprint] = (
                item.instance,
                shortcut.seal if shortcut is not None else None,
                item.role_id,
            )
        return result

    def _publish(
        self,
        serial: int,
        snapshot: SmartReconnectObservationSnapshot,
        *,
        expected_role_source_generation: int | None = None,
        shortcut_cache: dict[str, SmartReconnectShortcutObservation] | None = None,
        process_cache: dict[ProcessObservationCacheKey, str] | None = None,
        role_cache: dict[RoleObservationCacheKey, str] | None = None,
        role_cache_by_instance: dict[
            WindowInstanceToken,
            tuple[RoleObservationCacheKey, str],
        ] | None = None,
        shortcut_catalog_paths: tuple[str, ...] = (),
    ) -> SmartReconnectObservationSnapshot | None:
        with self._action_gate:
            with self._state_lock:
                expected_role_source_generation = (
                    self._role_source_generation
                    if expected_role_source_generation is None
                    else expected_role_source_generation
                )
                shortcut_cache = (
                    dict(self._shortcut_cache)
                    if shortcut_cache is None
                    else shortcut_cache
                )
                process_cache = (
                    dict(self._process_cache)
                    if process_cache is None
                    else process_cache
                )
                role_cache = (
                    dict(self._role_cache)
                    if role_cache is None
                    else role_cache
                )
                role_cache_by_instance = (
                    dict(self._role_cache_by_instance)
                    if role_cache_by_instance is None
                    else role_cache_by_instance
                )
                if (
                    self._closed
                    or self._request_serial != serial
                    or self._role_source_generation
                    != expected_role_source_generation
                ):
                    return None
                self._generation += 1
                now = time.monotonic()
                action_deadline = now + ACTION_LEASE_SECONDS
                static_state = self._static_state_for_snapshot(snapshot)
                changed_fingerprints = frozenset(
                    fingerprint
                    for fingerprint in (
                        self._last_static_by_fingerprint.keys()
                        | static_state.keys()
                        | set(snapshot.blocked_fingerprints)
                    )
                    if (
                        self._last_static_by_fingerprint.get(fingerprint)
                        != static_state.get(fingerprint)
                        or fingerprint in snapshot.blocked_fingerprints
                    )
                )
                if self._stable_snapshot is None or changed_fingerprints:
                    self._static_generation += 1
                current = SmartReconnectObservationSnapshot(
                    generation=self._generation,
                    request_serial=serial,
                    published_at_monotonic=now,
                    action_deadline_monotonic=action_deadline,
                    static_generation=self._static_generation,
                    changed_fingerprints=changed_fingerprints,
                    windows=snapshot.windows,
                    shortcuts=snapshot.shortcuts,
                    blocked_fingerprints=snapshot.blocked_fingerprints,
                    isolated_window_count=snapshot.isolated_window_count,
                    anonymous_isolated_window_count=(
                        snapshot.anonymous_isolated_window_count
                    ),
                    failure_codes=snapshot.failure_codes,
                    foreground_handle=snapshot.foreground_handle,
                )
                self._latest = current
                self._stable_snapshot = current
                self._action_snapshot = current
                self._action_lease = ObservationActionLease(
                    request_serial=serial,
                    observation_generation=current.generation,
                    deadline_monotonic=action_deadline,
                )
                self._last_static_by_fingerprint = static_state
                self._published_snapshot_without_wait = current
                self._published_request_serial = serial
                self._refresh_inflight = False
                live_role_keys = frozenset(
                    item.role_cache_key
                    for item in current.windows
                    if item.role_cache_key is not None
                )
                self._shortcut_cache = dict(shortcut_cache)
                self._process_cache = dict(process_cache)
                self._role_cache = {
                    key: value
                    for key, value in role_cache.items()
                    if key in live_role_keys
                }
                self._role_cache_by_instance = {
                    instance: cached
                    for instance, cached in role_cache_by_instance.items()
                    if cached[0] in live_role_keys
                }
                self._shortcut_catalog_paths = shortcut_catalog_paths
                self._shortcut_refresh_required = False
                return current

    def revalidate_reopen_seal(
        self,
        expected: ShortcutSeal,
    ) -> SmartReconnectSealWitness | None:
        if not isinstance(expected, ShortcutSeal):
            return None
        with self._refresh_lock:
            with self._state_lock:
                if not self._generation_is_current_unlocked(
                    self._latest.generation
                ):
                    return None
                observation_generation = self._action_snapshot.generation
                self._current_witnesses.pop(
                    expected.launch_fingerprint,
                    None,
                )
                self._published_witnesses_without_wait = tuple(
                    self._current_witnesses.values()
                )
            current = self._request(
                SmartReconnectObservationRequest(
                    stage="seal",
                    reference_dir=self._reference_dir,
                    title_keywords=self._title_keywords,
                    expected_seal=expected,
                ),
                ENUMERATION_TIMEOUT_SECONDS,
            )
            with self._state_lock:
                if (
                    self._closed
                    or not self._generation_is_current_unlocked(
                        observation_generation
                    )
                    or current != expected
                ):
                    return None
                self._witness_serial += 1
                witness = SmartReconnectSealWitness(
                    generation=observation_generation,
                    request_serial=self._witness_serial,
                    expected_seal=expected,
                )
                self._current_witnesses[expected.launch_fingerprint] = witness
                self._published_witnesses_without_wait = tuple(
                    self._current_witnesses.values()
                )
                return witness

    def seal_witness(
        self,
        expected: ShortcutSeal,
    ) -> SmartReconnectSealWitness | None:
        """Compatibility alias; product code uses the reopen-only name."""

        return self.revalidate_reopen_seal(expected)

    def witness_is_current(self, witness: SmartReconnectSealWitness) -> bool:
        with self._state_lock:
            return bool(
                witness is self._current_witnesses.get(
                    witness.expected_seal.launch_fingerprint
                )
                and not self._closed
                and self._generation_is_current_unlocked(witness.generation)
            )

    def seal_is_witnessed(self, expected: ShortcutSeal) -> bool:
        if not isinstance(expected, ShortcutSeal):
            return False
        with self._state_lock:
            witness = self._current_witnesses.get(expected.launch_fingerprint)
            return bool(
                witness is not None
                and witness.expected_seal == expected
                and self._generation_is_current_unlocked(witness.generation)
            )

    def seal_is_witnessed_without_wait(self, expected: ShortcutSeal) -> bool:
        """Check the immutable witness view inside a broker-held action."""

        if not isinstance(expected, ShortcutSeal):
            return False
        return any(
            witness.expected_seal == expected
            for witness in self._published_witnesses_without_wait
        )

    def close(self) -> bool:
        with self._action_gate:
            with self._state_lock:
                if not self._closed:
                    self._closed = True
                    self._request_serial += 1
                    self._current_witnesses.clear()
                    self._action_snapshot = None
                    self._action_lease = None
                    self._refresh_inflight = False
                    self._published_snapshot_without_wait = None
                    self._published_witnesses_without_wait = ()
        with self._pool_lock:
            with self._active_lock:
                active = tuple(self._active.values())
            for worker in active:
                self._finish_worker(worker, kill=False)
            with self._state_lock:
                self._started = False
        with self._active_lock:
            return not self._active and not self._slots
