"""Process-isolated, non-mutating observations for smart reconnect."""

from __future__ import annotations

import ctypes
import base64
import hashlib
import json
import math
import multiprocessing
import os
import subprocess
import time
from dataclasses import dataclass, field, replace
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
from core.window_instance import WindowInstanceToken
from services.role_id_template_service import RoleIdTemplateService


ENUMERATION_TIMEOUT_SECONDS = 3.0
WINDOW_TIMEOUT_SECONDS = 3.0
MAX_PARALLEL_WINDOWS = 4

_RESULT = TypeVar("_RESULT")


def _unknown_recognition() -> ScreenRecognition:
    return ScreenRecognition(
        state=ReconnectScreenState.UNKNOWN,
        score=None,
        click_point=None,
        reference_name=None,
    )


@dataclass(frozen=True, slots=True)
class SmartReconnectShortcutObservation:
    path: str
    fingerprint: str | None
    seal: ShortcutSeal | None
    failure_codes: tuple[str, ...] = ()


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


@dataclass(frozen=True, slots=True)
class SmartReconnectEnumerationResult:
    windows: tuple[WindowInfo, ...]
    shortcuts: tuple[SmartReconnectShortcutObservation, ...]
    failure_codes: tuple[str, ...] = ()
    foreground_handle: int | None = None


@dataclass(frozen=True, slots=True)
class SmartReconnectObservationSnapshot:
    generation: int
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
    visible_capture_enabled: bool = True
    obscured_capture_enabled: bool = True
    minimized_capture_enabled: bool = True
    expected_seal: ShortcutSeal | None = None


@dataclass(frozen=True, slots=True)
class SmartReconnectSealWitness:
    generation: int
    request_serial: int
    expected_seal: ShortcutSeal


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


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


def _shortcut_observations(
    paths: tuple[Path, ...],
) -> tuple[SmartReconnectShortcutObservation, ...]:
    if not paths:
        return ()
    resolver = PowerShellShortcutFingerprintResolver()
    try:
        raw = resolver.resolve(paths)
    except Exception:
        raw = {}
    normalized_raw: dict[str, str] = {}
    for path, fingerprint in raw.items():
        normalized = normalize_launch_fingerprint(fingerprint)
        if normalized is not None:
            normalized_raw[_normalized_path(Path(path))] = normalized
    identity_provider = Win32ShortcutFileIdentityProvider()
    observations: list[SmartReconnectShortcutObservation] = []
    for path in paths:
        normalized_path = _normalized_path(path)
        fingerprint = normalized_raw.get(normalized_path)
        if fingerprint is None:
            observations.append(
                SmartReconnectShortcutObservation(
                    path=normalized_path,
                    fingerprint=None,
                    seal=None,
                    failure_codes=("shortcut_identity_unresolved",),
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
                )
            )
            continue
        observations.append(
            SmartReconnectShortcutObservation(
                path=normalized_path,
                fingerprint=fingerprint,
                seal=seal,
            )
        )
    return tuple(observations)


