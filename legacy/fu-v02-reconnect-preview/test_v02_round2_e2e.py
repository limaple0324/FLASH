"""Round-2 fail-closed integration tests; no live game, input, or user data."""
from __future__ import annotations

import ast
from dataclasses import replace
import json
import os
from pathlib import Path
import queue
import shutil
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from runtime_asset_manifest import verify_runtime_assets
from session_identity import session_hmac
from test_flash_sync_status_window import Panel, Value
from v02_faithful_game_time import (
    APPROVED_SHORTCUTS, EVENT_QUEUE_SIZE, FIRST_SAMPLE_TIMEOUT_NS, FRESHNESS_TTL_NS,
    ApprovedShortcutCatalog, MultiGameClockSource,
)
from v02_game_clock import ClockSample, HEALTH_MAX_GAP_NS, SourceIdentity
from v02_game_clock_reader import (
    AcquisitionError, Candidate, FULL_SCAN_DEADLINE_NS, FULL_SCAN_INTERVAL_NS,
    FullScanCoordinator, GameClockReader,
)


HERE = Path(__file__).resolve().parent
FLASH_SYNC = HERE / "flash_sync_v02.py"
UTC_0434_MS = 4 * 3_600_000 + 34 * 60_000


class CountingValue(Value):
    def __init__(self, value):
        super().__init__(value)
        self.set_count = 0

    def set(self, value):
        self.set_count += 1
        super().set(value)


class MutableNative:
    def __init__(self, identity):
        self.current = identity

    def identity(self, hwnd):
        if hwnd != self.current.hwnd:
            raise AcquisitionError("來源視窗已失效")
        return self.current


class InteractiveReader:
    """A real worker-thread reader controlled by acknowledged test commands."""
    def __init__(self, identity):
        self.native = MutableNative(identity)
        self.expected_identity = None
        self.commands = queue.Queue()
        self.ready = threading.Event()

    def submit(self, item, checked_ns, reason=""):
        done = threading.Event()
        self.commands.put((item, reason, checked_ns, done))
        if not done.wait(2):
            raise AssertionError("reader command was not acknowledged")

    def stream(self, hwnd, cancel, publish):
        if hwnd != self.native.current.hwnd:
            raise AcquisitionError("來源視窗已失效")
        self.ready.set()
        while not cancel.is_set():
            try:
                item, reason, checked_ns, done = self.commands.get(timeout=0.02)
            except queue.Empty:
                continue
            try:
                publish(item, reason, checked_ns)
            finally:
                done.set()


class MultiSourceHarness:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        desktop = Path(self.temp.name)
        target = str(HERE / "GameLoader.exe")
        arguments = {label: f"launch-{index}" for index, label in enumerate(APPROVED_SHORTCUTS)}
        for label in APPROVED_SHORTCUTS:
            (desktop / f"{label}.lnk").write_bytes(b"test shortcut")
        catalog = ApprovedShortcutCatalog(
            lambda value: session_hmac("test-shortcut", value),
            resolver=lambda path: (target, arguments[Path(path).stem]),
            desktop=str(desktop),
        )
        normalized_target = os.path.normcase(os.path.abspath(target))
        self.identities = tuple(
            SourceIdentity(
                100 + index, 200 + index, 300 + index, 400 + index,
                session_hmac("test-shortcut", arguments[label]), "A" * 64,
                normalized_target,
            )
            for index, label in enumerate(APPROVED_SHORTCUTS)
        )
        self.selected = catalog.select(self.identities)
        if tuple(label for label, _identity in self.selected) != APPROVED_SHORTCUTS:
            raise AssertionError("approved shortcut selector failed")
        self.now = 100_000_000_000
        self.readers = [InteractiveReader(item) for item in self.identities]
        pending = list(self.readers)
        self.discovery_done = threading.Event()

        def discover(_cancel):
            self.discovery_done.set()
            return self.selected

        self.source = MultiGameClockSource(
            lambda: pending.pop(0), discover,
            monotonic_ns=lambda: self.now, thread_factory=threading.Thread,
        )

    def start(self):
        self.source.poll()
        if not self.discovery_done.wait(2):
            raise AssertionError("discovery did not complete")
        self.source.discovery.join(2)
        if self.source.discovery.is_alive():
            raise AssertionError("discovery worker did not exit")
        self.source.poll()
        for reader in self.readers:
            if not reader.ready.wait(2):
                raise AssertionError("stream did not start")
        return self

    def round(self, server_ms, *, checked_ns=None):
        if checked_ns is None:
            self.now += 250_000_000
            checked_ns = self.now
        for reader, identity in zip(self.readers, self.identities):
            reader.submit(
                ClockSample(identity, server_ms, checked_ns, 1, 1, 1000, "e2e"),
                checked_ns,
            )
        self.source.poll()

    def close(self):
        self.source.shutdown()
        workers = list(self.source._retired_workers)
        if self.source.discovery is not None:
            workers.append(self.source.discovery)
        for worker in dict.fromkeys(workers):
            worker.join(2)
            if worker.is_alive():
                raise AssertionError("worker did not stop")
        self.temp.cleanup()


