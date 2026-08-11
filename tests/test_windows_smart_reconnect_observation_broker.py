import base64
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

import adapters.windows_smart_reconnect_observation_broker as broker_module
from adapters.game_screen_recognizer import ScreenRecognition
from adapters.windows_background_capture import CaptureSample
from adapters.windows_smart_reconnect_observation_broker import (
    SmartReconnectEnumerationResult,
    SmartReconnectObservationRequest,
    SmartReconnectObservationSnapshot,
    SmartReconnectShortcutObservation,
    SmartReconnectWindowObservation,
    WindowsSmartReconnectObservationBroker,
    _discover_shortcut_paths,
    _shortcut_cache_key,
    _execute_observation_request,
    _observe_window,
)
from adapters.windows_window import WindowInfo
from core.reconnect_policy import ReconnectScreenState
from core.smart_reconnect_authorization import ShortcutFileIdentity, ShortcutSeal
from core.target_window_contract import (
    ObservationActionLease,
    ObservationFreshness,
    RoleObservationCacheKey,
    ShortcutObservationCacheKey,
)
from core.window_instance import WindowInstanceToken


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows only")

FINGERPRINT = "a" * 64
SECOND_FINGERPRINT = "c" * 64


def _create_real_windows_shortcuts(
    specifications: tuple[tuple[Path, Path, str], ...],
) -> None:
    script = r"""
$ErrorActionPreference = 'Stop'
$decoded = (
    [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String($env:FLASH_TEST_SHORTCUTS_B64)
    ) | ConvertFrom-Json
)
$specifications = @($decoded)
$shell = New-Object -ComObject WScript.Shell
foreach ($specification in $specifications) {
    $path = [string]$specification.Path
    $target = [string]$specification.Target
    $shortcut = $shell.CreateShortcut($path)
    $shortcut.TargetPath = $target
    $shortcut.Arguments = [string]$specification.Arguments
    $shortcut.WorkingDirectory = [IO.Path]::GetDirectoryName($target)
    $shortcut.Save()
}
"""
    environment = os.environ.copy()
    environment["FLASH_TEST_SHORTCUTS_B64"] = base64.b64encode(
        json.dumps(
            [
                {
                    "Path": os.fspath(path),
                    "Target": os.fspath(target),
                    "Arguments": arguments,
                }
                for path, target, arguments in specifications
            ],
            ensure_ascii=False,
        ).encode("utf-8")
    ).decode("ascii")
    completed = broker_module._run_system_powershell(
        (
            broker_module._system_powershell_path(),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            base64.b64encode(script.encode("utf-16-le")).decode("ascii"),
        ),
        capture_output=True,
        text=False,
        timeout=10,
        env=environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    assert completed.returncode == 0
    assert all(path.is_file() for path, _target, _arguments in specifications)


def _windows_powershell_version() -> str:
    completed = broker_module._run_system_powershell(
        (
            broker_module._system_powershell_path(),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$PSVersionTable.PSVersion.ToString()",
        ),
        capture_output=True,
        text=False,
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    assert completed.returncode == 0
    return completed.stdout.decode("utf-8-sig").strip()


def _fake_window(offset: int = 0) -> WindowInfo:
    return WindowInfo(
        handle=1001 + offset,
        title="Adobe Flash Player",
        visible=True,
        minimized=False,
        rect=(0, 0, 800, 600),
        process_id=2001 + offset,
        window_class="FlashWindow",
        launch_fingerprint=FINGERPRINT,
        thread_id=3001 + offset,
        process_lifecycle_token=4001 + offset,
    )


def _fake_seal(root: str) -> ShortcutSeal:
    path = os.path.normcase(os.path.abspath(os.path.join(root, "role.lnk")))
    return ShortcutSeal(
        ShortcutFileIdentity(path, 10, 20),
        "b" * 64,
        FINGERPRINT,
    )


def _scenario_window(fingerprint: str, offset: int) -> WindowInfo:
    return WindowInfo(
        handle=1101 + offset,
        title="Adobe Flash Player",
        visible=True,
        minimized=False,
        rect=(0, 0, 800, 600),
        process_id=2101 + offset,
        window_class="FlashWindow",
        launch_fingerprint=fingerprint,
        thread_id=3101 + offset,
        process_lifecycle_token=4101 + offset,
    )


def _scenario_seal(
    root: Path,
    fingerprint: str,
    name: str,
    digest: str,
) -> ShortcutSeal:
    path = os.path.normcase(os.path.abspath(os.fspath(root / name)))
    return ShortcutSeal(
        ShortcutFileIdentity(path, 10, 20),
        digest * 64,
        fingerprint,
    )


def _advance_enumeration_counter(root: Path) -> int:
    path = root / "enumeration.count"
    try:
        value = int(path.read_text(encoding="ascii"))
    except (OSError, ValueError):
        value = 0
    path.write_text(str(value + 1), encoding="ascii")
    return value


def _scenario_worker(request: SmartReconnectObservationRequest):
    root = Path(request.reference_dir)
    name = root.name
    if request.stage == "enumerate":
        cycle = _advance_enumeration_counter(root)
        first = _scenario_window(FINGERPRINT, 0)
        second = _scenario_window(SECOND_FINGERPRINT, 1)
        first_digest = (
            "d" if name.startswith("shortcut-change") and cycle == 1 else "a"
        )
        windows = (
            (second,)
            if name.startswith("closed-between") and cycle == 1
            else (first, second)
        )
        if name.startswith("stale-result") and cycle == 3:
            (root / "after-entered").write_text("1", encoding="ascii")
            time.sleep(0.5)
        first_seal = _scenario_seal(
            root,
            FINGERPRINT,
            "first.lnk",
            first_digest,
        )
        second_seal = _scenario_seal(
            root,
            SECOND_FINGERPRINT,
            "second.lnk",
            "b",
        )
        return SmartReconnectEnumerationResult(
            windows,
            (
                SmartReconnectShortcutObservation(
                    first_seal.file_identity.normalized_path,
                    FINGERPRINT,
                    first_seal,
                ),
                SmartReconnectShortcutObservation(
                    second_seal.file_identity.normalized_path,
                    SECOND_FINGERPRINT,
                    second_seal,
                ),
            ),
            foreground_handle=second.handle,
        )
    if request.stage == "window":
        window = request.window
        assert window is not None
        if (
            name.startswith("one-slot-hang")
            and window.launch_fingerprint == FINGERPRINT
        ):
            time.sleep(2)
        if (
            name.startswith("one-window-failure")
            and window.launch_fingerprint == FINGERPRINT
        ):
            raise RuntimeError("isolated worker failure")
        return SmartReconnectWindowObservation(
            window=window,
            instance=WindowInstanceToken.from_window(window),
            sample=CaptureSample(1, 1, b"\0\0\0\0", True),
            recognition=ScreenRecognition(
                state=ReconnectScreenState.CONNECTED,
                score=1.0,
                click_point=None,
                reference_name="connected",
            ),
            fresh_capture=True,
            capture_route="visible",
            role_id=(
                "100古"
                if window.launch_fingerprint == FINGERPRINT
                else "100靈"
            ),
            role_region_sha256=hashlib.sha256(
                (
                    "100古"
                    if window.launch_fingerprint == FINGERPRINT
                    else "100靈"
                ).encode("utf-8")
            ).hexdigest(),
        )
    if request.stage == "seal":
        return request.expected_seal
    raise AssertionError(request.stage)


def _attributable_item_hang_worker(
    request: SmartReconnectObservationRequest,
):
    root = Path(request.reference_dir)
    kind = root.name
    first = _scenario_window(FINGERPRINT, 0)
    second = _scenario_window(SECOND_FINGERPRINT, 1)
    first_seal = _scenario_seal(root, FINGERPRINT, "first.lnk", "a")
    second_seal = _scenario_seal(root, SECOND_FINGERPRINT, "second.lnk", "b")
    if request.stage == "enumerate":
        cycle = _advance_enumeration_counter(root)
        first_shortcut = SmartReconnectShortcutObservation(
            first_seal.file_identity.normalized_path,
            FINGERPRINT,
            first_seal,
        )
        if cycle >= 2 and kind.startswith("lnk-hang"):
            first_shortcut = SmartReconnectShortcutObservation(
                first_seal.file_identity.normalized_path,
                None,
                None,
                ("shortcut_observation_pending",),
            )
        first_window = first
        if cycle >= 2 and kind.startswith("pid-hang"):
            first_window = WindowInfo(
                handle=first.handle,
                title=first.title,
                visible=first.visible,
                minimized=first.minimized,
                rect=first.rect,
                process_id=first.process_id,
                window_class=first.window_class,
                launch_fingerprint=None,
                thread_id=first.thread_id,
                process_lifecycle_token=first.process_lifecycle_token,
            )
        return SmartReconnectEnumerationResult(
            windows=(first_window, second),
            shortcuts=(
                first_shortcut,
                SmartReconnectShortcutObservation(
                    second_seal.file_identity.normalized_path,
                    SECOND_FINGERPRINT,
                    second_seal,
                ),
            ),
            foreground_handle=second.handle,
        )
    if request.stage == "shortcut_static":
        path = request.shortcut_paths[0]
        if kind.startswith("lnk-hang") and path.endswith("first.lnk"):
            time.sleep(2)
        normalized = os.path.normcase(os.path.abspath(path))
        return SmartReconnectShortcutObservation(
            normalized,
            None,
            None,
            ("shortcut_observation_pending",),
            ShortcutObservationCacheKey(
                normalized,
                ShortcutFileIdentity(normalized, 1, 1),
                1,
                1,
                "e" * 64,
            ),
        )
    if request.stage == "shortcut":
        if kind.startswith("lnk-hang") and request.shortcut_paths[0].endswith(
            "first.lnk"
        ):
            time.sleep(2)
        return _execute_observation_request(request)
    if request.stage == "identity":
        if (
            kind.startswith("pid-hang")
            and request.window is not None
            and request.window.process_id == first.process_id
        ):
            time.sleep(2)
        return _execute_observation_request(request)
    if request.stage == "window":
        window = request.window
        return SmartReconnectWindowObservation(
            window=window,
            instance=WindowInstanceToken.from_window(window),
            sample=CaptureSample(1, 1, b"\0\0\0\0", True),
            recognition=ScreenRecognition(
                ReconnectScreenState.CONNECTED,
                1.0,
                None,
                "connected",
            ),
            fresh_capture=True,
            capture_route="visible",
            role_id="100古" if window.handle == first.handle else "100靈",
            role_region_sha256=hashlib.sha256(
                (
                    "100古" if window.handle == first.handle else "100靈"
                ).encode("utf-8")
            ).hexdigest(),
        )
    if request.stage == "seal":
        return request.expected_seal
    raise AssertionError(request.stage)


def _one_formal_shortcut_hang_worker(
    request: SmartReconnectObservationRequest,
):
    if (
        request.stage == "shortcut"
        and request.shortcut_paths
        and request.shortcut_paths[0].endswith("hang.lnk")
    ):
        time.sleep(5)
    return _execute_observation_request(request)


def _shortcut_static_hang_worker(
    request: SmartReconnectObservationRequest,
):
    if (
        request.stage == "shortcut_static"
        and request.shortcut_paths
        and request.shortcut_paths[0].endswith("hang.lnk")
    ):
        time.sleep(5)
    return _execute_observation_request(request)


def _fake_worker(request: SmartReconnectObservationRequest):
    root = request.reference_dir
    if request.stage == "enumerate":
        if Path(root).name.startswith("global"):
            return SmartReconnectEnumerationResult(
                (),
                (),
                ("window_enumeration_failed",),
            )
        seal = _fake_seal(root)
        windows = (
            (_fake_window(), _fake_window(1))
            if Path(root).name.startswith("duplicate")
            else (_fake_window(),)
        )
        return SmartReconnectEnumerationResult(
            windows,
            (
                SmartReconnectShortcutObservation(
                    seal.file_identity.normalized_path,
                    FINGERPRINT,
                    seal,
                ),
            ),
            foreground_handle=_fake_window().handle,
        )
    if request.stage == "window":
        if Path(root).name.startswith("hang"):
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                creationflags=flags,
            )
            Path(root, "descendant.pid").write_text(
                str(child.pid),
                encoding="ascii",
            )
            time.sleep(30)
        window = request.window
        assert window is not None
        return SmartReconnectWindowObservation(
            window=window,
            instance=WindowInstanceToken.from_window(window),
            sample=CaptureSample(1, 1, b"\0\0\0\0", True),
            recognition=ScreenRecognition(
                state=ReconnectScreenState.CONNECTED,
                score=1.0,
                click_point=None,
                reference_name="connected",
            ),
            fresh_capture=True,
            capture_route="visible",
            role_id="100古",
            role_region_sha256=hashlib.sha256(b"fake-role-region").hexdigest(),
        )
    if request.stage == "seal":
        return request.expected_seal
    raise AssertionError(request.stage)


def _process_is_alive(process_id: int) -> bool:
    synchronize = 0x00100000
    wait_timeout = 0x00000102
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel32.OpenProcess(synchronize, False, process_id)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
    finally:
        kernel32.CloseHandle(handle)


def _steady_fingerprint(index: int) -> str:
    return f"{index + 1:064x}"


def _record_worker_event(root: Path, value: str) -> None:
    with (root / f"worker-{os.getpid()}.log").open(
        "a",
        encoding="ascii",
    ) as stream:
        stream.write(value + "\n")


def _steady_fourteen_worker(request: SmartReconnectObservationRequest):
    root = Path(request.reference_dir)
    _record_worker_event(root, request.stage)
    if request.stage == "enumerate":
        _advance_enumeration_counter(root)
        windows = tuple(
            replace(
                _scenario_window(_steady_fingerprint(index), index),
                launch_fingerprint=None,
            )
            for index in range(14)
        )
        shortcuts = tuple(
            SmartReconnectShortcutObservation(
                path=os.path.normcase(os.path.abspath(path)),
                fingerprint=None,
                seal=None,
                failure_codes=("shortcut_observation_pending",),
            )
            for index, path in enumerate(request.shortcut_paths)
        )
        return SmartReconnectEnumerationResult(
            windows,
            shortcuts,
            foreground_handle=windows[-1].handle,
        )
    if request.stage == "shortcut_static":
        path = Path(request.shortcut_paths[0])
        index = int(path.stem.rsplit("-", 1)[-1])
        normalized = os.path.normcase(os.path.abspath(path))
        return SmartReconnectShortcutObservation(
            path=normalized,
            fingerprint=None,
            seal=None,
            failure_codes=("shortcut_observation_pending",),
            cache_key=ShortcutObservationCacheKey(
                normalized,
                ShortcutFileIdentity(normalized, 1, index + 1),
                path.stat().st_mtime_ns,
                path.stat().st_size,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            ),
        )
    if request.stage == "shortcut":
        return tuple(
            SmartReconnectShortcutObservation(
                path=os.path.normcase(os.path.abspath(path)),
                fingerprint=_steady_fingerprint(index),
                seal=ShortcutSeal(
                    ShortcutFileIdentity(
                        os.path.normcase(os.path.abspath(path)),
                        1,
                        index + 1,
                    ),
                    f"{index + 1:064x}",
                    _steady_fingerprint(index),
                ),
                cache_key=ShortcutObservationCacheKey(
                    os.path.normcase(os.path.abspath(path)),
                    ShortcutFileIdentity(
                        os.path.normcase(os.path.abspath(path)),
                        1,
                        index + 1,
                    ),
                    Path(path).stat().st_mtime_ns,
                    Path(path).stat().st_size,
                    hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                ),
            )
            for index, path in enumerate(request.shortcut_paths)
        )
    if request.stage == "identity":
        return tuple(
            replace(
                window,
                launch_fingerprint=_steady_fingerprint(
                    window.process_id - 2101
                ),
            )
            for window in request.windows
        )
    if request.stage == "window":
        window = request.window
        assert window is not None
        if not request.role_cache_hit:
            _record_worker_event(root, "role-read")
        index = window.process_id - 2101
        pixel_marker = int(
            (root / "enumeration.count").read_text(encoding="ascii")
        ) % 255
        return SmartReconnectWindowObservation(
            window=window,
            instance=WindowInstanceToken.from_window(window),
            sample=CaptureSample(
                1,
                1,
                bytes((pixel_marker, 0, 0, 0)),
                True,
            ),
            recognition=ScreenRecognition(
                ReconnectScreenState.CONNECTED,
                1.0,
                None,
                "connected",
            ),
            fresh_capture=True,
            capture_route="visible",
            role_id=request.cached_role_id or f"100角{index}",
            freshness=ObservationFreshness.PROVEN_CURRENT,
            role_region_sha256=hashlib.sha256(
                f"role-region-{index}".encode("ascii")
            ).hexdigest(),
        )
    if request.stage == "seal":
        return request.expected_seal
    raise AssertionError(request.stage)


def _role_generation_barrier_worker(
    request: SmartReconnectObservationRequest,
):
    root = Path(request.reference_dir)
    if request.stage == "enumerate":
        _advance_enumeration_counter(root)
        window = _scenario_window(FINGERPRINT, 0)
        seal = _scenario_seal(root, FINGERPRINT, "role.lnk", "a")
        return SmartReconnectEnumerationResult(
            windows=(window,),
            shortcuts=(SmartReconnectShortcutObservation(
                seal.file_identity.normalized_path,
                FINGERPRINT,
                seal,
            ),),
            foreground_handle=window.handle,
        )
    if request.stage == "window":
        if int((root / "enumeration.count").read_text(encoding="ascii")) >= 3:
            (root / "role-entered").write_text("1", encoding="ascii")
            deadline = time.monotonic() + 2
            while not (root / "role-release").exists():
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
        window = request.window
        assert window is not None
        return SmartReconnectWindowObservation(
            window=window,
            instance=WindowInstanceToken.from_window(window),
            sample=CaptureSample(1, 1, b"\0\0\0\0", True),
            recognition=ScreenRecognition(
                ReconnectScreenState.CONNECTED,
                1.0,
                None,
                "connected",
            ),
            fresh_capture=True,
            capture_route="visible",
            role_id=request.cached_role_id or "100古",
            freshness=ObservationFreshness.PROVEN_CURRENT,
            role_region_sha256=hashlib.sha256(
                b"role-generation-barrier"
            ).hexdigest(),
        )
    raise AssertionError(request.stage)


def _unproven_role_worker(request: SmartReconnectObservationRequest):
    root = Path(request.reference_dir)
    window = _scenario_window(FINGERPRINT, 0)
    seal = _scenario_seal(root, FINGERPRINT, "role.lnk", "a")
    if request.stage == "enumerate":
        return SmartReconnectEnumerationResult(
            windows=(window,),
            shortcuts=(SmartReconnectShortcutObservation(
                seal.file_identity.normalized_path,
                FINGERPRINT,
                seal,
            ),),
            foreground_handle=window.handle,
        )
    if request.stage == "window":
        instance = WindowInstanceToken.from_window(window)
        return SmartReconnectWindowObservation(
            window=window,
            instance=instance,
            sample=CaptureSample(1, 1, b"\1\0\0\0", True),
            recognition=ScreenRecognition(
                ReconnectScreenState.CONNECTED,
                1.0,
                None,
                "connected",
            ),
            fresh_capture=False,
            capture_route="obscured",
            role_id="100古",
            freshness=ObservationFreshness.UNPROVEN,
            role_cache_key=RoleObservationCacheKey(
                instance,
                FINGERPRINT,
                seal,
                1,
                "1" * 64,
            ),
        )
    if request.stage == "seal":
        return request.expected_seal
    raise AssertionError(request.stage)


def _worker_event_counts(root: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for path in root.glob("worker-*.log"):
        for line in path.read_text(encoding="ascii").splitlines():
            result[line] = result.get(line, 0) + 1
    return result


def test_spawn_refresh_publishes_immutable_two_phase_snapshot(tmp_path):
    broker = WindowsSmartReconnectObservationBroker(
        reference_dir=tmp_path,
        _worker_operation=_fake_worker,
    )

    snapshot = broker.refresh((tmp_path / "role.lnk",))

    assert snapshot.generation == 1
    assert snapshot.failure_codes == ()
    assert snapshot.blocked_fingerprints == frozenset()
    assert snapshot.window_for(FINGERPRINT).role_id == "100古"
    assert snapshot.shortcut_for(FINGERPRINT).seal == _fake_seal(str(tmp_path))
    assert snapshot.foreground_handle == _fake_window().handle
    assert broker.latest_snapshot() is snapshot
    assert broker.stable_snapshot() is snapshot
    action = broker.action_snapshot()
    assert action is not None and action[0] is snapshot
    forged = ObservationActionLease(
        action[1].request_serial,
        action[1].observation_generation,
        action[1].deadline_monotonic,
    )
    assert broker.run_if_action_current(forged, lambda: True) == (False, None)
    assert broker.run_if_action_current(action[1], lambda: "ok") == (
        True,
        "ok",
    )
    assert broker.is_generation_current(snapshot.generation) is True
    assert broker.batch_timeout_seconds(0) == 18
    assert broker.batch_timeout_seconds(5) == 24
    with pytest.raises(FrozenInstanceError):
        snapshot.generation = 2
    broker.set_visible_capture_enabled(False)
    assert broker.is_generation_current(snapshot.generation) is False
    assert broker.stable_snapshot() is snapshot
    assert broker.action_snapshot() is None
    assert broker.close() is True


def test_action_lease_expires_and_explicit_invalidation_keeps_stable_snapshot(
    tmp_path,
    monkeypatch,
):
    broker = WindowsSmartReconnectObservationBroker(
        reference_dir=tmp_path,
        _worker_operation=_fake_worker,
    )
    snapshot = broker.refresh((tmp_path / "role.lnk",))
    action = broker.action_snapshot()
    assert action is not None

    monkeypatch.setattr(
        broker_module.time,
        "monotonic",
        lambda: action[1].deadline_monotonic + 1,
    )
    assert broker.action_snapshot() is None
    assert broker.run_if_action_current(action[1], lambda: "late") == (
        False,
        None,
    )
    assert broker.stable_snapshot() is snapshot
    assert broker.invalidate_action() is True
    assert broker.stable_snapshot() is snapshot
    assert broker.close() is True


def test_fourteen_window_steady_refresh_reuses_four_workers_and_static_caches(
    tmp_path,
    monkeypatch,
):
    paths = tuple(tmp_path / f"role-{index:02d}.lnk" for index in range(14))
    for path in paths:
        path.write_bytes(path.name.encode("ascii"))
    broker = WindowsSmartReconnectObservationBroker(
        reference_dir=tmp_path,
        _worker_operation=_steady_fourteen_worker,
    )
    broker.set_identity_source(1, 1)
    process_starts = []
    base_process = broker_module.multiprocessing.process.BaseProcess
    original_start = base_process.start

    def counted_start(process):
        process_starts.append(process)
        return original_start(process)

    monkeypatch.setattr(base_process, "start", counted_start)

    assert broker.start() is True
    assert len(broker._active) == 4
    first_process_ids = tuple(
        broker._slots[index].process.pid for index in range(4)
    )
    first_epochs = tuple(broker._slot_epochs)
    try:
        first = broker.refresh(paths)
        first_counts = _worker_event_counts(tmp_path)
        second = broker.refresh(paths)
        second_counts = _worker_event_counts(tmp_path)
        steady_third = broker.refresh(paths)
        steady_third_counts = _worker_event_counts(tmp_path)
        broker.set_identity_source(2, 1)
        fourth = broker.refresh(paths)
        fourth_counts = _worker_event_counts(tmp_path)
        broker.set_identity_source(2, 2)
        fifth = broker.refresh(paths)
        fifth_counts = _worker_event_counts(tmp_path)
        final_epochs = tuple(broker._slot_epochs)
        final_process_ids = tuple(
            broker._slots[index].process.pid for index in range(4)
        )
    finally:
        assert broker.close() is True

    assert len(first.windows) == 14, first
    assert len(second.windows) == 14, second
    assert len(steady_third.windows) == 14, steady_third
    assert len(fourth.windows) == 14, fourth
    assert len(fifth.windows) == 14, fifth
    assert first.changed_fingerprints == frozenset(
        _steady_fingerprint(index) for index in range(14)
    )
    assert second.changed_fingerprints == frozenset()
    assert second.static_generation == first.static_generation
    assert final_epochs == first_epochs
    assert final_process_ids == first_process_ids
    assert len(process_starts) == 4
    assert first_counts.get("enumerate", 0) == 2
    assert first_counts.get("shortcut", 0) == 1
    assert first_counts.get("shortcut_static", 0) == 14
    assert first_counts.get("identity", 0) == 1
    assert first_counts.get("role-read", 0) == 14
    assert second_counts.get("shortcut", 0) == 1
    assert second_counts.get("enumerate", 0) == 4
    assert second_counts.get("shortcut_static", 0) == 14
    assert second_counts.get("identity", 0) == 1
    assert second_counts.get("role-read", 0) == 14
    assert first.windows[0].sample != second.windows[0].sample
    assert first.windows[0].role_id == second.windows[0].role_id
    assert steady_third_counts.get("enumerate", 0) == 6
    assert steady_third_counts.get("role-read", 0) == 14
    assert steady_third_counts.get("shortcut_static", 0) == 14
    assert steady_third_counts.get("shortcut", 0) == 1
    assert steady_third_counts.get("identity", 0) == 1
    assert (
        steady_third_counts.get("role-read", 0)
        - second_counts.get("role-read", 0)
    ) == 0
    assert (
        steady_third_counts.get("shortcut_static", 0)
        - second_counts.get("shortcut_static", 0)
    ) == 0
    assert (
        steady_third_counts.get("shortcut", 0)
        - second_counts.get("shortcut", 0)
    ) == 0
    assert (
        steady_third_counts.get("identity", 0)
        - second_counts.get("identity", 0)
    ) == 0
    assert len(process_starts) == 4
    assert fourth_counts.get("role-read", 0) == 28
    assert fourth_counts.get("shortcut", 0) == 1
    assert fourth_counts.get("enumerate", 0) == 8
    assert fourth_counts.get("shortcut_static", 0) == 14
    assert fourth_counts.get("identity", 0) == 1
    assert fifth_counts.get("enumerate", 0) == 10
    assert fifth_counts.get("shortcut_static", 0) == 28
    assert fifth_counts.get("shortcut", 0) == 1
    assert broker._active == {}


def test_role_generation_change_discards_candidate_cache_and_late_snapshot(
    tmp_path,
):
    broker = WindowsSmartReconnectObservationBroker(
        reference_dir=tmp_path,
        _worker_operation=_role_generation_barrier_worker,
    )
    broker.set_identity_source(1, 1)
    first = broker.refresh()
    result = []
    worker = threading.Thread(target=lambda: result.append(broker.refresh()))
    worker.start()
    deadline = time.monotonic() + 2
    while not (tmp_path / "role-entered").exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)

    broker.set_identity_source(2, 1)
    (tmp_path / "role-release").write_text("1", encoding="ascii")
    worker.join(3)

    assert worker.is_alive() is False
    assert result[0].failure_codes == ("observation_request_superseded",)
    assert broker.stable_snapshot() is first
    assert broker.action_snapshot() is None
    assert broker._role_cache == {}
    assert broker._role_cache_by_instance == {}
    assert broker.close() is True


def test_window_timeout_isolates_only_target_and_kills_descendant(tmp_path):
    root = tmp_path / "hang-observation"
    root.mkdir()
    broker = WindowsSmartReconnectObservationBroker(
        reference_dir=root,
        _worker_operation=_fake_worker,
    )

    snapshot = broker.refresh((root / "role.lnk",))

    assert snapshot.failure_codes == ()
    assert snapshot.windows == ()
    assert snapshot.blocked_fingerprints == frozenset((FINGERPRINT,))
    assert snapshot.isolated_window_count == 1
    process_id = int((root / "descendant.pid").read_text(encoding="ascii"))
    deadline = time.monotonic() + 2
    while _process_is_alive(process_id) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _process_is_alive(process_id) is False
    assert broker.close() is True


def test_global_enumeration_failure_rejects_whole_snapshot(tmp_path):
    root = tmp_path / "global-failure"
    root.mkdir()
    broker = WindowsSmartReconnectObservationBroker(
        reference_dir=root,
        _worker_operation=_fake_worker,
    )

    snapshot = broker.refresh()

    assert snapshot.windows == ()
    assert snapshot.failure_codes == ("window_enumeration_failed",)
    assert broker.close() is True


def test_duplicate_identity_counts_each_isolated_window(tmp_path):
    root = tmp_path / "duplicate-windows"
    root.mkdir()
    broker = WindowsSmartReconnectObservationBroker(
        reference_dir=root,
        _worker_operation=_fake_worker,
    )

    snapshot = broker.refresh((root / "role.lnk",))

    assert snapshot.windows == ()
    assert snapshot.blocked_fingerprints == frozenset((FINGERPRINT,))
    assert snapshot.isolated_window_count == 2
    assert snapshot.anonymous_isolated_window_count == 0
    assert broker.close() is True


def test_close_invalidates_inflight_result_and_cleans_worker_tree(tmp_path):
    root = tmp_path / "hang-close"
    root.mkdir()
    broker = WindowsSmartReconnectObservationBroker(
        reference_dir=root,
        _worker_operation=_fake_worker,
    )
    completed = threading.Event()

    thread = threading.Thread(
        target=lambda: (broker.refresh(), completed.set()),
        daemon=True,
    )
    thread.start()
    descendant_pid_path = root / "descendant.pid"
    descendant_process_id = 0
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            descendant_process_id = int(
                descendant_pid_path.read_text(encoding="ascii").strip()
            )
        except (OSError, ValueError):
            descendant_process_id = 0
        if descendant_process_id > 0:
            break
        time.sleep(0.02)
    assert descendant_process_id > 0
    worker_process_ids = tuple(
        worker.process.pid for worker in broker._active.values()
    )
    assert len(worker_process_ids) == 4

    assert broker.close() is True
    assert completed.wait(2)
    thread.join(1)
    process_ids = (*worker_process_ids, descendant_process_id)
    deadline = time.monotonic() + 2
    while (
        any(_process_is_alive(process_id) for process_id in process_ids)
        and time.monotonic() < deadline
    ):
        time.sleep(0.02)
    assert thread.is_alive() is False
    assert all(
        _process_is_alive(process_id) is False
        for process_id in process_ids
    )
    assert broker._active == {}
    assert broker._slots == {}
    assert broker.latest_snapshot() == SmartReconnectObservationSnapshot(0)


def test_shortcut_discovery_is_limited_to_desktop_and_123_first_level(
    tmp_path,
):
    desktop = tmp_path / "Desktop"
    child = desktop / "123"
    deeper = child / "nested"
    repeated = child / "123"
    other = desktop / "other"
    deeper.mkdir(parents=True)
    repeated.mkdir()
    other.mkdir()
    root_shortcut = desktop / "root.lnk"
    child_shortcut = child / "child.lnk"
    deep_shortcut = deeper / "deep.lnk"
    other_shortcut = other / "other.lnk"
    repeated_shortcut = repeated / "repeated.lnk"
    for path in (
        root_shortcut,
        child_shortcut,
        deep_shortcut,
        other_shortcut,
        repeated_shortcut,
    ):
        path.write_bytes(b"shortcut")

    discovered = _discover_shortcut_paths(
        (),
        (os.fspath(desktop), os.fspath(child), os.fspath(child)),
    )

    assert set(discovered) == {
        root_shortcut.resolve(),
        child_shortcut.resolve(),
    }


def test_shortcut_cache_key_detects_same_size_same_timestamp_content_change(
    tmp_path,
):
    shortcut = tmp_path / "role.lnk"
    shortcut.write_bytes(b"first-value")
    first_stat = shortcut.stat()
    first = _shortcut_cache_key(shortcut)

    shortcut.write_bytes(b"other-value")
    os.utime(
        shortcut,
        ns=(first_stat.st_atime_ns, first_stat.st_mtime_ns),
    )
    second = _shortcut_cache_key(shortcut)

    assert first is not None and second is not None
    assert first.modified_ns == second.modified_ns
    assert first.size == second.size
    assert first.content_sha256 != second.content_sha256


def test_shortcut_cache_key_detects_atomic_file_identity_replacement(tmp_path):
    shortcut = tmp_path / "role.lnk"
    replacement = tmp_path / "replacement.lnk"
    shortcut.write_bytes(b"same-value")
    replacement.write_bytes(b"same-value")
    first_stat = shortcut.stat()
    os.utime(
        replacement,
        ns=(first_stat.st_atime_ns, first_stat.st_mtime_ns),
    )
    first = _shortcut_cache_key(shortcut)

    os.replace(replacement, shortcut)
    second = _shortcut_cache_key(shortcut)

    assert first is not None and second is not None
    assert first.modified_ns == second.modified_ns
    assert first.size == second.size
    assert first.content_sha256 == second.content_sha256
    assert first.file_identity != second.file_identity


def test_one_shortcut_change_isolates_only_its_window(tmp_path):
    root = tmp_path / "shortcut-change"
    root.mkdir()
    broker = WindowsSmartReconnectObservationBroker(
        reference_dir=root,
        _worker_operation=_scenario_worker,
    )

    snapshot = broker.refresh()

    assert snapshot.failure_codes == ()
    assert snapshot.blocked_fingerprints == frozenset((FINGERPRINT,))
    assert tuple(
        item.window.launch_fingerprint for item in snapshot.windows
    ) == (SECOND_FINGERPRINT,)
    assert snapshot.isolated_window_count == 1
    assert FINGERPRINT not in broker._process_cache.values()
    assert all(
        key.fingerprint != FINGERPRINT for key in broker._role_cache
    )
    assert broker.close() is True


def test_one_window_worker_failure_keeps_safe_sibling(tmp_path):
    root = tmp_path / "one-window-failure"
    root.mkdir()
    broker = WindowsSmartReconnectObservationBroker(
        reference_dir=root,
        _worker_operation=_scenario_worker,
    )

    snapshot = broker.refresh()

    assert snapshot.failure_codes == ()
    assert snapshot.blocked_fingerprints == frozenset((FINGERPRINT,))
    assert tuple(
        item.window.launch_fingerprint for item in snapshot.windows
    ) == (SECOND_FINGERPRINT,)
    assert broker.close() is True


def test_one_hung_slot_is_rebuilt_while_sibling_slots_continue(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "one-slot-hang"
    root.mkdir()
    monkeypatch.setattr(broker_module, "WINDOW_TIMEOUT_SECONDS", 0.5)
    broker = WindowsSmartReconnectObservationBroker(
        reference_dir=root,
        _worker_operation=_scenario_worker,
    )
    assert broker.start() is True
    before_pids = tuple(broker._slots[index].process.pid for index in range(4))
    before_epochs = tuple(broker._slot_epochs)

    snapshot = broker.refresh()

    after_pids = tuple(broker._slots[index].process.pid for index in range(4))
    assert snapshot.failure_codes == ()
    assert snapshot.blocked_fingerprints == frozenset((FINGERPRINT,))
    assert tuple(
        item.window.launch_fingerprint for item in snapshot.windows
    ) == (SECOND_FINGERPRINT,)
    assert broker._slot_epochs[0] == before_epochs[0] + 1
    assert after_pids[0] != before_pids[0]
    assert after_pids[1:] == before_pids[1:]
    assert len(broker._active) == 4
    assert broker.close() is True


def test_window_closed_between_enumerations_is_counted_locally(tmp_path):
    root = tmp_path / "closed-between"
    root.mkdir()
    broker = WindowsSmartReconnectObservationBroker(
        reference_dir=root,
        _worker_operation=_scenario_worker,
    )

    snapshot = broker.refresh()

    assert snapshot.failure_codes == ()
    assert snapshot.blocked_fingerprints == frozenset((FINGERPRINT,))
    assert tuple(
        item.window.launch_fingerprint for item in snapshot.windows
    ) == (SECOND_FINGERPRINT,)
    assert snapshot.isolated_window_count == 1
    assert broker.close() is True


def test_superseded_refresh_never_returns_previous_published_snapshot(tmp_path):
    root = tmp_path / "stale-result"
    root.mkdir()
    broker = WindowsSmartReconnectObservationBroker(
        reference_dir=root,
        _worker_operation=_scenario_worker,
    )
    previous = broker.refresh()
    result = []
    thread = threading.Thread(target=lambda: result.append(broker.refresh()))
    thread.start()
    marker = root / "after-entered"
    deadline = time.monotonic() + 2
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()
    assert broker.stable_snapshot() is previous
    assert broker.published_snapshot_without_wait() is previous
    assert broker.action_snapshot() is None

    broker.set_visible_capture_enabled(False)
    thread.join(3)

    assert thread.is_alive() is False
    assert len(result) == 1
    assert result[0] is not previous
    assert result[0].generation == 0
    assert result[0].failure_codes == ("observation_request_superseded",)
    assert broker.latest_snapshot() is previous
    assert broker.is_generation_current(previous.generation) is False
    assert broker.close() is True


def test_second_worker_start_failure_cleans_first_started_worker(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "hang-start-failure"
    root.mkdir()
    broker = WindowsSmartReconnectObservationBroker(
        reference_dir=root,
        _worker_operation=_fake_worker,
    )
    original = broker._start_worker
    started_process_ids = []
    calls = 0

    def fail_second(slot_index):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second process start failed")
        worker = original(slot_index)
        started_process_ids.append(worker.process.pid)
        return worker

    monkeypatch.setattr(broker, "_start_worker", fail_second)
    assert broker.start() is False
    assert calls == 2
    assert len(started_process_ids) == 1
    assert _process_is_alive(started_process_ids[0]) is False
    assert broker._active == {}
    assert broker.close() is True


def test_seal_witness_is_process_fresh_and_invalidated_by_refresh(tmp_path):
    broker = WindowsSmartReconnectObservationBroker(
        reference_dir=tmp_path,
        _worker_operation=_fake_worker,
    )
    snapshot = broker.refresh()
    seal = snapshot.shortcut_for(FINGERPRINT).seal

    witness = broker.seal_witness(seal)

    assert witness is not None
    assert broker.witness_is_current(witness) is True
    assert broker.seal_is_witnessed(seal) is True
    broker.refresh()
    assert broker.witness_is_current(witness) is False
    assert broker.seal_is_witnessed(seal) is False
    assert broker.close() is True


def test_default_spawn_worker_runs_real_bounded_windows_and_capture_chain(
    tmp_path,
):
    shortcut = tmp_path / "invalid-but-scoped.lnk"
    shortcut.write_bytes(b"not a real shortcut")
    broker = WindowsSmartReconnectObservationBroker(reference_dir=tmp_path)

    enumerated = broker._request(
        broker._enumeration_request((os.fspath(shortcut),)),
        3.0,
    )
    observed = broker._request(
        SmartReconnectObservationRequest(
            stage="window",
            reference_dir=os.fspath(tmp_path),
            title_keywords=("adobe flash player",),
            window=_fake_window(),
        ),
        3.0,
    )

    assert isinstance(enumerated, SmartReconnectEnumerationResult)
    assert isinstance(observed, SmartReconnectWindowObservation)
    assert observed.instance == WindowInstanceToken.from_window(_fake_window())
    assert observed.role_id is None
    assert "role_visible_evidence_unavailable" in observed.failure_codes
    assert "background_capture_unknown" in observed.failure_codes
    assert broker.close() is True


def test_each_powershell_call_strictly_resets_then_restores_dll_directory(
    monkeypatch,
):
    events = []
    role_reads = []
    role_reads = []

    def reset():
        events.append("reset")
        return "previous"

    def run(command, **_kwargs):
        events.append(("run", tuple(command)))
        return subprocess.CompletedProcess(command, 0, b"{}", b"")

    def restore(previous):
        events.append(("restore", previous))

    monkeypatch.setattr(broker_module, "_worker_dll_directory_reset", reset)
    monkeypatch.setattr(broker_module.subprocess, "run", run)
    monkeypatch.setattr(
        broker_module,
        "_worker_dll_directory_restore",
        restore,
    )

    broker_module._run_system_powershell(("powershell.exe", "-Command", "1"))

    assert events[0] == "reset"
    assert events[1][0] == "run"
    assert events[2] == ("restore", "previous")

    events.clear()

    def reset_failure():
        events.append("reset-failed")
        raise broker_module._WorkerEnvironmentError("reset failed")

    monkeypatch.setattr(
        broker_module,
        "_worker_dll_directory_reset",
        reset_failure,
    )
    with pytest.raises(broker_module._WorkerEnvironmentError):
        broker_module._run_system_powershell(("powershell.exe",))
    assert events == ["reset-failed"]

    monkeypatch.setattr(broker_module, "_worker_dll_directory_reset", reset)

    def restore_failure(_previous):
        events.append("restore-failed")
        raise broker_module._WorkerEnvironmentError("restore failed")

    monkeypatch.setattr(
        broker_module,
        "_worker_dll_directory_restore",
        restore_failure,
    )
    events.clear()
    with pytest.raises(broker_module._WorkerEnvironmentError):
        broker_module._run_system_powershell(("powershell.exe",))
    assert events[0] == "reset"
    assert events[1][0] == "run"
    assert events[2] == "restore-failed"


def test_generation_check_and_memory_publish_are_one_atomic_broker_domain(
    tmp_path,
):
    broker = WindowsSmartReconnectObservationBroker(
        reference_dir=tmp_path,
        _worker_operation=_fake_worker,
    )
    snapshot = broker.refresh()
    callback_entered = threading.Event()
    release_callback = threading.Event()
    invalidation_complete = threading.Event()
    result = []

    publisher = threading.Thread(
        target=lambda: result.append(
            broker.run_if_generation_current(
                snapshot.generation,
                lambda: (
                    callback_entered.set(),
                    release_callback.wait(2),
                    "published",
                )[-1],
            )
        )
    )
    publisher.start()
    assert callback_entered.wait(1)
    invalidator = threading.Thread(
        target=lambda: (
            broker.set_visible_capture_enabled(False),
            invalidation_complete.set(),
        )
    )
    invalidator.start()
    assert invalidation_complete.wait(0.1) is False

    release_callback.set()
    publisher.join(2)
    invalidator.join(2)

    assert result == [(True, "published")]
    assert invalidation_complete.is_set()
    assert broker.is_generation_current(snapshot.generation) is False
    assert broker.close() is True


def test_stable_pointer_and_action_callback_never_hold_state_lock(tmp_path):
    broker = WindowsSmartReconnectObservationBroker(
        reference_dir=tmp_path,
        _worker_operation=_fake_worker,
    )
    snapshot = broker.refresh()
    action = broker.action_snapshot()
    assert action is not None
    state_held = threading.Event()
    release_state = threading.Event()

    def hold_state():
        with broker._state_lock:
            state_held.set()
            release_state.wait(2)

    holder = threading.Thread(target=hold_state)
    holder.start()
    assert state_held.wait(1)
    started = time.monotonic()
    assert broker.stable_snapshot() is snapshot
    assert time.monotonic() - started < 0.1
    release_state.set()
    holder.join(1)

    acquired = threading.Event()

    def prove_callback_state_is_free():
        def take_state():
            with broker._state_lock:
                acquired.set()

        probe = threading.Thread(target=take_state)
        probe.start()
        assert acquired.wait(0.5)
        probe.join(1)
        return "free"

    assert broker.run_if_action_current(
        action[1],
        prove_callback_state_is_free,
    ) == (True, "free")
    assert broker.close() is True


@pytest.mark.parametrize("kind", ("lnk-hang", "pid-hang"))
def test_formal_item_timeout_isolates_prior_target_and_keeps_sibling(
    tmp_path,
    monkeypatch,
    kind,
):
    root = tmp_path / kind
    root.mkdir()
    monkeypatch.setattr(broker_module, "WINDOW_TIMEOUT_SECONDS", 1.0)
    broker = WindowsSmartReconnectObservationBroker(
        reference_dir=root,
        _worker_operation=_attributable_item_hang_worker,
    )
    first = broker.refresh()

    second = broker.refresh()

    assert tuple(item.window.launch_fingerprint for item in first.windows) == (
        FINGERPRINT,
        SECOND_FINGERPRINT,
    )
    assert second.failure_codes == ()
    assert second.blocked_fingerprints == frozenset((FINGERPRINT,))
    assert tuple(item.window.launch_fingerprint for item in second.windows) == (
        SECOND_FINGERPRINT,
    )
    assert second.isolated_window_count == 1
    assert len(broker._active) == 4
    assert broker.close() is True


def test_windows_powershell_51_expands_multiple_paths_for_both_identity_batches(
    tmp_path,
):
    assert _windows_powershell_version().startswith("5.1")
    sleeper = tmp_path / "sleeping_identity_target.py"
    sleeper.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    identities = tuple(f"identity-{index:03d}" for index in range(127))
    paths = tuple(tmp_path / f"identity-{index:03d}.lnk" for index in range(127))
    arguments = tuple(
        subprocess.list2cmdline((os.fspath(sleeper), identity))
        for identity in identities
    )
    _create_real_windows_shortcuts(tuple(
        (path, Path(sys.executable), value)
        for path, value in zip(paths, arguments)
    ))
    expected_fingerprints = tuple(
        hashlib.sha256(value.encode("utf-8")).hexdigest()
        for value in arguments
    )

    static_observations = tuple(
        _execute_observation_request(SmartReconnectObservationRequest(
            stage="shortcut_static",
            reference_dir=os.fspath(tmp_path),
            title_keywords=("Adobe Flash Player",),
            shortcut_paths=(os.fspath(path),),
        ))
        for path in paths
    )
    shortcut_observations = _execute_observation_request(
        SmartReconnectObservationRequest(
            stage="shortcut",
            reference_dir=os.fspath(tmp_path),
            title_keywords=("Adobe Flash Player",),
            shortcut_paths=tuple(os.fspath(path) for path in paths),
            shortcut_static_observations=static_observations,
        )
    )
    assert isinstance(shortcut_observations, tuple)
    assert len(shortcut_observations) == 127
    assert tuple(item.fingerprint for item in shortcut_observations) == (
        expected_fingerprints
    )
    assert all(item.seal is not None for item in shortcut_observations)
    assert all(item.failure_codes == () for item in shortcut_observations)

    children: list[subprocess.Popen] = []
    try:
        for identity in identities[:12]:
            children.append(subprocess.Popen(
                (sys.executable, os.fspath(sleeper), identity),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ))
        assert len(children) == 12
        assert all(child.poll() is None for child in children)
        windows = tuple(
            replace(
                _fake_window(index),
                process_id=child.pid,
                launch_fingerprint=None,
            )
            for index, child in enumerate(children)
        )
        resolved_windows = _execute_observation_request(
            SmartReconnectObservationRequest(
                stage="identity",
                reference_dir=os.fspath(tmp_path),
                title_keywords=("Adobe Flash Player",),
                shortcut_paths=tuple(os.fspath(path) for path in paths),
                windows=windows,
            )
        )
        assert isinstance(resolved_windows, tuple)
        assert len(resolved_windows) == 12
        assert tuple(
            window.launch_fingerprint for window in resolved_windows
        ) == expected_fingerprints[:12]
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        assert all(child.poll() is not None for child in children)


def test_formal_complete_enumeration_accepts_bounded_partial_shortcut_batch(
    tmp_path,
    monkeypatch,
):
    hanging = tmp_path / "hang.lnk"
    sibling = tmp_path / "sibling.lnk"
    hanging.write_bytes(b"invalid")
    sibling.write_bytes(b"invalid")
    power_shell_calls = []

    def bounded_partial_result(command, **kwargs):
        power_shell_calls.append((tuple(command), kwargs["env"]))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                '{"1":"' + SECOND_FINGERPRINT + '"}'
            ).encode("utf-8"),
            stderr=b"",
        )

    monkeypatch.setattr(
        broker_module,
        "_run_system_powershell",
        bounded_partial_result,
    )
    broker = WindowsSmartReconnectObservationBroker(reference_dir=tmp_path)
    monkeypatch.setattr(
        broker,
        "_request",
        lambda request, _timeout: _execute_observation_request(request),
    )
    monkeypatch.setattr(
        broker,
        "_request_bounded",
        lambda requests, _timeout: tuple(
            _execute_observation_request(request) for request in requests
        ),
    )
    raw = SmartReconnectEnumerationResult(
        windows=(),
        shortcuts=tuple(
            SmartReconnectShortcutObservation(
                os.path.normcase(os.path.abspath(path)),
                None,
                None,
                ("shortcut_observation_pending",),
                _shortcut_cache_key(path),
            )
            for path in (hanging, sibling)
        ),
    )

    completed = broker._complete_enumeration(raw)

    assert len(power_shell_calls) == 1
    assert completed.shortcuts[0].failure_codes == (
        "shortcut_identity_unresolved",
    )
    assert completed.shortcuts[1].fingerprint == SECOND_FINGERPRINT
    assert completed.shortcuts[1].failure_codes == ()
    assert broker.close() is True


def test_shortcut_static_hang_skips_failed_path_and_resolves_sibling_once(
    tmp_path,
    monkeypatch,
):
    hanging = tmp_path / "hang.lnk"
    sibling = tmp_path / "sibling.lnk"
    hanging.write_bytes(b"hang")
    sibling.write_bytes(b"safe")
    monkeypatch.setattr(broker_module, "WINDOW_TIMEOUT_SECONDS", 1.0)
    broker = WindowsSmartReconnectObservationBroker(
        reference_dir=tmp_path,
        _worker_operation=_shortcut_static_hang_worker,
    )
    shortcut_batches = []

    def resolve_shortcut_batch(request, _timeout):
        shortcut_batches.append(request)

        def no_file_probe(_path):
            raise AssertionError("shortcut batch repeated static file I/O")

        monkeypatch.setattr(
            broker_module,
            "_shortcut_cache_key",
            no_file_probe,
        )
        monkeypatch.setattr(
            broker_module.Win32ShortcutFileIdentityProvider,
            "identity_for",
            no_file_probe,
        )
        monkeypatch.setattr(
            broker_module,
            "_run_system_powershell",
            lambda command, **_kwargs: subprocess.CompletedProcess(
                command,
                0,
                ('{"0":"' + SECOND_FINGERPRINT + '"}').encode("utf-8"),
                b"",
            ),
        )
        return _execute_observation_request(request)

    monkeypatch.setattr(broker, "_request", resolve_shortcut_batch)
    raw = SmartReconnectEnumerationResult(
        windows=(),
        shortcuts=tuple(
            SmartReconnectShortcutObservation(
                path=os.path.normcase(os.path.abspath(path)),
                fingerprint=None,
                seal=None,
                failure_codes=("shortcut_observation_pending",),
            )
            for path in (hanging, sibling)
        ),
    )

    completed = broker._complete_enumeration(raw)

    assert len(shortcut_batches) == 1
    assert shortcut_batches[0].shortcut_paths == (
        os.path.normcase(os.path.abspath(sibling)),
    )
    assert len(shortcut_batches[0].shortcut_static_observations) == 1
    assert completed.shortcuts[0].failure_codes == (
        "shortcut_static_timeout",
    )
    assert completed.shortcuts[0].cache_key is None
    assert completed.shortcuts[1].fingerprint == SECOND_FINGERPRINT
    assert completed.shortcuts[1].seal is not None
    assert completed.shortcuts[1].failure_codes == ()
    assert broker.close() is True


def test_formal_complete_enumeration_accepts_bounded_partial_process_batch(
    tmp_path,
    monkeypatch,
):
    paths = (tmp_path / "first.lnk", tmp_path / "second.lnk")
    for path in paths:
        path.write_bytes(b"shortcut")
    windows = tuple(
        replace(
            _scenario_window(fingerprint, index),
            launch_fingerprint=None,
        )
        for index, fingerprint in enumerate(
            (FINGERPRINT, SECOND_FINGERPRINT)
        )
    )
    shortcuts = tuple(
        SmartReconnectShortcutObservation(
            os.path.normcase(os.path.abspath(path)),
            fingerprint,
            _scenario_seal(tmp_path, fingerprint, path.name, digest),
            cache_key=_shortcut_cache_key(path),
        )
        for path, fingerprint, digest in zip(
            paths,
            (FINGERPRINT, SECOND_FINGERPRINT),
            ("a", "b"),
        )
    )
    power_shell_calls = []

    def bounded_partial_result(command, **kwargs):
        power_shell_calls.append((tuple(command), kwargs["env"]))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                '{"' + str(windows[1].process_id) + '":"'
                + SECOND_FINGERPRINT + '"}'
            ).encode("utf-8"),
            stderr=b"",
        )

    monkeypatch.setattr(
        broker_module,
        "_run_system_powershell",
        bounded_partial_result,
    )
    broker = WindowsSmartReconnectObservationBroker(reference_dir=tmp_path)
    monkeypatch.setattr(
        broker,
        "_request",
        lambda request, _timeout: _execute_observation_request(request),
    )
    monkeypatch.setattr(
        broker,
        "_request_bounded",
        lambda requests, _timeout: tuple(
            _execute_observation_request(request) for request in requests
        ),
    )

    completed = broker._complete_enumeration(
        SmartReconnectEnumerationResult(windows, shortcuts)
    )

    assert len(power_shell_calls) == 1
    assert completed.windows[0].launch_fingerprint is None
    assert completed.windows[1].launch_fingerprint == SECOND_FINGERPRINT
    assert broker.close() is True