class _ScopedPowerShellLaunchFingerprintResolver:
    """Resolve process identities from only the explicitly scoped shortcuts."""

    _SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
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
    $paths = @()
    if (-not [string]::IsNullOrWhiteSpace($encoded)) {
        $paths = @(
            [Text.Encoding]::UTF8.GetString(
                [Convert]::FromBase64String($encoded)
            ) | ConvertFrom-Json | ForEach-Object { [string]$_ }
        )
    }
    $shell = New-Object -ComObject WScript.Shell
    $shortcuts = @()
    foreach ($path in $paths) {
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
    $resolved = @{}
    foreach ($processId in $pids) {
        $process = Get-CimInstance Win32_Process -Filter (
            "ProcessId = $processId"
        ) -ErrorAction SilentlyContinue
        if ($null -eq $process) { continue }
        $commandLine = ([string]$process.CommandLine).TrimEnd()
        $executablePath = [string]$process.ExecutablePath
        $matches = @(
            $shortcuts | Where-Object {
                $arguments = [string]$_.Arguments
                $prefixLength = $commandLine.Length - $arguments.Length
                $commandLine.EndsWith(
                    $arguments,
                    [StringComparison]::Ordinal
                ) -and
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
        if ($matches.Count -ne 1) { continue }
        $sha256 = [Security.Cryptography.SHA256]::Create()
        try {
            $digest = $sha256.ComputeHash(
                [Text.Encoding]::UTF8.GetBytes([string]$matches[0])
            )
            $resolved[[string]$processId] = (
                [BitConverter]::ToString($digest).Replace('-', '').ToLowerInvariant()
            )
        } finally {
            $sha256.Dispose()
        }
    }
    Write-Output ($resolved | ConvertTo-Json -Compress)
} catch {
    Write-Output '{}'
}
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
        environment = os.environ.copy()
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
            completed = subprocess.run(
                (
                    "powershell.exe",
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
                timeout=12.0,
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
            )
            for path in paths
        ),
        foreground_handle=foreground_handle,
    )


def _resolve_shortcut_observation(
    request: SmartReconnectObservationRequest,
) -> SmartReconnectShortcutObservation:
    if len(request.shortcut_paths) != 1:
        raise ValueError("shortcut observation requires one path")
    path = Path(request.shortcut_paths[0])
    observations = _shortcut_observations((path,))
    if len(observations) == 1:
        return observations[0]
    return SmartReconnectShortcutObservation(
        path=_normalized_path(path),
        fingerprint=None,
        seal=None,
        failure_codes=("shortcut_observation_failed",),
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


def _observe_window(
    request: SmartReconnectObservationRequest,
) -> SmartReconnectWindowObservation:
    window = request.window
    if not isinstance(window, WindowInfo):
        raise ValueError("window observation requires a WindowInfo")
    instance = WindowInstanceToken.from_window(window)
    role_id: str | None = None
    role_failure: tuple[str, ...] = ()
    if instance is not None:
        try:
            role_result = RoleIdTemplateService().read(window.handle)
            role_id = role_result.role_id.strip() if role_result.success else None
            if not role_id:
                role_failure = ("role_identity_unresolved",)
        except Exception:
            role_failure = ("role_identity_unresolved",)
    else:
        role_failure = ("window_instance_incomplete",)

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
            recognition = ReferenceScreenRecognizer(
                Path(request.reference_dir)
            ).recognize_capture(sample)
            fresh = True
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
                background_sample
                if background_sample is not None
                and background_sample.api_succeeded
                else visible_sample
            )
            if sample is not None and sample.api_succeeded:
                recognition = ReferenceScreenRecognizer(
                    Path(request.reference_dir)
                ).recognize_capture(sample)
                fresh = True
            else:
                capture_failure = ("background_capture_unknown",)
    return SmartReconnectWindowObservation(
        window=window,
        instance=instance,
        sample=sample,
        recognition=recognition,
        fresh_capture=fresh,
        capture_route=route,
        role_id=role_id,
        failure_codes=tuple(dict.fromkeys((*role_failure, *capture_failure))),
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
    if request.stage == "identity":
        return _resolve_window_identity(request)
    if request.stage == "window":
        return _observe_window(request)
    if request.stage == "seal":
        return _revalidate_seal(request)
    raise ValueError("unsupported observation stage")


def _worker_bootstrap(
    gate,
    sender: Connection,
    request: SmartReconnectObservationRequest,
    operation: Callable[[SmartReconnectObservationRequest], object],
) -> None:
    try:
        if not gate.wait(5.0):
            return
        sender.send((True, operation(request)))
    except BaseException as error:
        try:
            sender.send((False, f"{type(error).__name__}: {error}"))
        except BaseException:
            pass
    finally:
        sender.close()


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
    receiver: Connection
    job: _WindowsJob
    deadline: float
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
        self._state_lock = RLock()
        self._active_lock = RLock()
        self._active: dict[int, _ActiveWorker] = {}
        self._request_serial = 0
        self._published_request_serial = 0
        self._generation = 0
        self._closed = False
        self._latest = SmartReconnectObservationSnapshot(generation=0)
        self._published_snapshot_without_wait: (
            SmartReconnectObservationSnapshot | None
        ) = None
        self._witness_serial = 0
        self._current_witnesses: dict[str, SmartReconnectSealWitness] = {}
        self._published_witnesses_without_wait: tuple[
            SmartReconnectSealWitness, ...
        ] = ()

    @staticmethod
    def batch_timeout_seconds(window_count: int) -> float:
        count = max(0, int(window_count))
        return (
            2.0 * ENUMERATION_TIMEOUT_SECONDS
            + math.ceil(count / MAX_PARALLEL_WINDOWS)
            * WINDOW_TIMEOUT_SECONDS
        )

    def latest_snapshot(self) -> SmartReconnectObservationSnapshot:
        with self._state_lock:
            return self._latest

    def current_snapshot(self) -> SmartReconnectObservationSnapshot | None:
        """Return the latest publication only while its request is current."""

        with self._state_lock:
            return (
                self._latest
                if self._generation_is_current_unlocked(
                    self._latest.generation
                )
                else None
            )

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
            and self._latest.generation == generation
            and self._published_request_serial == self._request_serial
        )

    def run_if_generation_current(
        self,
        generation: int,
        callback: Callable[[], _RESULT],
    ) -> tuple[bool, _RESULT | None]:
        """Run one memory-only commit while the published generation is fixed."""

        if not callable(callback):
            raise TypeError("callback must be callable")
        with self._state_lock:
            if not self._generation_is_current_unlocked(generation):
                return False, None
            return True, callback()

    def set_visible_capture_enabled(self, enabled: bool) -> None:
        with self._state_lock:
            self.set_capture_modes(
                visible=enabled,
                obscured=self._obscured_capture_enabled,
                minimized=self._minimized_capture_enabled,
            )

    def set_capture_modes(
        self,
        *,
        visible: bool,
        obscured: bool,
        minimized: bool,
    ) -> None:
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
            self._published_snapshot_without_wait = None
            self._published_witnesses_without_wait = ()

    def _next_request(self) -> int | None:
        with self._state_lock:
            if self._closed:
                return None
            self._request_serial += 1
            self._current_witnesses.clear()
            self._published_snapshot_without_wait = None
            self._published_witnesses_without_wait = ()
            return self._request_serial

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

    def _start_worker(
        self,
        request: SmartReconnectObservationRequest,
        timeout_seconds: float,
    ) -> _ActiveWorker:
        receiver, sender = self._context.Pipe(duplex=False)
        gate = self._context.Event()
        process = self._context.Process(
            target=_worker_bootstrap,
            args=(gate, sender, request, self._worker_operation),
            daemon=False,
        )
        started = False
        sender_closed = False
        job: _WindowsJob | None = None
        try:
            # Registration and close are serialized while the child is still
            # behind its gate.  Close can therefore never miss a started
            # process or report success before that process is registered.
            with self._state_lock:
                if self._closed:
                    raise RuntimeError("observation broker is closed")
                process.start()
                started = True
                sender.close()
                sender_closed = True
                job = _WindowsJob(process.pid)
                worker = _ActiveWorker(
                    process=process,
                    receiver=receiver,
                    job=job,
                    deadline=time.monotonic() + timeout_seconds,
                )
                with self._active_lock:
                    self._active[id(worker)] = worker
                gate.set()
                return worker
        except BaseException:
            if not sender_closed:
                sender.close()
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
            receiver.close()
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
            if kill:
                worker.job.close()
            worker.process.join(0.5)
            if worker.process.is_alive():
                worker.job.close()
                worker.process.terminate()
                worker.process.join(0.5)
            if worker.process.is_alive():
                kill_process = getattr(worker.process, "kill", None)
                if callable(kill_process):
                    kill_process()
                    worker.process.join(0.5)
            if worker.process.is_alive():
                return False
            worker.receiver.close()
            worker.job.close()
            try:
                worker.process.close()
            except (AttributeError, ValueError):
                pass
            with self._active_lock:
                if self._active.get(id(worker)) is worker:
                    self._active.pop(id(worker), None)
            return True

    def _request_many(
        self,
        requests: tuple[SmartReconnectObservationRequest, ...],
        timeout_seconds: float,
    ) -> tuple[object | None, ...]:
        if not requests:
            return ()
        workers: list[_ActiveWorker] = []
        try:
            for request in requests:
                workers.append(self._start_worker(request, timeout_seconds))
        except BaseException:
            for worker in workers:
                self._finish_worker(worker, kill=True)
            return tuple(None for _request in requests)
        results: list[object | None] = [None] * len(workers)
        pending = {worker.receiver: index for index, worker in enumerate(workers)}
        while pending:
            now = time.monotonic()
            expired = tuple(
                receiver
                for receiver, index in pending.items()
                if workers[index].deadline <= now
            )
            for receiver in expired:
                pending.pop(receiver, None)
            if not pending:
                break
            nearest = min(
                workers[index].deadline
                for index in pending.values()
            )
            try:
                ready = wait(tuple(pending), max(0.0, nearest - now))
            except (OSError, ValueError):
                break
            for receiver in ready:
                index = pending.pop(receiver)
                try:
                    succeeded, payload = receiver.recv()
                except (EOFError, OSError):
                    succeeded, payload = False, None
                if succeeded is True:
                    results[index] = payload
        for index, worker in enumerate(workers):
            self._finish_worker(worker, kill=results[index] is None)
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
    ) -> SmartReconnectEnumerationResult:
        shortcut_requests: list[SmartReconnectObservationRequest] = []
        shortcut_indexes: list[int] = []
        shortcuts = list(raw.shortcuts)
        for index, item in enumerate(shortcuts):
            if (
                normalize_launch_fingerprint(item.fingerprint) is not None
                and item.seal is not None
                and not item.failure_codes
            ):
                continue
            shortcut_indexes.append(index)
            shortcut_requests.append(
                SmartReconnectObservationRequest(
                    stage="shortcut",
                    reference_dir=self._reference_dir,
                    title_keywords=self._title_keywords,
                    shortcut_paths=(item.path,),
                )
            )
        shortcut_results = self._request_bounded(
            tuple(shortcut_requests),
            WINDOW_TIMEOUT_SECONDS,
        )
        for index, result in zip(shortcut_indexes, shortcut_results):
            if isinstance(result, SmartReconnectShortcutObservation):
                shortcuts[index] = result
            else:
                shortcuts[index] = SmartReconnectShortcutObservation(
                    path=shortcuts[index].path,
                    fingerprint=None,
                    seal=None,
                    failure_codes=("shortcut_observation_timeout",),
                )

        usable_paths = tuple(
            item.path
            for item in shortcuts
            if normalize_launch_fingerprint(item.fingerprint) is not None
            and item.seal is not None
            and not item.failure_codes
        )
        identity_requests: list[SmartReconnectObservationRequest] = []
        identity_indexes: list[int] = []
        windows = list(raw.windows)
        for index, window in enumerate(windows):
            if normalize_launch_fingerprint(window.launch_fingerprint) is not None:
                continue
            identity_indexes.append(index)
            identity_requests.append(
                SmartReconnectObservationRequest(
                    stage="identity",
                    reference_dir=self._reference_dir,
                    title_keywords=self._title_keywords,
                    shortcut_paths=usable_paths,
                    window=window,
                )
            )
        identity_results = self._request_bounded(
            tuple(identity_requests),
            WINDOW_TIMEOUT_SECONDS,
        )
        for index, result in zip(identity_indexes, identity_results):
            if isinstance(result, WindowInfo):
                windows[index] = result
            else:
                windows[index] = replace(
                    windows[index],
                    launch_fingerprint=None,
                )
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
            previous = self.latest_snapshot()
            serial = self._next_request()
            if serial is None:
                return self._invalid_snapshot("observation_broker_closed")
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
            before = self._complete_enumeration(before)
            unique_before, blocked, anonymous = self._unique_windows(
                before.windows
            )
            requests = tuple(
                SmartReconnectObservationRequest(
                    stage="window",
                    reference_dir=self._reference_dir,
                    title_keywords=self._title_keywords,
                    shortcut_paths=normalized_paths,
                    shortcut_roots=self._shortcut_roots,
                    window=window,
                    visible_capture_enabled=self._visible_capture_enabled,
                    obscured_capture_enabled=(
                        self._obscured_capture_enabled
                    ),
                    minimized_capture_enabled=(
                        self._minimized_capture_enabled
                    ),
                )
                for window, _instance in unique_before.values()
            )
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
            after = self._complete_enumeration(after)
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
                windows=tuple(results),
                shortcuts=after.shortcuts,
                blocked_fingerprints=frozenset(blocked_set),
                isolated_window_count=isolated,
                anonymous_isolated_window_count=after_anonymous,
                failure_codes=(),
                foreground_handle=after.foreground_handle,
            )
            published = self._publish(serial, snapshot)
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
        snapshot = SmartReconnectObservationSnapshot(
            generation=0,
            failure_codes=tuple(dict.fromkeys(failure_codes)),
        )
        published = self._publish(serial, snapshot)
        return (
            published
            if published is not None
            else self._invalid_snapshot("observation_request_superseded")
        )

    def _publish(
        self,
        serial: int,
        snapshot: SmartReconnectObservationSnapshot,
    ) -> SmartReconnectObservationSnapshot | None:
        with self._state_lock:
            if self._closed or self._request_serial != serial:
                return None
            self._generation += 1
            current = SmartReconnectObservationSnapshot(
                generation=self._generation,
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
            self._published_snapshot_without_wait = current
            self._published_request_serial = serial
            return current

    def seal_witness(
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
                observation_generation = self._latest.generation
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
        with self._state_lock:
            if not self._closed:
                self._closed = True
                self._request_serial += 1
                self._current_witnesses.clear()
                self._published_snapshot_without_wait = None
                self._published_witnesses_without_wait = ()
        with self._active_lock:
            active = tuple(self._active.values())
        for worker in active:
            self._finish_worker(worker, kill=True)
        with self._active_lock:
            return not self._active