class MultiGameClockEndToEndTests(unittest.TestCase):
    def harness(self):
        harness = MultiSourceHarness().start()
        self.addCleanup(harness.close)
        return harness

    def test_catalog_discovery_stream_consensus_anchor_and_display_end_to_end(self):
        harness = self.harness()
        for expected_count in (0, 0, 4):
            harness.round(UTC_0434_MS)
            self.assertEqual(len(harness.source.anchors), expected_count)
        self.assertEqual(harness.source.exact_time_of_day_ms(), 45_240_000)
        self.assertEqual(harness.source.timed_source_state(), "valid")
        self.assertEqual(harness.source.display.groups,
                         (("12:34:00.000", APPROVED_SHORTCUTS),))
        self.assertTrue(all(not row[2].is_set() for row in harness.source.streams.values()))

    def test_hwnd_reuse_after_discovery_is_terminal_and_cancels_all_streams(self):
        harness = self.harness()
        first = harness.readers[0]
        starting_epoch = harness.source.event_epoch
        first_worker = harness.source.streams[APPROVED_SHORTCUTS[0]][1]
        stream_cancels = tuple(
            row[2] for row in harness.source.streams.values()
        )
        first.native.current = replace(harness.identities[0], created=999)
        first.submit(
            ClockSample(harness.identities[0], UTC_0434_MS, harness.now, 1, 1, 1000, "e2e"),
            harness.now,
        )
        first_worker.join(2)
        self.assertGreater(harness.source.event_epoch, starting_epoch)
        self.assertTrue(harness.source.source_faulted)
        self.assertFalse(harness.source.streams)
        self.assertTrue(all(cancel.is_set() for cancel in stream_cancels))
        harness.source.poll()
        self.assertEqual(harness.source.status, "來源失效")
        self.assertFalse(harness.source.anchors)

    def test_checked_order_ttl_qpc_rollback_and_ui_stall_fail_closed(self):
        for fault in ("checked_reverse", "checked_expired", "qpc_reverse", "ui_stall"):
            with self.subTest(fault=fault):
                harness = MultiSourceHarness().start()
                try:
                    for _ in range(3):
                        harness.round(UTC_0434_MS)
                    if fault == "checked_reverse":
                        harness.round(UTC_0434_MS, checked_ns=harness.now - 1)
                    elif fault == "checked_expired":
                        harness.round(UTC_0434_MS,
                                      checked_ns=harness.now - FRESHNESS_TTL_NS - 1)
                    elif fault == "qpc_reverse":
                        harness.now -= 1
                        harness.source.poll()
                    else:
                        harness.now += FRESHNESS_TTL_NS + 1
                        harness.source.poll()
                    self.assertEqual(harness.source.status, "來源失效")
                    self.assertFalse(harness.source.anchors)
                finally:
                    harness.close()

    def test_out_of_band_overflow_and_old_epoch_events_cannot_resurrect(self):
        harness = self.harness()
        old_epoch = harness.source.event_epoch
        old_queue = harness.source.events
        while old_queue.qsize() < EVENT_QUEUE_SIZE:
            old_queue.put_nowait((old_epoch, "noise", (), harness.now))
        self.assertFalse(harness.source._emit("noise", (), harness.now, old_epoch))
        self.assertTrue(harness.source.overflow_fault.is_set())
        harness.source.poll()
        self.assertGreater(harness.source.event_epoch, old_epoch)
        stale_identity = harness.identities[0]
        old_queue.put_nowait((
            old_epoch, "progress",
            (APPROVED_SHORTCUTS[0], 1, stale_identity,
             ClockSample(stale_identity, UTC_0434_MS, harness.now, 1, 1, 1000, "e2e"), False),
            harness.now,
        )) if not old_queue.full() else None
        self.assertFalse(harness.source._emit("noise", (), harness.now, old_epoch))
        harness.source.poll()
        self.assertFalse(harness.source.anchors)
        self.assertEqual(harness.source.status, "來源失效")

    def test_minute_boundary_pauses_then_revalidates_without_cancelling_streams(self):
        harness = self.harness()
        for _ in range(3):
            harness.round(UTC_0434_MS)
        token = harness.source.token
        for label in APPROVED_SHORTCUTS:
            harness.source.stream_started_ns[label] = (
                harness.now - 105_000_000_000 - 1
            )
        prior_anchor_qpc = {
            label: harness.source.anchors[label][1] for label in APPROVED_SHORTCUTS
        }
        for index in range(3):
            harness.round(UTC_0434_MS + 60_000)
            self.assertEqual(harness.source.timed_source_state(), "valid", index)
            self.assertIsNotNone(
                harness.source.timed_action_time_of_day_ms(), index,
            )
        self.assertTrue(harness.source.revalidating)
        self.assertEqual(set(harness.source.anchors), set(APPROVED_SHORTCUTS))
        self.assertTrue(all(
            harness.source.anchors[label][1] >= prior_anchor_qpc[label]
            for label in APPROVED_SHORTCUTS
        ))
        self.assertEqual(harness.source.token, token)
        self.assertTrue(all(not row[2].is_set() for row in harness.source.streams.values()))
        for _ in range(3):
            harness.round(UTC_0434_MS + 60_000)
        self.assertFalse(harness.source.revalidating)
        self.assertEqual(harness.source.timed_source_state(), "valid")
        self.assertEqual(harness.source.exact_time_of_day_ms(), 45_300_000)
        self.assertEqual(harness.source.token, token)


