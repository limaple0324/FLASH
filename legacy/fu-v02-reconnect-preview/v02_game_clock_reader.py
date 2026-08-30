"""Read-only, explicit-window AVM clock acquisition for the supported x86 player.

Never opens arbitrary game processes, writes process memory, sends game input,
or logs memory/launch arguments. A missed/partial region invalidates uniqueness.
"""
from __future__ import annotations

import array
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, replace
import hashlib
import hmac
import math
import os
import struct
import threading
import time

from v02_game_clock import ClockSample, SourceIdentity, HEALTH_MAX_GAP_NS, SAMPLE_MAX_AGE_NS

PLAYER_SHA256 = "4AD607C31C4E24BA796A7BA873D83736EB026769001D49FBF2D8A5640FEDF2A6"
PROFILE_ID = "magic-school-avm2-a58a01f39e10c2c9"
ABC_LENGTH = 26_818_222
ABC_SHA256 = "A58A01F39E10C2C99362EEF371B00A2B40C5DC9D719E4AB2668EC0B74B2B125A"
TRAIT_PROFILES = (
    (5_315_459, 1638, "D79F03349CB637E3E10500DC88008105716BE9FC95F0A1E585DB3501DBF5ACAB", "SimpleCanvas"),
    (4_895_474, 1414, "50EC30EFAF03ECF198297E6EB0CBABFEE31EC7EA85E524A13862E33AAC4F2C0C", "Object"),
)
IDENTITY_CHECK_NS = 1_000_000_000
OBSERVATION_SLEEP_SECONDS = 0.25


class AcquisitionError(Exception):
    """Only fixed, non-sensitive reasons are exposed to the UI."""


class NonTargetObject(AcquisitionError):
    """A completed check positively ruled out a target, not an incomplete read."""


class MemoryInfo(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD),
                ("Padding", wintypes.WORD), ("RegionSize", ctypes.c_size_t),
                ("State", wintypes.DWORD), ("Protect", wintypes.DWORD),
                ("Type", wintypes.DWORD)]


