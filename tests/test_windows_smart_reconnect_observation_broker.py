import ctypes
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
    _execute_observation_request,
    _observe_window,
)
from adapters.windows_window import WindowInfo
from core.reconnect_policy import ReconnectScreenState
from core.smart_reconnect_authorization import ShortcutFileIdentity, ShortcutSeal
from core.window_instance import WindowInstanceToken


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows only")

FINGERPRINT = "a" * 64
SECOND_FINGERPRINT = "c" * 64


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
        time.sleep(2)
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
    assert broker.is_generation_current(snapshot.generation) is True
    assert broker.batch_timeout_seconds(0) == 6
    assert broker.batch_timeout_seconds(5) == 12
    with pytest.raises(FrozenInstanceError):
        snapshot.generation = 2
    broker.set_visible_capture_enabled(False)
    assert broker.is_generation_current(snapshot.generation) is False
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
    deadline = time.monotonic() + 2
    while not (root / "descendant.pid").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert (root / "descendant.pid").exists()

    assert broker.close() is True
    assert completed.wait(2)
    thread.join(1)
    assert thread.is_alive() is False
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

    def fail_second(request, timeout_seconds):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second process start failed")
        worker = original(request, timeout_seconds)
        started_process_ids.append(worker.process.pid)
        return worker

    monkeypatch.setattr(broker, "_start_worker", fail_second)
    requests = tuple(
        SmartReconnectObservationRequest(
            stage="window",
            reference_dir=os.fspath(root),
            title_keywords=("adobe flash player",),
            window=_fake_window(offset),
        )
        for offset in (0, 1)
    )

    assert broker._request_many(requests, 0.2) == (None, None)
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
    assert "role_identity_unresolved" in observed.failure_codes
    assert "background_capture_unknown" in observed.failure_codes
    assert broker.close() is True


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
    assert broker._active == {}
    assert broker.close() is True


def test_formal_shortcut_workers_timeout_independently(tmp_path):
    hanging = tmp_path / "hang.lnk"
    sibling = tmp_path / "sibling.lnk"
    hanging.write_bytes(b"invalid")
    sibling.write_bytes(b"invalid")
    broker = WindowsSmartReconnectObservationBroker(
        reference_dir=tmp_path,
        _worker_operation=_one_formal_shortcut_hang_worker,
    )
    requests = tuple(
        SmartReconnectObservationRequest(
            stage="shortcut",
            reference_dir=os.fspath(tmp_path),
            title_keywords=("adobe flash player",),
            shortcut_paths=(os.fspath(path),),
        )
        for path in (hanging, sibling)
    )

    results = broker._request_many(requests, 1.0)

    assert results[0] is None
    assert isinstance(results[1], SmartReconnectShortcutObservation)
    assert results[1].path == os.path.normcase(os.path.abspath(sibling))
    assert broker._active == {}
    assert broker.close() is True


@pytest.mark.parametrize(
    ("minimized", "visible_available", "expected_route"),
    (
        (False, True, "visible"),
        (False, False, "obscured"),
        (True, False, "minimized"),
    ),
)
def test_formal_capture_routes_are_passive_and_background_first(
    tmp_path,
    monkeypatch,
    minimized,
    visible_available,
    expected_route,
):
    events = []
    background_sample = CaptureSample(1, 1, b"\1\0\0\0", True)
    visible_sample = CaptureSample(1, 1, b"\2\0\0\0", True)

    class RoleReader:
        def read(self, _handle):
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

    monkeypatch.setattr(broker_module, "RoleIdTemplateService", RoleReader)
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
    assert observed.sample is background_sample
    assert observed.fresh_capture is True
    assert observed.recognition.state is ReconnectScreenState.CONNECTED
    assert events == (["background"] if minimized else ["background", "visible"])