class UiAndTimedIntegrationTests(unittest.TestCase):
    @staticmethod
    def timed_harness():
        tree = ast.parse(FLASH_SYNC.read_text(encoding="utf-8"))
        source_class = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                            and node.name == "FlashSyncApp")
        names = {"_set_timed_semantic", "_timed_target_remaining_ms",
                 "_timed_monotonic_ns", "_timed_signed_delta_ms",
                 "_initialize_timed_deadline", "_stop_timed_click_invalid",
                 "enable_timed_click", "poll_timed_click"}
        methods = [node for node in source_class.body
                   if isinstance(node, ast.FunctionDef) and node.name in names]
        namespace = {
            "DAY_MS": 86_400_000, "FRESHNESS_TTL_NS": FRESHNESS_TTL_NS,
            "TIMED_BACKWARD_CORRECTION_MAX_MS": 100,
            "TIMED_STATUS_ENABLED": "定時按下：已啟用",
            "TIMED_STATUS_WAITING": "定時按下：等待目標時間",
            "TIMED_STATUS_TRIGGERED": "定時按下：已觸發",
            "TIMED_STATUS_SOURCE_INVALID": "定時按下：來源失效",
            "TIMED_STATUS_VALUES": frozenset({
                "定時按下：已啟用", "定時按下：等待目標時間",
                "定時按下：已觸發", "定時按下：來源失效",
            }),
            "messagebox": MagicMock(),
            "time": time,
        }
        node = ast.ClassDef(name="TimedHarness", bases=[], keywords=[],
                            decorator_list=[], body=methods)
        exec(compile(ast.fix_missing_locations(ast.Module([node], [])),
                     "timed-harness", "exec"), namespace)
        app = namespace["TimedHarness"]()
        app.timed_click_enabled = Value(True)
        app.timed_click_target_text = Value("00:00")
        app.timed_click_status_text = Value("定時按下：來源失效")
        app.timed_click_hwnd = 101
        app.timed_click_point = (10, 10)
        app.timed_click_fired = False
        app.timed_click_after_id = None
        app.timed_click_remaining_ms = None
        app.timed_click_last_clock_ms = None
        app.timed_click_deadline_ns = None
        app.timed_click_target_ms = None
        app.timed_click_missed_target_ms = None
        app.timed_click_waiting_seen_after_init = False
        app.parse_target_time_ms = lambda _text: 0
        app.timed_click_target_failure = lambda: None
        app.write_log = MagicMock()
        app.schedule_timed_click_poll = MagicMock()
        app.fire_timed_click = MagicMock()
        return app

    def test_actual_toolbar_stringvars_are_deduped_at_set_boundary(self):
        app = Panel()
        app.game_time_source_text = CountingValue("initial")
        app.game_time_text = CountingValue("initial")
        app._game_time_source_payload = "initial"
        app._game_time_text_payload = "initial"
        app._set_game_time_source_text("initial")
        app._set_game_time_text("initial")
        app._set_game_time_source_text("changed")
        app._set_game_time_source_text("changed")
        app._set_game_time_text("changed")
        app._set_game_time_text("changed")
        self.assertEqual(app.game_time_source_text.set_count, 1)
        self.assertEqual(app.game_time_text.set_count, 1)

    def test_short_waiting_before_fixed_deadline_does_not_disable_or_reinitialize(self):
        app = self.timed_harness()
        app.timed_click_remaining_ms = 10_000
        app.timed_click_last_clock_ms = 1_000
        app.timed_click_deadline_ns = 20_000_000_000
        app.timed_click_target_ms = 11_000
        app._timed_monotonic_ns = lambda: 15_000_000_000
        app.timed_action_game_time_ms = lambda: None
        app.game_clock_source = SimpleNamespace(timed_source_state=lambda: "waiting")
        app.poll_timed_click()
        self.assertTrue(app.timed_click_enabled.get())
        self.assertEqual(app.timed_click_deadline_ns, 20_000_000_000)
        self.assertEqual(app.timed_click_status_text.get(), "定時按下：等待目標時間")
        app.schedule_timed_click_poll.assert_called_once_with(250)

    def test_initial_waiting_arms_then_first_valid_initializes_deadline_once(self):
        app = self.timed_harness()
        game_now = [None]
        monotonic_now = [1_000_000_000]
        state = ["waiting"]
        app.parse_target_time_ms = lambda _text: 2_000
        app.timed_action_game_time_ms = lambda: game_now[0]
        app.game_clock_source = SimpleNamespace(timed_source_state=lambda: state[0])
        app._timed_monotonic_ns = lambda: monotonic_now[0]

        app.enable_timed_click()
        self.assertTrue(app.timed_click_enabled.get())
        self.assertIsNone(app.timed_click_remaining_ms)
        self.assertIsNone(app.timed_click_last_clock_ms)
        self.assertIsNone(app.timed_click_deadline_ns)
        app.schedule_timed_click_poll.assert_called_once_with(250)

        app.schedule_timed_click_poll.reset_mock()
        state[0] = "valid"
        game_now[0] = 1_000
        app.poll_timed_click()
        fixed_deadline = monotonic_now[0] + 1_000_000_000
        self.assertEqual(app.timed_click_deadline_ns, fixed_deadline)
        self.assertEqual(app.timed_click_remaining_ms, 1_000)
        app.schedule_timed_click_poll.assert_called_once_with(5)

        app.schedule_timed_click_poll.reset_mock()
        monotonic_now[0] += 200_000_000
        game_now[0] = 1_200
        app.poll_timed_click()
        self.assertEqual(app.timed_click_deadline_ns, fixed_deadline)
        self.assertEqual(app.timed_click_remaining_ms, 800)
        app.schedule_timed_click_poll.assert_called_once_with(5)

    def test_waiting_across_fixed_deadline_is_missed_and_never_catches_up(self):
        app = self.timed_harness()
        game_now = [1_000]
        monotonic_now = [5_000_000_000]
        state = ["valid"]
        app.parse_target_time_ms = lambda _text: 2_000
        app.timed_action_game_time_ms = lambda: game_now[0]
        app.game_clock_source = SimpleNamespace(timed_source_state=lambda: state[0])
        app._timed_monotonic_ns = lambda: monotonic_now[0]
        app.enable_timed_click()
        fixed_deadline = monotonic_now[0] + 1_000_000_000
        self.assertEqual(app.timed_click_deadline_ns, fixed_deadline)

        state[0] = "waiting"
        game_now[0] = None
        monotonic_now[0] = fixed_deadline - 1
        app.poll_timed_click()
        self.assertTrue(app.timed_click_enabled.get())
        monotonic_now[0] = fixed_deadline + 1
        app.poll_timed_click()
        self.assertFalse(app.timed_click_enabled.get())
        self.assertEqual(app.timed_click_missed_target_ms, 2_000)
        app.fire_timed_click.assert_not_called()

        state[0] = "valid"
        game_now[0] = 2_001
        app.poll_timed_click()
        app.fire_timed_click.assert_not_called()

    def test_monotonic_deadline_never_fires_before_qualified_game_crossing(self):
        app = self.timed_harness()
        game_now = [1_000]
        monotonic_now = [5_000_000_000]
        app.parse_target_time_ms = lambda _text: 2_000
        app.timed_action_game_time_ms = lambda: game_now[0]
        app.game_clock_source = SimpleNamespace(timed_source_state=lambda: "valid")
        app._timed_monotonic_ns = lambda: monotonic_now[0]
        app.enable_timed_click()
        old_deadline = app.timed_click_deadline_ns

        monotonic_now[0] = old_deadline + 1
        game_now[0] = 1_900
        app.poll_timed_click()
        app.fire_timed_click.assert_not_called()
        self.assertEqual(app.timed_click_remaining_ms, 100)
        self.assertEqual(
            app.timed_click_deadline_ns,
            monotonic_now[0] + 100_000_000,
        )

        monotonic_now[0] += 50_000_000
        game_now[0] = 2_000
        app.poll_timed_click()
        app.fire_timed_click.assert_called_once_with(2_000, 2_000)

    def test_small_backward_correction_remaps_but_large_jump_fails_closed(self):
        for corrected_now, should_fault in ((950, False), (899, True), (4_001, True)):
            with self.subTest(corrected_now=corrected_now):
                app = self.timed_harness()
                game_now = [1_000]
                monotonic_now = [5_000_000_000]
                app.parse_target_time_ms = lambda _text: 2_000
                app.timed_action_game_time_ms = lambda: game_now[0]
                app.game_clock_source = SimpleNamespace(
                    timed_source_state=lambda: "valid",
                )
                app._timed_monotonic_ns = lambda: monotonic_now[0]
                app.enable_timed_click()
                monotonic_now[0] += 100_000_000
                game_now[0] = corrected_now
                app.poll_timed_click()
                app.fire_timed_click.assert_not_called()
                if should_fault:
                    self.assertFalse(app.timed_click_enabled.get())
                    self.assertEqual(
                        app.timed_click_status_text.get(), "定時按下：來源失效",
                    )
                else:
                    self.assertTrue(app.timed_click_enabled.get())
                    self.assertEqual(app.timed_click_remaining_ms, 1_050)
                    self.assertEqual(
                        app.timed_click_deadline_ns,
                        monotonic_now[0] + 1_050_000_000,
                    )

    def test_qualified_game_crossing_handles_midnight_without_24h_false_forward(self):
        app = self.timed_harness()
        game_now = [86_399_000]
        monotonic_now = [5_000_000_000]
        app.parse_target_time_ms = lambda _text: 500
        app.timed_action_game_time_ms = lambda: game_now[0]
        app.game_clock_source = SimpleNamespace(timed_source_state=lambda: "valid")
        app._timed_monotonic_ns = lambda: monotonic_now[0]
        app.enable_timed_click()
        self.assertEqual(app.timed_click_remaining_ms, 1_500)
        game_now[0] = 0
        monotonic_now[0] += 1_000_000_000
        app.poll_timed_click()
        app.fire_timed_click.assert_not_called()
        self.assertEqual(app.timed_click_remaining_ms, 500)
        game_now[0] = 500
        monotonic_now[0] += 500_000_000
        app.poll_timed_click()
        app.fire_timed_click.assert_called_once_with(500, 500)

    def test_earlier_time_of_day_waits_for_cross_midnight_transition(self):
        app = self.timed_harness()
        current = [86_399_000]
        app.timed_action_game_time_ms = lambda: current[0]
        app.game_clock_source = SimpleNamespace(timed_source_state=lambda: "valid")
        app.enable_timed_click()
        self.assertEqual(app.timed_click_remaining_ms, 1_000)
        app.fire_timed_click.assert_not_called()
        current[0] = 0
        app.poll_timed_click()
        app.fire_timed_click.assert_called_once_with(0, 0)