class NativeSource:
    """All process operations use QUERY_INFORMATION | VM_READ, never write rights."""
    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.nt = ctypes.WinDLL("ntdll", use_last_error=True)
        self.secret = os.urandom(32)  # session-only keyed digest; never persisted
        self.images = {}
        declarations = (
            (self.kernel.OpenProcess, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            (self.kernel.CloseHandle, [wintypes.HANDLE], wintypes.BOOL),
            (self.kernel.ReadProcessMemory, [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)], wintypes.BOOL),
            (self.kernel.VirtualQueryEx, [wintypes.HANDLE, ctypes.c_void_p,
                                         ctypes.POINTER(MemoryInfo), ctypes.c_size_t], ctypes.c_size_t),
            (self.kernel.GetProcessTimes, [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
            (self.kernel.QueryFullProcessImageNameW, [wintypes.HANDLE, wintypes.DWORD,
                                                    wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            (self.user.IsWindow, [wintypes.HWND], wintypes.BOOL),
            (self.user.GetWindowThreadProcessId, [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            (self.nt.NtQueryInformationProcess, [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p,
                                               wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)], wintypes.LONG),
        )
        for function, arguments, result in declarations:
            function.argtypes, function.restype = arguments, result

    def open(self, pid):
        handle = self.kernel.OpenProcess(0x0400 | 0x0010, False, pid)
        if not handle:
            raise AcquisitionError("來源程序無法唯讀開啟")
        return handle

    def close(self, handle):
        self.kernel.CloseHandle(handle)

    def launch_digest(self, handle):
        class UnicodeString(ctypes.Structure):
            _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                        ("Buffer", ctypes.c_void_p)]
        needed = wintypes.ULONG()
        self.nt.NtQueryInformationProcess(handle, 60, None, 0, ctypes.byref(needed))
        if not ctypes.sizeof(UnicodeString) < needed.value <= 131072:
            raise AcquisitionError("來源啟動指紋無法確認")
        buffer = ctypes.create_string_buffer(needed.value)
        if self.nt.NtQueryInformationProcess(handle, 60, buffer, len(buffer), ctypes.byref(needed)) < 0:
            raise AcquisitionError("來源啟動指紋無法確認")
        header = UnicodeString.from_buffer(buffer)
        base = ctypes.addressof(buffer)
        offset = int(header.Buffer or 0) - base
        if not (0 <= offset < len(buffer) and 0 < header.Length <= len(buffer) - offset):
            raise AcquisitionError("來源啟動指紋格式不符")
        try:
            command_line = buffer.raw[offset:offset + header.Length].decode("utf-16-le")
        except UnicodeDecodeError:
            raise AcquisitionError("來源啟動指紋格式不符") from None
        return self.argument_fingerprint(command_line, includes_executable=True)

    def argument_fingerprint(self, text, *, includes_executable=False):
        """Session-only HMAC of parsed arguments; raw arguments never escape."""
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        local_free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
        argc = ctypes.c_int()
        command_line = text if includes_executable else 'placeholder.exe ' + text
        shell.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
        argv = shell.CommandLineToArgvW(command_line, ctypes.byref(argc))
        if not argv:
            raise AcquisitionError("來源啟動指紋格式不符")
        try:
            values = [argv[index] for index in range(argc.value)]
        finally:
            local_free(argv)
        arguments = values[1:]
        payload = "\0".join(arguments).encode("utf-8", "surrogatepass")
        return hmac.new(self.secret, payload, hashlib.sha256).hexdigest()

    def identity(self, hwnd):
        pid = wintypes.DWORD()
        tid = self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not self.user.IsWindow(hwnd) or not pid.value or not tid:
            raise AcquisitionError("來源視窗已失效")
        handle = self.open(pid.value)
        try:
            stamps = [wintypes.FILETIME() for _ in range(4)]
            if not self.kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in stamps)):
                raise AcquisitionError("來源生命週期無法確認")
            created = (stamps[0].dwHighDateTime << 32) | stamps[0].dwLowDateTime
            path = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(path))
            if not self.kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(length)):
                raise AcquisitionError("來源播放器無法確認")
            stat = os.stat(path.value)
            key = (path.value, stat.st_size, stat.st_mtime_ns)
            if key not in self.images:
                with open(path.value, "rb") as image:
                    header = image.read(64)
                    if len(header) != 64 or header[:2] != b"MZ":
                        raise AcquisitionError("不支援的播放器格式")
                    image.seek(struct.unpack_from("<I", header, 60)[0])
                    pe = image.read(24)
                    if (len(pe) != 24 or pe[:4] != b"PE\0\0"
                            or struct.unpack_from("<H", pe, 4)[0] != 0x14c
                            or struct.unpack_from("<H", pe, 22)[0] & 0x20):
                        raise AcquisitionError("不支援的播放器位址空間")
                    image.seek(0)
                    digest = hashlib.file_digest(image, "sha256").hexdigest().upper()
                self.images[key] = digest
            image_hash = self.images[key]
            if image_hash != PLAYER_SHA256:
                raise AcquisitionError("不支援的播放器版本")
            fingerprint = self.launch_digest(handle)
            identity = SourceIdentity(int(hwnd), pid.value, int(tid), created, fingerprint, image_hash)
            current_pid = wintypes.DWORD()
            current_tid = self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(current_pid))
            if current_pid.value != identity.pid or current_tid != identity.tid:
                raise AcquisitionError("來源身分已改變")
            return identity
        except OSError:
            raise AcquisitionError("來源播放器無法驗證") from None
        finally:
            self.close(handle)

    def read(self, handle, address, amount):
        if address <= 0 or amount <= 0:
            raise AcquisitionError("遊戲結構指標無效")
        buffer = ctypes.create_string_buffer(amount)
        received = ctypes.c_size_t()
        if (not self.kernel.ReadProcessMemory(handle, address, buffer, amount, ctypes.byref(received))
                or received.value != amount):
            raise AcquisitionError("遊戲記憶體無法完整讀取")
        return buffer.raw

    def query(self, handle, address):
        info = MemoryInfo()
        if not self.kernel.VirtualQueryEx(handle, address, ctypes.byref(info), ctypes.sizeof(info)):
            raise AcquisitionError("遊戲記憶體無法完整列舉")
        return info


@dataclass(frozen=True)
class Candidate:
    object_address: int
    core_address: int


@dataclass(frozen=True)
class Observation:
    raw_ms: float
    start_ms: float
    lag_ms: float
    before_ns: int
    after_ns: int


