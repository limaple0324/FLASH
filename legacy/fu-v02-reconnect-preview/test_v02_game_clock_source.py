"""Deterministic source scheduling: no GUI, live process, RPM, or user settings."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import MagicMock

from v02_game_clock import ClockSample, GameClock, SourceIdentity, HEALTH_MAX_GAP_NS
from v02_game_clock_reader import AcquisitionError
from v02_game_clock_source import (AutoClockSource, SourceToken, enumerate_source_windows,
                                  DISCOVERY_INTERVAL_NS, FAILED_SOURCE_BACKOFF_NS,
                                  FIRST_SAMPLE_TIMEOUT_NS, RESULT_QUEUE_SIZE)

IDENTITY = SourceIdentity(11, 22, 33, 44, "session-hmac", "player-sha")
SECOND = SourceIdentity(12, 23, 34, 45, "session-hmac-2", "player-sha")
EPOCH = 1_787_700_000_000.0


class DeferredThread:
    """Explicit execution and release make UI/worker ownership testable."""
    def __init__(self, *, target, name, daemon):
        self.target, self.name, self.daemon = target, name, daemon
        self.alive = False

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def run(self):
        try:
            self.target()
        finally:
            self.alive = False


class SourceHarness:
    def __init__(self, identities=(IDENTITY, SECOND)):
        self.qpc = 1_000_000_000
        self.identities = {item.hwnd: item for item in identities}
        self.windows = list(self.identities)
        self.threads = []
        self.reader = SimpleNamespace(native=SimpleNamespace(identity=MagicMock(side_effect=self.identity)),
                                      stream=MagicMock())
        self.discover = MagicMock(side_effect=lambda _cancel: self.windows)
        self.clock = GameClock(lambda: self.qpc)
        self.source = AutoClockSource(self.clock, self.reader, self.discover,
                                     monotonic_ns=lambda: self.qpc, thread_factory=self.thread)

    def identity(self, hwnd):
        if hwnd not in self.identities:
            raise AcquisitionError("來源視窗已失效")
        return self.identities[hwnd]

    def thread(self, **kwargs):
        assert not any(item.alive for item in self.threads), "parallel reader/discovery"
        worker = DeferredThread(**kwargs)
        self.threads.append(worker)
        return worker

    def begin(self):
        self.source.poll()
        self.threads[-1].run()
        self.source.poll()
        return self.source.token

    def sample(self, **changes):
        return replace(ClockSample(self.source.token.identity or IDENTITY, EPOCH,
                                   self.qpc, 2_000_000, 30_000, 10_000, "tested-profile"), **changes)

    def publish(self, item=None, reason="", token=None, checked=None):
        self.source.results.put_nowait((token or self.source.token, "progress", (item, reason),
                                       self.qpc if checked is None else checked))

    def step(self, ns=1_000_000_000, *, heartbeat=True):
        self.qpc += ns
        if heartbeat and self.source.token.identity is not None:
            self.publish()
        self.source.poll()


class EnumerationTests(unittest.TestCase):
    def test_empty_success_failure_callback_error_and_cancel_are_distinct(self):
        user = MagicMock()
        user.EnumWindows.return_value = 1
        cancel = threading.Event()
        self.assertEqual(enumerate_source_windows(user, lambda f: f, lambda _: True, cancel), ())
        user.EnumWindows.return_value = 0
        with self.assertRaisesRegex(AcquisitionError, "列舉失敗"):
            enumerate_source_windows(user, lambda f: f, lambda _: True, cancel)
        user.EnumWindows.side_effect = lambda callback, _: callback(11, 0)
        def bad(_hwnd):
            raise OSError("private path must not escape")
        with self.assertRaisesRegex(AcquisitionError, "列舉失敗"):
            enumerate_source_windows(user, lambda f: f, bad, cancel)
        cancel.set()
        with self.assertRaisesRegex(AcquisitionError, "取消"):
            enumerate_source_windows(user, lambda f: f, lambda _: True, cancel)

    def test_only_open_visible_flash_and_hwnd_dedup(self):
        user = MagicMock()
        user.IsWindow.side_effect = lambda hwnd: hwnd != 4
        user.IsWindowVisible.side_effect = lambda hwnd: hwnd != 3
        def enum(callback, _):
            for hwnd in (1, 1, 2, 3, 4):
                self.assertTrue(callback(hwnd, 0))
            return 1
        user.EnumWindows.side_effect = enum
        found = enumerate_source_windows(user, lambda f: f, lambda hwnd: hwnd != 2, threading.Event())
        self.assertEqual(found, (1,))


class AutoSourceTests(unittest.TestCase):
    def test_policy_constants_are_waiting_not_reader_accuracy_changes(self):
        self.assertEqual(DISCOVERY_INTERVAL_NS, 5_000_000_000)
        self.assertEqual(FAILED_SOURCE_BACKOFF_NS, 5_000_000_000)
        self.assertEqual(FIRST_SAMPLE_TIMEOUT_NS, 105_000_000_000)
        self.assertEqual(RESULT_QUEUE_SIZE, 8)

    def test_empty_waits_throttled_then_late_client_is_found_without_selection(self):
        h = SourceHarness(())
        h.begin()
        self.assertIsNone(h.clock.text())
        self.assertIn("尚無", h.source.status)
        for _ in range(4):
            h.step(heartbeat=False)
        self.assertEqual(len(h.threads), 1)
        h.identities[11] = IDENTITY
        h.windows = [11]
        h.step(heartbeat=False)
        self.assertEqual(len(h.threads), 2)
        self.assertEqual(h.discover.call_count, 1)  # job not run on UI
        h.threads[-1].run()
        h.source.poll()
        self.assertEqual(h.source.token.identity, IDENTITY)

    def test_discovery_error_is_not_empty_and_keeps_retry_throttle(self):
        for error in (OSError("private"), AcquisitionError("來源視窗列舉失敗")):
            h = SourceHarness(())
            h.discover.side_effect = error
            h.begin()
            self.assertIn("失敗", h.source.status)
            self.assertNotIn("尚無", h.source.status)
            h.step(heartbeat=False)
            self.assertEqual(len(h.threads), 1)

    def test_rejected_identity_does_not_claim_successful_empty_and_next_candidate_wins(self):
        h = SourceHarness((SECOND,))
        h.windows = [999, SECOND.hwnd]
        h.begin()
        self.assertEqual(h.source.token.identity, SECOND)
        h = SourceHarness(())
        h.windows = [999]
        h.begin()
        self.assertIn("未通過來源身分驗證", h.source.status)

    def test_deduplicate_hwnd_and_same_process_without_duplicate_stream_scan(self):
        other_hwnd = replace(IDENTITY, hwnd=100)
        h = SourceHarness((IDENTITY, other_hwnd, SECOND))
        h.windows = [11, 11, 100, 12]
        h.begin()
        self.assertEqual(h.reader.native.identity.call_count, 3)
        self.assertEqual(h.source.candidates, (IDENTITY, SECOND))
        h.publish(reason="invalid object")
        h.source.poll()
        h.threads[-1].alive = False
        h.source.poll()
        self.assertEqual(h.source.token.identity, SECOND)

    def test_healthy_source_stays_fixed_continuously_despite_candidate_order_changes(self):
        h = SourceHarness()
        token = h.begin()
        h.publish(h.sample())
        h.source.poll()
        for count in range(1, 131):
            h.qpc += 1_000_000_000
            h.windows.reverse()
            h.publish(h.sample(server_ms=EPOCH + count * 1000) if count % 10 == 0 else None)
            h.source.poll()
            self.assertEqual(h.source.token, token)
            self.assertIsNotNone(h.clock.text())
        self.assertEqual(h.discover.call_count, 1)
        self.assertEqual(len(h.threads), 2)

    def test_failed_first_candidate_invalidates_before_next_and_waits_for_release(self):
        h = SourceHarness()
        old = h.begin()
        h.publish(h.sample())
        h.source.poll()
        h.publish(reason="RPM failed")
        h.source.poll()
        self.assertIsNone(h.clock.text())
        self.assertNotEqual(h.source.token, old)
        self.assertTrue(h.source.cancel.is_set())
        self.assertEqual(len(h.threads), 2)
        h.source.poll()
        self.assertEqual(len(h.threads), 2)
        h.threads[-1].alive = False
        h.source.poll()
        self.assertEqual(h.source.token.identity, SECOND)

    def test_heartbeat_only_deadline_is_fixed_and_next_untried_client_gets_turn(self):
        h = SourceHarness()
        token = h.begin()
        deadline = h.source.first_sample_deadline_ns
        for _ in range(104):
            h.step()
            self.assertEqual(h.source.token, token)
            self.assertEqual(h.source.first_sample_deadline_ns, deadline)
        h.step()
        self.assertIn("初次有效校時樣本逾時", h.source.status)
        self.assertIsNone(h.clock.text())
        h.threads[-1].alive = False
        h.source.poll()
        self.assertEqual(h.source.token.identity, SECOND)

    def test_legal_long_scan_progress_survives_three_seconds_but_no_fake_health(self):
        h = SourceHarness()
        token = h.begin()
        for _ in range(90):
            h.step()
        self.assertEqual(h.source.token, token)
        h.publish(h.sample())
        h.source.poll()
        self.assertIsNotNone(h.clock.text())
        for _ in range(4):
            h.step(heartbeat=False)
        self.assertIsNone(h.clock.text())
        self.assertIn("中斷", h.source.status)

    def test_backoff_and_fair_retry_never_let_first_bad_client_starve_second(self):
        h = SourceHarness()
        h.begin()
        for expected in (IDENTITY, SECOND):
            self.assertEqual(h.source.token.identity, expected)
            h.publish(reason="invalid object")
            h.source.poll()
            h.threads[-1].alive = False
            h.source.poll()
        self.assertIsNone(h.source.token.identity)
        self.assertEqual(len(h.threads), 3)
        for _ in range(4):
            h.step(heartbeat=False)
        self.assertEqual(len(h.threads), 3)
        h.step(heartbeat=False)
        self.assertEqual(h.source.worker_kind, "discover")
        h.threads[-1].run()
        h.source.poll()
        self.assertEqual(h.source.token.identity, IDENTITY)

    def test_close_and_pid_hwnd_reuse_fence_all_identity_fields(self):
        for field in ("hwnd", "pid", "tid", "created", "launch_fingerprint", "image_sha256"):
            h = SourceHarness()
            h.begin()
            value = "different" if field in ("launch_fingerprint", "image_sha256") else 900
            h.publish(h.sample(identity=replace(IDENTITY, **{field: value})))
            h.source.poll()
            self.assertIsNone(h.clock.text(), field)
            self.assertIn("身分不符", h.source.status)

    def test_identity_rechecked_before_stream_and_before_heartbeat(self):
        for during_stream in (False, True):
            h = SourceHarness()
            h.begin()
            reused = replace(IDENTITY, created=100)
            if during_stream:
                def stream(hwnd, cancel, publish):
                    h.identities[11] = reused
                    publish(None, "", h.qpc)
                h.reader.stream.side_effect = stream
            else:
                h.identities[11] = reused
            h.threads[-1].run()
            h.source.poll()
            self.assertIsNone(h.clock.text())
            self.assertEqual(h.source.token.identity, SECOND)
            if not during_stream:
                h.reader.stream.assert_not_called()

    def test_stale_generation_heartbeat_or_sample_cannot_revive_new_source(self):
        h = SourceHarness()
        old = h.begin()
        h.publish(reason="failure")
        h.source.poll()
        h.threads[-1].alive = False
        h.source.poll()
        current = h.source.token
        h.qpc += 1
        h.publish(h.sample(identity=IDENTITY), token=old)
        h.publish(token=old, checked=h.qpc + 999)
        h.source.poll()
        self.assertEqual(h.source.token, current)
        self.assertIsNone(h.clock.sample)
        h.publish(h.sample())
        h.source.poll()
        self.assertEqual(h.clock.sample.identity, SECOND)

    def test_error_cancel_suspend_rollback_and_old_health_precede_queue_drain(self):
        for fault in ("error", "cancel", "sleep", "old-health", "rollback"):
            h = SourceHarness()
            token = h.begin()
            h.publish(h.sample())
            h.source.poll()
            if fault == "error":
                h.publish(reason="read failed")
            elif fault == "cancel":
                h.source.cancel.set()
            elif fault == "rollback":
                h.qpc -= 1
            else:
                h.qpc += HEALTH_MAX_GAP_NS + 1
                if fault == "old-health":
                    h.source.last_poll_ns = h.qpc
            h.publish(h.sample(server_ms=EPOCH + 1000))
            h.source.poll()
            self.assertIsNone(h.clock.sample, fault)
            self.assertNotEqual(h.source.token, token)

    def test_expired_sample_with_healthy_progress_invalidates_and_does_not_repeat(self):
        h = SourceHarness()
        old = h.begin()
        h.publish(h.sample())
        h.source.poll()
        for _ in range(31):
            h.step()
        generation = h.source.generation
        self.assertIsNone(h.clock.sample)
        self.assertIn("過期", h.source.status)
        for _ in range(35):
            h.qpc += 1_000_000_000
            h.publish(token=old)
            h.source.poll()
        self.assertEqual(h.source.generation, generation)

    def test_consumer_first_model_fault_fences_old_events_once(self):
        for fault in ("expired", "gap", "rollback"):
            h = SourceHarness()
            old = h.begin()
            h.publish(h.sample())
            h.source.poll()
            if fault == "expired":
                for _ in range(30):
                    h.step()
                h.qpc += 1
            elif fault == "gap":
                h.qpc += HEALTH_MAX_GAP_NS + 1
            else:
                h.qpc -= 1
            h.source.last_poll_ns = h.source.health_ns = h.qpc
            self.assertIsNone(h.clock.time_of_day_ms())
            h.publish(h.sample(server_ms=EPOCH + 1000), token=old)
            h.source.poll()
            generation = h.source.generation
            self.assertEqual(h.source.invalidation_seen, 1)
            self.assertIsNone(h.clock.sample)
            for _ in range(35):
                h.qpc += 1_000_000_000
                h.publish(token=old)
                h.source.poll()
            self.assertEqual(h.source.generation, generation)

    def test_large_correction_needs_new_generation_and_fresh_edge(self):
        h = SourceHarness((IDENTITY,))
        old = h.begin()
        h.publish(h.sample())
        h.source.poll()
        h.qpc += 1_000_000_000
        h.publish(h.sample(server_ms=EPOCH + 1251))
        h.source.poll()
        self.assertIsNone(h.clock.sample)
        generation = h.source.generation
        for _ in range(6):
            h.step(heartbeat=False)
            h.publish(h.sample(server_ms=EPOCH + 1251), token=old)
            h.source.poll()
        self.assertEqual(h.source.generation, generation)
        h.threads[-1].alive = False
        h.source.poll()
        h.threads[-1].run()  # rediscovery
        h.source.poll()
        h.qpc += 1
        fresh = h.sample(server_ms=EPOCH + 8000)
        h.publish(fresh)
        h.source.poll()
        self.assertIs(h.clock.sample, fresh)
        self.assertNotEqual(h.source.token, old)

    def test_queue_full_cancels_without_overwriting_error_or_running_another_worker(self):
        h = SourceHarness()
        h.begin()
        def fill(hwnd, cancel, publish):
            publish(h.sample(), "", h.qpc)
            publish(None, "failure", h.qpc)
            for _ in range(20):
                publish(None, "", h.qpc)
        h.reader.stream.side_effect = fill
        h.threads[-1].run()
        self.assertEqual(h.source.results.qsize(), RESULT_QUEUE_SIZE)
        self.assertEqual(list(h.source.results.queue)[1][2][1], "failure")
        self.assertTrue(h.source.cancel.is_set())
        h.source.poll()
        self.assertIsNone(h.clock.sample)
        self.assertEqual(h.source.token.identity, SECOND)

    def test_natural_end_and_unexpected_exception_are_terminal_and_private(self):
        for error in (None, OSError("secret launch arguments and path")):
            h = SourceHarness()
            h.begin()
            h.reader.stream.side_effect = error
            h.threads[-1].run()
            queued = list(h.source.results.queue)
            self.assertNotIn("secret", str(queued))
            self.assertEqual(queued[-1][1], "error")
            h.source.poll()
            self.assertEqual(h.source.token.identity, SECOND)

    def test_shutdown_discovery_and_stream_cancels_fences_and_never_restarts(self):
        for discovery in (True, False):
            h = SourceHarness()
            if discovery:
                h.source.poll()
            else:
                h.begin()
                h.publish(h.sample())
                h.source.poll()
            old = h.source.token
            h.source.shutdown()
            generation = h.source.generation
            self.assertTrue(h.source.is_busy())
            self.assertTrue(h.source.cancel.is_set())
            self.assertIsNone(h.clock.sample)
            h.publish(h.sample(), token=old)
            h.source.shutdown()
            h.source.poll()
            h.threads[-1].run()
            h.source.poll()
            self.assertFalse(h.source.is_busy())
            self.assertEqual(h.source.generation, generation)
            self.assertIsNone(h.clock.sample)

    def test_expired_future_and_out_of_order_progress_cannot_extend_health(self):
        for offset in (-HEALTH_MAX_GAP_NS - 1, 1, -1):
            h = SourceHarness()
            h.begin()
            h.publish(h.sample())
            h.source.poll()
            h.publish(checked=h.qpc + offset)
            h.source.poll()
            self.assertIsNone(h.clock.sample, offset)
            self.assertIn("過期或逆序", h.source.status)

    def test_discovery_taking_over_five_seconds_does_not_loop_without_attempt(self):
        h = SourceHarness((IDENTITY,))
        def slow_discover(_cancel):
            for _ in range(6):
                h.step(heartbeat=False)
            return h.windows
        h.discover.side_effect = slow_discover
        h.begin()
        self.assertEqual(h.source.token.identity, IDENTITY)
        self.assertEqual(h.discover.call_count, 1)

    def test_real_worker_cancellation_waits_for_finally_before_reusing_reader(self):
        h = SourceHarness()
        entered, in_finally, release = threading.Event(), threading.Event(), threading.Event()
        started = []
        def stream(hwnd, cancel, publish):
            started.append(hwnd)
            entered.set()
            try:
                cancel.wait(2)
            finally:
                in_finally.set()
                release.wait(2)
        h.reader.stream.side_effect = stream
        h.source.thread_factory = threading.Thread
        source = h.source
        try:
            source.poll()
            source.worker.join(2)
            self.assertFalse(source.worker.is_alive())
            source.poll()
            self.assertTrue(entered.wait(2))
            old = source.worker
            source.invalidate("synthetic failure")
            self.assertTrue(in_finally.wait(2))
            for _ in range(20):
                source.poll()
                self.assertIs(source.worker, old)
            self.assertEqual(started, [11])
            release.set()
            old.join(2)
            self.assertFalse(old.is_alive())
            entered.clear()
            source.poll()
            self.assertTrue(entered.wait(2))
            self.assertEqual(started, [11, 12])
        finally:
            source.shutdown()
            release.set()
            if source.worker is not None:
                source.worker.join(3)
                self.assertFalse(source.worker.is_alive())


if __name__ == "__main__":
    unittest.main()
