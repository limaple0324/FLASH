"""Fail-closed four-shortcut game-time selection, consensus and scheduling."""
from __future__ import annotations

from collections import defaultdict, deque
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import math
import os
import queue
import threading
import time
import uuid

from v02_game_clock import ClockSample, HEALTH_MAX_GAP_NS, SAMPLE_MAX_AGE_NS, SourceIdentity
from v02_game_clock_reader import (
    ClockReading, EDGE_RESPONSE_MAX_MS, FULL_SCAN_DEADLINE_NS, GameClockReader,
)


APPROVED_SHORTCUTS = ("120古", "120靈", "大排", "餐廳")
DISCOVERY_INTERVAL_NS = 5_000_000_000
READ_INTERVAL_NS = 250_000_000
PUBLISH_INTERVAL_NS = 250_000_000
CONSENSUS_SAMPLES = 3
EVENT_QUEUE_SIZE = 64
FIRST_SAMPLE_TIMEOUT_MARGIN_NS = 30_000_000_000
FIRST_SAMPLE_TIMEOUT_NS = (
    len(APPROVED_SHORTCUTS) * FULL_SCAN_DEADLINE_NS + FIRST_SAMPLE_TIMEOUT_MARGIN_NS
)
FRESHNESS_TTL_NS = HEALTH_MAX_GAP_NS
DAY_MS = 86_400_000
UTC8_MS = 28_800_000
EDGE_RESIDUAL_MAX_MS = 25
EDGE_BRACKET_MAX_MS = 25
BASE_QUALIFIED_ANCHOR_LEASE_NS = (
    SAMPLE_MAX_AGE_NS
    + (EDGE_RESPONSE_MAX_MS + EDGE_BRACKET_MAX_MS) * 1_000_000
)
MULTI_ANCHOR_SPREAD_TOLERANCE_MS = min(
    100, 2 * (EDGE_RESIDUAL_MAX_MS + EDGE_BRACKET_MAX_MS),
)
PENDING_HANDOFF_POLL_SLACK_NS = 2 * READ_INTERVAL_NS
PENDING_CONSENSUS_HANDOFF_MAX_NS = min(
    1_000_000_000,
    (CONSENSUS_SAMPLES - 1) * READ_INTERVAL_NS + PENDING_HANDOFF_POLL_SLACK_NS,
)
QUEUED_EDGE_POLL_HANDOFF_MAX_NS = 2 * READ_INTERVAL_NS
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
class QueuedEdgeMarker:
    label: str
    generation: int
    identity: SourceIdentity
    server_ms: float
    value: str
    edge: ClockSample
    checked_ns: int
    epoch: int
    revision: int


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
    def __init__(self, lock=None):
        self._lock = lock or threading.RLock()
        self.samples = {name: deque(maxlen=CONSENSUS_SAMPLES) for name in APPROVED_SHORTCUTS}
        self.committed = {}
        self.generation = {name: 0 for name in APPROVED_SHORTCUTS}
        self._revision = {name: 0 for name in APPROVED_SHORTCUTS}
        self.resample_all = False

    def _invalidate_locked(self, label, generation):
        if label not in self.samples or generation < self.generation[label]:
            return False
        self.samples[label].clear()
        self.committed.pop(label, None)
        self.generation[label] = generation
        self._revision[label] += 1
        return True

    def invalidate(self, label, generation):
        with self._lock:
            return self._invalidate_locked(label, generation)

    def generation_for(self, label):
        with self._lock:
            return self.generation[label]

    def fence(self, label, generation):
        """Reject an in-flight data calculation without clearing display state."""
        with self._lock:
            if (label not in self.samples
                    or self.generation[label] != generation):
                return False
            self._revision[label] += 1
            return True

    def add(self, sample: FaithfulSample):
        try:
            label = sample.label
            value = sample.value
            precision = sample.precision
            generation = sample.generation
        except (AttributeError, TypeError, ValueError):
            return False

        # Snapshot first and perform comparisons outside the shared event lock.
        # A queue overflow can therefore invalidate immediately instead of
        # waiting behind arbitrary sample comparison code.  The revision fence
        # below prevents that stale computation from committing afterwards.
        with self._lock:
            if (label not in self.samples or not value
                    or self.generation[label] != generation):
                return False
            revision = self._revision[label]
            prior_bucket = tuple(self.samples[label])
        bucket = deque(prior_bucket, maxlen=CONSENSUS_SAMPLES)
        bucket.append(sample)
        if len(bucket) < CONSENSUS_SAMPLES:
            with self._lock:
                if (self._revision[label] != revision
                        or self.generation[label] != generation):
                    return False
                self.samples[label] = bucket
                self._revision[label] += 1
            return False
        keys = {(item.generation, item.value, item.precision) for item in bucket}
        if len(keys) != 1:
            with self._lock:
                if (self._revision[label] != revision
                        or self.generation[label] != generation):
                    return False
                self.samples[label] = bucket
                self._revision[label] += 1
            return False

        current = (value, precision)
        with self._lock:
            if (self._revision[label] != revision
                    or self.generation[label] != generation):
                return False
            previous = self.committed.get(label)
            changed = previous != current
            if previous is not None and changed and previous[0][:5] != current[0][:5]:
                self.resample_all = True
            self.samples[label] = bucket
            self.committed[label] = current
            self._revision[label] += 1
            return changed

    def confirmed_revision(self, sample: FaithfulSample):
        try:
            label = sample.label
            generation = sample.generation
            current = (sample.value, sample.precision)
        except (AttributeError, TypeError, ValueError):
            return None
        with self._lock:
            if (label not in self.samples
                    or self.generation[label] != generation):
                return None
            revision = self._revision[label]
            bucket = tuple(self.samples[label])
            committed = self.committed.get(label)
        confirmed = (len(bucket) == CONSENSUS_SAMPLES
                     and all(item == sample for item in bucket)
                     and committed == current)
        if not confirmed:
            return None
        with self._lock:
            if (self._revision[label] != revision
                    or self.generation[label] != generation
                    or self.committed.get(label) != current):
                return None
            return revision

    def confirmed(self, sample: FaithfulSample) -> bool:
        return self.confirmed_revision(sample) is not None

    def matching_tail_count(self, label, generation, value, precision):
        with self._lock:
            if label not in self.samples or self.generation[label] != generation:
                return 0
            count = 0
            for item in reversed(tuple(self.samples[label])):
                if (getattr(item, "generation", None) != generation
                        or getattr(item, "value", None) != value
                        or getattr(item, "precision", None) != precision):
                    break
                count += 1
            return count

    def revision_is_current(self, label, generation, revision):
        with self._lock:
            return (label in self.samples
                    and self.generation[label] == generation
                    and self._revision[label] == revision)

    def resample_requested(self):
        with self._lock:
            return self.resample_all

    def clear_resample_request(self):
        with self._lock:
            self.resample_all = False

    def pause_for_boundary(self):
        with self._lock:
            for label in APPROVED_SHORTCUTS:
                self._invalidate_locked(label, self.generation[label])
            self.resample_all = False

    def display(self, readable_labels=APPROVED_SHORTCUTS):
        with self._lock:
            committed = dict(self.committed)
        groups = defaultdict(list)
        for label in APPROVED_SHORTCUTS:
            if label in committed:
                groups[committed[label][0]].append(label)
        ordered = tuple((value, tuple(labels)) for value, labels in groups.items())
        unreadable = tuple(label for label in readable_labels if label not in committed)
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

    def reset_publish(self, read_labels=()):
        """Reset generation-scoped publication/read state, not scan cadence."""
        with self._lock:
            self.last_publish = None
            self.last_payload = None
            for label in read_labels:
                self.last_read.pop(label, None)


