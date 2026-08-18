"""遊戲到「輔」的單向伺服器時間資料橋接。"""

from __future__ import annotations

import json
import ctypes
import math
import socket
import threading
import time
from ctypes import wintypes
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from services.server_clock import (
    PROTOCOL_VERSION,
    ServerClock,
    ServerTimeSample,
    ServerTimeSourceIdentity,
)


class ServerTimeBridge:
    """只接收遊戲輸出的資料；不提供任何反向遊戲命令。"""

    def __init__(
        self,
        clock: ServerClock,
        *,
        source_validator: Callable[[ServerTimeSourceIdentity], bool] | None = None,
        transport_identity_resolver: Callable[[int], ServerTimeSourceIdentity | None]
        | None = None,
    ) -> None:
        self._clock = clock
        self._source_validator = source_validator
        self._transport_identity_resolver = transport_identity_resolver
        self._last_sequence_by_source: dict[ServerTimeSourceIdentity, int] = {}

    @staticmethod
    def _mapping(payload: object) -> Mapping[str, Any] | None:
        if isinstance(payload, Mapping):
            return payload
        if isinstance(payload, (bytes, bytearray, str)):
            try:
                decoded = json.loads(
                    payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else payload
                )
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            return decoded if isinstance(decoded, Mapping) else None
        return None

    def ingest(
        self,
        payload: object,
        *,
        transport_process_id: int | None = None,
        sample_monotonic_ns: int | None = None,
    ) -> bool:
        data = self._mapping(payload)
        if data is None:
            return False
        try:
            identity_data = data["source_instance_identity"]
            declared_identity = self._declared_identity(identity_data)
            identity = self._identity_from_transport(
                identity_data,
                declared_identity,
                transport_process_id,
            )
            if identity is None:
                return False
            sample = ServerTimeSample(
                protocol_version=data["protocol_version"],
                source_instance_identity=identity,
                server_now_ms=data["server_now_ms"],
                sample_local_flash_timer=data["sample_local_flash_timer"],
                sample_sequence=data["sample_sequence"],
            )
        except (KeyError, TypeError, ValueError):
            return False
        if transport_process_id is not None and identity.process_id != transport_process_id:
            return False
        if self._source_validator is not None:
            try:
                if not self._source_validator(identity):
                    return False
            except Exception:
                return False
        previous = self._last_sequence_by_source.get(identity)
        if previous is not None and sample.sample_sequence <= previous:
            return False
        accepted = self._clock.calibrate_once(
            sample,
            sample_monotonic_ns=sample_monotonic_ns,
        )
        if accepted:
            self._last_sequence_by_source[identity] = sample.sample_sequence
        return accepted

    @staticmethod
    def _declared_identity(
        identity_data: object,
    ) -> ServerTimeSourceIdentity | None:
        if not isinstance(identity_data, Mapping):
            return None
        try:
            return ServerTimeSourceIdentity(
                handle=identity_data["handle"],
                process_id=identity_data["process_id"],
                thread_id=identity_data["thread_id"],
                lifecycle=identity_data["lifecycle"],
                fingerprint=identity_data["fingerprint"],
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _identity_from_transport(
        self,
        identity_data: object,
        declared_identity: ServerTimeSourceIdentity | None,
        transport_process_id: int | None,
    ) -> ServerTimeSourceIdentity | None:
        if declared_identity is not None:
            if (
                transport_process_id is not None
                and declared_identity.process_id != transport_process_id
            ):
                return None
            if self._transport_identity_resolver is None or transport_process_id is None:
                return declared_identity
            resolved = self._resolve_transport_identity(transport_process_id)
            return declared_identity if resolved == declared_identity else None

        if identity_data != "transport-bound" or transport_process_id is None:
            return None
        return self._resolve_transport_identity(transport_process_id)

    def _resolve_transport_identity(
        self,
        transport_process_id: int,
    ) -> ServerTimeSourceIdentity | None:
        if self._transport_identity_resolver is None:
            return None
        try:
            return self._transport_identity_resolver(transport_process_id)
        except Exception:
            return None

    @staticmethod
    def encode(sample: ServerTimeSample) -> bytes:
        payload = asdict(sample)
        payload["source_instance_identity"] = asdict(sample.source_instance_identity)
        return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class ServerTimeBridgeServer:
    """只在本機迴路介面接收遊戲輸出的單向時間資料。"""

    def __init__(
        self,
        bridge: ServerTimeBridge,
        *,
        host: str = "127.0.0.1",
        port: int = 37842,
    ) -> None:
        if host != "127.0.0.1":
            raise ValueError("server-time bridge must remain loopback-only")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        self._bridge = bridge
        self._host = host
        self._port = port
        self._server_socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()

    @property
    def address(self) -> tuple[str, int] | None:
        with self._lock:
            if self._server_socket is None:
                return None
            host, port = self._server_socket.getsockname()[:2]
            return str(host), int(port)

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> tuple[str, int]:
        with self._lock:
            if self.running:
                address = self.address
                if address is None:
                    raise RuntimeError("bridge listener has no address")
                return address
            server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_socket.bind((self._host, self._port))
            server_socket.listen(4)
            server_socket.settimeout(0.25)
            self._server_socket = server_socket
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._serve,
                name="server-time-bridge",
                daemon=True,
            )
            self._thread.start()
            address = self.address
            if address is None:
                raise RuntimeError("bridge listener failed to start")
            return address

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            server_socket = self._server_socket
            self._server_socket = None
        if server_socket is not None:
            try:
                server_socket.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._thread = None

    def _serve(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                server_socket = self._server_socket
            if server_socket is None:
                return
            try:
                client, _address = server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                client.settimeout(1.0)
                self._read_client(client)
            finally:
                try:
                    client.close()
                except OSError:
                    pass

    def _read_client(self, client: socket.socket) -> None:
        transport_process_id = None
        for _ in range(20):
            transport_process_id = self._transport_process_id(client)
            if transport_process_id is not None:
                break
            if self._stop_event.wait(0.01):
                return
        if transport_process_id is None:
            return
        buffer = bytearray()
        while not self._stop_event.is_set():
            try:
                chunk = client.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                return
            buffer.extend(chunk)
            if buffer.startswith(b"<policy-file-request/>"):
                if b"\x00" not in buffer:
                    continue
                self._send_socket_policy(client)
                return
            if buffer.startswith(b"GET "):
                if b"\r\n\r\n" not in buffer:
                    continue
                self._read_http_request(client, bytes(buffer), transport_process_id)
                return
            while b"\n" in buffer:
                line, _, remainder = buffer.partition(b"\n")
                buffer = bytearray(remainder)
                if line:
                    self._bridge.ingest(
                        line,
                        transport_process_id=transport_process_id,
                    )

    def _read_http_request(
        self,
        client: socket.socket,
        request: bytes,
        transport_process_id: int,
    ) -> None:
        try:
            request_line = request.split(b"\r\n", 1)[0].decode("ascii")
            method, target, _version = request_line.split(" ", 2)
            parsed = urlsplit(target)
        except (UnicodeDecodeError, ValueError):
            return
        if method != "GET":
            return
        if parsed.path == "/crossdomain.xml":
            self._send_http_response(
                client,
                200,
                "application/xml",
                (
                    b'<?xml version="1.0"?>'
                    b'<cross-domain-policy><allow-access-from domain="*" '
                    b'to-ports="37842"/></cross-domain-policy>'
                ),
            )
            return
        if parsed.path != "/v1/server-time":
            return
        query = parse_qs(parsed.query, keep_blank_values=True)
        required = (
            "protocol_version",
            "source_instance_identity",
            "server_now_ms",
            "sample_local_flash_timer",
            "sample_sequence",
        )
        if any(len(query.get(key, ())) != 1 for key in required):
            return
        try:
            payload: Mapping[str, Any] = {
                "protocol_version": int(query["protocol_version"][0]),
                "source_instance_identity": query["source_instance_identity"][0],
                "server_now_ms": float(query["server_now_ms"][0]),
                "sample_local_flash_timer": float(query["sample_local_flash_timer"][0]),
                "sample_sequence": int(query["sample_sequence"][0]),
            }
        except ValueError:
            return
        self._bridge.ingest(payload, transport_process_id=transport_process_id)
        self._send_http_response(client, 204, "text/plain", b"")

    @staticmethod
    def _send_http_response(
        client: socket.socket,
        status: int,
        content_type: str,
        body: bytes,
    ) -> None:
        reason = "No Content" if status == 204 else "OK"
        response = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii") + body
        try:
            client.sendall(response)
        except OSError:
            pass

    @staticmethod
    def _send_socket_policy(client: socket.socket) -> None:
        policy = (
            b'<?xml version="1.0"?>'
            b'<cross-domain-policy><allow-access-from domain="*" '
            b'to-ports="37842"/></cross-domain-policy>\x00'
        )
        try:
            client.sendall(policy)
        except OSError:
            pass

    @staticmethod
    def _transport_process_id(client: socket.socket) -> int | None:
        """由本機 TCP 表取得連線另一端的實際程序代號。"""
        if not hasattr(ctypes, "WinDLL"):
            return None
        try:
            local_host, local_port = client.getsockname()[:2]
            peer_host, peer_port = client.getpeername()[:2]
        except OSError:
            return None
        if local_host != "127.0.0.1" or peer_host != "127.0.0.1":
            return None
        try:
            kernel32 = ctypes.WinDLL("iphlpapi", use_last_error=True)
            get_table = kernel32.GetExtendedTcpTable
            get_table.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.c_bool,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_ulong,
            ]
            get_table.restype = ctypes.c_ulong
            AF_INET = 2
            TCP_TABLE_OWNER_PID_ALL = 5
            size = ctypes.c_ulong(0)
            result = get_table(
                None,
                ctypes.byref(size),
                False,
                AF_INET,
                TCP_TABLE_OWNER_PID_ALL,
                0,
            )
            if result not in (0, 122) or size.value <= 0:
                return None
            table = ctypes.create_string_buffer(size.value)
            result = get_table(
                table,
                ctypes.byref(size),
                False,
                AF_INET,
                TCP_TABLE_OWNER_PID_ALL,
                0,
            )
            if result != 0:
                return None
            count = ctypes.c_ulong.from_buffer(table).value
            row_size = 24
            offset = ctypes.sizeof(ctypes.c_ulong)
            for _ in range(count):
                row = table[offset : offset + row_size]
                if len(row) < row_size:
                    return None
                state, local_addr = struct_unpack_u32(row, 0), struct_unpack_u32(row, 4)
                local_port_value = int.from_bytes(row[8:10], "big")
                remote_addr = struct_unpack_u32(row, 12)
                remote_port_value = int.from_bytes(row[16:18], "big")
                owner_pid = struct_unpack_u32(row, 20)
                if (
                    state == 5
                    and local_port_value == peer_port
                    and remote_port_value == local_port
                    and remote_addr == _ipv4_to_int(local_host)
                    and local_addr == _ipv4_to_int(peer_host)
                ):
                    return owner_pid
                offset += row_size
        except (OSError, AttributeError, ctypes.ArgumentError, ValueError):
            return None
        return None