class EdgeTracker:
    """Only separately observed natural request/response pairs can be anchors."""
    def __init__(self, identity, previous):
        self.identity, self.previous = identity, previous
        self.pending_request = None
        self.last_request = None
        self.last_quality = None

    def feed(self, current):
        previous = self.previous
        if (current.before_ns < previous.after_ns
                or not 0 <= current.after_ns - current.before_ns <= 20_000_000):
            raise AcquisitionError("觀察時間逆序或讀取延遲過高")
        if current.before_ns - previous.after_ns > HEALTH_MAX_GAP_NS:
            raise AcquisitionError("自然更新觀察中斷")
        request_changed = current.start_ms != previous.start_ms
        response_changed = current.raw_ms != previous.raw_ms
        if request_changed:
            if self.pending_request is not None:
                raise AcquisitionError("偵測到請求重疊，無法可靠配對回應")
            if not 1000 <= current.start_ms - previous.start_ms <= 30_000:
                raise AcquisitionError("請求時間逆序或不連續")
            if self.last_request is not None:
                elapsed_ms = (current.before_ns - self.last_request[1]) / 1_000_000
                if abs(current.start_ms - self.last_request[0] - elapsed_ms) > 250:
                    raise AcquisitionError("請求時間與單調時鐘不連續")
            self.last_request = (current.start_ms, current.before_ns)
            if not response_changed:
                self.pending_request = (current.start_ms, current.before_ns)
        self.previous = current
        if not response_changed:
            return None
        delta = current.raw_ms - previous.raw_ms
        bracket = current.after_ns - previous.before_ns
        residual = current.raw_ms - current.start_ms - current.lag_ms
        paired = self.pending_request is not None and self.pending_request[0] == current.start_ms
        elapsed_ms = ((current.after_ns - self.pending_request[1]) / 1_000_000
                      if paired else float("inf"))
        quality = (1000 <= delta <= 30_000 and 0 < bracket <= 25_000_000
                   and paired and 0 <= residual <= 100 and abs(residual - elapsed_ms) <= 25)
        self.pending_request = None
        reason = ("accepted-quality" if quality else "unpaired-response" if not paired
                  else "interval-or-bracket-rejected")
        self.last_quality = (residual, bracket / 1_000_000,
                             (current.after_ns - current.before_ns) / 1_000_000, reason)
        if not quality:
            return None
        return ClockSample(self.identity, current.raw_ms,
                           (previous.before_ns + current.after_ns) // 2,
                           bracket, current.after_ns - current.before_ns, delta,
                           PROFILE_ID, residual, (self.last_quality,))


def validate_values(raw, start, offset, lag, _legacy_wall_ms=None, *, allow_pending_request=False):
    # startTime is written before the response updates raw/timeLag. A bounded
    # negative relation can therefore be a pending request, never a valid anchor.
    minimum_relation = -30_000 if allow_pending_request else -10_000
    return (all(math.isfinite(value) for value in (raw, start, offset, lag))
            and 1_000_000_000_000 <= raw <= 2_200_000_000_000
            and 1_000_000_000_000 <= start <= 2_200_000_000_000
            and offset == -28_800_000.0 and abs(lag) <= 86_400_000
            and minimum_relation <= raw - start - lag <= 10_000)


