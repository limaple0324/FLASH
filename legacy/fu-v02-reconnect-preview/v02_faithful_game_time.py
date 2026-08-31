"""Fail-closed four-shortcut game-time selection, consensus and scheduling."""
from __future__ import annotations

from collections import defaultdict, deque
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
import queue
import threading
import time
import uuid

from v02_game_clock import HEALTH_MAX_GAP_NS, SourceIdentity


APPROVED_SHORTCUTS = ("120古", "120靈", "大排", "餐廳")
DISCOVERY_INTERVAL_NS = 5_000_000_000
READ_INTERVAL_NS = 250_000_000
PUBLISH_INTERVAL_NS = 250_000_000
CONSENSUS_SAMPLES = 3
EVENT_QUEUE_SIZE = 64
FIRST_SAMPLE_TIMEOUT_NS = 105_000_000_000
FRESHNESS_TTL_NS = HEALTH_MAX_GAP_NS
FOLDERID_DESKTOP = "B4BFCC3A-DB2C-424C-B029-7FE99A87C641"


def _normalized_target(path: str) -> str:
    value = str(path or "").strip()
    if not value:
        return ""
    try:
        return os.path.normcase(os.path.abspath(os.path.expandvars(value)))
    except (OSError, ValueError):
        return ""


def windows_desktop_known_folder() -> str | None:
    """Resolve the current user's Desktop Known Folder, never Public Desktop."""
    if os.name != "nt":
        return None

    class GUID(ctypes.Structure):
        _fields_ = (("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8))

    folder_id = GUID.from_buffer_copy(uuid.UUID(FOLDERID_DESKTOP).bytes_le)
    result = ctypes.c_wchar_p()
    ole32 = None
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        shell32.SHGetKnownFolderPath.argtypes = (
            ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_wchar_p),
        )
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
        if shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(result)) != 0:
            return None
        path = _normalized_target(result.value or "")
        return path or None
    except (AttributeError, OSError):
        return None
    finally:
        if result and ole32 is not None:
            try:
                ole32.CoTaskMemFree(result)
            except OSError:
                pass


@dataclass(frozen=True)
class ShortcutBinding:
    label: str
    normalized_target: str
    launch_identity: str

    @property
    def argument_fingerprint(self) -> str:
        """Compatibility spelling; the value is still session-only HMAC data."""
        return self.launch_identity


@dataclass(frozen=True)
class FaithfulSample:
    label: str
    generation: int
    value: str
    precision: str


@dataclass(frozen=True)
class FaithfulDisplay:
    groups: tuple[tuple[str, tuple[str, ...]], ...]
    unreadable: tuple[str, ...]

    def text(self) -> str:
        lines = [f"{value}：{'、'.join(labels)}" for value, labels in self.groups]
        if self.unreadable:
            lines.append(f"無法讀取：{'、'.join(self.unreadable)}")
        return "\n".join(lines) if lines else "尚未讀取"


class ApprovedShortcutCatalog:
    """Resolve only exact names on the current user's Desktop Known Folder."""
    def __init__(self, fingerprint, resolver=None, desktop=None):
        self.fingerprint = fingerprint
        self.resolver = resolver or self._resolve_windows_shortcut
        self.desktop = _normalized_target(desktop) if desktop is not None else windows_desktop_known_folder()

    @staticmethod
    def _resolve_windows_shortcut(path):
        import win32com.client
        shortcut = win32com.client.Dispatch("WScript.Shell").CreateShortcut(path)
        return shortcut.TargetPath, shortcut.Arguments

    def bindings(self):
        if not self.desktop or not os.path.isdir(self.desktop):
            return ()
        result = []
        seen_labels = set()
        seen_keys = set()
        seen_launch_identities = set()
        for label in APPROVED_SHORTCUTS:
            path = os.path.join(self.desktop, label + ".lnk")
            if not os.path.isfile(path):
                continue
            try:
                target, arguments = self.resolver(path)
                normalized_target = _normalized_target(target)
                launch_identity = self.fingerprint(arguments) if arguments else ""
            except Exception:
                continue
            key = (normalized_target, launch_identity)
            if (not normalized_target or not launch_identity or label in seen_labels
                    or key in seen_keys or launch_identity in seen_launch_identities):
                return ()
            seen_labels.add(label)
            seen_keys.add(key)
            seen_launch_identities.add(launch_identity)
            result.append(ShortcutBinding(label, normalized_target, launch_identity))
        return tuple(result)

    def select(self, identities):
        bindings = self.bindings()
        binding_keys = {(item.normalized_target, item.launch_identity): item for item in bindings}
        binding_launches = {item.launch_identity for item in bindings}
        if len(binding_keys) != len(bindings):
            return ()
        matches = {}
        seen_hwnds = set()
        seen_processes = set()
        seen_identities = set()
        for identity in identities:
            try:
                key = (_normalized_target(identity.normalized_target), identity.launch_fingerprint)
                process = (int(identity.pid), int(identity.created))
                hwnd = int(identity.hwnd)
                full = (hwnd, process, int(identity.tid), key, identity.image_sha256)
            except (AttributeError, TypeError, ValueError):
                continue
            binding = binding_keys.get(key)
            if binding is None:
                if identity.launch_fingerprint in binding_launches:
                    return ()
                continue
            if (hwnd in seen_hwnds or process in seen_processes or full in seen_identities
                    or binding.label in matches):
                return ()
            seen_hwnds.add(hwnd)
            seen_processes.add(process)
            seen_identities.add(full)
            matches[binding.label] = identity
        return tuple((label, matches[label]) for label in APPROVED_SHORTCUTS if label in matches)