class ScanCoordinatorTests(unittest.TestCase):
    def test_default_readers_share_one_module_coordinator(self):
        first = GameClockReader(native=MagicMock())
        second = GameClockReader(native=MagicMock())
        self.assertIs(first.scan_coordinator, second.scan_coordinator)

    def test_full_scans_are_serialized_spaced_and_wait_is_cancellable(self):
        now = [0]
        coordinator = FullScanCoordinator(
            monotonic_ns=lambda: now[0], interval_ns=FULL_SCAN_INTERVAL_NS,
            wait_slice_seconds=0.005,
        )
        entered_first = threading.Event()
        entered_second = threading.Event()
        release_first = threading.Event()
        cancel_first = threading.Event()
        cancel_second = threading.Event()
        starts = []
        errors = []

        def first_operation():
            starts.append(now[0])
            entered_first.set()
            release_first.wait(2)

        def second_operation():
            starts.append(now[0])
            entered_second.set()

        first = threading.Thread(
            target=lambda: coordinator.run(cancel_first, first_operation), daemon=True,
        )
        second = threading.Thread(
            target=lambda: coordinator.run(cancel_second, second_operation), daemon=True,
        )
        first.start()
        self.assertTrue(entered_first.wait(2))
        second.start()
        self.assertFalse(entered_second.wait(0.05))
        release_first.set()
        first.join(2)
        self.assertFalse(entered_second.wait(0.05))
        now[0] = FULL_SCAN_INTERVAL_NS
        self.assertTrue(entered_second.wait(2))
        second.join(2)
        self.assertEqual(starts, [0, FULL_SCAN_INTERVAL_NS])

        now[0] += 1
        cancelled = threading.Event()
        waiter_cancel = threading.Event()

        def wait_for_slot():
            try:
                coordinator.run(waiter_cancel, lambda: None)
            except AcquisitionError as exc:
                errors.append(str(exc))
            finally:
                cancelled.set()

        waiter = threading.Thread(target=wait_for_slot, daemon=True)
        waiter.start()
        waiter_cancel.set()
        self.assertTrue(cancelled.wait(2))
        waiter.join(2)
        self.assertEqual(errors, ["來源發現已取消"])

    def test_waiting_reader_heartbeats_keep_multi_source_fresh_without_early_scan(self):
        now = [0]
        coordinator = FullScanCoordinator(
            monotonic_ns=lambda: now[0], interval_ns=FULL_SCAN_INTERVAL_NS,
            wait_slice_seconds=0.002,
        )
        blocker_entered = threading.Event()
        release_blocker = threading.Event()
        blocker_cancel = threading.Event()

        def block_scan():
            blocker_entered.set()
            if not release_blocker.wait(2):
                raise AssertionError("coordinator blocker was not released")

        blocker = threading.Thread(
            target=lambda: coordinator.run(blocker_cancel, block_scan), daemon=True,
        )
        blocker.start()
        self.assertTrue(blocker_entered.wait(2))

        source_id = SourceIdentity(
            101, 201, 301, 401, "launch:120古", "A" * 64,
            os.path.normcase(os.path.abspath("GameLoader.exe")),
        )

        class WaitingNative:
            def __init__(self):
                self.identity_checks = []

            def identity(self, hwnd):
                self.identity_checks.append((hwnd, now[0]))
                return source_id

            @staticmethod
            def open(_pid):
                return 99

            @staticmethod
            def close(_handle):
                return None

        class WaitingReader(GameClockReader):
            def __init__(self):
                self.native_probe = WaitingNative()
                super().__init__(
                    native=self.native_probe, monotonic_ns=lambda: now[0],
                    scan_coordinator=coordinator,
                )
                self.waiting = threading.Event()
                self.wait_progress = threading.Event()
                self.scan_started = threading.Event()

            def full_scan(self, handle, cancel):
                self.waiting.set()
                return super().full_scan(handle, cancel)

            def _read_progress(self):
                super()._read_progress()
                self.wait_progress.set()

            def scan(self, _handle, _cancel):
                self.scan_started.set()
                return Candidate(0x1000, 0x2000)

        reader = WaitingReader()
        model = MultiGameClockSource(
            lambda: reader, lambda _cancel: (), monotonic_ns=lambda: now[0],
            thread_factory=threading.Thread,
        )
        label = APPROVED_SHORTCUTS[0]
        starting_epoch = model.event_epoch
        model._start_stream(label, source_id)
        self.assertTrue(reader.waiting.wait(2))
        model.source_faulted = False
        model.scheduler.last_discovery = now[0]
        model.poll()
        checked = []
        try:
            for instant in (1_000_000_000, 2_000_000_000, HEALTH_MAX_GAP_NS + 1):
                reader.wait_progress.clear()
                now[0] = instant
                reader.wait_progress.wait(0.1)
                model.poll()
                checked.append(model.last_checked_ns.get(label))

            self.assertEqual(model.event_epoch, starting_epoch)
            self.assertIn(label, model.streams)
            self.assertFalse(model.streams[label][2].is_set())
            self.assertFalse(reader.scan_started.is_set())
            self.assertEqual(checked, [1_000_000_000, 2_000_000_000, HEALTH_MAX_GAP_NS + 1])
            self.assertTrue(all(b - a < HEALTH_MAX_GAP_NS
                                for a, b in zip(checked, checked[1:])))
            self.assertGreaterEqual(len(reader.native_probe.identity_checks), len(checked) + 1)
            self.assertEqual(model.timed_source_state(), "waiting")
        finally:
            model.invalidate("來源失效")
            blocker_cancel.set()
            release_blocker.set()
            blocker.join(2)
            for worker in list(model._retired_workers):
                worker.join(2)

    def test_four_waiters_are_fifo_bounded_and_scan_timeout_budget_has_margin(self):
        self.assertGreaterEqual(
            FIRST_SAMPLE_TIMEOUT_NS,
            len(APPROVED_SHORTCUTS) * FULL_SCAN_DEADLINE_NS + 30_000_000_000,
        )
        now = [0]
        coordinator = FullScanCoordinator(
            monotonic_ns=lambda: now[0], interval_ns=0, wait_slice_seconds=0.002,
        )
        release_first = threading.Event()
        first_entered = threading.Event()
        queued = [threading.Event() for _ in range(4)]
        order = []

        def run(index):
            def operation():
                order.append(index)
                if index == 0:
                    first_entered.set()
                    release_first.wait(2)
            coordinator.run(
                threading.Event(), operation,
                wait_progress=lambda: queued[index].set(),
            )

        workers = [threading.Thread(target=run, args=(index,), daemon=True)
                   for index in range(4)]
        workers[0].start()
        self.assertTrue(first_entered.wait(2))
        for index in range(1, 4):
            workers[index].start()
            self.assertTrue(queued[index].wait(2))
        release_first.set()
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(order, [0, 1, 2, 3])