@pytest.mark.parametrize(
    (
        "minimized",
        "visible_available",
        "expected_route",
        "expected_freshness",
    ),
    (
        (False, True, "visible", ObservationFreshness.PROVEN_CURRENT),
        (False, False, "obscured", ObservationFreshness.UNPROVEN),
        (True, False, "minimized", ObservationFreshness.UNPROVEN),
    ),
)
def test_only_visible_desktop_pixels_are_proven_current(
    tmp_path,
    monkeypatch,
    minimized,
    visible_available,
    expected_route,
    expected_freshness,
):
    events = []
    role_reads = []
    background_sample = CaptureSample(1, 1, b"\1\0\0\0", True)
    visible_sample = CaptureSample(1, 1, b"\2\0\0\0", True)

    class RoleReader:
        def read(self, _handle):
            role_reads.append(len(role_reads) + 1)
            return f"100古{len(role_reads)}"
            return "100古"
            return type("RoleResult", (), {"success": True, "role_id": "100古"})()

    class BackgroundProvider:
        def capture(self, _handle):
            events.append("background")
            return background_sample

    class VisibleProvider:
        def capture(self, _handle):
            events.append("visible")
            return visible_sample if visible_available else None

    class Recognizer:
        def __init__(self, _root):
            pass

        def recognize_capture(self, _sample):
            return ScreenRecognition(
                ReconnectScreenState.CONNECTED,
                1.0,
                None,
                "connected",
            )

    monkeypatch.setattr(broker_module, "WindowsRoleIdOcrReader", RoleReader)
    monkeypatch.setattr(
        broker_module,
        "role_id_region_sample",
        lambda sample: sample,
    )
    monkeypatch.setattr(
        broker_module,
        "Win32PrintWindowProvider",
        BackgroundProvider,
    )
    monkeypatch.setattr(
        broker_module,
        "Win32VisibleRegionCaptureProvider",
        VisibleProvider,
    )
    monkeypatch.setattr(broker_module, "ReferenceScreenRecognizer", Recognizer)
    window = replace(_fake_window(), minimized=minimized)
    request = SmartReconnectObservationRequest(
        stage="window",
        reference_dir=os.fspath(tmp_path),
        title_keywords=("adobe flash player",),
        window=window,
        visible_capture_enabled=True,
        obscured_capture_enabled=True,
        minimized_capture_enabled=True,
    )

    observed = _observe_window(request)

    assert observed.capture_route == expected_route
    assert observed.sample is (
        visible_sample if expected_route == "visible" else background_sample
    )
    assert observed.freshness is expected_freshness
    assert observed.fresh_capture is (
        expected_freshness is ObservationFreshness.PROVEN_CURRENT
    )
    assert observed.recognition.state is (
        ReconnectScreenState.CONNECTED
        if expected_freshness is ObservationFreshness.PROVEN_CURRENT
        else ReconnectScreenState.UNKNOWN
    )
    assert events == (["background"] if minimized else ["background", "visible"])
    if expected_freshness is ObservationFreshness.PROVEN_CURRENT:
        assert role_reads == [1]
        assert observed.role_id is not None
        assert observed.role_cache_key is None
    else:
        assert role_reads == []
        assert observed.role_id is None
        assert observed.role_cache_key is None