class FaithfulConsensus:
    def __init__(self):
        self.samples = {name: deque(maxlen=CONSENSUS_SAMPLES) for name in APPROVED_SHORTCUTS}
        self.committed = {}
        self.generation = {name: 0 for name in APPROVED_SHORTCUTS}
        self.resample_all = False

    def invalidate(self, label, generation):
        if label not in self.samples or generation < self.generation[label]:
            return False
        self.samples[label].clear()
        self.committed.pop(label, None)
        self.generation[label] = generation
        return True

    def add(self, sample: FaithfulSample):
        if (sample.label not in self.samples or not sample.value
                or self.generation[sample.label] != sample.generation):
            return False
        bucket = self.samples[sample.label]
        bucket.append(sample)
        if len(bucket) < CONSENSUS_SAMPLES:
            return False
        keys = {(item.generation, item.value, item.precision) for item in bucket}
        if len(keys) != 1:
            return False
        previous = self.committed.get(sample.label)
        current = (sample.value, sample.precision)
        changed = previous != current
        if previous is not None and changed and previous[0][:5] != current[0][:5]:
            self.resample_all = True
        self.committed[sample.label] = current
        return changed

    def confirmed(self, sample: FaithfulSample) -> bool:
        bucket = self.samples.get(sample.label, ())
        return (len(bucket) == CONSENSUS_SAMPLES
                and all(item == sample for item in bucket)
                and self.committed.get(sample.label) == (sample.value, sample.precision))

    def pause_for_boundary(self):
        for label in APPROVED_SHORTCUTS:
            self.invalidate(label, self.generation[label])
        self.resample_all = False

    def display(self, readable_labels=APPROVED_SHORTCUTS):
        groups = defaultdict(list)
        for label in APPROVED_SHORTCUTS:
            if label in self.committed:
                groups[self.committed[label][0]].append(label)
        ordered = tuple((value, tuple(labels)) for value, labels in groups.items())
        unreadable = tuple(label for label in readable_labels if label not in self.committed)
        return FaithfulDisplay(ordered, unreadable)


class FaithfulScheduler:
    def __init__(self, monotonic_ns=time.perf_counter_ns):
        self.now = monotonic_ns
        self.last_discovery = None
        self.last_read = {}
        self.last_publish = None
        self.last_payload = None
        self._lock = threading.Lock()

    def allow_discovery(self):
        with self._lock:
            now = self.now()
            if (self.last_discovery is not None
                    and now - self.last_discovery < DISCOVERY_INTERVAL_NS):
                return False
            self.last_discovery = now
            return True

    def allow_read(self, label, checked_ns=None):
        with self._lock:
            now = self.now() if checked_ns is None else checked_ns
            previous = self.last_read.get(label)
            if previous is not None and not now - previous >= READ_INTERVAL_NS:
                return False
            self.last_read[label] = now
            return True

    def allow_publish(self, payload):
        with self._lock:
            now = self.now()
            if payload == self.last_payload:
                return False
            if self.last_publish is not None and now - self.last_publish < PUBLISH_INTERVAL_NS:
                return False
            self.last_publish, self.last_payload = now, payload
            return True


