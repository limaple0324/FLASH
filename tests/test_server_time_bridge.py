import socket
import time
import os
import struct
import warnings
from types import SimpleNamespace

from services.server_clock import ServerClock
from services.server_time_bridge import (
    ProcessMemoryServerTimeCandidate,
    ProcessMemoryServerTimeReader,
    ServerTimeBridge,
    ServerTimeBridgeServer,
)
from tests.test_server_clock import IDENTITY, sample


def test_bridge_ingests_one_valid_sample_and_rejects_recalibration():
    clock = ServerClock(source_validator=lambda _: True)
    bridge = ServerTimeBridge(clock, source_validator=lambda _: True)
    payload = {
        "protocol_version": 1,
        "source_instance_identity": {
            "handle": IDENTITY.handle,
            "process_id": IDENTITY.process_id,
            "thread_id": IDENTITY.thread_id,
            "lifecycle": IDENTITY.lifecycle,
            "fingerprint": IDENTITY.fingerprint,
        },
        "server_now_ms": 1_234_567,
        "sample_local_flash_timer": 88,
        "sample_sequence": 1,
    }
    assert bridge.ingest(payload) is True
    assert bridge.ingest({**payload, "server_now_ms": 1_234_568, "sample_sequence": 2}) is False


def test_bridge_rejects_bad_identity_and_bad_payload():
    clock = ServerClock(source_validator=lambda _: True)
    bridge = ServerTimeBridge(clock, source_validator=lambda _: False)
    assert bridge.ingest({"not": "a sample"}) is False
    encoded = ServerTimeBridge.encode(sample())
    assert bridge.ingest(encoded) is False


def test_bridge_rejects_transport_process_mismatch():
    clock = ServerClock(source_validator=lambda _: True)
    bridge = ServerTimeBridge(clock, source_validator=lambda _: True)
    assert bridge.ingest(
        ServerTimeBridge.encode(sample()),
        transport_process_id=IDENTITY.process_id + 1,
    ) is False


def test_bridge_accepts_transport_bound_identity_only_with_unique_resolver_match():
    clock = ServerClock(source_validator=lambda _: True)
    transport_identity = type(IDENTITY)(
        handle=IDENTITY.handle,
        process_id=os.getpid(),
        thread_id=IDENTITY.thread_id,
        lifecycle=IDENTITY.lifecycle,
        fingerprint=IDENTITY.fingerprint,
    )
    bridge = ServerTimeBridge(
        clock,
        source_validator=lambda _: True,
        transport_identity_resolver=lambda process_id: (
            transport_identity if process_id == os.getpid() else None
        ),
    )
    payload = {
        "protocol_version": 1,
        "source_instance_identity": "transport-bound",
        "server_now_ms": 1_234_567,
        "sample_local_flash_timer": 88,
        "sample_sequence": 1,
    }
    assert bridge.ingest(payload, transport_process_id=os.getpid()) is True


def test_bridge_rejects_transport_bound_identity_without_resolver_match():
    clock = ServerClock(source_validator=lambda _: True)
    bridge = ServerTimeBridge(clock, source_validator=lambda _: True)
    assert bridge.ingest(
        {
            "protocol_version": 1,
            "source_instance_identity": "transport-bound",
            "server_now_ms": 1_234_567,
            "sample_local_flash_timer": 88,
            "sample_sequence": 1,
        },
        transport_process_id=os.getpid(),
    ) is False


def test_loopback_server_rejects_forged_process_id_on_real_connection():
    import os

    clock = ServerClock(source_validator=lambda _: True)
    bridge = ServerTimeBridge(clock, source_validator=lambda _: True)
    server = ServerTimeBridgeServer(bridge, port=0)
    host, port = server.start()
    try:
        forged = type(IDENTITY)(
            handle=IDENTITY.handle,
            process_id=os.getpid() + 1,
            thread_id=IDENTITY.thread_id,
            lifecycle=IDENTITY.lifecycle,
            fingerprint=IDENTITY.fingerprint,
        )
        client = socket.create_connection((host, port), timeout=1.0)
        client.sendall(ServerTimeBridge.encode(sample(identity=forged)))
        time.sleep(0.2)
        client.close()
        assert clock.calibration_count == 0
    finally:
        server.stop()


