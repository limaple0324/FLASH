from __future__ import annotations

import ast
from dataclasses import replace
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import flash_sync_v02 as appmod
import v02_game_clock_reader as reader_mod
from runtime_paths import sanitized_record
from v02_game_clock import ClockSample, SAMPLE_MAX_AGE_NS, SourceIdentity
from v02_faithful_game_time import (
    APPROVED_SHORTCUTS,
    ApprovedShortcutCatalog,
    BASE_QUALIFIED_ANCHOR_LEASE_NS,
    EVENT_QUEUE_SIZE,
    FaithfulConsensus,
    FaithfulSample,
    MultiGameClockSource,
    QUEUED_EDGE_POLL_HANDOFF_MAX_NS,
    windows_desktop_known_folder,
)


HERE = Path(__file__).resolve().parent
FLASH_SYNC = HERE / "flash_sync_v02.py"


def identity(hwnd: int = 101, pid: int = 201, label: str = "120古") -> SourceIdentity:
    return SourceIdentity(
        hwnd, pid, 301, 401, "launch:" + label, "A" * 64,
        os.path.normcase(os.path.abspath("GameLoader.exe")),
    )


def sample(source: SourceIdentity, server_ms: int, anchor_ns: int) -> ClockSample:
    return ClockSample(source, server_ms, anchor_ns, 1, 1, 1000, "test")


class InlineThread:
    def __init__(self, *, target, **_kwargs):
        self.target = target
        self.alive = False

    def start(self):
        self.alive = True
        try:
            self.target()
        finally:
            self.alive = False

    def is_alive(self):
        return self.alive


class FakeReader:
    def __init__(self, source, rows):
        self.source = source
        self.rows = rows
        self.native = SimpleNamespace(identity=lambda hwnd: self.source)

    def stream(self, hwnd, cancel, publish):
        if hwnd != self.source.hwnd:
            raise AssertionError("unexpected hwnd")
        for row in self.rows:
            if cancel.is_set():
                return
            publish(*row)


class ShortcutIdentityTests(unittest.TestCase):
    def _catalog(self, rows):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        desktop = Path(root.name)
        for label in APPROVED_SHORTCUTS:
            (desktop / f"{label}.lnk").write_bytes(b"lnk")
        return ApprovedShortcutCatalog(
            lambda arguments: "launch:" + arguments,
            resolver=lambda path: rows[Path(path).stem],
            desktop=str(desktop),
        )

    def test_binding_contains_normalized_target_label_and_session_launch_identity(self):
        rows = {label: ("GameLoader.exe", label) for label in APPROVED_SHORTCUTS}
        bindings = self._catalog(rows).bindings()
        self.assertEqual(tuple(item.label for item in bindings), APPROVED_SHORTCUTS)
        for item in bindings:
            self.assertTrue(item.normalized_target)
            self.assertEqual(item.launch_identity, "launch:" + item.label)
            self.assertFalse(hasattr(item, "arguments"))

    def test_selector_checks_target_and_rejects_duplicate_or_many_to_one(self):
        rows = {label: ("GameLoader.exe", label) for label in APPROVED_SHORTCUTS}
        catalog = self._catalog(rows)
        valid = [identity(index + 1, index + 11, label)
                 for index, label in enumerate(APPROVED_SHORTCUTS)]
        self.assertEqual(tuple(label for label, _item in catalog.select(valid)), APPROVED_SHORTCUTS)
        wrong_target = list(valid)
        wrong_target[0] = replace(wrong_target[0], normalized_target=os.path.abspath("other.exe"))
        self.assertEqual(catalog.select(wrong_target), ())

    def test_selector_ambiguity_fails_closed_without_dict_last_wins(self):
        rows = {label: ("GameLoader.exe", label) for label in APPROVED_SHORTCUTS}
        catalog = self._catalog(rows)
        one = identity(1, 11, "120古")
        duplicate_process = SourceIdentity(
            2, one.pid, one.tid, one.created, one.launch_fingerprint,
            one.image_sha256, one.normalized_target,
        )
        self.assertEqual(catalog.select((one, duplicate_process)), ())

    def test_duplicate_launch_identity_across_different_targets_fails_closed(self):
        rows = {label: (f"{label}.exe", "same") for label in APPROVED_SHORTCUTS}
        self.assertEqual(self._catalog(rows).bindings(), ())

    def test_default_desktop_is_windows_known_folder_and_has_no_profile_fallback(self):
        source = (HERE / "v02_faithful_game_time.py").read_text(encoding="utf-8")
        self.assertIn("SHGetKnownFolderPath", source)
        self.assertNotIn('os.environ.get("USERPROFILE"', source)
        self.assertNotIn("Public\\Desktop", source)

    def test_windows_known_folder_resolves_current_user_desktop_on_host(self):
        self.assertEqual(os.name, "nt")
        desktop = windows_desktop_known_folder()
        self.assertTrue(desktop)
        self.assertTrue(Path(desktop).is_dir())