@dataclass(frozen=True, slots=True)
class ProcessMemoryServerTimeCandidate:
    server_time_address: int
    core_address: int
    server_time_ms: float
    start_time_ms: float
    server_time_offset_ms: float
    time_lag_ms: float
    sample_monotonic_ns: int


class ProcessMemoryServerTimeReader:
    """從正式遊戲實例唯讀定位 AVM 內已計算的伺服器時間。

    定位必須同時驗證 MiniMapCanvas、Core 與固定欄位關係；不使用歷史
    位址，也不把程序內其他碰巧像時間的數字當成校正來源。
    """

    _PROCESS_QUERY_INFORMATION = 0x0400
    _PROCESS_VM_READ = 0x0010
    _MEM_COMMIT = 0x1000
    _MEM_PRIVATE = 0x20000
    _MEM_IMAGE = 0x1000000
    _PAGE_GUARD = 0x100
    _READABLE_PROTECTS = frozenset((0x02, 0x04, 0x08, 0x20, 0x40, 0x80))
    _SCAN_LIMIT = 0x80000000
    _CHUNK_SIZE = 4 * 1024 * 1024
    _MIN_EPOCH_MS = 1_000_000_000_000.0
    _MAX_EPOCH_MS = 2_200_000_000_000.0
    _MAX_TIME_PAIR_DELTA_MS = 86_400_000.0
    _MAX_SERVER_OFFSET_MS = 50_400_000.0
    _MAX_TIME_LAG_MS = 86_400_000.0
    _MAX_TIME_RELATION_ERROR_MS = 10_000.0
    _SERVER_TIME_FIELD_OFFSET = 0x410
    _CORE_FIELD_OFFSET = 0x398
    _CORE_SERVER_OFFSET_FIELD_OFFSET = 0x158
    _CORE_TIME_LAG_FIELD_OFFSET = 0x168

    class _MemoryBasicInformation(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_void_p),
            ("AllocationBase", ctypes.c_void_p),
            ("AllocationProtect", wintypes.DWORD),
            ("PartitionId", wintypes.WORD),
            ("Padding", wintypes.WORD),
            ("RegionSize", ctypes.c_size_t),
            ("State", wintypes.DWORD),
            ("Protect", wintypes.DWORD),
            ("Type", wintypes.DWORD),
        ]

    def __init__(
        self,
        window_provider: Callable[[], tuple[object, ...]],
        bridge: ServerTimeBridge,
        *,
        poll_seconds: float = 0.25,
        retry_seconds: float = 1.0,
        wall_tolerance_ms: int = 120_000,
        wall_clock_ns: Callable[[], int] = time.time_ns,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        local_flash_offset_ms: Callable[[], float] | None = None,
    ) -> None:
        if poll_seconds <= 0 or retry_seconds <= 0:
            raise ValueError("memory reader timing values must be positive")
        if wall_tolerance_ms <= 0:
            raise ValueError("wall_tolerance_ms must be positive")
        self._window_provider = window_provider
        self._bridge = bridge
        self._poll_seconds = float(poll_seconds)
        self._retry_seconds = float(retry_seconds)
        self._wall_tolerance_ms = int(wall_tolerance_ms)
        self._wall_clock_ns = wall_clock_ns
        self._monotonic_ns = monotonic_ns
        self._local_flash_offset_ms = (
            local_flash_offset_ms or self._system_local_flash_offset_ms
        )
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_attempt_ns: dict[tuple[int, int], int] = {}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="server-time-memory-reader",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._thread = None

    @staticmethod
    def _readable(protect: int) -> bool:
        return not (protect & ProcessMemoryServerTimeReader._PAGE_GUARD) and (
            protect & 0xFF
        ) in ProcessMemoryServerTimeReader._READABLE_PROTECTS

    @staticmethod
    def _memory_api():
        if not hasattr(ctypes, "WinDLL"):
            return None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        open_process.restype = wintypes.HANDLE
        read_memory = kernel32.ReadProcessMemory
        read_memory.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        read_memory.restype = ctypes.wintypes.BOOL
        query = kernel32.VirtualQueryEx
        query.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.POINTER(ProcessMemoryServerTimeReader._MemoryBasicInformation),
            ctypes.c_size_t,
        ]
        query.restype = ctypes.c_size_t
        return kernel32, open_process, read_memory, query

    @staticmethod
    def _system_local_flash_offset_ms() -> float:
        offset = datetime.now().astimezone().utcoffset()
        return -float(offset.total_seconds() * 1000.0) if offset is not None else 0.0

    @staticmethod
    def _read_process_bytes(
        read_memory,
        process_handle,
        address: int,
        amount: int,
    ) -> bytes | None:
        if address <= 0 or amount <= 0:
            return None
        buffer = ctypes.create_string_buffer(amount)
        read = ctypes.c_size_t()
        if not read_memory(
            process_handle,
            ctypes.c_void_p(address),
            buffer,
            amount,
            ctypes.byref(read),
        ):
            return None
        if read.value != amount:
            return None
        return buffer.raw

    def _read_u32(self, read_memory, process_handle, address: int) -> int | None:
        data = self._read_process_bytes(read_memory, process_handle, address, 4)
        return int.from_bytes(data, "little") if data is not None else None

    def _read_f64(self, read_memory, process_handle, address: int) -> float | None:
        data = self._read_process_bytes(read_memory, process_handle, address, 8)
        if data is None:
            return None
        return float(ctypes.c_double.from_buffer_copy(data).value)

    def _memory_info(self, query, process_handle, address: int):
        info = self._MemoryBasicInformation()
        if not query(
            process_handle,
            ctypes.c_void_p(address),
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            return None
        return info

    def _image_pointer(self, query, process_handle, address: int) -> bool:
        info = self._memory_info(query, process_handle, address)
        return bool(
            info is not None
            and info.State == self._MEM_COMMIT
            and info.Type == self._MEM_IMAGE
            and self._readable(int(info.Protect))
        )

    def _avm_string(
        self,
        read_memory,
        process_handle,
        string_address: int,
    ) -> str | None:
        buffer_address = self._read_u32(
            read_memory, process_handle, string_address + 8
        )
        length = self._read_u32(read_memory, process_handle, string_address + 16)
        if buffer_address is None or length is None or not 0 < length <= 128:
            return None
        raw = self._read_process_bytes(
            read_memory,
            process_handle,
            buffer_address,
            length,
        )
        if raw is None:
            return None
        try:
            value = raw.decode("ascii")
        except UnicodeDecodeError:
            return None
        return value if all(32 <= ord(char) <= 126 for char in value) else None

    def _class_is(
        self,
        read_memory,
        query,
        process_handle,
        object_address: int,
        expected_name: str,
    ) -> bool:
        cpp_vtable = self._read_u32(read_memory, process_handle, object_address)
        avm_vtable = self._read_u32(
            read_memory, process_handle, object_address + 8
        )
        if (
            cpp_vtable is None
            or avm_vtable is None
            or not self._image_pointer(query, process_handle, cpp_vtable)
        ):
            return False
        traits = self._read_u32(read_memory, process_handle, avm_vtable + 20)
        if traits is None:
            return False
        name = self._read_u32(read_memory, process_handle, traits + 72)
        if name is None:
            return False
        return self._avm_string(read_memory, process_handle, name) == expected_name

    def _candidate_field_addresses(
        self,
        data: bytes,
        base_address: int,
        now_ms: float,
    ) -> tuple[int, ...]:
        try:
            import numpy as np
        except ImportError:
            return ()
        first_offset = (-base_address) & 7
        count = (len(data) - first_offset) // 8
        if count < 2:
            return ()
        values = np.frombuffer(
            data,
            dtype="<f8",
            count=count,
            offset=first_offset,
        )
        current = values[:-1]
        following = values[1:]
        low = now_ms - self._wall_tolerance_ms
        high = now_ms + self._wall_tolerance_ms
        with np.errstate(invalid="ignore", over="ignore"):
            indexes = np.flatnonzero(
                np.isfinite(current)
                & np.isfinite(following)
                & (current >= max(low, self._MIN_EPOCH_MS))
                & (current <= min(high, self._MAX_EPOCH_MS))
                & (following >= max(low, self._MIN_EPOCH_MS))
                & (following <= min(high, self._MAX_EPOCH_MS))
                & (np.abs(current - following) < self._MAX_TIME_PAIR_DELTA_MS)
            )
        return tuple(
            base_address + first_offset + int(index) * 8 for index in indexes
        )

    def _candidate_from_field(
        self,
        read_memory,
        query,
        process_handle,
        field_address: int,
        now_ms: float,
    ) -> ProcessMemoryServerTimeCandidate | None:
        object_address = field_address - self._SERVER_TIME_FIELD_OFFSET
        if object_address <= 0 or not self._class_is(
            read_memory,
            query,
            process_handle,
            object_address,
            "MiniMapCanvas",
        ):
            return None
        core_address = self._read_u32(
            read_memory,
            process_handle,
            object_address + self._CORE_FIELD_OFFSET,
        )
        if core_address is None or not self._class_is(
            read_memory,
            query,
            process_handle,
            core_address,
            "Core",
        ):
            return None
        server_time_ms = self._read_f64(
            read_memory, process_handle, field_address
        )
        start_time_ms = self._read_f64(
            read_memory, process_handle, field_address + 8
        )
        server_time_offset_ms = self._read_f64(
            read_memory,
            process_handle,
            core_address + self._CORE_SERVER_OFFSET_FIELD_OFFSET,
        )
        time_lag_ms = self._read_f64(
            read_memory,
            process_handle,
            core_address + self._CORE_TIME_LAG_FIELD_OFFSET,
        )
        values = (
            server_time_ms,
            start_time_ms,
            server_time_offset_ms,
            time_lag_ms,
        )
        if any(value is None or not math.isfinite(value) for value in values):
            return None
        assert server_time_ms is not None
        assert start_time_ms is not None
        assert server_time_offset_ms is not None
        assert time_lag_ms is not None
        if not (
            self._MIN_EPOCH_MS <= server_time_ms <= self._MAX_EPOCH_MS
            and self._MIN_EPOCH_MS <= start_time_ms <= self._MAX_EPOCH_MS
            and abs(server_time_ms - now_ms) <= self._wall_tolerance_ms
            and abs(start_time_ms - now_ms) <= self._wall_tolerance_ms
            and abs(server_time_ms - start_time_ms) < self._MAX_TIME_PAIR_DELTA_MS
            and abs(server_time_offset_ms) <= self._MAX_SERVER_OFFSET_MS
            and abs(time_lag_ms) <= self._MAX_TIME_LAG_MS
            and abs(server_time_ms - start_time_ms - time_lag_ms)
            <= self._MAX_TIME_RELATION_ERROR_MS
        ):
            return None
        return ProcessMemoryServerTimeCandidate(
            server_time_address=field_address,
            core_address=core_address,
            server_time_ms=server_time_ms,
            start_time_ms=start_time_ms,
            server_time_offset_ms=server_time_offset_ms,
            time_lag_ms=time_lag_ms,
            sample_monotonic_ns=int(self._monotonic_ns()),
        )

    def _scan_process(
        self,
        process_id: int,
        now_ms: float,
    ) -> tuple[ProcessMemoryServerTimeCandidate, ...]:
        api = self._memory_api()
        if api is None:
            return ()
        kernel32, open_process, read_memory, query = api
        handle = open_process(
            self._PROCESS_QUERY_INFORMATION | self._PROCESS_VM_READ,
            False,
            process_id,
        )
        if not handle:
            return ()
        candidates: list[ProcessMemoryServerTimeCandidate] = []
        seen_addresses: set[int] = set()
        address = 0
        try:
            info_size = ctypes.sizeof(self._MemoryBasicInformation)
            while address < self._SCAN_LIMIT:
                info = self._MemoryBasicInformation()
                if not query(handle, ctypes.c_void_p(address), ctypes.byref(info), info_size):
                    break
                base = int(info.BaseAddress or 0)
                size = int(info.RegionSize)
                if (
                    info.State == self._MEM_COMMIT
                    and info.Type == self._MEM_PRIVATE
                    and size > 0
                    and self._readable(int(info.Protect))
                ):
                    position = base
                    tail = b""
                    tail_address = 0
                    while position < base + size:
                        amount = min(self._CHUNK_SIZE, base + size - position)
                        data = self._read_process_bytes(
                            read_memory,
                            handle,
                            position,
                            amount,
                        )
                        if data is None:
                            position += amount
                            tail = b""
                            continue
                        block = tail + data
                        block_address = tail_address if tail else position
                        for field_address in self._candidate_field_addresses(
                            block,
                            block_address,
                            now_ms,
                        ):
                            if field_address in seen_addresses:
                                continue
                            seen_addresses.add(field_address)
                            candidate = self._candidate_from_field(
                                read_memory,
                                query,
                                handle,
                                field_address,
                                now_ms,
                            )
                            if candidate is not None:
                                candidates.append(candidate)
                        if len(block) >= 8:
                            tail = block[-8:]
                            tail_address = block_address + len(block) - 8
                        position += amount
                next_address = base + max(size, 0x1000)
                if next_address <= address:
                    break
                address = next_address
        finally:
            kernel32.CloseHandle(handle)
        return tuple(candidates)

    def _windows(self) -> tuple[object, ...]:
        try:
            values = self._window_provider()
        except Exception:
            return ()
        return tuple(values) if isinstance(values, tuple) else ()

    def _candidate_identity(self, window: object) -> ServerTimeSourceIdentity | None:
        try:
            return ServerTimeSourceIdentity(
                handle=window.handle,
                process_id=window.process_id,
                thread_id=window.thread_id,
                lifecycle=window.process_lifecycle_token,
                fingerprint=window.launch_fingerprint,
            )
        except (AttributeError, TypeError, ValueError):
            return None

    def _try_window(self, window: object) -> bool:
        identity = self._candidate_identity(window)
        if identity is None:
            return False
        scan_now_ms = self._wall_clock_ns() / 1_000_000.0
        candidates = self._scan_process(identity.process_id, scan_now_ms)
        if len(candidates) != 1:
            return False
        candidate = candidates[0]
        server_now_ms = candidate.server_time_ms
        if not math.isfinite(server_now_ms) or server_now_ms < 0:
            return False
        sample = ServerTimeSample(
            protocol_version=PROTOCOL_VERSION,
            source_instance_identity=identity,
            server_now_ms=server_now_ms,
            sample_local_flash_timer=candidate.start_time_ms,
            sample_sequence=1,
        )
        return self._bridge.ingest(
            ServerTimeBridge.encode(sample),
            sample_monotonic_ns=candidate.sample_monotonic_ns,
        )

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if self._bridge._clock.snapshot().state == ServerClock.CALIBRATED:
                return
            windows = sorted(
                self._windows(),
                key=lambda window: int(
                    getattr(window, "process_lifecycle_token", 0) or 0
                ),
                reverse=True,
            )
            seen_instances: set[tuple[int, int]] = set()
            for window in windows:
                if self._stop_event.is_set():
                    return
                identity = self._candidate_identity(window)
                if identity is None:
                    continue
                instance_key = (identity.process_id, identity.lifecycle)
                if instance_key in seen_instances:
                    continue
                seen_instances.add(instance_key)
                now_ns = int(self._monotonic_ns())
                last_attempt_ns = self._last_attempt_ns.get(instance_key)
                if (
                    last_attempt_ns is not None
                    and now_ns - last_attempt_ns
                    < int(self._retry_seconds * 1_000_000_000)
                ):
                    continue
                self._last_attempt_ns[instance_key] = now_ns
                if self._try_window(window):
                    return
            self._stop_event.wait(self._poll_seconds)


def struct_unpack_u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")


def _ipv4_to_int(address: str) -> int:
    return int.from_bytes(socket.inet_aton(address), "little")