class AssetAndApiFailClosedTests(unittest.TestCase):
    @staticmethod
    def stage_without(relative_path):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        payload = json.loads((HERE / "RUNTIME_ASSET_MANIFEST.json").read_text(encoding="utf-8"))
        shutil.copy2(HERE / "RUNTIME_ASSET_MANIFEST.json", root / "RUNTIME_ASSET_MANIFEST.json")
        for item in payload["assets"] + payload["required_files"]:
            if item["path"] == relative_path:
                continue
            destination = root.joinpath(*Path(item["path"]).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(HERE.joinpath(*Path(item["path"]).parts), destination)
        return temporary, root

    def test_source_assets_pass_and_each_required_category_missing_fails(self):
        verified = verify_runtime_assets(HERE)
        self.assertEqual(verified, {"ok": True, "asset_count": 82, "reason": ""})
        payload = json.loads((HERE / "RUNTIME_ASSET_MANIFEST.json").read_text(encoding="utf-8"))
        for prefix in ("templates/", "manor_assets/", "fishing_evidence/"):
            missing = next(item["path"] for item in payload["assets"]
                           if item["path"].startswith(prefix))
            with self.subTest(prefix=prefix, missing=missing):
                temporary, root = self.stage_without(missing)
                try:
                    self.assertEqual(verify_runtime_assets(root)["reason"],
                                     "asset_missing_or_size")
                finally:
                    temporary.cleanup()

    def test_api_shape_baseline_missing_or_corrupt_is_a_hard_failure(self):
        import verify_v02_api_boundaries as verifier
        tree = verifier.parse_source(FLASH_SYNC)
        index = verifier.literal_assignment(tree, "MODULE_API_METHOD_INDEX_V02")
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing.json"
            errors, digest = verifier.validate_api_shape_baseline(tree, index, missing)
            self.assertTrue(errors)
            self.assertEqual(digest, "")
            corrupt = Path(temporary) / "corrupt.json"
            corrupt.write_text("{}", encoding="utf-8")
            errors, digest = verifier.validate_api_shape_baseline(tree, index, corrupt)
            self.assertTrue(errors)
            self.assertEqual(digest, "")


if __name__ == "__main__":
    unittest.main()