class ConsensusAndEventTests(unittest.TestCase):
    def test_retired_workers_are_identity_deduped_and_finished_are_pruned_outside_lock(self):
        model = MultiGameClockSource(
            lambda: None, lambda _cancel: (), monotonic_ns=lambda: 1,
            thread_factory=InlineThread,
        )

        class LockAwareWorker:
            def __init__(self, alive):
                self.alive = alive
                self.calls = 0

            def is_alive(self):
                self.calls += 1
                owned = getattr(model._event_lock, "_is_owned", lambda: False)()
                if owned:
                    raise AssertionError("is_alive called while event lock held")
                return self.alive

        worker = LockAwareWorker(True)
        cancel = threading.Event()
        source_id = identity()
        label = APPROVED_SHORTCUTS[0]
        model.discovery = worker
        model.streams[label] = (
            source_id, worker, cancel, 1, model.event_epoch,
        )

        model.invalidate("來源失效")
        self.assertTrue(cancel.is_set())
        self.assertEqual(model._retired_workers, [worker])
        self.assertTrue(model.is_busy())
        self.assertEqual(model._retired_workers, [worker])

        worker.alive = False
        self.assertFalse(model.is_busy())
        self.assertEqual(model._retired_workers, [])
        self.assertGreaterEqual(worker.calls, 2)

    def test_normal_poll_prunes_finished_retired_with_revision_safe_merge(self):
        now = [1_000_000_000]
        model = MultiGameClockSource(
            lambda: None, lambda _cancel: (), monotonic_ns=lambda: now[0],
            thread_factory=InlineThread,
        )

        class LockAwareWorker:
            def __init__(self, alive):
                self.alive = alive
                self.calls = 0

            def is_alive(self):
                self.calls += 1
                if getattr(model._event_lock, "_is_owned", lambda: False)():
                    raise AssertionError("is_alive called while event lock held")
                return self.alive

        live = LockAwareWorker(True)
        late_live = LockAwareWorker(True)

        class AppendingFinishedWorker(LockAwareWorker):
            def __init__(self):
                super().__init__(False)
                self.appended = False

            def is_alive(self):
                result = super().is_alive()
                if not self.appended:
                    self.appended = True
                    with model._event_lock:
                        model._retire_worker_locked(late_live)
                return result

        discovery = LockAwareWorker(True)
        model.discovery = discovery
        model.source_faulted = True
        model.last_poll_ns = now[0]
        with model._event_lock:
            model._retire_worker_locked(live)
            model._retire_worker_locked(AppendingFinishedWorker())

        # Normal acquisition polls, without any is_busy() call, must bound the
        # retired list while preserving both an already-live worker and one
        # appended after the prune snapshot was taken.
        model.poll()
        self.assertEqual(model._retired_workers, [live, late_live])
        revision_after_merge = model._retired_revision

        for index in range(12):
            with model._event_lock:
                model._retire_worker_locked(LockAwareWorker(False))
            now[0] += 1
            model.poll()
            self.assertEqual(model._retired_workers, [live, late_live], index)

        self.assertGreater(model._retired_revision, revision_after_merge)
        self.assertGreaterEqual(live.calls, 13)
        # late_live was appended after the first prune snapshot, so the merge
        # must preserve it uninspected until the following poll.
        self.assertEqual(late_live.calls, 12)

    def test_global_invalidate_clears_boundary_request_before_next_generation(self):
        now = [1_000_000_000]
        model = MultiGameClockSource(
            lambda: None, lambda _cancel: (), monotonic_ns=lambda: now[0],
            thread_factory=InlineThread,
        )
        model.consensus.resample_all = True
        model.invalidate("來源失效")
        self.assertFalse(model.consensus.resample_all)

        label = APPROVED_SHORTCUTS[0]
        source_id = identity()
        generation = model.consensus.generation[label]
        epoch = model.event_epoch
        model.streams[label] = (
            source_id, SimpleNamespace(is_alive=lambda: True),
            threading.Event(), generation, epoch,
        )
        model.stream_started_ns[label] = now[0]
        model.last_checked_ns[label] = now[0]
        model.last_poll_ns = now[0]
        model.source_faulted = False
        model.discovery = SimpleNamespace(is_alive=lambda: True)
        model.scheduler.last_discovery = now[0]
        pause = MagicMock(wraps=model.consensus.pause_for_boundary)
        model.consensus.pause_for_boundary = pause

        for index in range(3):
            checked = now[0] + index * 250_000_000
            self.assertTrue(model._emit(
                "progress",
                (label, generation, source_id,
                 reader_mod.ClockReading(source_id, 16_440_000), False),
                checked, epoch,
            ))
        now[0] += 500_000_000
        model.poll()

        pause.assert_not_called()
        self.assertIn(label, model.consensus.committed)
        self.assertFalse(model.revalidating)

    def test_stream_override_cannot_inherit_identity_attestation_capability(self):
        now = 1_000_000_000
        label = APPROVED_SHORTCUTS[0]
        source_id = identity()
        changed = replace(source_id, created=source_id.created + 1)

        class MutableNative:
            def __init__(self):
                self.current = source_id
                self.identity_calls = 0

            def identity(self, _hwnd):
                self.identity_calls += 1
                return self.current

        class UnsafeOverrideReader(reader_mod.GameClockReader):
            def __init__(self):
                super().__init__(
                    native=MutableNative(), monotonic_ns=lambda: now,
                )

            def stream(self, _hwnd, cancel, publish):
                # This override does not implement GameClockReader's identity
                # fences.  Merely inheriting the base class flag must not make
                # the wrapper trust it.
                self.native.current = changed
                publish(
                    reader_mod.ClockReading(source_id, 16_440_000), "", now,
                )
                cancel.set()

        reader = UnsafeOverrideReader()
        model = MultiGameClockSource(
            lambda: reader, lambda _cancel: (), monotonic_ns=lambda: now,
            thread_factory=InlineThread,
        )
        epoch = model.event_epoch
        self.assertTrue(model._start_stream(label, source_id))

        self.assertGreater(model.event_epoch, epoch)
        self.assertTrue(model.source_faulted)
        self.assertFalse(model.streams)
        self.assertTrue(model.events.empty())
        self.assertGreaterEqual(reader.native.identity_calls, 1)

    def test_stale_generation_is_rejected_without_reversing_control_generation(self):
        consensus = FaithfulConsensus()
        consensus.invalidate("120古", 7)
        accepted = consensus.add(FaithfulSample("120古", 6, "12:34", "minute"))
        self.assertFalse(accepted)
        self.assertEqual(consensus.generation["120古"], 7)

    def test_end_to_end_source_requires_three_confirmed_samples_before_anchor(self):
        now = [1_000_000_000]
        source_id = identity()
        rows = [(sample(source_id, 1_700_000_000_000, now[0]), "", now[0])]
        model = MultiGameClockSource(
            lambda: FakeReader(source_id, rows),
            lambda _cancel: (("120古", source_id),),
            monotonic_ns=lambda: now[0],
            thread_factory=InlineThread,
        )
        model.poll()
        self.assertNotIn("120古", model.anchors)

    def test_queue_overflow_sets_out_of_band_fault_and_rotates_epoch(self):
        model = MultiGameClockSource(
            lambda: None, lambda _cancel: (), monotonic_ns=lambda: 1,
            thread_factory=InlineThread,
        )
        epoch = model.event_epoch
        while True:
            try:
                model.events.put_nowait((epoch, "noise", (), 1))
            except queue.Full:
                break
        model._emit("sample", (), 1, epoch)
        self.assertTrue(model.overflow_fault.is_set())
        model.poll()
        self.assertGreater(model.event_epoch, epoch)
        self.assertEqual(model.status, "來源失效")

    def test_mid_poll_overflow_is_immediate_and_rolls_back_without_publish(self):
        now = 1_000_000_000
        source_id = identity()
        label = APPROVED_SHORTCUTS[0]
        server_ms = 16_440_000
        model = MultiGameClockSource(
            lambda: None, lambda _cancel: (), monotonic_ns=lambda: now,
            thread_factory=InlineThread,
        )
        generation = 1
        model.consensus.invalidate(label, generation)
        faithful = FaithfulSample(label, generation, "12:34:00.000", "millisecond")
        for _ in range(3):
            model.consensus.add(faithful)
        cancel = threading.Event()
        worker = SimpleNamespace(is_alive=lambda: True)
        model.streams[label] = (source_id, worker, cancel, generation, model.event_epoch)
        model.stream_started_ns[label] = now
        model.last_checked_ns[label] = now
        model.last_sample_ns[label] = now
        model.committed_ns[label] = now
        model.anchors[label] = (server_ms, now, now, generation)
        model.source_faulted = False
        model.last_poll_ns = now
        model.discovery = SimpleNamespace(is_alive=lambda: True)
        model.scheduler.last_discovery = now
        model.display = model.consensus.display((label,))
        model.status = model.display.text()

        item = sample(source_id, server_ms, now)
        epoch = model.event_epoch
        self.assertTrue(model._emit(
            "progress", (label, generation, source_id, item, False),
            now, epoch,
        ))
        for _ in range(EVENT_QUEUE_SIZE - 1):
            self.assertTrue(model._emit("noise", (), now, epoch))

        processing = threading.Barrier(2)
        overflow_done = threading.Event()
        immediate = []
        original_add = model.consensus.add

        def add_with_interleaving(value):
            processing.wait(timeout=2)
            if not overflow_done.wait(2):
                raise AssertionError("overflow worker did not finish")
            return original_add(value)

        model.consensus.add = add_with_interleaving
        published = []
        original_publish = model.scheduler.allow_publish

        def record_publish(payload):
            published.append(payload)
            return original_publish(payload)

        model.scheduler.allow_publish = record_publish

        def overflow_worker():
            processing.wait(timeout=2)
            emitted = True
            while emitted:
                emitted = model._emit("noise", (), now, epoch)
            immediate.append((
                emitted, model.overflow_fault.is_set(),
                model.timed_source_state(), model.timed_action_time_of_day_ms(),
            ))
            overflow_done.set()

        race = threading.Thread(target=overflow_worker, daemon=True)
        race.start()
        model.poll()
        race.join(2)
        self.assertFalse(race.is_alive(), "overflow interleaving deadlocked")
        self.assertEqual(immediate, [(False, True, "fault", None)])
        self.assertFalse(model.anchors)
        self.assertFalse(model.consensus.committed)
        self.assertEqual(model.status, "來源失效")
        self.assertEqual(published, [])
        self.assertIsNone(model.timed_action_time_of_day_ms())

    def test_stream_clear_races_do_not_break_health_boundary_or_tk_tick(self):
        now = 1_000_000_000

        for phase in ("health", "boundary"):
            with self.subTest(phase=phase):
                processing = threading.Barrier(2)
                overflow_done = threading.Event()

                class BlockingChecked:
                    blocked = False

                    def __rsub__(self, value):
                        if not self.blocked:
                            self.blocked = True
                            processing.wait(timeout=2)
                            if not overflow_done.wait(2):
                                raise AssertionError("health overflow did not finish")
                        return value - now

                class BoundaryClock:
                    calls = 0

                    def __call__(self):
                        self.calls += 1
                        if self.calls == 3:
                            processing.wait(timeout=2)
                            if not overflow_done.wait(2):
                                raise AssertionError("boundary overflow did not finish")
                        return now

                monotonic = BoundaryClock() if phase == "boundary" else lambda: now

                model = MultiGameClockSource(
                    lambda: None, lambda _cancel: (), monotonic_ns=monotonic,
                    thread_factory=InlineThread,
                )
                identities = [identity(101 + index, 201 + index, label)
                              for index, label in enumerate(APPROVED_SHORTCUTS[:2])]
                for label, source_id in zip(APPROVED_SHORTCUTS[:2], identities):
                    generation = 1
                    model.consensus.invalidate(label, generation)
                    model.streams[label] = (
                        source_id, SimpleNamespace(is_alive=lambda: True),
                        threading.Event(), generation, model.event_epoch,
                    )
                    model.last_checked_ns[label] = now
                    model.stream_started_ns[label] = now
                model.source_faulted = False
                model.last_poll_ns = now
                if phase == "health":
                    model.last_checked_ns[APPROVED_SHORTCUTS[0]] = BlockingChecked()
                else:
                    label = APPROVED_SHORTCUTS[0]
                    source_id = identities[0]
                    model.consensus.resample_all = True
                    model.events.put_nowait((
                        model.event_epoch, "progress",
                        (label, 1, source_id, sample(source_id, 16_440_000, now), False),
                        now,
                    ))

                published = []
                original_publish = model.scheduler.allow_publish

                def record_publish(payload):
                    published.append(payload)
                    return original_publish(payload)

                model.scheduler.allow_publish = record_publish
                immediate = []
                epoch = model.event_epoch

                def overflow_worker():
                    processing.wait(timeout=2)
                    while True:
                        try:
                            model.events.put_nowait((epoch, "noise", (), now))
                        except queue.Full:
                            break
                    emitted = model._emit("noise", (), now, epoch)
                    immediate.append((
                        emitted, model.overflow_fault.is_set(),
                        model.timed_source_state(), model.timed_action_time_of_day_ms(),
                    ))
                    overflow_done.set()

                race = threading.Thread(target=overflow_worker, daemon=True)
                race.start()
                app = SimpleNamespace(
                    game_time_tick_after_id="pending", closing_app=False,
                    poll_game_clock_acquisition=model.poll,
                    update_estimated_game_time_label=MagicMock(),
                    schedule_game_time_tick=MagicMock(),
                )
                raised = []
                try:
                    appmod.FlashSyncApp.poll_game_time_tick(app)
                except Exception as exc:
                    raised.append(exc)
                race.join(2)

                self.assertFalse(race.is_alive(), "stream-clear interleaving deadlocked")
                self.assertEqual(raised, [])
                app.update_estimated_game_time_label.assert_called_once_with()
                app.schedule_game_time_tick.assert_called_once_with()
                self.assertEqual(immediate, [(False, True, "fault", None)])
                self.assertEqual(published, [])
                self.assertFalse(model.anchors)
                self.assertFalse(model.consensus.committed)
                self.assertEqual(model.status, "來源失效")

    def test_consensus_clear_races_do_not_break_tk_tick_or_resurrect_state(self):
        now = 1_000_000_000
        label = APPROVED_SHORTCUTS[0]
        source_id = identity()
        value = "12:34:00.000"

        for phase in ("add", "confirmed"):
            with self.subTest(phase=phase):
                processing = threading.Barrier(2)
                overflow_done = threading.Event()

                def interleave():
                    processing.wait(timeout=2)
                    if not overflow_done.wait(2):
                        raise AssertionError(f"{phase} overflow did not finish")

                class BlockingAddSample:
                    label = APPROVED_SHORTCUTS[0]
                    value = "12:34:00.000"
                    precision = "millisecond"

                    @property
                    def generation(self):
                        interleave()
                        return 1

                class BlockingConfirmedSample:
                    label = APPROVED_SHORTCUTS[0]
                    generation = 1
                    value = "12:34:00.000"
                    precision = "millisecond"

                    def __eq__(self, other):
                        interleave()
                        return (self.label, self.generation, self.value, self.precision) == (
                            other.label, other.generation, other.value, other.precision,
                        )

                model = MultiGameClockSource(
                    lambda: None, lambda _cancel: (), monotonic_ns=lambda: now,
                    thread_factory=InlineThread,
                )
                generation = 1
                model.consensus.invalidate(label, generation)
                bucket = model.consensus.samples[label]
                if phase == "add":
                    bucket.extend((BlockingAddSample(), FaithfulSample(
                        label, generation, value, "millisecond",
                    )))
                else:
                    bucket.extend((BlockingConfirmedSample(), FaithfulSample(
                        label, generation, value, "millisecond",
                    )))
                model.consensus.committed[label] = (value, "millisecond")
                model.streams[label] = (
                    source_id, SimpleNamespace(is_alive=lambda: True),
                    threading.Event(), generation, model.event_epoch,
                )
                model.stream_started_ns[label] = now
                model.last_checked_ns[label] = now
                model.last_sample_ns[label] = now
                model.committed_ns[label] = now
                model.anchors[label] = (16_440_000, now, now, generation)
                model.source_faulted = False
                model.last_poll_ns = now
                model.discovery = SimpleNamespace(is_alive=lambda: True)
                model.scheduler.last_discovery = now
                model.display = model.consensus.display((label,))
                model.status = model.display.text()
                self.assertTrue(model._emit(
                    "progress",
                    (label, generation, source_id,
                     sample(source_id, 16_440_000, now), False),
                    now, model.event_epoch,
                ))

                published = []
                original_publish = model.scheduler.allow_publish

                def record_publish(payload):
                    published.append(payload)
                    return original_publish(payload)

                model.scheduler.allow_publish = record_publish
                immediate = []
                epoch = model.event_epoch

                def overflow_worker():
                    processing.wait(timeout=2)
                    emitted = True
                    while emitted:
                        emitted = model._emit("noise", (), now, epoch)
                    immediate.append((
                        emitted, model.overflow_fault.is_set(),
                        model.timed_source_state(), model.timed_action_time_of_day_ms(),
                    ))
                    overflow_done.set()

                race = threading.Thread(target=overflow_worker, daemon=True)
                race.start()
                app = SimpleNamespace(
                    game_time_tick_after_id="pending", closing_app=False,
                    poll_game_clock_acquisition=model.poll,
                    update_estimated_game_time_label=MagicMock(),
                    schedule_game_time_tick=MagicMock(),
                )
                raised = []
                try:
                    appmod.FlashSyncApp.poll_game_time_tick(app)
                except Exception as exc:
                    raised.append(exc)
                race.join(2)

                self.assertFalse(race.is_alive(), "consensus-clear interleaving deadlocked")
                self.assertEqual(raised, [])
                app.update_estimated_game_time_label.assert_called_once_with()
                app.schedule_game_time_tick.assert_called_once_with()
                self.assertEqual(immediate, [(False, True, "fault", None)])
                self.assertEqual(published, [])
                self.assertFalse(model.consensus.committed)
                self.assertFalse(model.anchors)
                self.assertEqual(model.display.groups, ())
                self.assertEqual(model.status, "來源失效")

    def test_startup_exceptions_fail_closed_without_breaking_tk_tick(self):
        now = 1_000_000_000
        source_id = identity()

        for phase in (
                "discovery_thread_factory", "discovery_start",
                "stream_reader_factory", "stream_thread_factory", "stream_start"):
            with self.subTest(phase=phase):
                captured = {}

                def reader_factory():
                    if phase == "stream_reader_factory":
                        raise RuntimeError("reader factory")
                    return FakeReader(source_id, ())

                class RaisingStart:
                    def __init__(self, target):
                        self.target = target

                    def start(self):
                        if phase == "discovery_start":
                            captured["cancel"] = model.discovery_cancel
                        else:
                            captured["cancel"] = model.streams[APPROVED_SHORTCUTS[0]][2]
                        raise RuntimeError("worker start")

                    @staticmethod
                    def is_alive():
                        return False

                def thread_factory(*, target, **_kwargs):
                    if phase in ("discovery_thread_factory", "stream_thread_factory"):
                        raise RuntimeError("thread factory")
                    return RaisingStart(target)

                model = MultiGameClockSource(
                    reader_factory, lambda _cancel: ((APPROVED_SHORTCUTS[0], source_id),),
                    monotonic_ns=lambda: now, thread_factory=thread_factory,
                )
                if phase.startswith("stream_"):
                    model.discovery = SimpleNamespace(is_alive=lambda: True)
                    model.events.put_nowait((
                        model.event_epoch, "discovered",
                        ((APPROVED_SHORTCUTS[0], source_id),), now,
                    ))
                starting_epoch = model.event_epoch
                app = SimpleNamespace(
                    game_time_tick_after_id="pending", closing_app=False,
                    poll_game_clock_acquisition=model.poll,
                    update_estimated_game_time_label=MagicMock(),
                    schedule_game_time_tick=MagicMock(),
                )
                raised = []
                try:
                    appmod.FlashSyncApp.poll_game_time_tick(app)
                except Exception as exc:
                    raised.append(exc)

                self.assertEqual(raised, [])
                app.update_estimated_game_time_label.assert_called_once_with()
                app.schedule_game_time_tick.assert_called_once_with()
                self.assertGreater(model.event_epoch, starting_epoch)
                self.assertTrue(model.source_faulted)
                self.assertEqual(model.status, "來源失效")
                self.assertFalse(model.streams)
                self.assertIsNone(model.timed_action_time_of_day_ms())
                if phase in ("discovery_start", "stream_start"):
                    self.assertTrue(captured["cancel"].is_set())
                if phase == "discovery_start":
                    self.assertIsNone(model.discovery)

    def test_same_payload_republishes_after_invalidate_without_split_brain(self):
        now = [1_000_000_000]
        label = APPROVED_SHORTCUTS[0]
        source_id = identity()
        server_ms = 16_440_000
        model = MultiGameClockSource(
            lambda: None, lambda _cancel: (), monotonic_ns=lambda: now[0],
            thread_factory=InlineThread,
        )

        def arm_and_queue(generation, first_checked):
            epoch = model.event_epoch
            model.consensus.invalidate(label, generation)
            model.streams[label] = (
                source_id, SimpleNamespace(is_alive=lambda: True),
                threading.Event(), generation, epoch,
            )
            model.stream_started_ns[label] = first_checked
            model.last_checked_ns[label] = first_checked
            model.source_faulted = False
            model.discovery = SimpleNamespace(is_alive=lambda: True)
            model.scheduler.last_discovery = first_checked
            for index in range(3):
                checked = first_checked + index * 250_000_000
                self.assertTrue(model._emit(
                    "progress",
                    (label, generation, source_id,
                     sample(source_id, server_ms, checked), False),
                    checked, epoch,
                ))
            now[0] = first_checked + 500_000_000
            model.poll()

        arm_and_queue(1, now[0])
        expected_display = model.consensus.display(APPROVED_SHORTCUTS)
        self.assertEqual(model.display, expected_display)
        self.assertEqual(model.status, expected_display.text())
        self.assertEqual(model.timed_source_state(), "valid")
        self.assertIn(label, model.scheduler.last_read)

        model.scheduler.last_discovery = 123
        model.invalidate("來源失效")
        self.assertNotIn(label, model.scheduler.last_read)
        self.assertEqual(model.scheduler.last_discovery, 123)
        next_generation = model.consensus.generation[label]
        arm_and_queue(next_generation, now[0] + 250_000_000)

        self.assertEqual(model.consensus.display(APPROVED_SHORTCUTS), expected_display)
        self.assertEqual(model.timed_source_state(), "valid")
        self.assertIsNotNone(model.timed_action_time_of_day_ms())
        self.assertEqual(model.display, expected_display)
        self.assertEqual(model.status, expected_display.text())

    def test_timed_requires_every_active_stream_to_be_confirmed_and_anchored(self):
        now = 1_000_000_000
        model = MultiGameClockSource(
            lambda: None, lambda _cancel: (), monotonic_ns=lambda: now,
            thread_factory=InlineThread,
        )
        active = APPROVED_SHORTCUTS[:2]
        for index, label in enumerate(active):
            source_id = identity(101 + index, 201 + index, label)
            generation = 1
            model.consensus.invalidate(label, generation)
            model.streams[label] = (
                source_id, SimpleNamespace(is_alive=lambda: True),
                threading.Event(), generation, model.event_epoch,
            )
            model.stream_started_ns[label] = now
            model.last_checked_ns[label] = now
        confirmed = FaithfulSample(active[0], 1, "12:34:00.000", "millisecond")
        for _ in range(3):
            model.consensus.add(confirmed)
        model.last_sample_ns[active[0]] = now
        model.committed_ns[active[0]] = now
        model.anchors[active[0]] = (16_440_000, now, now, 1)
        model.source_faulted = False
        model.last_poll_ns = now

        self.assertEqual(model.timed_source_state(), "waiting")
        self.assertIsNone(model.timed_action_time_of_day_ms())
        self.assertIsNone(model.exact_time_of_day_ms())
        self.assertIn(active[1], model.consensus.display(active).unreadable)

        del model.streams[active[1]]
        model.last_checked_ns.pop(active[1])
        model.stream_started_ns.pop(active[1])
        self.assertEqual(model.timed_source_state(), "valid")
        self.assertEqual(model.timed_action_time_of_day_ms(), 45_240_000)
        self.assertEqual(model.exact_time_of_day_ms(), 45_240_000)

    @staticmethod
    def _clock_reading(source_id, server_ms, edge=None):
        reading_type = getattr(reader_mod, "ClockReading", None)
        if reading_type is None:
            return SimpleNamespace(identity=source_id, server_ms=server_ms, edge=edge)
        return reading_type(source_id, server_ms, edge)

    def _reading_model(self, source_id, checked_values, readings):
        label = APPROVED_SHORTCUTS[0]
        now = checked_values[-1]
        model = MultiGameClockSource(
            lambda: None, lambda _cancel: (), monotonic_ns=lambda: now,
            thread_factory=InlineThread,
        )
        generation = 1
        model.consensus.invalidate(label, generation)
        model.streams[label] = (
            source_id, SimpleNamespace(is_alive=lambda: True), threading.Event(),
            generation, model.event_epoch,
        )
        model.stream_started_ns[label] = checked_values[0]
        model.last_checked_ns[label] = checked_values[0]
        model.source_faulted = False
        model.last_poll_ns = checked_values[0]
        model.scheduler.last_discovery = now
        for checked, reading in zip(checked_values, readings):
            model.events.put_nowait((
                model.event_epoch, "progress",
                (label, generation, source_id, reading, False), checked,
            ))
        model.poll()
        return model

    def _armed_timed_model(self, server_ms=16_440_000):
        label = APPROVED_SHORTCUTS[0]
        source_id = identity()
        now = [10_000_000_000]
        model = MultiGameClockSource(
            lambda: None, lambda _cancel: (), monotonic_ns=lambda: now[0],
            thread_factory=InlineThread,
        )
        generation = 1
        model.consensus.invalidate(label, generation)
        faithful = FaithfulSample(
            label, generation, model._format_server_ms(server_ms),
            "millisecond",
        )
        for _ in range(3):
            model.consensus.add(faithful)
        model.streams[label] = (
            source_id, SimpleNamespace(is_alive=lambda: True),
            threading.Event(), generation, model.event_epoch,
        )
        model.stream_started_ns[label] = now[0]
        model.last_checked_ns[label] = now[0]
        model.last_sample_ns[label] = now[0]
        model.committed_ns[label] = now[0]
        model.last_poll_ns = now[0]
        model.source_faulted = False
        model.discovery = SimpleNamespace(is_alive=lambda: True)
        model.scheduler.last_discovery = now[0]
        model.anchors[label] = (server_ms, now[0], now[0], generation)
        return model, now, label, source_id, generation

    def test_emit_revokes_timed_eligibility_before_poll_for_reset_and_fatal_events(self):
        for condition in (
                "reset", "stream_error", "progress_reason", "discovery_error"):
            with self.subTest(condition=condition):
                model, now, label, source_id, generation = self._armed_timed_model()
                epoch = model.event_epoch
                if condition == "reset":
                    item = reader_mod.ClockReading(
                        source_id, 16_440_001, None, True,
                    )
                    self.assertTrue(model._emit(
                        "progress",
                        (label, generation, source_id, item, False),
                        now[0], epoch,
                    ))
                    self.assertFalse(model.source_faulted)
                    self.assertNotIn(label, model.anchors)
                    self.assertEqual(model.event_epoch, epoch)
                    self.assertEqual(model.consensus.display((label,)).groups,
                                     (("12:34:00.000", (label,)),))
                    self.assertEqual(model.timed_source_state(), "waiting")
                elif condition == "stream_error":
                    self.assertFalse(model._emit(
                        "stream_error", (label, generation), now[0], epoch,
                    ))
                elif condition == "progress_reason":
                    self.assertFalse(model._emit(
                        "progress",
                        (label, generation, source_id, None, True),
                        now[0], epoch,
                    ))
                else:
                    self.assertFalse(model._emit(
                        "discovery_error", (), now[0], epoch,
                    ))
                self.assertIsNone(model.timed_action_time_of_day_ms())
                if condition != "reset":
                    self.assertTrue(model.source_faulted)
                    self.assertGreater(model.event_epoch, epoch)
                    self.assertTrue(model.events.empty())

    def test_discovered_emit_preflights_roster_before_poll(self):
        second_label = APPROVED_SHORTCUTS[1]
        for condition in ("same", "add", "loss", "change", "invalid", "stale"):
            with self.subTest(condition=condition):
                model, now, label, source_id, _generation = self._armed_timed_model()
                epoch = model.event_epoch
                second = identity(102, 202, second_label)
                if condition == "same":
                    payload = ((label, source_id),)
                elif condition == "add":
                    payload = ((label, source_id), (second_label, second))
                elif condition == "loss":
                    payload = ()
                elif condition == "change":
                    payload = ((label, replace(source_id, created=999)),)
                elif condition == "invalid":
                    payload = ((label, source_id), (label, source_id))
                else:
                    payload = ((label, source_id),)
                    epoch -= 1
                emitted = model._emit("discovered", payload, now[0], epoch)
                if condition in ("same", "add"):
                    self.assertTrue(emitted)
                    self.assertFalse(model.source_faulted)
                    expected = "waiting" if condition == "add" else "valid"
                    self.assertEqual(model.timed_source_state(), expected)
                    self.assertEqual(
                        model.queued_roster_change_epoch,
                        model.event_epoch if condition == "add" else None,
                    )
                elif condition == "stale":
                    self.assertFalse(emitted)
                    self.assertFalse(model.source_faulted)
                    self.assertEqual(model.timed_source_state(), "valid")
                    self.assertIsNone(model.queued_roster_change_epoch)
                else:
                    self.assertFalse(emitted)
                    self.assertTrue(model.source_faulted)
                    self.assertEqual(model.timed_source_state(), "fault")
                    self.assertIsNone(model.queued_roster_change_epoch)

    def test_roster_revision_survives_older_discovery_snapshot_cutoff(self):
        model, now, label, source_id, _generation = self._armed_timed_model()
        epoch = model.event_epoch
        second_label = APPROVED_SHORTCUTS[1]
        second = identity(102, 202, second_label)
        old_roster = ((label, source_id),)
        new_roster = old_roster + ((second_label, second),)
        self.assertTrue(model._emit("discovered", old_roster, now[0], epoch))

        snapshot_processing = threading.Event()
        release_snapshot = threading.Event()
        original_valid_discovery = model._valid_discovery
        poll_thread = None

        def barrier_valid_discovery(payload):
            if (payload == old_roster
                    and threading.current_thread() is poll_thread):
                snapshot_processing.set()
                self.assertTrue(release_snapshot.wait(2))
            return original_valid_discovery(payload)

        model._valid_discovery = barrier_valid_discovery
        poll_thread = threading.Thread(target=model.poll, daemon=True)
        poll_thread.start()
        self.assertTrue(snapshot_processing.wait(2), "old roster was not snapshotted")
        self.assertTrue(model._emit("discovered", new_roster, now[0], epoch))
        newer_revision = model.queued_roster_change_revision
        self.assertIsNotNone(newer_revision)
        self.assertEqual(model.timed_source_state(), "waiting")
        release_snapshot.set()
        poll_thread.join(2)
        self.assertFalse(poll_thread.is_alive(), "poll deadlocked on roster revision")

        self.assertEqual(model.queued_roster_change_revision, newer_revision)
        self.assertEqual(model.timed_source_state(), "waiting")

        class PassiveThread:
            def __init__(self, *, target, **_kwargs):
                self.target = target

            def start(self):
                return None

            @staticmethod
            def is_alive():
                return True

        model.thread_factory = PassiveThread
        now[0] += 250_000_000
        model.scheduler.last_discovery = now[0]
        model.poll()
        self.assertIn(second_label, model.streams)
        self.assertIsNone(model.queued_roster_change_revision)
        self.assertEqual(model.timed_source_state(), "waiting")

    def test_reset_revision_blocks_snapshotted_edge_anchor_resurrection(self):
        model, now, label, source_id, generation = self._armed_timed_model()
        epoch = model.event_epoch
        old_server_ms = 16_440_000
        corrected_server_ms = old_server_ms + 1_000
        old_edge = ClockSample(
            source_id, old_server_ms, now[0], 1_000_000, 10_000,
            30_000, "qualified", 20.0,
        )
        old_reading = self._clock_reading(source_id, old_server_ms, old_edge)
        self.assertTrue(model._emit(
            "progress", (label, generation, source_id, old_reading, False),
            now[0], epoch,
        ))

        snapshot_processing = threading.Event()
        release_snapshot = threading.Event()
        original_validate = model._validated_progress_item
        poll_thread = None

        def barrier_validate(item, expected, checked_ns):
            if (item is old_reading
                    and threading.current_thread() is poll_thread):
                snapshot_processing.set()
                self.assertTrue(release_snapshot.wait(2))
            return original_validate(item, expected, checked_ns)

        model._validated_progress_item = barrier_validate
        poll_thread = threading.Thread(target=model.poll, daemon=True)
        poll_thread.start()
        self.assertTrue(snapshot_processing.wait(2), "old edge was not snapshotted")
        reset = reader_mod.ClockReading(
            source_id, corrected_server_ms, None, True,
        )
        self.assertTrue(model._emit(
            "progress", (label, generation, source_id, reset, False),
            now[0] + 1, epoch,
        ))
        floor = model.anchor_control_floor[label]
        self.assertEqual(model.timed_source_state(), "waiting")
        release_snapshot.set()
        poll_thread.join(2)
        self.assertFalse(poll_thread.is_alive(), "poll deadlocked on anchor floor")
        self.assertNotIn(label, model.anchors)
        self.assertEqual(model.anchor_control_floor[label], floor)
        self.assertEqual(model.timed_source_state(), "waiting")

        # The reset itself is the first corrected display snapshot.  A later
        # qualified edge plus the third matching snapshot may establish the
        # corrected anchor; the pre-reset edge never participates.
        now[0] += 250_000_001
        model.scheduler.last_discovery = now[0]
        model.poll()
        corrected_edge = ClockSample(
            source_id, corrected_server_ms, now[0], 1_000_000, 10_000,
            30_000, "qualified", 20.0,
        )
        self.assertTrue(model._emit(
            "progress",
            (label, generation, source_id,
             self._clock_reading(source_id, corrected_server_ms, corrected_edge),
             False),
            now[0], epoch,
        ))
        now[0] += 250_000_000
        self.assertTrue(model._emit(
            "progress",
            (label, generation, source_id,
             self._clock_reading(source_id, corrected_server_ms), False),
            now[0], epoch,
        ))
        model.scheduler.last_discovery = now[0]
        model.poll()
        self.assertEqual(model.anchors[label][0], corrected_server_ms)
        self.assertEqual(model.anchors[label][1], corrected_edge.anchor_ns)
        self.assertEqual(model.timed_source_state(), "valid")
    def test_frozen_raw_snapshots_drive_display_but_never_create_anchor(self):
        source_id = identity()
        server_ms = 16_440_000
        checked = (1_000_000_000, 1_250_000_000, 1_500_000_000)
        readings = tuple(self._clock_reading(source_id, server_ms) for _ in checked)
        model = self._reading_model(source_id, checked, readings)

        label = APPROVED_SHORTCUTS[0]
        self.assertEqual(model.consensus.display((label,)).groups,
                         (("12:34:00.000", (label,)),))
        self.assertFalse(model.anchors)
        self.assertEqual(model.timed_source_state(), "waiting")
        self.assertIsNone(model.timed_action_time_of_day_ms())

    def test_qualified_edge_promotes_only_after_three_matching_snapshots_and_never_reanchors(self):
        source_id = identity()
        server_ms = 16_440_000
        checked = (1_000_000_000, 1_250_000_000, 1_500_000_000)
        edge = ClockSample(
            source_id, server_ms, checked[0], 1_000_000, 10_000, 1000,
            "qualified", 20.0,
        )
        readings = (
            self._clock_reading(source_id, server_ms, edge),
            self._clock_reading(source_id, server_ms),
            self._clock_reading(source_id, server_ms),
        )
        model = self._reading_model(source_id, checked, readings)
        label = APPROVED_SHORTCUTS[0]

        self.assertEqual(model.anchors[label][1], edge.anchor_ns)
        original_anchor = model.anchors[label]
        self.assertEqual(model.timed_source_state(), "valid")

        model.scheduler.last_discovery = checked[-1]
        for index in range(1, 4):
            later = checked[-1] + index * 250_000_000
            model.events.put_nowait((
                model.event_epoch, "progress",
                (label, 1, source_id,
                 self._clock_reading(source_id, server_ms), False), later,
            ))
        model.scheduler.now = lambda: checked[-1] + 750_000_000
        model.poll()
        self.assertEqual(model.anchors[label], original_anchor)

    def test_snapshot_health_and_anchor_freshness_are_independent(self):
        source_id = identity()
        label = APPROVED_SHORTCUTS[0]
        server_ms = 16_440_000
        initial = (1_000_000_000, 1_250_000_000, 1_500_000_000)

        for with_edge in (False, True):
            with self.subTest(with_edge=with_edge):
                edge = (ClockSample(
                    source_id, server_ms, initial[0], 1_000_000, 10_000,
                    1000, "qualified", 20.0,
                ) if with_edge else None)
                readings = (
                    self._clock_reading(source_id, server_ms, edge),
                    self._clock_reading(source_id, server_ms),
                    self._clock_reading(source_id, server_ms),
                )
                model = self._reading_model(source_id, initial, readings)
                now = [initial[-1]]
                model.scheduler.now = lambda: now[0]
                expected_display = model.display

                for second in range(2, 11):
                    now[0] = second * 1_000_000_000
                    model.scheduler.last_discovery = now[0]
                    model.events.put_nowait((
                        model.event_epoch, "progress",
                        (label, 1, source_id,
                         self._clock_reading(source_id, server_ms), False),
                        now[0],
                    ))
                    model.poll()

                self.assertFalse(model.source_faulted)
                self.assertEqual(model.display, expected_display)
                if with_edge:
                    self.assertEqual(model.timed_source_state(), "valid")
                    self.assertEqual(
                        model.timed_action_time_of_day_ms(),
                        (server_ms + 28_800_000 + 9_000) % 86_400_000,
                    )
                else:
                    self.assertFalse(model.anchors)
                    self.assertEqual(model.timed_source_state(), "waiting")
                    self.assertIsNone(model.timed_action_time_of_day_ms())

                if with_edge:
                    for second in range(11, 33):
                        now[0] = second * 1_000_000_000
                        model.scheduler.last_discovery = now[0]
                        model.events.put_nowait((
                            model.event_epoch, "progress",
                            (label, 1, source_id,
                             self._clock_reading(source_id, server_ms), False),
                            now[0],
                        ))
                        model.poll()
                    self.assertGreater(now[0] - initial[0], SAMPLE_MAX_AGE_NS)
                    self.assertFalse(model.source_faulted)
                    self.assertEqual(model.display, expected_display)
                    self.assertEqual(model.timed_source_state(), "waiting")
                    self.assertIsNone(model.timed_action_time_of_day_ms())

    def test_pending_consensus_handoff_grace_is_bounded_and_conditioned(self):
        label = APPROVED_SHORTCUTS[0]
        source_id = identity()
        old_server_ms = 16_440_000
        new_server_ms = old_server_ms + 30_000
        anchor_ns = 1_000_000_000
        edge_ns = anchor_ns + SAMPLE_MAX_AGE_NS

        def grace_model():
            now = [edge_ns + 250_000_000]
            model = MultiGameClockSource(
                lambda: None, lambda _cancel: (), monotonic_ns=lambda: now[0],
                thread_factory=InlineThread,
            )
            generation = 1
            model.consensus.invalidate(label, generation)
            old_value = model._format_server_ms(old_server_ms)
            old_sample = FaithfulSample(label, generation, old_value, "millisecond")
            for _ in range(3):
                model.consensus.add(old_sample)
            model.streams[label] = (
                source_id, SimpleNamespace(is_alive=lambda: True),
                threading.Event(), generation, model.event_epoch,
            )
            model.stream_started_ns[label] = anchor_ns
            model.last_checked_ns[label] = now[0]
            model.last_sample_ns[label] = now[0]
            model.committed_ns[label] = now[0]
            model.last_poll_ns = now[0]
            model.source_faulted = False
            model.anchors[label] = (
                old_server_ms, anchor_ns, anchor_ns, generation,
            )
            edge = ClockSample(
                source_id, new_server_ms, edge_ns, 1_000_000, 10_000,
                30_000, "qualified", 20.0,
            )
            model.pending_edges[label] = (edge, generation)
            model.consensus.add(FaithfulSample(
                label, generation, model._format_server_ms(new_server_ms),
                "millisecond",
            ))
            return model, now, edge

        model, now, _edge = grace_model()
        self.assertEqual(model.timed_source_state(), "valid")
        self.assertIsNotNone(model.timed_action_time_of_day_ms())
        model.consensus.add(FaithfulSample(
            label, 1, model._format_server_ms(new_server_ms), "millisecond",
        ))
        now[0] = edge_ns + 500_000_000
        model.last_checked_ns[label] = now[0]
        model.last_sample_ns[label] = now[0]
        model.committed_ns[label] = now[0]
        model.last_poll_ns = now[0]
        self.assertEqual(model.timed_source_state(), "valid")

        now[0] = edge_ns + 1_000_000_000
        model.last_checked_ns[label] = now[0]
        model.last_sample_ns[label] = now[0]
        model.committed_ns[label] = now[0]
        model.last_poll_ns = now[0]
        self.assertEqual(model.timed_source_state(), "valid")
        now[0] += 1
        model.last_checked_ns[label] = now[0]
        model.last_sample_ns[label] = now[0]
        model.committed_ns[label] = now[0]
        model.last_poll_ns = now[0]
        self.assertEqual(model.timed_source_state(), "waiting")

        for condition in (
                "no_pending", "consensus_failure", "value_change",
                "generation", "stream_identity", "edge_identity",
                "boundary", "fault"):
            with self.subTest(condition=condition):
                model, now, edge = grace_model()
                if condition == "no_pending":
                    model.pending_edges.clear()
                elif condition == "consensus_failure":
                    model.consensus.add(FaithfulSample(
                        label, 1, model._format_server_ms(new_server_ms + 1),
                        "millisecond",
                    ))
                elif condition == "value_change":
                    model.pending_edges[label] = (
                        replace(edge, server_ms=new_server_ms + 1), 1,
                    )
                elif condition == "generation":
                    model.pending_edges[label] = (edge, 2)
                elif condition == "stream_identity":
                    row = model.streams[label]
                    model.streams[label] = (
                        replace(source_id, created=999), *row[1:],
                    )
                elif condition == "edge_identity":
                    model.pending_edges[label] = (
                        replace(edge, identity=replace(source_id, created=999)), 1,
                    )
                elif condition == "boundary":
                    model.revalidating = True
                else:
                    model.source_faulted = True

                expected = ("fault" if condition == "fault" else
                            "valid" if condition == "boundary" else "waiting")
                self.assertEqual(model.timed_source_state(), expected)
                if condition == "boundary":
                    self.assertIsNotNone(model.timed_action_time_of_day_ms())
                else:
                    self.assertIsNone(model.timed_action_time_of_day_ms())

    def test_queued_edge_handoff_is_atomic_bounded_and_never_promotes_early(self):
        label = APPROVED_SHORTCUTS[0]
        source_id = identity()
        old_server_ms = 16_440_000
        new_server_ms = old_server_ms + 30_000
        anchor_ns = 1_000_000_000
        edge_ns = anchor_ns + SAMPLE_MAX_AGE_NS
        checked_ns = anchor_ns + BASE_QUALIFIED_ANCHOR_LEASE_NS
        now = [checked_ns]
        model = MultiGameClockSource(
            lambda: None, lambda _cancel: (), monotonic_ns=lambda: now[0],
            thread_factory=InlineThread,
        )
        generation = 1
        model.consensus.invalidate(label, generation)
        old_sample = FaithfulSample(
            label, generation, model._format_server_ms(old_server_ms),
            "millisecond",
        )
        for _ in range(3):
            model.consensus.add(old_sample)
        model.streams[label] = (
            source_id, SimpleNamespace(is_alive=lambda: True),
            threading.Event(), generation, model.event_epoch,
        )
        model.stream_started_ns[label] = anchor_ns
        model.last_checked_ns[label] = checked_ns
        model.last_sample_ns[label] = checked_ns
        model.committed_ns[label] = checked_ns
        model.last_poll_ns = checked_ns
        model.source_faulted = False
        model.discovery = SimpleNamespace(is_alive=lambda: True)
        model.scheduler.last_discovery = checked_ns
        model.anchors[label] = (
            old_server_ms, anchor_ns, anchor_ns, generation,
        )
        edge = ClockSample(
            source_id, new_server_ms, edge_ns, 1_000_000, 10_000,
            30_000, "qualified", 20.0,
        )
        reading = self._clock_reading(source_id, new_server_ms, edge)
        payload = (label, generation, source_id, reading, False)

        # _emit's event-lock critical section must not acquire the scheduler
        # lock.  The worker-to-queue handoff itself installs the marker.
        real_clock = model.scheduler.now
        model.scheduler.now = MagicMock(side_effect=AssertionError("lock inversion"))
        self.assertTrue(model._emit(
            "progress", payload, checked_ns, model.event_epoch,
        ))
        model.scheduler.now = real_clock
        self.assertIn(label, model.queued_edges)
        self.assertNotIn(label, model.pending_edges)
        self.assertEqual(model.anchors[label][1], anchor_ns)

        now[0] = checked_ns + QUEUED_EDGE_POLL_HANDOFF_MAX_NS
        for table in (
                model.last_checked_ns, model.last_sample_ns,
                model.committed_ns):
            table[label] = now[0]
        model.last_poll_ns = now[0]
        self.assertEqual(model.timed_source_state(), "valid")
        self.assertIsNotNone(model.timed_action_time_of_day_ms())
        self.assertEqual(model.anchors[label][1], anchor_ns)

        now[0] += 1
        for table in (
                model.last_checked_ns, model.last_sample_ns,
                model.committed_ns):
            table[label] = now[0]
        model.last_poll_ns = now[0]
        self.assertEqual(model.timed_source_state(), "waiting")
        self.assertIsNone(model.timed_action_time_of_day_ms())

        # Poll atomically consumes the marker into the existing pending-edge
        # consensus path; it still may not promote on the first snapshot.
        now[0] = checked_ns + 250_000_000
        model.last_poll_ns = checked_ns
        model.last_checked_ns[label] = checked_ns
        model.last_sample_ns[label] = checked_ns
        model.committed_ns[label] = checked_ns
        model.scheduler.last_discovery = now[0]
        model.poll()
        self.assertNotIn(label, model.queued_edges)
        self.assertIn(label, model.pending_edges)
        self.assertEqual(model.anchors[label][1], anchor_ns)
        self.assertEqual(model.timed_source_state(), "valid")

    def test_queued_edge_marker_clears_on_conflict_fault_boundary_and_overflow(self):
        label = APPROVED_SHORTCUTS[0]
        source_id = identity()
        old_server_ms = 16_440_000
        new_server_ms = old_server_ms + 30_000
        anchor_ns = 1_000_000_000
        edge_ns = anchor_ns + SAMPLE_MAX_AGE_NS

        def armed_model():
            checked_ns = anchor_ns + BASE_QUALIFIED_ANCHOR_LEASE_NS
            now = [checked_ns]
            model = MultiGameClockSource(
                lambda: None, lambda _cancel: (), monotonic_ns=lambda: now[0],
                thread_factory=InlineThread,
            )
            generation = 1
            model.consensus.invalidate(label, generation)
            old_sample = FaithfulSample(
                label, generation, model._format_server_ms(old_server_ms),
                "millisecond",
            )
            for _ in range(3):
                model.consensus.add(old_sample)
            model.streams[label] = (
                source_id, SimpleNamespace(is_alive=lambda: True),
                threading.Event(), generation, model.event_epoch,
            )
            model.stream_started_ns[label] = anchor_ns
            model.last_checked_ns[label] = checked_ns
            model.last_sample_ns[label] = checked_ns
            model.committed_ns[label] = checked_ns
            model.last_poll_ns = checked_ns
            model.source_faulted = False
            model.discovery = SimpleNamespace(is_alive=lambda: True)
            model.scheduler.last_discovery = checked_ns
            model.anchors[label] = (
                old_server_ms, anchor_ns, anchor_ns, generation,
            )
            edge = ClockSample(
                source_id, new_server_ms, edge_ns, 1_000_000, 10_000,
                30_000, "qualified", 20.0,
            )
            reading = self._clock_reading(source_id, new_server_ms, edge)
            self.assertTrue(model._emit(
                "progress", (label, generation, source_id, reading, False),
                checked_ns, model.event_epoch,
            ))
            self.assertIn(label, model.queued_edges)
            return model, now, generation

        for condition in (
                "value", "reason", "generation", "identity", "boundary",
                "invalidate", "overflow"):
            with self.subTest(condition=condition):
                model, now, generation = armed_model()
                epoch = model.event_epoch
                if condition == "value":
                    changed = self._clock_reading(source_id, new_server_ms + 1)
                    self.assertTrue(model._emit(
                        "progress",
                        (label, generation, source_id, changed, False),
                        now[0] + 1, epoch,
                    ))
                elif condition == "reason":
                    self.assertFalse(model._emit(
                        "progress", (label, generation, source_id, None, True),
                        now[0] + 1, epoch,
                    ))
                elif condition == "generation":
                    row = model.streams[label]
                    model.streams[label] = (*row[:3], generation + 1, row[4])
                    model.poll()
                elif condition == "identity":
                    row = model.streams[label]
                    model.streams[label] = (
                        replace(source_id, created=999), *row[1:],
                    )
                    model.poll()
                elif condition == "boundary":
                    model.consensus.resample_all = True
                    model.poll()
                elif condition == "invalidate":
                    model.invalidate("來源失效")
                else:
                    while model._emit("noise", (), now[0], epoch):
                        pass
                self.assertNotIn(label, model.queued_edges)
                now[0] = anchor_ns + BASE_QUALIFIED_ANCHOR_LEASE_NS + 1
                for table in (
                        model.last_checked_ns, model.last_sample_ns,
                        model.committed_ns):
                    if label in table:
                        table[label] = now[0]
                if not model.source_faulted:
                    model.last_poll_ns = now[0]
                self.assertNotEqual(model.timed_source_state(), "valid")

    def test_fresh_pending_or_queued_edge_never_resurrects_a_sixty_second_anchor(self):
        label = APPROVED_SHORTCUTS[0]
        source_id = identity()
        old_server_ms = 16_440_000
        anchor_ns = 1_000_000_000
        edge_ns = anchor_ns + 60_000_000_000
        new_server_ms = old_server_ms + 60_000

        for phase in ("pending", "queued"):
            with self.subTest(phase=phase):
                now = [edge_ns]
                model = MultiGameClockSource(
                    lambda: None, lambda _cancel: (), monotonic_ns=lambda: now[0],
                    thread_factory=InlineThread,
                )
                generation = 1
                model.consensus.invalidate(label, generation)
                old_sample = FaithfulSample(
                    label, generation, model._format_server_ms(old_server_ms),
                    "millisecond",
                )
                for _ in range(3):
                    model.consensus.add(old_sample)
                model.streams[label] = (
                    source_id, SimpleNamespace(is_alive=lambda: True),
                    threading.Event(), generation, model.event_epoch,
                )
                model.stream_started_ns[label] = anchor_ns
                model.last_checked_ns[label] = now[0]
                model.last_sample_ns[label] = now[0]
                model.committed_ns[label] = now[0]
                model.last_poll_ns = now[0]
                model.source_faulted = False
                model.anchors[label] = (
                    old_server_ms, anchor_ns, anchor_ns, generation,
                )
                edge = ClockSample(
                    source_id, new_server_ms, edge_ns, 1_000_000, 10_000,
                    30_000, "qualified", 20.0,
                )
                if phase == "pending":
                    model.pending_edges[label] = (edge, generation)
                    model.consensus.add(FaithfulSample(
                        label, generation,
                        model._format_server_ms(new_server_ms), "millisecond",
                    ))
                else:
                    reading = self._clock_reading(source_id, new_server_ms, edge)
                    self.assertTrue(model._emit(
                        "progress",
                        (label, generation, source_id, reading, False),
                        edge_ns, model.event_epoch,
                    ))
                    self.assertIn(label, model.queued_edges)
                now[0] += 1
                model.last_checked_ns[label] = now[0]
                model.last_sample_ns[label] = now[0]
                model.committed_ns[label] = now[0]
                model.last_poll_ns = now[0]
                self.assertEqual(model.timed_source_state(), "waiting")
                self.assertIsNone(model.timed_action_time_of_day_ms())

    def test_handoff_continuity_rejects_101ms_until_new_anchor_is_confirmed(self):
        label = APPROVED_SHORTCUTS[0]
        source_id = identity()
        old_server_ms = 16_440_000
        anchor_ns = 1_000_000_000
        edge_ns = anchor_ns + SAMPLE_MAX_AGE_NS
        new_server_ms = old_server_ms + 30_101
        now = [edge_ns]
        model = MultiGameClockSource(
            lambda: None, lambda _cancel: (), monotonic_ns=lambda: now[0],
            thread_factory=InlineThread,
        )
        generation = 1
        model.consensus.invalidate(label, generation)
        old_sample = FaithfulSample(
            label, generation, model._format_server_ms(old_server_ms),
            "millisecond",
        )
        for _ in range(3):
            model.consensus.add(old_sample)
        model.streams[label] = (
            source_id, SimpleNamespace(is_alive=lambda: True),
            threading.Event(), generation, model.event_epoch,
        )
        model.stream_started_ns[label] = anchor_ns
        model.last_checked_ns[label] = edge_ns
        model.last_sample_ns[label] = edge_ns
        model.committed_ns[label] = edge_ns
        model.last_poll_ns = edge_ns
        model.source_faulted = False
        model.discovery = SimpleNamespace(is_alive=lambda: True)
        model.anchors[label] = (
            old_server_ms, anchor_ns, anchor_ns, generation,
        )
        edge = ClockSample(
            source_id, new_server_ms, edge_ns, 1_000_000, 10_000,
            30_000, "qualified", 20.0,
        )
        reading = self._clock_reading(source_id, new_server_ms, edge)
        self.assertTrue(model._emit(
            "progress", (label, generation, source_id, reading, False),
            edge_ns, model.event_epoch,
        ))

        now[0] = anchor_ns + BASE_QUALIFIED_ANCHOR_LEASE_NS + 1
        model.last_poll_ns = now[0]
        self.assertEqual(model.timed_source_state(), "waiting")

        def poll_at(instant, qualified=None):
            now[0] = instant
            model.scheduler.last_discovery = instant
            if qualified is None:
                model.events.put_nowait((
                    model.event_epoch, "progress",
                    (label, generation, source_id,
                     self._clock_reading(source_id, new_server_ms), False),
                    instant,
                ))
            model.poll()

        poll_at(anchor_ns + BASE_QUALIFIED_ANCHOR_LEASE_NS + 250_000_000, edge)
        self.assertIn(label, model.pending_edges)
        self.assertEqual(model.consensus.matching_tail_count(
            label, generation, model._format_server_ms(new_server_ms),
            "millisecond",
        ), 1)
        self.assertEqual(model.timed_source_state(), "waiting")
        poll_at(anchor_ns + BASE_QUALIFIED_ANCHOR_LEASE_NS + 500_000_000)
        self.assertEqual(model.consensus.matching_tail_count(
            label, generation, model._format_server_ms(new_server_ms),
            "millisecond",
        ), 2)
        self.assertEqual(model.timed_source_state(), "waiting")
        poll_at(anchor_ns + BASE_QUALIFIED_ANCHOR_LEASE_NS + 750_000_000)
        self.assertNotIn(label, model.pending_edges)
        self.assertEqual(model.anchors[label][1], edge_ns)
        self.assertEqual(model.timed_source_state(), "valid")

    def test_four_fixed_phase_sources_renew_twice_at_30s_without_timed_gap(self):
        now = [1_250_000_000]
        model = MultiGameClockSource(
            lambda: None, lambda _cancel: (), monotonic_ns=lambda: now[0],
            thread_factory=InlineThread,
        )
        base_server_ms = 16_380_000
        phases_ns = (0, 20_000_000, 40_000_000, 60_000_000)
        generation = 1
        identities = {}
        for index, (label, phase_ns) in enumerate(zip(APPROVED_SHORTCUTS, phases_ns)):
            source_id = identity(101 + index, 201 + index, label)
            identities[label] = source_id
            model.consensus.invalidate(label, generation)
            faithful = FaithfulSample(
                label, generation, model._format_server_ms(base_server_ms),
                "millisecond",
            )
            for _ in range(3):
                model.consensus.add(faithful)
            anchor_ns = 1_000_000_000 + phase_ns
            model.streams[label] = (
                source_id, SimpleNamespace(is_alive=lambda: True),
                threading.Event(), generation, model.event_epoch,
            )
            model.stream_started_ns[label] = anchor_ns
            model.last_checked_ns[label] = now[0]
            model.last_sample_ns[label] = now[0]
            model.committed_ns[label] = now[0]
            model.anchors[label] = (
                base_server_ms, anchor_ns, anchor_ns, generation,
            )
        model.last_poll_ns = now[0]
        model.source_faulted = False
        model.scheduler.last_discovery = now[0]
        self.assertEqual(model.timed_source_state(), "valid")

        schedule = []
        for label, phase_ns in zip(APPROVED_SHORTCUTS, phases_ns):
            anchor_ns = 1_000_000_000 + phase_ns
            for tick in range(1, 243):
                schedule.append((anchor_ns + tick * 250_000_000, label, tick))
        schedule.sort()

        for instant, label, tick in schedule:
            now[0] = instant
            cycle = tick // 120
            server_ms = base_server_ms + cycle * 29_999
            edge = None
            if tick in (120, 240):
                edge = ClockSample(
                    identities[label], server_ms, instant,
                    1_000_000, 10_000, 29_999, "qualified", 20.0,
                )
            model.scheduler.last_discovery = instant
            model.events.put_nowait((
                model.event_epoch, "progress",
                (label, generation, identities[label],
                 self._clock_reading(identities[label], server_ms, edge), False),
                instant,
            ))
            model.poll()
            self.assertFalse(model.source_faulted, (label, tick))
            self.assertEqual(model.timed_source_state(), "valid", (label, tick))
            self.assertIsNotNone(model.timed_action_time_of_day_ms(), (label, tick))

        self.assertGreater(now[0] - 1_000_000_000, 2 * 30_000_000_000)
        self.assertEqual(set(model.anchors), set(APPROVED_SHORTCUTS))
        for label, phase_ns in zip(APPROVED_SHORTCUTS, phases_ns):
            self.assertEqual(model.anchors[label][0], base_server_ms + 2 * 29_999)
            self.assertEqual(
                model.anchors[label][1],
                1_000_000_000 + phase_ns + 2 * 30_000_000_000,
            )

    def test_pending_edge_is_revision_fenced_until_third_snapshot_and_cleared_on_change(self):
        source_id = identity()
        label = APPROVED_SHORTCUTS[0]
        first_ms = 16_440_000
        second_ms = first_ms + 1
        now = [1_000_000_000]
        model = MultiGameClockSource(
            lambda: None, lambda _cancel: (), monotonic_ns=lambda: now[0],
            thread_factory=InlineThread,
        )
        generation = 1
        model.consensus.invalidate(label, generation)
        model.streams[label] = (
            source_id, SimpleNamespace(is_alive=lambda: True), threading.Event(),
            generation, model.event_epoch,
        )
        model.stream_started_ns[label] = now[0]
        model.last_checked_ns[label] = now[0]
        model.last_poll_ns = now[0]
        model.source_faulted = False
        edge = ClockSample(
            source_id, first_ms, now[0], 1_000_000, 10_000, 1000,
            "qualified", 20.0,
        )

        def submit(server_ms, qualified=None):
            now[0] += 250_000_000
            model.scheduler.last_discovery = now[0]
            model.events.put_nowait((
                model.event_epoch, "progress",
                (label, generation, source_id,
                 self._clock_reading(source_id, server_ms, qualified), False),
                now[0],
            ))
            model.poll()

        submit(first_ms, edge)
        self.assertIn(label, model.pending_edges)
        submit(first_ms)
        self.assertIn(label, model.pending_edges)
        submit(first_ms)
        self.assertNotIn(label, model.pending_edges)
        self.assertEqual(model.anchors[label][1], edge.anchor_ns)

        model.invalidate("來源失效")
        self.assertFalse(model.pending_edges)
        self.assertFalse(model.anchors)

        generation = model.consensus.generation[label]
        model.consensus.invalidate(label, generation)
        model.streams[label] = (
            source_id, SimpleNamespace(is_alive=lambda: True), threading.Event(),
            generation, model.event_epoch,
        )
        model.stream_started_ns[label] = now[0]
        model.last_checked_ns[label] = now[0]
        model.last_poll_ns = now[0]
        model.source_faulted = False
        changed_edge = replace(edge, anchor_ns=now[0], server_ms=first_ms)
        submit(first_ms, changed_edge)
        self.assertIn(label, model.pending_edges)
        submit(second_ms)
        self.assertNotIn(label, model.pending_edges)
        self.assertFalse(model.anchors)

    def test_fourth_source_commits_display_after_scan_without_waiting_for_edge(self):
        source_id = identity()
        label = APPROVED_SHORTCUTS[0]
        server_ms = 16_440_000
        now = [120_000_000_000]
        model = MultiGameClockSource(
            lambda: None, lambda _cancel: (), monotonic_ns=lambda: now[0],
            thread_factory=InlineThread,
        )
        generation = 1
        model.consensus.invalidate(label, generation)
        model.streams[label] = (
            source_id, SimpleNamespace(is_alive=lambda: True), threading.Event(),
            generation, model.event_epoch,
        )
        model.stream_started_ns[label] = 0
        model.last_checked_ns[label] = now[0]
        model.last_poll_ns = now[0]
        model.source_faulted = False
        for _ in range(3):
            now[0] += 250_000_000
            model.scheduler.last_discovery = now[0]
            model.events.put_nowait((
                model.event_epoch, "progress",
                (label, generation, source_id,
                 self._clock_reading(source_id, server_ms), False), now[0],
            ))
            model.poll()

        self.assertEqual(now[0], 120_750_000_000)
        self.assertFalse(model.source_faulted)
        self.assertIn(label, model.consensus.committed)
        self.assertEqual(model.display.groups, (("12:34:00.000", (label,)),))
        self.assertFalse(model.anchors)
        self.assertEqual(model.timed_source_state(), "waiting")

    def test_full_150ms_reader_burst_does_not_overflow_real_multi_queue_or_stop_tk_tick(self):
        source_id = identity()
        label = APPROVED_SHORTCUTS[0]

        class VerifiedNative:
            def identity(self, _hwnd):
                return source_id

            def live_token(self, _hwnd, _handle):
                return (source_id.hwnd, source_id.pid, source_id.tid, source_id.created)

            @staticmethod
            def open(_pid):
                return 99

            @staticmethod
            def close(_handle):
                return None

        class FullBurstReader(reader_mod.GameClockReader):
            stream_identity_verified = True

            def __init__(self):
                self.qpc = 1_000_000_000
                super().__init__(native=VerifiedNative(), monotonic_ns=lambda: self.qpc)
                self.finished = threading.Event()
                self.burst_sleeps = 0

            @staticmethod
            def full_scan(_handle, _cancel):
                return reader_mod.Candidate(0x1000, 0x2000)

            @staticmethod
            def validate_structure(_handle, _candidate):
                return None

            def observe(self, handle, candidate):
                self.validate_structure(handle, candidate)
                return self._read_observation_values(handle, candidate)

            def _read_observation_values(self, _handle, _candidate):
                start = 1_000_000_000_000 + (1000 if self.qpc >= 1_250_000_000 else 0)
                raw = 1_000_000_000_000 + (1020 if self.qpc >= 1_250_000_000 else 0)
                return reader_mod.Observation(
                    raw, start, 0, self.qpc, self.qpc + 10_000,
                )

            def stream(self, hwnd, cancel, publish):
                try:
                    return super().stream(hwnd, cancel, publish)
                finally:
                    self.finished.set()

        reader = FullBurstReader()
        model = MultiGameClockSource(
            lambda: reader, lambda _cancel: (), monotonic_ns=lambda: reader.qpc,
            thread_factory=threading.Thread,
        )
        starting_epoch = model.event_epoch
        max_burst_polls = (
            reader_mod.NORMAL_OBSERVATION_INTERVAL_NS
            + reader_mod.EDGE_RESPONSE_MAX_MS * 1_000_000
            + reader_mod.BURST_POLL_INTERVAL_NS
        ) // reader_mod.BURST_POLL_INTERVAL_NS

        def sleep(seconds):
            reader.qpc += int(round(seconds * 1_000_000_000))
            if seconds == reader_mod.ADAPTIVE_BURST_SLEEP_SECONDS:
                reader.burst_sleeps += 1
                if reader.burst_sleeps == max_burst_polls:
                    reader._stream_cancel.set()

        with patch("v02_game_clock_reader.time.sleep", side_effect=sleep):
            self.assertTrue(model._start_stream(label, source_id))
            self.assertTrue(reader.finished.wait(2), "150 ms burst worker did not finish")

        model.source_faulted = False
        model.scheduler.last_discovery = reader.qpc
        app = SimpleNamespace(
            game_time_tick_after_id="pending", closing_app=False,
            poll_game_clock_acquisition=model.poll,
            update_estimated_game_time_label=MagicMock(),
            schedule_game_time_tick=MagicMock(),
        )
        appmod.FlashSyncApp.poll_game_time_tick(app)

        self.assertEqual(reader.burst_sleeps, max_burst_polls)
        self.assertLessEqual(
            reader.burst_sleeps * reader_mod.BURST_POLL_INTERVAL_NS,
            reader_mod.ADAPTIVE_BURST_HARD_MAX_NS,
        )
        self.assertEqual(model.event_epoch, starting_epoch)
        self.assertFalse(model.overflow_fault.is_set())
        self.assertEqual(model.timed_source_state(), "waiting")
        app.update_estimated_game_time_label.assert_called_once_with()
        app.schedule_game_time_tick.assert_called_once_with()

    def test_prediction_reset_keeps_display_healthy_but_revokes_timed_anchor(self):
        source_id = identity()
        label = APPROVED_SHORTCUTS[0]
        server_ms = 16_440_000
        checked = (1_000_000_000, 1_250_000_000, 1_500_000_000)
        edge = ClockSample(
            source_id, server_ms, checked[0], 1_000_000, 10_000,
            1000, "qualified", 20.0,
        )
        model = self._reading_model(source_id, checked, (
            self._clock_reading(source_id, server_ms, edge),
            self._clock_reading(source_id, server_ms),
            self._clock_reading(source_id, server_ms),
        ))
        epoch = model.event_epoch
        expected_display = model.display
        now = checked[-1] + 250_000_000
        model.scheduler.now = lambda: now
        model.scheduler.last_discovery = now
        model.events.put_nowait((
            epoch, "progress",
            (label, 1, source_id,
             reader_mod.ClockReading(source_id, server_ms, None, True), False),
            now,
        ))
        model.poll()

        self.assertEqual(model.event_epoch, epoch)
        self.assertFalse(model.source_faulted)
        self.assertEqual(model.display, expected_display)
        self.assertIn(label, model.consensus.committed)
        self.assertNotIn(label, model.anchors)
        self.assertEqual(model.timed_source_state(), "waiting")

    def test_raw_only_unpaired_response_revokes_anchor_before_and_after_poll(self):
        source_id = identity()
        raw_base = 1_700_000_000_000

        class VerifiedNative:
            def identity(self, _hwnd):
                return source_id

            def live_token(self, _hwnd, _handle):
                return (source_id.hwnd, source_id.pid, source_id.tid, source_id.created)

            @staticmethod
            def open(_pid):
                return 99

            @staticmethod
            def close(_handle):
                return None

        class RawOnlyReader(reader_mod.GameClockReader):
            def __init__(self):
                self.qpc = 1_000_000_000
                super().__init__(
                    native=VerifiedNative(), monotonic_ns=lambda: self.qpc,
                )

            @staticmethod
            def full_scan(_handle, _cancel):
                return reader_mod.Candidate(0x1000, 0x2000)

            @staticmethod
            def validate_structure(_handle, _candidate):
                return None

            def _read_observation_values(self, _handle, _candidate):
                raw = raw_base + (1_000 if self.qpc >= 1_250_000_000 else 0)
                return reader_mod.Observation(
                    raw, raw_base, 0, self.qpc, self.qpc + 10_000,
                )

        reader = RawOnlyReader()
        cancel = threading.Event()
        readings = []

        def sleep(seconds):
            reader.qpc += int(round(seconds * 1_000_000_000))

        def capture(item, _reason, _checked_ns):
            if item is not None:
                readings.append(item)
                cancel.set()

        with patch("v02_game_clock_reader.time.sleep", side_effect=sleep):
            reader.stream(source_id.hwnd, cancel, capture)

        self.assertEqual(len(readings), 1)
        reading = readings[0]
        self.assertIsNone(reading.edge)
        self.assertTrue(reading.reset_anchor)

        model, now, label, expected, generation = self._armed_timed_model(
            server_ms=raw_base,
        )
        epoch = model.event_epoch
        checked_ns = now[0] + 1
        emitted_reading = reader_mod.ClockReading(
            expected, reading.server_ms, None, reading.reset_anchor,
        )
        self.assertTrue(model._emit(
            "progress",
            (label, generation, expected, emitted_reading, False),
            checked_ns, epoch,
        ))
        self.assertEqual(model.timed_source_state(), "waiting")
        self.assertIsNone(model.timed_action_time_of_day_ms())
        now[0] += 250_000_000
        model.scheduler.last_discovery = now[0]
        model.poll()
        self.assertEqual(model.event_epoch, epoch)
        self.assertFalse(model.source_faulted)
        self.assertNotIn(label, model.anchors)
        self.assertEqual(model.timed_source_state(), "waiting")

    def test_transient_pair_at_response_deadline_retries_but_persistent_split_faults(self):
        source_id = identity()
        label = APPROVED_SHORTCUTS[0]

        class VerifiedNative:
            def identity(self, _hwnd):
                return source_id

            def live_token(self, _hwnd, _handle):
                return (source_id.hwnd, source_id.pid, source_id.tid, source_id.created)

            @staticmethod
            def open(_pid):
                return 99

            @staticmethod
            def close(_handle):
                return None

        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                class TransientReader(reader_mod.GameClockReader):
                    stream_identity_verified = True

                    def __init__(self):
                        self.qpc = 1_000_000_000
                        super().__init__(
                            native=VerifiedNative(), monotonic_ns=lambda: self.qpc,
                        )
                        self.finished = threading.Event()
                        self.published = []
                        self.endpoint_split = False

                    @staticmethod
                    def full_scan(_handle, _cancel):
                        return reader_mod.Candidate(0x1000, 0x2000)

                    @staticmethod
                    def validate_structure(_handle, _candidate):
                        return None

                    def _read_observation_values(self, _handle, _candidate):
                        if self.qpc >= 1_351_000_000:
                            if persistent or not self.endpoint_split:
                                self.endpoint_split = True
                                raise reader_mod.TransientObservation("split")
                            raw = 1_000_000_001_100
                        else:
                            raw = 1_000_000_000_000
                        start = (1_000_000_001_000
                                 if self.qpc >= 1_250_000_000 else 1_000_000_000_000)
                        return reader_mod.Observation(
                            raw, start, 0, self.qpc, self.qpc + 10_000,
                        )

                    def stream(self, hwnd, cancel, publish):
                        def capture(item, reason, checked_ns):
                            publish(item, reason, checked_ns)
                            if item is not None:
                                self.published.append(item)
                                if item.edge is not None:
                                    cancel.set()
                        try:
                            return super().stream(hwnd, cancel, capture)
                        finally:
                            self.finished.set()

                reader = TransientReader()
                model = MultiGameClockSource(
                    lambda: reader, lambda _cancel: (), monotonic_ns=lambda: reader.qpc,
                    thread_factory=threading.Thread,
                )
                epoch = model.event_epoch

                def sleep(seconds):
                    reader.qpc += int(round(seconds * 1_000_000_000))

                with patch("v02_game_clock_reader.time.sleep", side_effect=sleep):
                    self.assertTrue(model._start_stream(label, source_id))
                    self.assertTrue(reader.finished.wait(2), "transient stream did not finish")

                if persistent:
                    self.assertGreater(model.event_epoch, epoch)
                    self.assertTrue(model.source_faulted)
                    self.assertEqual(model.status, "來源失效")
                else:
                    model.source_faulted = False
                    model.discovery = SimpleNamespace(is_alive=lambda: True)
                    model.scheduler.last_discovery = reader.qpc
                    model.poll()
                    self.assertEqual(model.event_epoch, epoch)
                    edges = [item.edge for item in reader.published if item.edge is not None]
                    self.assertEqual(len(edges), 1)
                    self.assertEqual(edges[0].response_interval_ms, 100)
                    self.assertFalse(model.overflow_fault.is_set())

    def test_poll_cutoff_is_sampled_after_queue_snapshot_for_discovery_and_progress(self):
        old_now = 1_000_000_000
        event_now = old_now + 1
        label = APPROVED_SHORTCUTS[0]
        source_id = identity()

        for kind in ("discovered", "progress"):
            with self.subTest(kind=kind):
                model = MultiGameClockSource(
                    lambda: None, lambda _cancel: (), monotonic_ns=lambda: old_now,
                    thread_factory=InlineThread,
                )
                generation = 1
                epoch = model.event_epoch
                model.consensus.invalidate(label, generation)
                model.streams[label] = (
                    source_id, SimpleNamespace(is_alive=lambda: True),
                    threading.Event(), generation, epoch,
                )
                model.stream_started_ns[label] = old_now
                model.last_checked_ns[label] = old_now
                model.last_poll_ns = old_now
                model.source_faulted = False
                model.discovery = SimpleNamespace(is_alive=lambda: True)
                model.scheduler.last_discovery = old_now
                payload = (((label, source_id),) if kind == "discovered" else (
                    label, generation, source_id, None, False,
                ))

                release_emit = threading.Event()
                emit_attempted = threading.Event()
                emit_done = threading.Event()
                first_clock = [True]

                def worker():
                    release_emit.wait(2)
                    emit_attempted.set()
                    model._emit(kind, payload, event_now, epoch)
                    emit_done.set()

                def cutoff_clock():
                    if first_clock[0]:
                        first_clock[0] = False
                        release_emit.set()
                        self.assertTrue(emit_attempted.wait(2))
                        # On the old order the emitter completes before poll takes
                        # the event lock.  On the fixed order it remains blocked
                        # behind the queue snapshot and is handled next poll.
                        emit_done.wait(0.05)
                        return old_now
                    return event_now

                race = threading.Thread(target=worker, daemon=True)
                race.start()
                model.scheduler.now = cutoff_clock
                model.poll()
                race.join(2)
                self.assertFalse(race.is_alive(), "cutoff emitter deadlocked")
                model.scheduler.now = lambda: event_now
                model.poll()

                self.assertEqual(model.event_epoch, epoch)
                self.assertIn(label, model.streams)
                self.assertFalse(model.source_faulted)
                if kind == "progress":
                    self.assertEqual(model.last_checked_ns[label], event_now)

    def test_multi_anchor_uses_circular_median_and_rejects_excess_spread(self):
        now = 40_000_000_000

        def source_with_estimates(order, estimates):
            model = MultiGameClockSource(
                lambda: None, lambda _cancel: (), monotonic_ns=lambda: now,
                thread_factory=InlineThread,
            )
            for label, estimate in zip(order, estimates):
                source_id = identity(100 + len(model.streams), 200 + len(model.streams), label)
                generation = 1
                model.consensus.invalidate(label, generation)
                faithful = FaithfulSample(label, generation, "00:00:00.000", "millisecond")
                for _ in range(3):
                    model.consensus.add(faithful)
                model.streams[label] = (
                    source_id, SimpleNamespace(is_alive=lambda: True), threading.Event(),
                    generation, model.event_epoch,
                )
                model.stream_started_ns[label] = now
                model.last_checked_ns[label] = now
                model.last_sample_ns[label] = now
                model.committed_ns[label] = now
                server_ms = (estimate - 28_800_000) % 86_400_000
                model.anchors[label] = (server_ms, now, now, generation)
            model.source_faulted = False
            model.last_poll_ns = now
            return model

        labels = APPROVED_SHORTCUTS[:2]
        first = source_with_estimates(labels, (86_399_980, 20))
        reversed_labels = tuple(reversed(labels))
        second = source_with_estimates(reversed_labels, (20, 86_399_980))
        self.assertEqual(first.timed_source_state(), "valid")
        self.assertEqual(first.timed_action_time_of_day_ms(), 0)
        self.assertEqual(second.timed_action_time_of_day_ms(), 0)

        excessive = source_with_estimates(labels, (0, 101))
        self.assertEqual(excessive.timed_source_state(), "waiting")
        self.assertIsNone(excessive.timed_action_time_of_day_ms())

    def test_periodic_discovery_adds_new_approved_source_and_timed_waits(self):
        now = [0]
        first_label, second_label = APPROVED_SHORTCUTS[:2]
        first = identity(101, 201, first_label)
        second = identity(102, 202, second_label)
        discoveries = []
        rosters = [((first_label, first),), ((first_label, first), (second_label, second))]

        def discover(_cancel):
            discoveries.append(now[0])
            return rosters.pop(0)

        model = MultiGameClockSource(
            lambda: FakeReader(second, ()), discover,
            monotonic_ns=lambda: now[0], thread_factory=InlineThread,
        )
        generation = 1
        model.consensus.invalidate(first_label, generation)
        faithful = FaithfulSample(first_label, generation, "12:34:00.000", "millisecond")
        for _ in range(3):
            model.consensus.add(faithful)
        model.streams[first_label] = (
            first, SimpleNamespace(is_alive=lambda: True), threading.Event(),
            generation, model.event_epoch,
        )
        model.stream_started_ns[first_label] = now[0]
        model.last_checked_ns[first_label] = now[0]
        model.last_sample_ns[first_label] = now[0]
        model.committed_ns[first_label] = now[0]
        model.anchors[first_label] = (16_440_000, now[0], now[0], generation)
        model.source_faulted = False
        model.last_poll_ns = now[0]
        model.scheduler.last_discovery = now[0]

        class PassiveThread:
            def __init__(self, *, target, **_kwargs):
                self.target = target

            def start(self):
                return None

            @staticmethod
            def is_alive():
                return True

        for second_count in range(1, 12):
            now[0] = second_count * 1_000_000_000
            self.assertTrue(model._emit(
                "progress",
                (first_label, generation, first,
                 sample(first, 16_440_000, now[0]), False),
                now[0], model.event_epoch,
            ))
            model.poll()
            if second_count == 4:
                self.assertEqual(discoveries, [])
                self.assertEqual(model.timed_source_state(), "valid")
            if second_count == 10:
                # Discovery of the added source is already visible through the
                # revision-fenced pre-poll roster marker.
                self.assertEqual(model.timed_source_state(), "waiting")
                model.thread_factory = PassiveThread

        self.assertEqual(discoveries, [5_000_000_000, 10_000_000_000])
        self.assertIn(second_label, model.streams)
        self.assertEqual(model.timed_source_state(), "waiting")
        self.assertIsNone(model.timed_action_time_of_day_ms())

    def test_discovery_roster_loss_or_identity_change_invalidates_generation(self):
        now = 1_000_000_000
        label = APPROVED_SHORTCUTS[0]
        existing = identity()
        changed = replace(existing, created=existing.created + 1)
        for payload in ((), ((label, changed),)):
            with self.subTest(payload=payload):
                model = MultiGameClockSource(
                    lambda: None, lambda _cancel: (), monotonic_ns=lambda: now,
                    thread_factory=InlineThread,
                )
                model.consensus.invalidate(label, 1)
                model.streams[label] = (
                    existing, SimpleNamespace(is_alive=lambda: True),
                    threading.Event(), 1, model.event_epoch,
                )
                model.stream_started_ns[label] = now
                model.last_checked_ns[label] = now
                model.source_faulted = False
                starting_epoch = model.event_epoch
                model.events.put_nowait((starting_epoch, "discovered", payload, now))
                model.poll()

                self.assertGreater(model.event_epoch, starting_epoch)
                self.assertFalse(model.streams)
                self.assertTrue(model.source_faulted)
                self.assertEqual(model.status, "來源失效")
                self.assertIsNone(model.timed_action_time_of_day_ms())


