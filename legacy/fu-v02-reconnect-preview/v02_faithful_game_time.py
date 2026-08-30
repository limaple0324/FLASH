"""Fail-closed faithful game-time selection, consensus, grouping and throttling."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import os
import queue
import threading
import time

APPROVED_SHORTCUTS = ("120古", "120靈", "大排", "餐廳")
DISCOVERY_INTERVAL_NS = 5_000_000_000
READ_INTERVAL_NS = 250_000_000
PUBLISH_INTERVAL_NS = 250_000_000
CONSENSUS_SAMPLES = 3


@dataclass(frozen=True)
class ShortcutBinding:
    label: str
    argument_fingerprint: str


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
    """Resolve only the four named desktop shortcuts; never invent a fallback."""
    def __init__(self, fingerprint, resolver=None, desktop=None):
        self.fingerprint = fingerprint
        self.resolver = resolver or self._resolve_windows_shortcut
        self.desktop = desktop or os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")

    @staticmethod
    def _resolve_windows_shortcut(path):
        import win32com.client  # packaged Windows dependency
        shortcut = win32com.client.Dispatch("WScript.Shell").CreateShortcut(path)
        return shortcut.TargetPath, shortcut.Arguments

    def bindings(self):
        result = []
        for label in APPROVED_SHORTCUTS:
            path = os.path.join(self.desktop, label + ".lnk")
            if not os.path.isfile(path):
                continue
            try:
                target, arguments = self.resolver(path)
                if not target or not arguments:
                    continue
                result.append(ShortcutBinding(label, self.fingerprint(arguments)))
            except Exception:
                continue
        return tuple(result)

    def select(self, identities):
        by_fingerprint = {item.argument_fingerprint: item.label for item in self.bindings()}
        selected = []
        for identity in identities:
            label = by_fingerprint.get(identity.launch_fingerprint)
            if label:
                selected.append((label, identity))
        return tuple(selected)


class FaithfulConsensus:
    def __init__(self):
        self.samples = {name: deque(maxlen=CONSENSUS_SAMPLES) for name in APPROVED_SHORTCUTS}
        self.committed = {}
        self.generation = {}
        self.resample_all = False

    def invalidate(self, label, generation):
        self.samples[label].clear()
        self.committed.pop(label, None)
        self.generation[label] = generation

    def add(self, sample: FaithfulSample):
        if sample.label not in self.samples or not sample.value:
            return False
        if self.generation.get(sample.label) != sample.generation:
            self.invalidate(sample.label, sample.generation)
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

    def display(self, readable_labels=APPROVED_SHORTCUTS):
        groups = defaultdict(list)
        for label in APPROVED_SHORTCUTS:
            if label in self.committed:
                groups[self.committed[label][0]].append(label)
        ordered = tuple((value, tuple(labels)) for value, labels in groups.items())
        unreadable = tuple(label for label in readable_labels if label not in self.committed)
        return FaithfulDisplay(ordered, unreadable)


class FaithfulScheduler:
    """Injectable rate gates used by the production source and direct tests."""
    def __init__(self, monotonic_ns=time.perf_counter_ns):
        self.now = monotonic_ns
        self.last_discovery = None
        self.last_read = {}
        self.last_publish = None
        self.last_payload = None

    def allow_discovery(self):
        now = self.now()
        if self.last_discovery is not None and now - self.last_discovery < DISCOVERY_INTERVAL_NS:
            return False
        self.last_discovery = now
        return True

    def allow_read(self, label):
        now = self.now()
        previous = self.last_read.get(label)
        if previous is not None and now - previous < READ_INTERVAL_NS:
            return False
        self.last_read[label] = now
        return True

    def allow_publish(self, payload):
        now = self.now()
        if payload == self.last_payload:
            return False
        if self.last_publish is not None and now - self.last_publish < PUBLISH_INTERVAL_NS:
            return False
        self.last_publish, self.last_payload = now, payload
        return True


class MultiGameClockSource:
    """One read-only stream per approved shortcut identity, with grouped output."""
    def __init__(self, reader_factory, discover, *, monotonic_ns=time.perf_counter_ns,
                 thread_factory=threading.Thread):
        self.reader_factory = reader_factory
        self.discover = discover
        self.scheduler = FaithfulScheduler(monotonic_ns)
        self.thread_factory = thread_factory
        self.consensus = FaithfulConsensus()
        self.events = queue.Queue(maxsize=64)
        self.streams = {}
        self.discovery = None
        self.discovery_cancel = threading.Event()
        self.available = set(APPROVED_SHORTCUTS)
        self.anchors = {}
        self.display = FaithfulDisplay((), APPROVED_SHORTCUTS)
        self.status = "遊戲時間：尚未讀取（僅四個核准桌面捷徑）"
        self.closed = False

    @property
    def token(self):
        return tuple((label, self.consensus.generation.get(label, 0)) for label in APPROVED_SHORTCUTS)

    def is_busy(self):
        return ((self.discovery is not None and self.discovery.is_alive())
                or any(worker.is_alive() for _identity, worker, _cancel, _generation
                       in self.streams.values()))

    def invalidate(self, reason="來源已失效"):
        for label in APPROVED_SHORTCUTS:
            self.consensus.invalidate(label, self.consensus.generation.get(label, 0) + 1)
        self.anchors.clear()
        self.status = f"遊戲時間：尚未讀取（{reason}）"

    def _emit(self, event):
        try:
            self.events.put_nowait(event)
        except queue.Full:
            pass

    def _start_discovery(self):
        if self.discovery is not None and self.discovery.is_alive():
            return
        self.discovery_cancel = threading.Event()
        def run():
            try:
                self._emit(("discovered", tuple(self.discover(self.discovery_cancel))))
            except Exception:
                self._emit(("discovery_error", ()))
        self.discovery = self.thread_factory(target=run, name="v02-clock-discovery", daemon=True)
        self.discovery.start()

    def _start_stream(self, label, identity):
        prior = self.streams.get(label)
        if prior and prior[0] == identity and prior[1].is_alive():
            return
        if prior:
            prior[2].set()
        cancel = threading.Event()
        generation = self.consensus.generation.get(label, 0) + 1
        self.consensus.invalidate(label, generation)
        reader = self.reader_factory()
        def run():
            try:
                def publish(sample, reason, _checked):
                    if reason:
                        raise RuntimeError("source invalid")
                    if sample is not None and self.scheduler.allow_read(label):
                        self._emit(("sample", (label, generation, sample)))
                reader.stream(identity.hwnd, cancel, publish)
            except Exception:
                self._emit(("stream_error", (label, generation)))
        worker = self.thread_factory(target=run, name=f"v02-clock-{label}", daemon=True)
        self.streams[label] = (identity, worker, cancel, generation)
        worker.start()

    @staticmethod
    def _format_server_ms(server_ms):
        value = int(server_ms + 28_800_000) % 86_400_000
        return f"{value // 3_600_000:02d}:{value // 60_000 % 60:02d}:{value // 1000 % 60:02d}.{value % 1000:03d}"

    def poll(self):
        if self.closed:
            return
        if self.scheduler.allow_discovery():
            self._start_discovery()
        for _ in range(64):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "discovered":
                selected = {label: identity for label, identity in payload if label in APPROVED_SHORTCUTS}
                for label, identity in selected.items():
                    self._start_stream(label, identity)
                for label in set(self.streams) - set(selected):
                    self.streams[label][2].set()
                    self.streams.pop(label, None)
                    self.consensus.invalidate(label, self.consensus.generation.get(label, 0) + 1)
            elif kind == "sample":
                label, generation, sample = payload
                current = self.streams.get(label)
                if current and current[3] == generation:
                    value = self._format_server_ms(sample.server_ms)
                    self.consensus.add(FaithfulSample(label, generation, value, "millisecond"))
                    self.anchors[label] = (int(sample.server_ms), sample.anchor_ns)
            elif kind == "stream_error":
                label, generation = payload
                current = self.streams.get(label)
                if current and current[3] == generation:
                    self.consensus.invalidate(label, generation + 1)
        if self.consensus.resample_all:
            # A source crossed a displayed boundary. All sources must earn a new
            # three-sample same-generation consensus before a new grouping appears.
            for label in APPROVED_SHORTCUTS:
                generation = self.consensus.generation.get(label, 0)
                self.consensus.invalidate(label, generation)
            self.consensus.resample_all = False
        display = self.consensus.display(APPROVED_SHORTCUTS)
        payload = (display.groups, display.unreadable)
        if self.scheduler.allow_publish(payload):
            self.display = display
            self.status = display.text()

    def exact_time_of_day_ms(self):
        values = {value for value, _precision in self.consensus.committed.values()}
        if len(values) != 1:
            return None
        value = next(iter(values))
        hour, minute, rest = value.split(":")
        second, millis = rest.split(".")
        return (((int(hour) * 60 + int(minute)) * 60 + int(second)) * 1000 + int(millis))

    def timed_action_time_of_day_ms(self):
        """Internal-only QPC estimate; never returned by display/status/grouping."""
        if self.exact_time_of_day_ms() is None:
            return None
        labels = [label for label in APPROVED_SHORTCUTS if label in self.consensus.committed]
        if not labels or labels[0] not in self.anchors:
            return None
        server_ms, anchor_ns = self.anchors[labels[0]]
        elapsed = max(0, self.scheduler.now() - anchor_ns) // 1_000_000
        return int(server_ms + 28_800_000 + elapsed) % 86_400_000

    def shutdown(self):
        self.closed = True
        self.invalidate("程式正在關閉")
        self.discovery_cancel.set()
        for _identity, _worker, cancel, _generation in self.streams.values():
            cancel.set()