def test_unproven_background_role_is_removed_from_snapshot_and_caches(
    tmp_path,
):
    broker = WindowsSmartReconnectObservationBroker(
        reference_dir=tmp_path,
        _worker_operation=_unproven_role_worker,
    )

    snapshot = broker.refresh()
    observed = snapshot.window_for(FINGERPRINT)

    assert observed is not None
    assert observed.capture_route == "obscured"
    assert observed.freshness is ObservationFreshness.UNPROVEN
    assert observed.fresh_capture is False
    assert observed.role_id is None
    assert observed.role_cache_key is None
    assert broker._role_cache == {}
    assert broker._role_cache_by_instance == {}
    assert broker.close() is True


def test_role_cache_reuses_only_while_role_region_is_unchanged(
    tmp_path,
    monkeypatch,
):
    role_reads = []
    visible_sample = [CaptureSample(2, 1, b"\2\0\0\0\3\0\0\0", True)]
    role_sample = [CaptureSample(1, 1, b"\7\0\0\0", True)]
    role_ids = ["100古"]

    class RoleReader:
        def read(self, _sample):
            role_reads.append(len(role_reads) + 1)
            return role_ids[0]

    class BackgroundProvider:
        def capture(self, _handle):
            return CaptureSample(1, 1, b"\1\0\0\0", True)

    class VisibleProvider:
        def capture(self, _handle):
            return visible_sample[0]

    class Recognizer:
        def __init__(self, _root):
            pass

        def recognize_capture(self, _sample):
            return ScreenRecognition(
                ReconnectScreenState.CONNECTED,
                1.0,
                None,
                "connected",
            )

    monkeypatch.setattr(broker_module, "WindowsRoleIdOcrReader", RoleReader)
    monkeypatch.setattr(
        broker_module,
        "role_id_region_sample",
        lambda _sample: role_sample[0],
    )
    monkeypatch.setattr(
        broker_module,
        "Win32PrintWindowProvider",
        BackgroundProvider,
    )
    monkeypatch.setattr(
        broker_module,
        "Win32VisibleRegionCaptureProvider",
        VisibleProvider,
    )
    monkeypatch.setattr(broker_module, "ReferenceScreenRecognizer", Recognizer)
    window = _fake_window()
    base_request = SmartReconnectObservationRequest(
        stage="window",
        reference_dir=os.fspath(tmp_path),
        title_keywords=("adobe flash player",),
        window=window,
    )

    first = _observe_window(base_request)
    assert first.role_region_sha256 is not None
    first_key = RoleObservationCacheKey(
        WindowInstanceToken.from_window(window),
        FINGERPRINT,
        _fake_seal(os.fspath(tmp_path)),
        1,
        first.role_region_sha256,
    )
    same = _observe_window(replace(
        base_request,
        cached_role_id=first.role_id,
        role_cache_hit=True,
        role_cache_key=first_key,
    ))
    visible_sample[0] = CaptureSample(
        2,
        1,
        b"\4\0\0\0\5\0\0\0",
        True,
    )
    non_role_changed = _observe_window(replace(
        base_request,
        cached_role_id=first.role_id,
        role_cache_hit=True,
        role_cache_key=first_key,
    ))
    role_sample[0] = CaptureSample(1, 1, bytes((8, 0, 0, 0)), True)
    role_ids[0] = "100靈"
    role_changed = _observe_window(replace(
        base_request,
        cached_role_id=first.role_id,
        role_cache_hit=True,
        role_cache_key=first_key,
    ))
    assert role_changed.role_region_sha256 is not None
    changed_key = replace(
        first_key,
        role_region_sha256=role_changed.role_region_sha256,
    )
    stable_new_role = _observe_window(replace(
        base_request,
        cached_role_id=role_changed.role_id,
        role_cache_hit=True,
        role_cache_key=changed_key,
    ))

    assert role_reads == [1, 2]
    assert same.role_id == first.role_id
    assert non_role_changed.role_id == first.role_id
    assert non_role_changed.role_region_sha256 == first.role_region_sha256
    assert role_changed.role_id == "100靈"
    assert role_changed.role_region_sha256 != first.role_region_sha256
    assert stable_new_role.role_id == role_changed.role_id
    assert stable_new_role.role_region_sha256 == role_changed.role_region_sha256