class GameClockReader:
    def __init__(self, native=None, monotonic_ns=time.perf_counter_ns, **_legacy):
        self.native = native or NativeSource()
        self.now = monotonic_ns
        self._profile_cache = None
        self._progress_hook = None
        self._stream_cancel = None

    def _read_progress(self):
        if self._stream_cancel is not None and self._stream_cancel.is_set():
            raise AcquisitionError("來源選擇已改變")
        if self._progress_hook is not None:
            self._progress_hook()

    @staticmethod
    def readable(info):
        return (info.State == 0x1000 and not info.Protect & 0x100
                and info.Protect & 0xff in (2, 4, 8, 0x20, 0x40, 0x80))

    def u32(self, handle, address):
        return struct.unpack("<I", self.native.read(handle, address, 4))[0]

    def avm_string(self, handle, address):
        pointer = self.u32(handle, address + 8)
        size = self.u32(handle, address + 16)
        if not 0 < size <= 128:
            raise AcquisitionError("遊戲字串結構不符")
        try:
            return self.native.read(handle, pointer, size).decode("ascii")
        except UnicodeDecodeError:
            raise AcquisitionError("遊戲字串結構不符") from None

    def class_traits(self, handle, address, name):
        # Timestamp-like pairs can occur near the start of an allocation. A
        # successfully queried, unreadable putative object is not an AVM object;
        # query/RPM failures themselves must still invalidate the whole scan.
        if not 0 < address < 0x80000000 - 12:
            raise NonTargetObject("非遊戲物件位址")
        object_info = self.native.query(handle, address)
        if not self.readable(object_info):
            raise NonTargetObject("非可讀遊戲物件")
        vtable = self.u32(handle, address)
        if not 0 < vtable < 0x80000000:
            raise NonTargetObject("非遊戲物件指標")
        info = self.native.query(handle, vtable)
        if not self.readable(info) or info.Type != 0x1000000:
            raise NonTargetObject("非遊戲物件結構")
        avm = self.u32(handle, address + 8)
        traits = self.u32(handle, avm + 20)
        if self.avm_string(handle, self.u32(handle, traits + 72)) != name:
            raise NonTargetObject("非目標遊戲物件類型")
        return traits

    def validate_structure(self, handle, candidate):
        mini = self.class_traits(handle, candidate.object_address, "MiniMapCanvas")
        core = self.class_traits(handle, candidate.core_address, "Core")
        if self.u32(handle, candidate.object_address + 0x398) != candidate.core_address:
            raise AcquisitionError("遊戲物件關係已改變")
        self.validate_slot_profile(handle, mini, core)

    def validate_slot_profile(self, handle, mini, core):
        positions = []
        bases = []
        for traits, (offset, size, digest, base_name) in zip((mini, core), TRAIT_PROFILES):
            parent = self.u32(handle, traits + 8)
            if self.avm_string(handle, self.u32(handle, parent + 72)) != base_name:
                raise AcquisitionError("遊戲繼承結構不符")
            position = self.u32(handle, traits + 0x58)
            record = self.native.read(handle, position, size)
            if hashlib.sha256(record).hexdigest().upper() != digest:
                raise AcquisitionError("遊戲欄位或型別版本不符")
            positions.append(position)
            bases.append(position - offset)
        if bases[0] != bases[1] or not 0 < bases[0] < 0x80000000 - ABC_LENGTH:
            raise AcquisitionError("遊戲ABC來源關係不符")
        key = (handle, mini, core, bases[0])
        if self._profile_cache == key:
            return
        if self.native.read(handle, bases[0], 4) != b"\x10\x00\x2e\x00":
            raise AcquisitionError("不支援的遊戲ABC版本")
        digest = hashlib.sha256()
        position, end = bases[0], bases[0] + ABC_LENGTH
        while position < end:
            if self._stream_cancel is not None and self._stream_cancel.is_set():
                raise AcquisitionError("來源選擇已改變")
            info = self.native.query(handle, position)
            region_base = int(info.BaseAddress or 0)
            region_end = region_base + int(info.RegionSize)
            if not self.readable(info) or not region_base <= position < region_end:
                raise AcquisitionError("遊戲ABC無法完整驗證")
            amount = min(65536, end - position, region_end - position)
            digest.update(self.native.read(handle, position, amount))
            self._read_progress()
            position += amount
        if digest.hexdigest().upper() != ABC_SHA256:
            raise AcquisitionError("遊戲常數池或方法版本不符")
        if positions != [self.u32(handle, traits + 0x58) for traits in (mini, core)]:
            raise AcquisitionError("遊戲版本結構已改變")
        self._profile_cache = key

    def observe(self, handle, candidate):
        self.validate_structure(handle, candidate)
        before = self.now()
        pair = self.native.read(handle, candidate.object_address + 0x410, 16)
        core_values = self.native.read(handle, candidate.core_address + 0x158, 24)
        # Seqlock-style reread rejects a game update split across the two reads.
        repeated = self.native.read(handle, candidate.object_address + 0x410, 16)
        after = self.now()
        if pair != repeated or not 0 <= after - before <= 20_000_000:
            raise AcquisitionError("取樣跨越更新或延遲過高")
        raw, start = struct.unpack("<dd", pair)
        offset, lag = struct.unpack_from("<d", core_values)[0], struct.unpack_from("<d", core_values, 16)[0]
        if not validate_values(raw, start, offset, lag,
                               allow_pending_request=True):
            raise AcquisitionError("遊戲時間數值或UTC+8關係不符")
        return Observation(raw, start, lag, before, after)

    @staticmethod
    def candidate_addresses(data, base):
        alignment = (-base) & 7
        count = (len(data) - alignment) // 8
        if count < 2:
            return
        values = array.array("d")
        values.frombytes(data[alignment:alignment + count * 8])
        low, high = 1_000_000_000_000, 2_200_000_000_000
        for index, value in enumerate(values[:-1]):
            if low <= value <= high and low <= values[index + 1] <= high:
                yield base + alignment + index * 8

    def scan(self, handle, cancel):
        self._profile_cache = None  # revalidate the full live ABC at each uniqueness scan
        deadline = self.now() + 30_000_000_000
        candidates = set()
        seen = set()
        address = 0
        # This exact non-LAA x86 player has a 2 GiB user address space.
        while address < 0x80000000:
            if cancel.is_set() or self.now() > deadline:
                raise AcquisitionError("來源改變或掃描逾時")
            info = self.native.query(handle, address)
            self._read_progress()
            base, size = int(info.BaseAddress or 0), int(info.RegionSize)
            if size <= 0 or base + size <= address:
                raise AcquisitionError("記憶體範圍無法確認")
            if info.Type == 0x20000 and self.readable(info):
                position, tail = base, b""
                while position < min(base + size, 0x80000000):
                    if cancel.is_set() or self.now() > deadline:
                        raise AcquisitionError("來源改變或掃描逾時")
                    amount = min(4 * 1024 * 1024, base + size - position)
                    data = self.native.read(handle, position, amount)
                    self._read_progress()
                    for field in self.candidate_addresses(tail + data, position - len(tail)):
                        if field in seen or field <= 0x410:
                            continue
                        seen.add(field)
                        try:
                            obj = field - 0x410
                            self.class_traits(handle, obj, "MiniMapCanvas")
                        except NonTargetObject:
                            continue
                        # Once recognized as MiniMapCanvas, incomplete/unknown metadata
                        # may not be silently skipped in favour of another candidate.
                        candidate = Candidate(obj, self.u32(handle, obj + 0x398))
                        self.observe(handle, candidate)
                        candidates.add(candidate)
                    tail = data[-8:]
                    position += amount
            address = base + size
        if len(candidates) != 1:
            raise AcquisitionError("沒有唯一且版本已驗證的遊戲時間物件")
        return next(iter(candidates))

    def stream(self, hwnd, cancel, publish):
        """One long-lived read-only handle; each accepted edge gets a FULL rescan.

        publish(sample-or-None, reason, validated-progress-QPC) is background-only.
        A scan can miss a natural edge: the post-scan baseline discards all pairing
        state, never inventing requests or rebasing the original sample anchor.
        """
        identity = self.native.identity(hwnd)
        handle = self.native.open(identity.pid)
        last_progress = self.now()
        last_identity_check = None

        def progress(force=False):
            nonlocal last_progress, last_identity_check
            now = self.now()
            if cancel.is_set():
                raise AcquisitionError("來源選擇已改變")
            if not 0 <= now - last_progress <= HEALTH_MAX_GAP_NS:
                raise AcquisitionError("來源讀取中斷或單調時鐘逆序")
            last_progress = now
            if force or last_identity_check is None or now - last_identity_check >= IDENTITY_CHECK_NS:
                if self.native.identity(hwnd) != identity:
                    raise AcquisitionError("來源身分已改變")
                checked = self.now()
                if not 0 <= checked - now <= HEALTH_MAX_GAP_NS:
                    raise AcquisitionError("來源身分驗證中斷")
                last_progress = last_identity_check = checked
                publish(None, "", checked)

        self._stream_cancel, self._progress_hook = cancel, progress
        try:
            progress(force=True)
            candidate = self.scan(handle, cancel)
            progress(force=True)
            self.observe(handle, candidate)  # post-scan baseline; never published
            progress()
            while not cancel.is_set():
                # Four read attempts per second at most; this is a read-only
                # faithful snapshot stream, not a millisecond polling loop.
                time.sleep(OBSERVATION_SLEEP_SECONDS)
                current = self.observe(handle, candidate)
                progress()
                # Identity/structure is checked without repeating the 2 GiB scan or
                # 26.8 MiB ABC digest for every sample. Any failed check closes the
                # stream; discovery must establish a fresh generation.
                self.validate_structure(handle, candidate)
                progress()
                now = self.now()
                sample = ClockSample(
                    identity=identity,
                    server_ms=current.raw_ms,
                    anchor_ns=(current.before_ns + current.after_ns) // 2,
                    observation_bracket_ns=current.after_ns - current.before_ns,
                    read_latency_ns=current.after_ns - current.before_ns,
                    transition_ms=1000.0,
                    profile=PROFILE_ID,
                )
                publish(sample, "", now)
                if cancel.is_set():
                    return
        finally:
            self._progress_hook = self._stream_cancel = None
            self.native.close(handle)

    def acquire(self, hwnd, cancel):
        identity = self.native.identity(hwnd)
        handle = self.native.open(identity.pid)
        try:
            if self.native.identity(hwnd) != identity:
                raise AcquisitionError("來源身分已改變")
            candidate = self.scan(handle, cancel)
            if cancel.is_set() or self.native.identity(hwnd) != identity:
                raise AcquisitionError("來源身分已改變")
            # A NEW read after the complete uniqueness scan: scan samples are never anchors.
            previous = self.observe(handle, candidate)
            deadline = self.now() + 45_000_000_000
            samples = []
            qualities = []
            observed_edges = 0
            pending_request = None
            last_request = None
            while not cancel.is_set():
                if self.now() > deadline:
                    break
                # Python 3.12 Windows sleep uses a process-local high-resolution
                # waitable timer; Event.wait is quantized by the system tick.
                # No busy wait or global timer-resolution change.
                time.sleep(0.001)
                current = self.observe(handle, candidate)
                request_changed = current.start_ms != previous.start_ms
                response_changed = current.raw_ms != previous.raw_ms
                if request_changed:
                    # startTime has no request ID. A second request before a response
                    # destroys observable pairing; never relabel it as a low-latency response.
                    if pending_request is not None:
                        raise AcquisitionError("偵測到請求重疊，無法可靠配對回應")
                    request_delta = current.start_ms - previous.start_ms
                    if not 1000 <= request_delta <= 30_000:
                        raise AcquisitionError("請求時間逆序或不連續")
                    if last_request is not None:
                        elapsed_ms = (current.before_ns - last_request[1]) / 1_000_000
                        if abs(current.start_ms - last_request[0] - elapsed_ms) > 250:
                            raise AcquisitionError("請求時間與單調時鐘不連續")
                    last_request = (current.start_ms, current.before_ns)
                    if not response_changed:
                        pending_request = (current.start_ms, current.before_ns)
                if current.raw_ms == previous.raw_ms:
                    previous = current
                    continue
                delta = current.raw_ms - previous.raw_ms
                bracket = current.after_ns - previous.before_ns
                observed_edges += 1
                residual = current.raw_ms - current.start_ms - current.lag_ms
                paired = pending_request is not None and pending_request[0] == current.start_ms
                # The locally observed request->response duration must also agree
                # with the game's Date-based interval. This is not network RTT.
                elapsed_ms = ((current.after_ns - pending_request[1]) / 1_000_000
                              if paired else float("inf"))
                quality = (1000 <= delta <= 30_000 and 0 < bracket <= 25_000_000
                           and paired and 0 <= residual <= 100
                           and abs(residual - elapsed_ms) <= 25)
                reason = ("accepted-quality" if quality else "unpaired-response" if not paired
                          else "interval-or-bracket-rejected")
                qualities.append((residual, bracket / 1_000_000,
                                  (current.after_ns - current.before_ns) / 1_000_000, reason))
                pending_request = None
                if quality:
                    samples.append(ClockSample(
                        identity, current.raw_ms, (previous.before_ns + current.after_ns) // 2,
                        bracket, current.after_ns - current.before_ns, delta, PROFILE_ID, residual,
                    ))
                previous = current
                if observed_edges < 3:
                    continue
                break
            if cancel.is_set():
                raise AcquisitionError("來源選擇已改變")
            if not samples:
                raise AcquisitionError("未取得延遲合格的自然更新")
            # A short bounded window selects the smallest observed request/response
            # interval; no RTT/2 or absolute-network-accuracy claim is made.
            sample = replace(min(samples, key=lambda item: (item.response_interval_ms, item.observation_bracket_ns)),
                             quality_observations=tuple(qualities))
            # Objects/uniqueness may change while waiting. Rescan before acceptance,
            # keeping the ORIGINAL edge QPC anchor (never the later scan time).
            if self.scan(handle, cancel) != candidate:
                raise AcquisitionError("遊戲時間物件已改變")
            self.validate_structure(handle, candidate)
            if cancel.is_set() or self.native.identity(hwnd) != identity:
                raise AcquisitionError("來源身分已改變")
            return sample
        finally:
            self.native.close(handle)