def test_loopback_server_delivers_one_way_sample_to_clock():
    tick = [10_000_000_000]
    clock = ServerClock(monotonic_ns=lambda: tick[0], source_validator=lambda _: True)
    bridge = ServerTimeBridge(clock, source_validator=lambda _: True)
    server = ServerTimeBridgeServer(bridge, port=0)
    host, port = server.start()
    try:
        payload = {
            "protocol_version": 1,
            "source_instance_identity": {
                "handle": IDENTITY.handle,
                "process_id": os.getpid(),
                "thread_id": IDENTITY.thread_id,
                "lifecycle": IDENTITY.lifecycle,
                "fingerprint": IDENTITY.fingerprint,
            },
            "server_now_ms": 2_000_000,
            "sample_local_flash_timer": 99,
            "sample_sequence": 1,
        }
        network_identity = type(IDENTITY)(
            handle=IDENTITY.handle,
            process_id=os.getpid(),
            thread_id=IDENTITY.thread_id,
            lifecycle=IDENTITY.lifecycle,
            fingerprint=IDENTITY.fingerprint,
        )
        client = socket.create_connection((host, port), timeout=1.0)
        client.sendall(
            ServerTimeBridge.encode(
                sample(2_000_000, identity=network_identity)
            )
        )
        deadline = time.monotonic() + 1.0
        while clock.calibration_count != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        client.close()
        assert clock.calibration_count == 1
        assert clock.snapshot().server_base_ms == 2_000_000
    finally:
        server.stop()
    assert server.running is False


def test_loopback_server_accepts_transport_bound_http_sample_once():
    clock = ServerClock(source_validator=lambda _: True)
    identity = type(IDENTITY)(
        handle=IDENTITY.handle,
        process_id=os.getpid(),
        thread_id=IDENTITY.thread_id,
        lifecycle=IDENTITY.lifecycle,
        fingerprint=IDENTITY.fingerprint,
    )
    bridge = ServerTimeBridge(
        clock,
        source_validator=lambda _: True,
        transport_identity_resolver=lambda process_id: (
            identity if process_id == os.getpid() else None
        ),
    )
    server = ServerTimeBridgeServer(bridge, port=0)
    host, port = server.start()
    try:
        client = socket.create_connection((host, port), timeout=1.0)
        client.sendall(
            b"GET /v1/server-time?protocol_version=1&source_instance_identity="
            b"transport-bound&server_now_ms=2000000&sample_local_flash_timer=99"
            b"&sample_sequence=1 HTTP/1.1\r\nHost: localhost\r\n\r\n"
        )
        response = client.recv(1024)
        deadline = time.monotonic() + 1.0
        while clock.calibration_count != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        client.close()
        assert response.startswith(b"HTTP/1.1 204")
        assert clock.calibration_count == 1
        assert clock.snapshot().server_base_ms == 2_000_000
    finally:
        server.stop()


def test_loopback_server_serves_only_static_flash_policy():
    clock = ServerClock(source_validator=lambda _: True)
    bridge = ServerTimeBridge(clock, source_validator=lambda _: True)
    server = ServerTimeBridgeServer(bridge, port=0)
    host, port = server.start()
    try:
        client = socket.create_connection((host, port), timeout=1.0)
        client.sendall(b"<policy-file-request/>\x00")
        response = client.recv(1024)
        client.close()
        assert b"cross-domain-policy" in response
        assert clock.calibration_count == 0
    finally:
        server.stop()


def process_memory_candidate(now_ms=1_800_000_000_000.0):
    return ProcessMemoryServerTimeCandidate(
        server_time_address=0x1000,
        core_address=0x2000,
        server_time_ms=now_ms + 1_668.0,
        start_time_ms=now_ms,
        server_time_offset_ms=-28_800_000.0,
        time_lag_ms=1_668.0,
    )


def test_memory_reader_calibrates_once_from_unique_exact_game_candidate():
    tick = [10_000_000_000]
    clock = ServerClock(
        monotonic_ns=lambda: tick[0],
        source_validator=lambda _: True,
    )
    bridge = ServerTimeBridge(clock, source_validator=lambda _: True)
    window = SimpleNamespace(
        handle=IDENTITY.handle,
        process_id=IDENTITY.process_id,
        thread_id=IDENTITY.thread_id,
        process_lifecycle_token=IDENTITY.lifecycle,
        launch_fingerprint=IDENTITY.fingerprint,
    )
    reader = ProcessMemoryServerTimeReader(
        lambda: (window,),
        bridge,
        wall_clock_ns=lambda: 1_800_000_000_000_000_000,
        monotonic_ns=lambda: tick[0],
        local_flash_offset_ms=lambda: -28_800_000.0,
    )
    reader._scan_process = lambda _pid, _now: (process_memory_candidate(),)
    assert reader._try_window(window) is True
    assert clock.calibration_count == 1
    assert clock.snapshot().server_base_ms == 1_800_000_001_668
    tick[0] += 2_000_000_000
    assert clock.now_ms() == 1_800_000_003_668