class PrivacyAndUiTests(unittest.TestCase):
    def test_persistence_scrubs_legacy_and_session_sensitive_identity_fields(self):
        cleaned = sanitized_record({
            "process_command_line": "discard-me",
            "process_identity": "old deterministic digest",
            "command_line": "discard-me",
            "username": "discard-me",
            "password": "discard-me",
            "nested": {"launch_identity": "session-only", "safe": 1},
        })
        self.assertEqual(cleaned, {"nested": {"safe": 1}})

    def test_process_identity_is_process_lifetime_hmac_not_cross_session_sha(self):
        code = (
            "import sys;sys.path.insert(0,r'.');"
            "import dpi_policy;print(dpi_policy.process_identity('C:/GameLoader.exe','opaque-launch-arguments'))"
        )
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        first = subprocess.check_output(
            [sys.executable, "-c", code], cwd=HERE, env=env, text=True,
        ).strip()
        second = subprocess.check_output(
            [sys.executable, "-c", code], cwd=HERE, env=env, text=True,
        ).strip()
        self.assertTrue(first and second)
        self.assertNotEqual(first, second)

    def test_tk_toolbar_boundary_dedupes_both_stringvars(self):
        tree = ast.parse(FLASH_SYNC.read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                   and node.name == "FlashSyncApp")
        methods = {node.name: ast.unparse(node) for node in cls.body
                   if isinstance(node, ast.FunctionDef)}
        self.assertIn("_set_game_time_source_text", methods)
        self.assertIn("_set_game_time_text", methods)
        self.assertNotIn("game_time_source_text.set", methods["poll_game_clock_acquisition"])
        self.assertNotIn("game_time_text.set", methods["update_estimated_game_time_label"])
        direct_source_sets = [name for name, body in methods.items()
                              if "game_time_source_text.set" in body]
        direct_time_sets = [name for name, body in methods.items()
                            if "game_time_text.set" in body]
        self.assertEqual(direct_source_sets, ["_set_game_time_source_text"])
        self.assertEqual(direct_time_sets, ["_set_game_time_text"])

    def test_timed_status_and_logs_use_only_four_semantic_payloads(self):
        text = FLASH_SYNC.read_text(encoding="utf-8")
        for forbidden in ("定時按下：剩", "第一下觸發時差值", "定時按下：未啟用",
                          "定時按下：時鐘失效", "定時按下：目標時間無效"):
            self.assertNotIn(forbidden, text)
        for required in ("定時按下：已啟用", "定時按下：等待目標時間",
                         "定時按下：已觸發", "定時按下：來源失效"):
            self.assertIn(required, text)

    def test_cross_midnight_target_is_explicitly_next_day(self):
        tree = ast.parse(FLASH_SYNC.read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                   and node.name == "FlashSyncApp")
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef)
                      and node.name == "_timed_target_remaining_ms")
        namespace = {"DAY_MS": 86_400_000}
        harness = ast.ClassDef(
            name="Harness", bases=[], keywords=[], body=[method], decorator_list=[],
        )
        exec(compile(ast.fix_missing_locations(ast.Module([harness], [])), "timed", "exec"), namespace)
        app = namespace["Harness"]()
        self.assertEqual(app._timed_target_remaining_ms(86_399_000, 0), 1_000)
        self.assertEqual(app._timed_target_remaining_ms(1_000, 500), 86_399_500)


class PackagingTests(unittest.TestCase):
    def test_runtime_asset_manifest_is_complete_and_hashes_every_binary_asset(self):
        path = HERE / "RUNTIME_ASSET_MANIFEST.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        assets = payload["assets"]
        self.assertGreater(len(assets), 0)
        self.assertTrue(any(item["path"].startswith("templates/") for item in assets))
        self.assertTrue(any(item["path"].startswith("manor_assets/") for item in assets))
        self.assertTrue(any(item["path"].startswith("fishing_evidence/") for item in assets))
        for item in assets:
            self.assertEqual(set(item), {"path", "bytes", "sha256"})
            self.assertNotIn(":\\", item["path"])
            self.assertEqual(len(item["sha256"]), 64)

    def test_spec_bundles_runtime_asset_manifest(self):
        self.assertIn("RUNTIME_ASSET_MANIFEST.json",
                      (HERE / "fu_preview.spec").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