class MultiGameClockSource:
    """Control-plane generations fence every discovery and stream event."""
    def __init__(self, reader_factory, discover, *, monotonic_ns=time.perf_counter_ns,
                 thread_factory=threading.Thread):
        self.reader_factory = reader_factory
        self.discover = discover
        self.scheduler = FaithfulScheduler(monotonic_ns)
        self.thread_factory = thread_factory
        self.consensus = FaithfulConsensus()
        self.events = queue.Queue(maxsize=EVENT_QUEUE_SIZE)
        self.event_epoch = 0
        self.overflow_fault = threading.Event()
        self._overflow_epoch = None
        self._event_lock = threading.Lock()
        self.streams = {}
        self.discovery = None
        self.discovery_cancel = threading.Event()
        self._retired_workers = []
        self.anchors = {}
        self.last_checked_ns = {}
        self.last_sample_ns = {}
        self.committed_ns = {}
        self.stream_started_ns = {}
        self.last_poll_ns = None
        self.revalidating = False
        self.source_faulted = True
        self.display = FaithfulDisplay((), APPROVED_SHORTCUTS)
        self.status = "來源失效"
        self.closed = False

    @property
    def token(self):
        return (self.event_epoch,
                tuple((label, self.consensus.generation[label]) for label in APPROVED_SHORTCUTS))

    def is_busy(self):
        workers = [item[1] for item in self.streams.values()] + self._retired_workers
        return ((self.discovery is not None and self.discovery.is_alive())
                or any(worker.is_alive() for worker in workers))

    def _cancel_workers(self):
        self.discovery_cancel.set()
        if self.discovery is not None:
            self._retired_workers.append(self.discovery)
        for _identity, worker, cancel, _generation, _epoch in self.streams.values():
            cancel.set()
            self._retired_workers.append(worker)
        self.streams.clear()

    def _invalidate_locked(self, reason="來源失效", *, preserve_overflow=False):
        self._cancel_workers()
        self.event_epoch += 1
        self.events = queue.Queue(maxsize=EVENT_QUEUE_SIZE)
        if not preserve_overflow:
            self.overflow_fault.clear()
            self._overflow_epoch = None
        for label in APPROVED_SHORTCUTS:
            self.consensus.invalidate(label, self.consensus.generation[label] + 1)
        self.anchors.clear()
        self.last_checked_ns.clear()
        self.last_sample_ns.clear()
        self.committed_ns.clear()
        self.stream_started_ns.clear()
        self.revalidating = False
        self.source_faulted = True
        self.display = FaithfulDisplay((), APPROVED_SHORTCUTS)
        self.status = "來源失效"

    def invalidate(self, reason="來源失效"):
        with self._event_lock:
            self._invalidate_locked(reason)

    def _emit(self, kind, payload, checked_ns, epoch):
        with self._event_lock:
            if epoch != self.event_epoch:
                return False
            try:
                self.events.put_nowait((epoch, kind, payload, checked_ns))
                return True
            except queue.Full:
                self._invalidate_locked("來源失效", preserve_overflow=True)
                self._overflow_epoch = self.event_epoch
                self.overflow_fault.set()
                return False

    def _start_discovery(self):
        if self.discovery is not None and self.discovery.is_alive():
            return
        self.discovery_cancel = threading.Event()
        epoch = self.event_epoch
        cancel = self.discovery_cancel

        def run():
            try:
                result = tuple(self.discover(cancel))
                self._emit("discovered", result, self.scheduler.now(), epoch)
            except Exception:
                self._emit("discovery_error", (), self.scheduler.now(), epoch)

        self.discovery = self.thread_factory(target=run, name="v02-clock-discovery", daemon=True)
        self.discovery.start()

    def _start_stream(self, label, identity):
        prior = self.streams.get(label)
        if prior is not None:
            if prior[0] == identity and prior[1].is_alive():
                return
            self.invalidate("來源失效")
            return
        cancel = threading.Event()
        generation = self.consensus.generation[label] + 1
        self.consensus.invalidate(label, generation)
        epoch = self.event_epoch
        reader = self.reader_factory()
        try:
            reader.expected_identity = identity
        except Exception:
            pass

        def run():
            try:
                native = getattr(reader, "native", None)
                if native is None or native.identity(identity.hwnd) != identity:
                    raise RuntimeError("identity")

                def publish(item, reason, checked_ns):
                    if cancel.is_set():
                        return
                    if native.identity(identity.hwnd) != identity:
                        raise RuntimeError("identity")
                    if item is not None and item.identity != identity:
                        raise RuntimeError("identity")
                    self._emit(
                        "progress", (label, generation, identity, item, bool(reason)),
                        checked_ns, epoch,
                    )

                reader.stream(identity.hwnd, cancel, publish)
                if not cancel.is_set() and native.identity(identity.hwnd) != identity:
                    raise RuntimeError("identity")
                if not cancel.is_set():
                    self._emit("stream_error", (label, generation), self.scheduler.now(), epoch)
            except Exception:
                self._emit("stream_error", (label, generation), self.scheduler.now(), epoch)

        worker = self.thread_factory(target=run, name=f"v02-clock-{label}", daemon=True)
        self.streams[label] = (identity, worker, cancel, generation, epoch)
        started = self.scheduler.now()
        self.stream_started_ns[label] = started
        self.last_checked_ns[label] = started
        worker.start()

    @staticmethod
    def _format_server_ms(server_ms):
        value = int(server_ms + 28_800_000) % 86_400_000
        return f"{value // 3_600_000:02d}:{value // 60_000 % 60:02d}:{value // 1000 % 60:02d}.{value % 1000:03d}"

    @staticmethod
    def _valid_discovery(payload):
        selected = {}
        seen_processes = set()
        seen_identities = set()
        for row in payload:
            if not isinstance(row, tuple) or len(row) != 2:
                return None
            label, identity = row
            if label not in APPROVED_SHORTCUTS or not isinstance(identity, SourceIdentity):
                return None
            process = (identity.pid, identity.created)
            if (label in selected or process in seen_processes or identity in seen_identities
                    or not identity.normalized_target):
                return None
            selected[label] = identity
            seen_processes.add(process)
            seen_identities.add(identity)
        return selected

    def _fresh_checked(self, label, checked_ns, now):
        if not isinstance(checked_ns, int) or not 0 <= now - checked_ns <= FRESHNESS_TTL_NS:
            return False
        previous = self.last_checked_ns.get(label)
        return previous is None or checked_ns >= previous

    def _health_fault(self, now):
        for label in self.streams:
            checked = self.last_checked_ns.get(label)
            if checked is None or not 0 <= now - checked <= FRESHNESS_TTL_NS:
                return True
            if label in self.consensus.committed:
                sample_checked = self.last_sample_ns.get(label)
                committed_checked = self.committed_ns.get(label)
                anchor = self.anchors.get(label)
                if (sample_checked is None or committed_checked is None or anchor is None
                        or not 0 <= now - sample_checked <= FRESHNESS_TTL_NS
                        or not 0 <= now - committed_checked <= FRESHNESS_TTL_NS
                        or not 0 <= now - anchor[2] <= FRESHNESS_TTL_NS):
                    return True
            elif now - self.stream_started_ns.get(label, now) > FIRST_SAMPLE_TIMEOUT_NS:
                return True
        return False

    def poll(self):
        if self.closed:
            return
        now = self.scheduler.now()
        previous = self.last_poll_ns
        self.last_poll_ns = now
        if previous is not None and not 0 <= now - previous <= FRESHNESS_TTL_NS:
            self.invalidate("來源失效")
            return
        with self._event_lock:
            if self.overflow_fault.is_set() and self._overflow_epoch == self.event_epoch:
                self.overflow_fault.clear()
                self._overflow_epoch = None
                return
            poll_epoch = self.event_epoch
            pending = []
            for _ in range(EVENT_QUEUE_SIZE):
                try:
                    pending.append(self.events.get_nowait())
                except queue.Empty:
                    break
        if self._health_fault(now):
            self.invalidate("來源失效")
            return
        if not self.streams and (self.discovery is None or not self.discovery.is_alive()):
            if self.scheduler.allow_discovery():
                self._start_discovery()
        boundary = False
        for epoch, kind, payload, checked_ns in pending:
            if epoch != poll_epoch or poll_epoch != self.event_epoch:
                continue
            if kind == "discovery_error":
                self.invalidate("來源失效")
                return
            if kind == "discovered":
                if not isinstance(checked_ns, int) or not 0 <= now - checked_ns <= FRESHNESS_TTL_NS:
                    self.invalidate("來源失效")
                    return
                selected = self._valid_discovery(payload)
                if selected is None:
                    self.invalidate("來源失效")
                    return
                if not selected:
                    self.source_faulted = True
                    self.status = "來源失效"
                    continue
                self.source_faulted = False
                for label in APPROVED_SHORTCUTS:
                    if label in selected:
                        self._start_stream(label, selected[label])
                continue
            if kind == "stream_error":
                label, generation = payload
                current = self.streams.get(label)
                if current is not None and current[3] == generation:
                    self.invalidate("來源失效")
                    return
                continue
            if kind != "progress":
                continue
            label, generation, expected, item, has_reason = payload
            current = self.streams.get(label)
            if current is None or current[3] != generation or current[4] != epoch:
                continue
            if (expected != current[0] or has_reason
                    or not self._fresh_checked(label, checked_ns, now)):
                self.invalidate("來源失效")
                return
            self.last_checked_ns[label] = checked_ns
            if item is None:
                continue
            if (item.identity != expected or not isinstance(item.anchor_ns, int)
                    or not 0 <= checked_ns - item.anchor_ns <= FRESHNESS_TTL_NS):
                self.invalidate("來源失效")
                return
            if not self.scheduler.allow_read(label, checked_ns):
                continue
            self.last_sample_ns[label] = checked_ns
            value = self._format_server_ms(item.server_ms)
            faithful = FaithfulSample(label, generation, value, "millisecond")
            self.consensus.add(faithful)
            if self.consensus.confirmed(faithful):
                self.anchors[label] = (int(item.server_ms), item.anchor_ns, checked_ns, generation)
                self.committed_ns[label] = checked_ns
            boundary = boundary or self.consensus.resample_all
        if boundary:
            self.consensus.pause_for_boundary()
            self.anchors.clear()
            self.committed_ns.clear()
            self.revalidating = True
            boundary_started_ns = self.scheduler.now()
            for label in self.streams:
                self.stream_started_ns[label] = boundary_started_ns
        active = set(self.streams)
        if self.revalidating and active and active.issubset(self.consensus.committed):
            self.revalidating = False
        with self._event_lock:
            if (poll_epoch != self.event_epoch
                    or (self.overflow_fault.is_set()
                        and self._overflow_epoch == self.event_epoch)):
                self._invalidate_locked("來源失效")
                return
            if self._health_fault(self.scheduler.now()):
                self._invalidate_locked("來源失效")
                return
            display = self.consensus.display(APPROVED_SHORTCUTS)
            payload = (display.groups, display.unreadable)
            if self.scheduler.allow_publish(payload):
                self.display = display
                self.status = display.text()

    def _exact_time_of_day_ms_locked(self):
        if self.revalidating or self.source_faulted:
            return None
        values = {value for value, _precision in self.consensus.committed.values()}
        if len(values) != 1:
            return None
        value = next(iter(values))
        hour, minute, rest = value.split(":")
        second, millis = rest.split(".")
        return (((int(hour) * 60 + int(minute)) * 60 + int(second)) * 1000 + int(millis))

    def exact_time_of_day_ms(self):
        with self._event_lock:
            return self._exact_time_of_day_ms_locked()

    def _timed_source_state_locked(self, now):
        if (self.overflow_fault.is_set() and self._overflow_epoch == self.event_epoch):
            return "fault"
        if (self.source_faulted or self.last_poll_ns is None
                or not 0 <= now - self.last_poll_ns <= FRESHNESS_TTL_NS
                or self._health_fault(now)):
            return "fault"
        if self.revalidating or self._exact_time_of_day_ms_locked() is None:
            return "waiting"
        return "valid"

    def timed_source_state(self):
        now = self.scheduler.now()
        with self._event_lock:
            return self._timed_source_state_locked(now)

    def timed_action_time_of_day_ms(self):
        now = self.scheduler.now()
        with self._event_lock:
            if self._timed_source_state_locked(now) != "valid":
                return None
            labels = [label for label in APPROVED_SHORTCUTS
                      if label in self.consensus.committed]
            if not labels:
                return None
            anchor = self.anchors.get(labels[0])
            if anchor is None:
                return None
            server_ms, anchor_ns, checked_ns, generation = anchor
            if (generation != self.consensus.generation[labels[0]]
                    or not 0 <= now - checked_ns <= FRESHNESS_TTL_NS
                    or not 0 <= now - anchor_ns <= FRESHNESS_TTL_NS):
                return None
            elapsed = (now - anchor_ns) // 1_000_000
            return int(server_ms + 28_800_000 + elapsed) % 86_400_000

    def shutdown(self):
        if self.closed:
            return
        self.closed = True
        self.invalidate("來源失效")