def test_memory_reader_rejects_multiple_exact_candidates():
    clock = ServerClock(source_validator=lambda _: True)
    bridge = ServerTimeBridge(clock, source_validator=lambda _: True)
    window = SimpleNamespace(
        handle=IDENTITY.handle,
        process_id=IDENTITY.process_id,
        thread_id=IDENTITY.thread_id,
        process_lifecycle_token=IDENTITY.lifecycle,
        launch_fingerprint=IDENTITY.fingerprint,
    )
    reader = ProcessMemoryServerTimeReader(
        lambda: (window,),
        bridge,
        wall_clock_ns=lambda: 1_800_000_000_000_000_000,
        local_flash_offset_ms=lambda: -28_800_000.0,
    )
    reader._scan_process = lambda _pid, _now: (
        process_memory_candidate(),
        ProcessMemoryServerTimeCandidate(
            server_time_address=0x3000,
            core_address=0x4000,
            server_time_ms=1_800_000_001_668.0,
            start_time_ms=1_800_000_000_000.0,
            server_time_offset_ms=-28_800_000.0,
            time_lag_ms=1_668.0,
        ),
    )
    assert reader._try_window(window) is False
    assert clock.calibration_count == 0


def test_memory_reader_waits_for_game_opened_after_fu_and_calibrates_automatically():
    windows = []
    clock = ServerClock(source_validator=lambda _: True)
    bridge = ServerTimeBridge(clock, source_validator=lambda _: True)
    window = SimpleNamespace(
        handle=IDENTITY.handle,
        process_id=IDENTITY.process_id,
        thread_id=IDENTITY.thread_id,
        process_lifecycle_token=IDENTITY.lifecycle,
        launch_fingerprint=IDENTITY.fingerprint,
    )
    reader = ProcessMemoryServerTimeReader(
        lambda: tuple(windows),
        bridge,
        poll_seconds=0.01,
        retry_seconds=0.01,
        wall_clock_ns=lambda: 1_800_000_000_000_000_000,
        local_flash_offset_ms=lambda: -28_800_000.0,
    )
    reader._scan_process = lambda _pid, _now: (process_memory_candidate(),)
    reader.start()
    try:
        time.sleep(0.03)
        assert clock.calibration_count == 0
        windows.append(window)
        deadline = time.monotonic() + 1.0
        while clock.calibration_count == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert clock.calibration_count == 1
        assert reader.running is False
    finally:
        reader.stop()


def test_memory_reader_vector_gate_finds_only_adjacent_current_epoch_pair():
    reader = ProcessMemoryServerTimeReader(lambda: (), ServerTimeBridge(
        ServerClock(source_validator=lambda _: True),
        source_validator=lambda _: True,
    ))
    now_ms = 1_800_000_000_000.0
    data = struct.pack(
        "<dddddd",
        float("inf"),
        float("-inf"),
        12.0,
        now_ms + 1_668.0,
        now_ms,
        44.0,
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert reader._candidate_field_addresses(data, 0x1000, now_ms) == (
            0x1018,
        )
    assert caught == []


def test_memory_reader_requires_exact_minimap_and_core_object_chain():
    reader = ProcessMemoryServerTimeReader(lambda: (), ServerTimeBridge(
        ServerClock(source_validator=lambda _: True),
        source_validator=lambda _: True,
    ))
    field = 0x5000
    object_address = field - reader._SERVER_TIME_FIELD_OFFSET
    core_address = 0x7000
    classes = {
        (object_address, "MiniMapCanvas"),
        (core_address, "Core"),
    }
    reader._class_is = (
        lambda _read, _query, _handle, address, name: (address, name) in classes
    )
    reader._read_u32 = lambda _read, _handle, address: (
        core_address if address == object_address + reader._CORE_FIELD_OFFSET else None
    )
    doubles = {
        field: 1_800_000_001_668.0,
        field + 8: 1_800_000_000_000.0,
        core_address + reader._CORE_SERVER_OFFSET_FIELD_OFFSET: -28_800_000.0,
        core_address + reader._CORE_TIME_LAG_FIELD_OFFSET: 1_668.0,
    }
    reader._read_f64 = lambda _read, _handle, address: doubles.get(address)
    assert reader._candidate_from_field(
        object(), object(), object(), field, 1_800_000_000_000.0
    ) == ProcessMemoryServerTimeCandidate(
        server_time_address=field,
        core_address=core_address,
        server_time_ms=1_800_000_001_668.0,
        start_time_ms=1_800_000_000_000.0,
        server_time_offset_ms=-28_800_000.0,
        time_lag_ms=1_668.0,
    )
    classes.remove((core_address, "Core"))
    assert reader._candidate_from_field(
        object(), object(), object(), field, 1_800_000_000_000.0
    ) is None