class MultiGameClockSource:
    """Control-plane generations fence every discovery and stream event."""
    def __init__(self, reader_factory, discover, *, monotonic_ns=time.perf_counter_ns,
                 thread_factory=threading.Thread):
        self.reader_factory = reader_factory
        self.discover = discover
        self.scheduler = FaithfulScheduler(monotonic_ns)
        self.thread_factory = thread_factory
        self._event_lock = threading.RLock()
        self.consensus = FaithfulConsensus(self._event_lock)
        self.events = queue.Queue(maxsize=EVENT_QUEUE_SIZE)
        self.event_epoch = 0
        self.event_revision = 0
        self.overflow_fault = threading.Event()
        self._overflow_epoch = None
        self.streams = {}
        self.discovery = None
        self.discovery_cancel = threading.Event()
        self._retired_workers = []
        self._retired_worker_revisions = {}
        self._retired_revision = 0
        self.anchors = {}
        self.pending_edges = {}
        self.queued_edges = {}
        self.queued_roster_change_epoch = None
        self.queued_roster_change_revision = None
        self.anchor_control_floor = {}
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
        with self._event_lock:
            return (self.event_epoch,
                    tuple((label, self.consensus.generation[label])
                          for label in APPROVED_SHORTCUTS))

    def is_busy(self):
        self._prune_retired_workers()
        with self._event_lock:
            workers = tuple(item[1] for item in self.streams.values())
            retired = tuple(self._retired_workers)
            discovery = self.discovery
        candidates = []
        seen = set()
        for worker in ((discovery,) if discovery is not None else ()) + workers + retired:
            marker = id(worker)
            if marker not in seen:
                seen.add(marker)
                candidates.append(worker)
        alive_by_id = {}
        for worker in candidates:
            try:
                alive_by_id[id(worker)] = bool(worker.is_alive())
            except Exception:
                # An uninspectable worker remains busy/fail-closed; never drop
                # a possibly running thread merely because inspection failed.
                alive_by_id[id(worker)] = True
        return any(alive_by_id.values())

    def _retire_worker_locked(self, worker):
        if (worker is not None
                and all(existing is not worker for existing in self._retired_workers)):
            self._retired_revision += 1
            self._retired_workers.append(worker)
            self._retired_worker_revisions[id(worker)] = self._retired_revision

    def _prune_retired_workers(self):
        """Prune finished workers without invoking worker code under the event lock."""
        with self._event_lock:
            snapshot_revision = self._retired_revision
            snapshot = tuple(
                (worker, self._retired_worker_revisions.get(id(worker)))
                for worker in self._retired_workers
            )
        alive_by_snapshot = {}
        for worker, worker_revision in snapshot:
            try:
                alive = bool(worker.is_alive())
            except Exception:
                # Inspection failure cannot prove that a worker is finished.
                alive = True
            alive_by_snapshot[(id(worker), worker_revision)] = (worker, alive)
        with self._event_lock:
            kept = []
            kept_ids = set()
            changed = False
            for worker in self._retired_workers:
                marker = id(worker)
                worker_revision = self._retired_worker_revisions.get(marker)
                probed = alive_by_snapshot.get((marker, worker_revision))
                same_snapshot_worker = probed is not None and probed[0] is worker
                if marker in kept_ids:
                    changed = True
                    continue
                if same_snapshot_worker and not probed[1]:
                    if self._retired_worker_revisions.get(marker) == worker_revision:
                        self._retired_worker_revisions.pop(marker, None)
                    changed = True
                    continue
                kept_ids.add(marker)
                kept.append(worker)
            if changed:
                self._retired_workers = kept
                # Record the merge independently from concurrent appends.  A
                # later prune only acts on the exact worker/revision snapshot
                # it inspected, so an append after snapshot cannot be removed.
                self._retired_revision = max(
                    self._retired_revision, snapshot_revision,
                ) + 1

    def _cancel_workers(self):
        self.discovery_cancel.set()
        if self.discovery is not None:
            self._retire_worker_locked(self.discovery)
            self.discovery = None
        for _identity, worker, cancel, _generation, _epoch in self.streams.values():
            cancel.set()
            self._retire_worker_locked(worker)
        self.streams.clear()

    def _invalidate_locked(self, reason="來源失效", *, preserve_overflow=False):
        self._cancel_workers()
        self.event_epoch += 1
        self.events = queue.Queue(maxsize=EVENT_QUEUE_SIZE)
        self.scheduler.reset_publish(APPROVED_SHORTCUTS)
        if not preserve_overflow:
            self.overflow_fault.clear()
            self._overflow_epoch = None
        for label in APPROVED_SHORTCUTS:
            self.consensus.invalidate(label, self.consensus.generation[label] + 1)
        self.consensus.clear_resample_request()
        self.anchors.clear()
        self.pending_edges.clear()
        self.queued_edges.clear()
        self.queued_roster_change_epoch = None
        self.queued_roster_change_revision = None
        self.anchor_control_floor.clear()
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

    @staticmethod
    def _validated_progress_item(item, expected, checked_ns):
        reading = None
        edge = None
        if isinstance(item, ClockReading):
            reading, edge = item, item.edge
        elif isinstance(item, ClockSample):
            # Compatibility for existing injected readers/tests: a
            # ClockSample is already a qualified edge, never a snapshot.
            reading, edge = ClockReading(item.identity, item.server_ms, item), item
        elif item is not None:
            return None, None, False
        valid_reading = (reading is None or (
            reading.identity == expected
            and isinstance(reading.server_ms, (int, float))
            and math.isfinite(reading.server_ms)
            and type(reading.reset_anchor) is bool
        ))
        valid_edge = (edge is None or (
            isinstance(checked_ns, int)
            and isinstance(edge, ClockSample)
            and reading is not None
            and not reading.reset_anchor
            and edge.identity == expected
            and edge.server_ms == reading.server_ms
            and isinstance(edge.anchor_ns, int)
            and 0 < edge.anchor_ns <= checked_ns
            and 0 <= checked_ns - edge.anchor_ns <= SAMPLE_MAX_AGE_NS
            and 0 < edge.observation_bracket_ns <= 25_000_000
            and 0 <= edge.read_latency_ns <= 20_000_000
            and 1000 <= edge.transition_ms <= 30_000
            and math.isfinite(edge.response_interval_ms)
            and 0 <= edge.response_interval_ms <= 100
            and bool(edge.profile)
        ))
        return reading, edge, bool(valid_reading and valid_edge)

    def _prepare_queued_edge_locked(self, kind, payload, checked_ns, epoch,
                                    revision):
        """Return (label-to-clear, validated marker) without other locks."""
        if kind == "discovery_error":
            self.queued_edges.clear()
            return None, None
        if kind == "stream_error":
            try:
                label, _generation = payload
            except (TypeError, ValueError):
                return None, None
            self.queued_edges.pop(label, None)
            return None, None
        if kind != "progress" or not isinstance(payload, tuple) or len(payload) != 5:
            return None, None
        label, generation, expected, item, has_reason = payload
        current = self.streams.get(label)
        reading, edge, valid = self._validated_progress_item(
            item, expected, checked_ns,
        )
        if (current is None or current[0] != expected
                or current[3] != generation or current[4] != epoch
                or has_reason or not valid):
            return label, None
        existing = self.queued_edges.get(label)
        if (reading is not None and (
                reading.reset_anchor
                or (existing is not None
                    and reading.server_ms != existing.server_ms))):
            return label, None
        if edge is None:
            return None, None
        return label, QueuedEdgeMarker(
            label, generation, expected, edge.server_ms,
            self._format_server_ms(edge.server_ms), edge, checked_ns, epoch,
            revision,
        )

    def _consume_queued_edge_locked(self, label, generation, expected,
                                    edge, checked_ns, epoch, revision):
        marker = self.queued_edges.get(label)
        if (marker is not None and marker.generation == generation
                and marker.identity == expected and marker.edge == edge
                and marker.checked_ns == checked_ns and marker.epoch == epoch
                and marker.revision == revision):
            self.queued_edges.pop(label, None)

    def _emit(self, kind, payload, checked_ns, epoch):
        with self._event_lock:
            if epoch != self.event_epoch:
                return False
            reset_label = None
            roster_change = False
            if kind == "discovery_error":
                self._invalidate_locked("來源失效")
                return False
            if kind == "stream_error":
                try:
                    label, generation = payload
                except (TypeError, ValueError):
                    self._invalidate_locked("來源失效")
                    return False
                current = self.streams.get(label)
                if current is not None and current[3] == generation and current[4] == epoch:
                    self._invalidate_locked("來源失效")
                return False
            if kind == "discovered":
                selected = self._valid_discovery(payload)
                current_identities = {
                    label: stream[0] for label, stream in self.streams.items()
                }
                if (selected is None
                        or any(selected.get(label) != identity
                               for label, identity in current_identities.items())):
                    self._invalidate_locked("來源失效")
                    return False
                if any(label not in current_identities for label in selected):
                    roster_change = True
            elif kind == "progress":
                if not isinstance(payload, tuple) or len(payload) != 5:
                    self._invalidate_locked("來源失效")
                    return False
                label, generation, expected, item, has_reason = payload
                current = self.streams.get(label)
                if (current is None or current[0] != expected
                        or current[3] != generation or current[4] != epoch):
                    return False
                reading, _edge, valid = self._validated_progress_item(
                    item, expected, checked_ns,
                )
                if has_reason or not valid or not isinstance(checked_ns, int):
                    self._invalidate_locked("來源失效")
                    return False
                if reading is not None and reading.reset_anchor:
                    reset_label = label
            revision = self.event_revision + 1
            clear_label, marker = self._prepare_queued_edge_locked(
                kind, payload, checked_ns, epoch, revision,
            )
            if clear_label is not None:
                self.queued_edges.pop(clear_label, None)
            try:
                self.events.put_nowait((
                    epoch, revision, kind, payload, checked_ns,
                ))
                self.event_revision = revision
                if marker is not None:
                    self.queued_edges[marker.label] = marker
                if roster_change:
                    self.queued_roster_change_epoch = epoch
                    self.queued_roster_change_revision = revision
                if reset_label is not None:
                    self.anchor_control_floor[reset_label] = revision
                    current = self.streams.get(reset_label)
                    if current is not None:
                        self.consensus.fence(reset_label, current[3])
                    self.anchors.pop(reset_label, None)
                    self.pending_edges.pop(reset_label, None)
                    self.queued_edges.pop(reset_label, None)
                return True
            except queue.Full:
                self._invalidate_locked("來源失效", preserve_overflow=True)
                self._overflow_epoch = self.event_epoch
                self.overflow_fault.set()
                return False

    def _epoch_valid_locked(self, epoch):
        return (epoch == self.event_epoch
                and not (self.overflow_fault.is_set()
                         and self._overflow_epoch == self.event_epoch))

    def _start_discovery(self, expected_epoch=None):
        with self._event_lock:
            if (expected_epoch is not None
                    and not self._epoch_valid_locked(expected_epoch)):
                return False
            prior = self.discovery
        if prior is not None and prior.is_alive():
            return True
        cancel = threading.Event()
        with self._event_lock:
            epoch = self.event_epoch if expected_epoch is None else expected_epoch
            if not self._epoch_valid_locked(epoch):
                return False

        def run():
            try:
                if cancel.is_set():
                    return
                result = tuple(self.discover(cancel))
                if not cancel.is_set():
                    self._emit("discovered", result, self.scheduler.now(), epoch)
            except Exception:
                self._emit("discovery_error", (), self.scheduler.now(), epoch)

        try:
            worker = self.thread_factory(
                target=run, name="v02-clock-discovery", daemon=True,
            )
        except Exception:
            with self._event_lock:
                if self._epoch_valid_locked(epoch):
                    self._invalidate_locked("來源失效")
            return False
        with self._event_lock:
            if not self._epoch_valid_locked(epoch):
                cancel.set()
                return False
            self.discovery_cancel = cancel
            self.discovery = worker
        try:
            worker.start()
        except Exception:
            with self._event_lock:
                if self.discovery is worker and self.event_epoch == epoch:
                    self._invalidate_locked("來源失效")
            return False
        return True

    def _start_stream(self, label, identity, expected_epoch=None):
        with self._event_lock:
            if (expected_epoch is not None
                    and not self._epoch_valid_locked(expected_epoch)):
                return False
            prior = self.streams.get(label)
        if prior is not None:
            if prior[0] == identity and prior[1].is_alive():
                return True
            with self._event_lock:
                if (self.streams.get(label) is prior
                        and (expected_epoch is None
                             or self._epoch_valid_locked(expected_epoch))):
                    self._invalidate_locked("來源失效")
            return False
        cancel = threading.Event()
        try:
            reader = self.reader_factory()
        except Exception:
            with self._event_lock:
                epoch = self.event_epoch if expected_epoch is None else expected_epoch
                if self._epoch_valid_locked(epoch):
                    self._invalidate_locked("來源失效")
            return False
        try:
            reader.expected_identity = identity
        except Exception:
            pass
        generation = None
        epoch = None

        def run():
            try:
                if cancel.is_set():
                    return
                native = getattr(reader, "native", None)
                reader_type = type(reader)
                stream_attests_identity = (
                    reader_type is GameClockReader
                    or reader_type.__dict__.get(
                        "stream_identity_verified", False,
                    ) is True
                )
                if native is None:
                    raise RuntimeError("identity")
                if (not stream_attests_identity
                        and native.identity(identity.hwnd) != identity):
                    raise RuntimeError("identity")

                def publish(item, reason, checked_ns):
                    if cancel.is_set():
                        return
                    # Production GameClockReader has already checked the cheap
                    # live token for every callback and performs its named full
                    # fences internally.  Legacy/injected readers must either
                    # satisfy that explicit class contract or take this
                    # fail-closed full-identity fallback on every callback.
                    if (not stream_attests_identity
                            and native.identity(identity.hwnd) != identity):
                        raise RuntimeError("identity")
                    if item is not None and item.identity != identity:
                        raise RuntimeError("identity")
                    self._emit(
                        "progress", (label, generation, identity, item, bool(reason)),
                        checked_ns, epoch,
                    )

                reader.stream(identity.hwnd, cancel, publish)
                if (not cancel.is_set() and not stream_attests_identity
                        and native.identity(identity.hwnd) != identity):
                    raise RuntimeError("identity")
                if not cancel.is_set():
                    self._emit("stream_error", (label, generation), self.scheduler.now(), epoch)
            except Exception:
                self._emit("stream_error", (label, generation), self.scheduler.now(), epoch)

        try:
            worker = self.thread_factory(
                target=run, name=f"v02-clock-{label}", daemon=True,
            )
        except Exception:
            with self._event_lock:
                epoch = self.event_epoch if expected_epoch is None else expected_epoch
                if self._epoch_valid_locked(epoch):
                    self._invalidate_locked("來源失效")
            return False
        started = self.scheduler.now()
        with self._event_lock:
            epoch = self.event_epoch if expected_epoch is None else expected_epoch
            if not self._epoch_valid_locked(epoch) or label in self.streams:
                cancel.set()
                return False
            generation = self.consensus.generation[label] + 1
            self.consensus.invalidate(label, generation)
            self.anchors.pop(label, None)
            self.pending_edges.pop(label, None)
            self.queued_edges.pop(label, None)
            self.anchor_control_floor.pop(label, None)
            self.last_sample_ns.pop(label, None)
            self.committed_ns.pop(label, None)
            self.streams[label] = (identity, worker, cancel, generation, epoch)
            self.stream_started_ns[label] = started
            self.last_checked_ns[label] = started
        try:
            worker.start()
        except Exception:
            with self._event_lock:
                current = self.streams.get(label)
                if current is not None and current[1] is worker and current[4] == epoch:
                    self._invalidate_locked("來源失效")
            return False
        return True

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

    @staticmethod
    def _fresh_checked(previous, checked_ns, now):
        if not isinstance(checked_ns, int) or not 0 <= now - checked_ns <= FRESHNESS_TTL_NS:
            return False
        return previous is None or checked_ns >= previous

    def _health_snapshot_locked(self, stream_items=None):
        items = tuple(self.streams.items()) if stream_items is None else stream_items
        return tuple((
            label,
            self.last_checked_ns.get(label),
            label in self.consensus.committed,
            self.last_sample_ns.get(label),
            self.committed_ns.get(label),
            self.stream_started_ns.get(label),
        ) for label, _stream in items)

    @staticmethod
    def _health_fault_from_snapshot(now, snapshot):
        for (label, checked, committed, sample_checked, committed_checked,
             stream_started) in snapshot:
            if checked is None or not 0 <= now - checked <= FRESHNESS_TTL_NS:
                return True
            if committed:
                if (sample_checked is None or committed_checked is None
                        or not 0 <= now - sample_checked <= FRESHNESS_TTL_NS
                        or not 0 <= now - committed_checked <= FRESHNESS_TTL_NS):
                    return True
            elif now - (now if stream_started is None else stream_started) > FIRST_SAMPLE_TIMEOUT_NS:
                return True
        return False

    def poll(self):
        self._prune_retired_workers()
        with self._event_lock:
            if self.closed:
                return
            poll_epoch = self.event_epoch
            pending = []
            for _ in range(EVENT_QUEUE_SIZE):
                try:
                    pending.append(self.events.get_nowait())
                except queue.Empty:
                    break
            # This cutoff is sampled after the queue snapshot under the same
            # lock used by _emit. Events linearized later remain for next poll;
            # every event in this batch must be no newer than this cutoff.
            now = self.scheduler.now()
            previous = self.last_poll_ns
            self.last_poll_ns = now
            if previous is not None and not 0 <= now - previous <= FRESHNESS_TTL_NS:
                self._invalidate_locked("來源失效")
                return
            if self.overflow_fault.is_set() and self._overflow_epoch == self.event_epoch:
                self.overflow_fault.clear()
                self._overflow_epoch = None
                return
            stream_items = tuple(self.streams.items())
            discovery = self.discovery
            health_snapshot = self._health_snapshot_locked(stream_items)
        if self._health_fault_from_snapshot(now, health_snapshot):
            with self._event_lock:
                if self._epoch_valid_locked(poll_epoch):
                    self._invalidate_locked("來源失效")
            return
        discovery_alive = discovery is not None and discovery.is_alive()
        if not discovery_alive:
            if self.scheduler.allow_discovery():
                if not self._start_discovery(poll_epoch):
                    with self._event_lock:
                        if not self._epoch_valid_locked(poll_epoch):
                            return
        for event in pending:
            if not isinstance(event, tuple) or len(event) not in (4, 5):
                with self._event_lock:
                    if self._epoch_valid_locked(poll_epoch):
                        self._invalidate_locked("來源失效")
                return
            if len(event) == 5:
                epoch, event_revision, kind, payload, checked_ns = event
            else:
                # Compatibility for deterministic fixtures which inject events
                # directly.  Production events always carry a revision.
                epoch, kind, payload, checked_ns = event
                event_revision = 0
            with self._event_lock:
                if not self._epoch_valid_locked(poll_epoch):
                    return
            if epoch != poll_epoch:
                continue
            if kind == "discovery_error":
                with self._event_lock:
                    if self._epoch_valid_locked(poll_epoch):
                        self._invalidate_locked("來源失效")
                return
            if kind == "discovered":
                if not isinstance(checked_ns, int) or not 0 <= now - checked_ns <= FRESHNESS_TTL_NS:
                    with self._event_lock:
                        if self._epoch_valid_locked(poll_epoch):
                            self._invalidate_locked("來源失效")
                    return
                selected = self._valid_discovery(payload)
                if selected is None:
                    with self._event_lock:
                        if self._epoch_valid_locked(poll_epoch):
                            self._invalidate_locked("來源失效")
                    return
                with self._event_lock:
                    if not self._epoch_valid_locked(poll_epoch):
                        return
                    current_identities = {
                        label: stream[0] for label, stream in self.streams.items()
                    }
                    if any(selected.get(label) != identity
                           for label, identity in current_identities.items()):
                        self._invalidate_locked("來源失效")
                        return
                    additions = tuple(
                        (label, selected[label]) for label in APPROVED_SHORTCUTS
                        if label in selected and label not in current_identities
                    )
                    if not selected:
                        self.source_faulted = True
                        self.status = "來源失效"
                        continue
                    self.source_faulted = False
                for label, identity in additions:
                    if not self._start_stream(
                            label, identity, expected_epoch=poll_epoch):
                        with self._event_lock:
                            if not self._epoch_valid_locked(poll_epoch):
                                return
                with self._event_lock:
                    if not self._epoch_valid_locked(poll_epoch):
                        return
                    installed = {
                        label: stream[0] for label, stream in self.streams.items()
                    }
                    if any(installed.get(label) != identity
                           for label, identity in selected.items()):
                        self._invalidate_locked("來源失效")
                        return
                    if (self.queued_roster_change_epoch == poll_epoch
                            and self.queued_roster_change_revision is not None
                            and event_revision >= self.queued_roster_change_revision):
                        self.queued_roster_change_epoch = None
                        self.queued_roster_change_revision = None
                continue
            if kind == "stream_error":
                label, generation = payload
                with self._event_lock:
                    current = (self.streams.get(label)
                               if self._epoch_valid_locked(poll_epoch) else None)
                    if current is not None and current[3] == generation:
                        self._invalidate_locked("來源失效")
                        return
                continue
            if kind != "progress":
                continue
            label, generation, expected, item, has_reason = payload
            reading, edge, valid_item = self._validated_progress_item(
                item, expected, checked_ns,
            )
            with self._event_lock:
                current = (self.streams.get(label)
                           if self._epoch_valid_locked(poll_epoch) else None)
                previous_checked = self.last_checked_ns.get(label)
                control_floor = self.anchor_control_floor.get(label, 0)
            if event_revision < control_floor:
                continue
            if current is None or current[3] != generation or current[4] != epoch:
                with self._event_lock:
                    self._consume_queued_edge_locked(
                        label, generation, expected, edge, checked_ns, epoch,
                        event_revision,
                    )
                continue
            if (expected != current[0] or has_reason
                    or not valid_item
                    or not self._fresh_checked(previous_checked, checked_ns, now)):
                with self._event_lock:
                    if self._epoch_valid_locked(poll_epoch):
                        self._invalidate_locked("來源失效")
                return
            allowed = reading is not None and self.scheduler.allow_read(label, checked_ns)
            value = (self._format_server_ms(reading.server_ms)
                     if reading is not None else None)
            faithful = (FaithfulSample(label, generation, value, "millisecond")
                        if value is not None else None)
            # Consensus may compare injected/sample values.  Run its
            # revision-fenced calculation without the event lock so an
            # overflow/reset can linearize immediately.  The final state
            # transaction below rechecks epoch, stream, and control floor.
            confirmed_revision = None
            if reading is not None and (allowed or edge is not None):
                if allowed:
                    self.consensus.add(faithful)
                confirmed_revision = self.consensus.confirmed_revision(faithful)
            with self._event_lock:
                if not self._epoch_valid_locked(poll_epoch):
                    return
                latest = self.streams.get(label)
                if (latest is None or latest[0] != expected
                        or latest[3] != generation or latest[4] != epoch):
                    self._consume_queued_edge_locked(
                        label, generation, expected, edge, checked_ns, epoch,
                        event_revision,
                    )
                    continue
                if event_revision < self.anchor_control_floor.get(label, 0):
                    continue
                self.last_checked_ns[label] = checked_ns
                if allowed:
                    self.last_sample_ns[label] = checked_ns
                if reading is not None and reading.reset_anchor:
                    self.anchors.pop(label, None)
                    self.pending_edges.pop(label, None)
                    self.queued_edges.pop(label, None)
                pending_edge = self.pending_edges.get(label)
                if (pending_edge is not None and (
                        pending_edge[1] != generation
                        or (reading is not None
                            and pending_edge[0].server_ms != reading.server_ms))):
                    self.pending_edges.pop(label, None)
                if edge is not None:
                    self.pending_edges[label] = (edge, generation)
                if reading is not None and (allowed or edge is not None):
                    # A qualified edge is also its value's first snapshot.  It
                    # must cross queue-marker -> pending -> consensus under one
                    # event-lock transaction, so timed readers cannot observe a
                    # second empty handoff window.
                    if (confirmed_revision is not None
                            and self.consensus.revision_is_current(
                                label, generation, confirmed_revision,
                            )):
                        if allowed:
                            self.committed_ns[label] = checked_ns
                        pending = self.pending_edges.get(label)
                        if pending is not None and pending[1] == generation:
                            qualified = pending[0]
                            if (self._format_server_ms(qualified.server_ms) == value
                                    and 0 <= now - qualified.anchor_ns <= SAMPLE_MAX_AGE_NS):
                                self.anchors[label] = (
                                    int(qualified.server_ms), qualified.anchor_ns,
                                    checked_ns, generation,
                                )
                                self.pending_edges.pop(label, None)
                self._consume_queued_edge_locked(
                    label, generation, expected, edge, checked_ns, epoch,
                    event_revision,
                )

        with self._event_lock:
            if not self._epoch_valid_locked(poll_epoch):
                return
            boundary = self.consensus.resample_requested()
        if boundary:
            boundary_started_ns = self.scheduler.now()
            with self._event_lock:
                if not self._epoch_valid_locked(poll_epoch):
                    return
                if self.consensus.resample_requested():
                    stream_labels = tuple(self.streams)
                    self.consensus.pause_for_boundary()
                    # Minute-boundary resampling is a display-consensus event.
                    # Same-generation, identity-fenced qualified anchors and
                    # their bounded handoffs remain valid for timed actions.
                    self.committed_ns.clear()
                    self.revalidating = True
                    started = dict(self.stream_started_ns)
                    for label in stream_labels:
                        started[label] = boundary_started_ns
                    self.stream_started_ns = started

        final_now = self.scheduler.now()
        with self._event_lock:
            if not self._epoch_valid_locked(poll_epoch):
                return
            active = set(self.streams)
            if (self.revalidating and active
                    and active.issubset(self.consensus.committed)):
                self.revalidating = False
            final_health = self._health_snapshot_locked()
        health_fault = self._health_fault_from_snapshot(final_now, final_health)
        with self._event_lock:
            if not self._epoch_valid_locked(poll_epoch):
                return
            if health_fault:
                self._invalidate_locked("來源失效")
                return
            if self.source_faulted:
                return
            display = self.consensus.display(APPROVED_SHORTCUTS)
            payload = (display.groups, display.unreadable)
            if self.scheduler.allow_publish(payload):
                self.display = display
                self.status = display.text()

    def _exact_time_of_day_ms_locked(self):
        if self.revalidating or self.source_faulted:
            return None
        active = set(self.streams)
        if not active or not active.issubset(self.consensus.committed):
            return None
        values = {self.consensus.committed[label][0] for label in active}
        if len(values) != 1:
            return None
        value = next(iter(values))
        hour, minute, rest = value.split(":")
        second, millis = rest.split(".")
        return (((int(hour) * 60 + int(minute)) * 60 + int(second)) * 1000 + int(millis))

    def exact_time_of_day_ms(self):
        with self._event_lock:
            return self._exact_time_of_day_ms_locked()

    @staticmethod
    def _circular_median(values):
        ordered = sorted(int(value) % DAY_MS for value in values)
        if not ordered:
            return None
        if len(ordered) == 1:
            return ordered[0]
        gaps = [b - a for a, b in zip(ordered, ordered[1:])]
        gaps.append(ordered[0] + DAY_MS - ordered[-1])
        cut = (max(range(len(gaps)), key=gaps.__getitem__) + 1) % len(ordered)
        unfolded = ordered[cut:] + [value + DAY_MS for value in ordered[:cut]]
        if unfolded[-1] - unfolded[0] > MULTI_ANCHOR_SPREAD_TOLERANCE_MS:
            return None
        middle = len(unfolded) // 2
        median = (unfolded[middle] if len(unfolded) % 2
                  else (unfolded[middle - 1] + unfolded[middle]) // 2)
        return median % DAY_MS

    def _timed_anchor_time_locked(self, now):
        active = set(self.streams)
        if not active:
            return None
        estimates = []
        for label in active:
            anchor = self.anchors.get(label)
            if anchor is None:
                return None
            server_ms, anchor_ns, _checked_ns, generation = anchor
            if (generation != self.consensus.generation[label]
                    or not isinstance(anchor_ns, int)
                    or not 0 <= now - anchor_ns):
                return None
            if (now - anchor_ns > BASE_QUALIFIED_ANCHOR_LEASE_NS
                    and not (
                        self._pending_anchor_handoff_locked(
                            label, now, generation,
                        )
                        or self._queued_anchor_handoff_locked(
                            label, now, generation,
                        )
                    )):
                return None
            elapsed = (now - anchor_ns) // 1_000_000
            estimates.append(int(server_ms + UTC8_MS + elapsed) % DAY_MS)
        return self._circular_median(estimates)

    def _pending_anchor_handoff_locked(self, label, now, generation):
        if self.source_faulted:
            return False
        pending = self.pending_edges.get(label)
        stream = self.streams.get(label)
        anchor = self.anchors.get(label)
        if pending is None or stream is None or pending[1] != generation:
            return False
        edge = pending[0]
        if (not isinstance(edge, ClockSample)
                or anchor is None
                or not isinstance(anchor[1], int)
                or not 0 <= now - anchor[1] <= (
                    BASE_QUALIFIED_ANCHOR_LEASE_NS
                    + PENDING_CONSENSUS_HANDOFF_MAX_NS
                )
                or edge.identity != stream[0]
                or generation != stream[3]
                or not isinstance(edge.anchor_ns, int)
                or not 0 <= now - edge.anchor_ns <= PENDING_CONSENSUS_HANDOFF_MAX_NS
                or not self._anchor_edge_continuity_locked(
                    label, edge, generation,
                )):
            return False
        value = self._format_server_ms(edge.server_ms)
        progress = self.consensus.matching_tail_count(
            label, generation, value, "millisecond",
        )
        return 0 < progress < CONSENSUS_SAMPLES

    def _anchor_edge_continuity_locked(self, label, edge, generation):
        stream = self.streams.get(label)
        anchor = self.anchors.get(label)
        if (stream is None or anchor is None or not isinstance(edge, ClockSample)
                or edge.identity != stream[0] or generation != stream[3]
                or anchor[3] != generation or not isinstance(anchor[1], int)
                or not isinstance(edge.anchor_ns, int)
                or edge.anchor_ns <= anchor[1]):
            return False
        elapsed_ms = (edge.anchor_ns - anchor[1]) // 1_000_000
        expected_server_ms = (int(anchor[0]) + elapsed_ms) % DAY_MS
        observed_server_ms = int(edge.server_ms) % DAY_MS
        delta = abs(observed_server_ms - expected_server_ms)
        return min(delta, DAY_MS - delta) <= MULTI_ANCHOR_SPREAD_TOLERANCE_MS

    def _queued_anchor_handoff_locked(self, label, now, generation):
        if self.source_faulted:
            return False
        marker = self.queued_edges.get(label)
        stream = self.streams.get(label)
        anchor = self.anchors.get(label)
        if marker is None or stream is None or anchor is None:
            return False
        _old_server_ms, old_anchor_ns, _old_checked_ns, old_generation = anchor
        edge = marker.edge
        if (marker.label != label or marker.generation != generation
                or marker.epoch != self.event_epoch
                or marker.identity != stream[0]
                or marker.revision < self.anchor_control_floor.get(label, 0)
                or generation != stream[3] or old_generation != generation
                or marker.server_ms != edge.server_ms
                or marker.value != self._format_server_ms(marker.server_ms)
                or not isinstance(marker.checked_ns, int)
                or not isinstance(old_anchor_ns, int)
                or not isinstance(edge.anchor_ns, int)
                or not 0 <= now - marker.checked_ns <= QUEUED_EDGE_POLL_HANDOFF_MAX_NS
                or not 0 <= now - old_anchor_ns <= (
                    BASE_QUALIFIED_ANCHOR_LEASE_NS
                    + QUEUED_EDGE_POLL_HANDOFF_MAX_NS
                )
                or not self._anchor_edge_continuity_locked(
                    label, edge, generation,
                )):
            return False
        return True

    def _timed_source_state_locked(self, now):
        if (self.overflow_fault.is_set() and self._overflow_epoch == self.event_epoch):
            return "fault"
        health_snapshot = self._health_snapshot_locked()
        if (self.source_faulted or self.last_poll_ns is None
                or not 0 <= now - self.last_poll_ns <= FRESHNESS_TTL_NS
                or self._health_fault_from_snapshot(now, health_snapshot)):
            return "fault"
        active = set(self.streams)
        if (self.queued_roster_change_epoch == self.event_epoch
                or not active
                or self._timed_anchor_time_locked(now) is None):
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
            return self._timed_anchor_time_locked(now)

    def shutdown(self):
        if self.closed:
            return
        self.closed = True
        self.invalidate("來源失效")
