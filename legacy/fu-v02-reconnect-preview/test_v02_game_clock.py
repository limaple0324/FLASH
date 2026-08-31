"""Scoped deterministic clock/source/panel contracts; no game or user config input."""
from __future__ import annotations

import ast
import hashlib
import inspect
from dataclasses import replace
from pathlib import Path
import queue
import struct
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from v02_game_clock import (ClockSample, GameClock, SourceIdentity, HEALTH_MAX_GAP_NS,
                            SAMPLE_MAX_AGE_NS, MAX_SLEW_ERROR_MS, SLEW_MS_PER_SECOND)
from v02_game_clock_reader import (
    ADAPTIVE_BURST_HARD_MAX_NS, ADAPTIVE_BURST_SAMPLE_MAX_NS,
    AcquisitionError, BURST_OBSERVATION_SLACK_NS, BURST_POLL_INTERVAL_NS,
    Candidate, EDGE_RESPONSE_MAX_MS, GameClockReader, EdgeTracker,
    NonTargetObject, Observation, PLAYER_SHA256,
    PREDICTIVE_REQUEST_WINDOW_MAX_NS, predict_burst_window, validate_values,
)
from v02_faithful_game_time import APPROVED_SHORTCUTS, MultiGameClockSource
from v02_game_clock_bar import ClockBar
from test_flash_sync_status_window import Panel, Value, Group, NAMESPACE

IDENTITY = SourceIdentity(11, 22, 33, 44, "session-hmac", PLAYER_SHA256)
EPOCH = 1_787_700_000_000.0
SOURCE = Path(__file__).with_name("flash_sync_v02.py")


def sample(**changes):
    return replace(ClockSample(IDENTITY, EPOCH, 1_000_000_000, 2_000_000, 30_000,
                               10_000, "tested-profile"), **changes)


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_000_000_000
        self.clock = GameClock(lambda: self.now)

    def test_no_sample_no_time_and_duplicate_rejected(self):
        self.assertIsNone(self.clock.utc_ms())
        self.assertIsNone(self.clock.time_of_day_ms())
        self.assertIsNone(self.clock.text())
        self.assertTrue(self.clock.calibrate_once(sample()))
        self.assertFalse(self.clock.calibrate_once(sample(server_ms=EPOCH + 50_000)))
        self.now += 123_456_789
        self.assertEqual(self.clock.utc_ms(), EPOCH + 123)
        self.assertEqual(self.clock.sample.identity, IDENTITY)

    def test_wall_jump_and_local_timezone_cannot_change_clock(self):
        self.clock.calibrate_once(sample())
        with patch("time.time_ns", return_value=0), patch("time.localtime", side_effect=AssertionError):
            self.now += 3_000_000_000
            self.assertEqual(self.clock.utc_ms(), EPOCH + 3000)

    def test_utc8_rollover_and_monotonic_regression(self):
        midnight_utc = 1_787_760_000_000
        value = midnight_utc - midnight_utc % 86_400_000 + 16 * 3_600_000 - 1
        self.clock.calibrate_once(sample(server_ms=value))
        self.assertEqual(self.clock.text(), "23:59:59.999")
        self.now += 1_000_000
        self.assertEqual(self.clock.text(), "00:00:00.000")
        self.now = 0
        self.assertIsNone(self.clock.utc_ms())

    def test_reject_bad_sample_without_calibrating(self):
        for changes in ({"server_ms": float("nan")}, {"server_ms": float("inf")},
                        {"server_ms": 1.0}, {"anchor_ns": self.now + 1},
                        {"observation_bracket_ns": 25_000_001}, {"read_latency_ns": 20_000_001},
                        {"transition_ms": 0}, {"profile": ""}):
            with self.subTest(changes=changes):
                self.assertFalse(self.clock.calibrate_once(sample(**changes)))
                self.assertIsNone(self.clock.sample)

    def tick(self, milliseconds):
        values = []
        for _ in range(milliseconds // 100):
            self.now += 100_000_000
            values.append(self.clock.utc_ms())
        return values

    def test_positive_and_negative_slew_converge_without_backwards_time(self):
        for error in (-250, -100, 100, 250):
            self.setUp()
            self.assertTrue(self.clock.accept_sample(sample()))
            self.tick(1000)
            before = self.clock.utc_ms()
            self.assertTrue(self.clock.accept_sample(sample(anchor_ns=self.now, server_ms=EPOCH + 1000 + error)))
            self.assertEqual(self.clock.last_correction_ms, error)
            self.assertEqual(self.clock.utc_ms(), before)  # no immediate step
            values = self.tick(5000)
            self.assertTrue(all(a <= b for a, b in zip([before] + values, values)))
            self.assertEqual(values[-1], EPOCH + 6000 + error)
            # A fixed target needs at most |error| / 50 seconds, not arbitrary inputs.
            self.assertEqual(MAX_SLEW_ERROR_MS / SLEW_MS_PER_SECOND, 5)

    def test_new_target_during_slew_recomputes_bounded_correction(self):
        self.clock.accept_sample(sample())
        self.tick(1000)
        self.clock.accept_sample(sample(anchor_ns=self.now, server_ms=EPOCH + 1200))
        self.tick(1000)
        self.assertTrue(self.clock.accept_sample(sample(anchor_ns=self.now, server_ms=EPOCH + 1900)))
        self.assertEqual(self.clock.last_correction_ms, -150)
        self.assertEqual(self.tick(3000)[-1], EPOCH + 4900)

    def test_large_correction_invalidates_and_same_or_older_edge_cannot_revive(self):
        self.clock.accept_sample(sample())
        self.tick(1000)
        bad = sample(anchor_ns=self.now, server_ms=EPOCH + 1251)
        self.assertFalse(self.clock.accept_sample(bad))
        self.assertIsNone(self.clock.utc_ms())
        self.assertFalse(self.clock.accept_sample(bad))
        self.assertFalse(self.clock.accept_sample(sample()))
        self.now += 100_000_000
        self.assertTrue(self.clock.accept_sample(sample(anchor_ns=self.now, server_ms=EPOCH + 1351)))

    def test_freshness_uses_original_anchor_not_receipt_and_expires(self):
        self.clock.accept_sample(sample())
        self.tick(30000)
        self.assertIsNotNone(self.clock.utc_ms())
        self.now += 1
        self.assertIsNone(self.clock.utc_ms())
        self.assertIsNone(self.clock.sample)
        stale = sample(anchor_ns=self.now - SAMPLE_MAX_AGE_NS - 1, server_ms=EPOCH + 1)
        self.assertFalse(self.clock.accept_sample(stale))

    def test_sleep_reentry_qpc_rollback_and_explicit_invalidation_fence(self):
        for action in ("sleep", "rollback", "explicit"):
            self.setUp()
            self.clock.accept_sample(sample())
            if action == "sleep":
                self.now += HEALTH_MAX_GAP_NS + 1
            elif action == "rollback":
                self.now -= 1
            else:
                self.clock.invalidate()
            self.assertIsNone(self.clock.utc_ms())
            self.assertFalse(self.clock.accept_sample(sample()))

    def test_invalidation_version_records_transitions_not_repeated_none_reads(self):
        self.assertEqual(self.clock.invalidation_version, 0)
        for _ in range(3):
            self.assertIsNone(self.clock.utc_ms())
        self.clock.invalidate()
        self.assertEqual(self.clock.invalidation_version, 0)
        self.now += 1
        self.assertTrue(self.clock.accept_sample(sample(anchor_ns=self.now)))
        self.now -= 1
        self.assertIsNone(self.clock.utc_ms())
        self.assertEqual(self.clock.invalidation_version, 1)
        for _ in range(3):
            self.assertIsNone(self.clock.utc_ms())
            self.clock.invalidate()
        self.assertEqual(self.clock.invalidation_version, 1)

    def test_duplicate_out_of_order_and_source_mix_do_not_reanchor(self):
        first = sample()
        self.clock.accept_sample(first)
        self.tick(1000)
        for item in (first, sample(anchor_ns=999_999_999, server_ms=EPOCH + 1000),
                     sample(anchor_ns=self.now, server_ms=EPOCH)):
            self.assertFalse(self.clock.accept_sample(item))
            self.assertIs(self.clock.sample, first)
        self.assertFalse(self.clock.accept_sample(sample(anchor_ns=self.now, server_ms=EPOCH + 1000,
                                                        identity=replace(IDENTITY, created=99))))
        self.assertIsNone(self.clock.sample)

    def test_repeated_samples_reduce_long_term_drift(self):
        self.clock.accept_sample(sample())
        accepted = 0
        for second in range(1, 1801):
            self.tick(1000)
            if second % 10 == 0:
                # Independent synthetic source is 20ppm faster than QPC.
                self.assertTrue(self.clock.accept_sample(sample(
                    anchor_ns=self.now, server_ms=EPOCH + second * 1000.02)))
                accepted += 1
        self.assertEqual(accepted, 180)
        self.assertLess(abs(self.clock.utc_ms() - (EPOCH + 1_800_036)), 2)
        self.assertGreater(abs((EPOCH + 1_800_000) - (EPOCH + 1_800_036)), 30)

    def test_value_filters_are_not_accuracy_claims(self):
        self.assertTrue(validate_values(EPOCH, EPOCH - 1500, -28_800_000, 1470, EPOCH))
        for values in ((float("nan"), EPOCH, -28_800_000, 0, EPOCH),
                       (float("inf"), EPOCH, -28_800_000, 0, EPOCH),
                       (1, EPOCH, -28_800_000, 0, EPOCH),
                       (EPOCH, EPOCH, 0, 0, EPOCH),
                       (EPOCH, EPOCH, -28_800_000, 10001, EPOCH)):
            self.assertFalse(validate_values(*values))
        self.assertTrue(validate_values(EPOCH, EPOCH, -28_800_000, 0, EPOCH + 120001))


class EdgeReader(GameClockReader):
    def __init__(self, observations=None):
        self.qpc = 1_000_000_000
        self.native = MagicMock()
        self.native.identity.return_value = IDENTITY
        self.native.open.return_value = 99
        self.now = lambda: self.qpc
        self.wall = lambda: int(EPOCH * 1_000_000)
        self.scan_count = 0
        self.expected = Candidate(0x1000, 0x2000)
        self.reads = iter(observations or [
            (0, -1000, 970, 1),
            (0, 8970, 970, 1), (0, 8970, 970, 39), (10000, 8970, 990, 1),
            (10000, 18970, 990, 9960), (10000, 18970, 990, 19), (20000, 18970, 1010, 1),
            (20000, 28970, 1010, 9980), (20000, 28970, 1010, 34), (30000, 28970, 995, 1),
        ])

    def scan(self, handle, cancel):
        self.scan_count += 1
        self.qpc += 2_000_000_000  # the expensive scan must NOT replace an edge anchor
        return self.expected

    def observe(self, handle, candidate):
        delta, start, lag, step_ms = next(self.reads)
        self.qpc += step_ms * 1_000_000
        return Observation(EPOCH + delta, EPOCH + start, lag,
                           self.qpc, self.qpc + 10_000)

    def validate_structure(self, handle, candidate):
        pass


class ReaderTests(unittest.TestCase):
    def test_fresh_edges_minimum_interval_selection_and_final_unique_scan(self):
        reader = EdgeReader()
        with patch("v02_game_clock_reader.time.sleep"):
            result = reader.acquire(11, threading.Event())
        self.assertEqual(reader.scan_count, 2)
        self.assertEqual(result.server_ms, EPOCH + 20000)
        self.assertEqual(result.response_interval_ms, 20)
        self.assertLess(result.anchor_ns, reader.qpc - 1_000_000_000)
        self.assertEqual(result.observation_bracket_ns, 1_010_000)
        reader.native.close.assert_called_once_with(99)

    def test_no_initial_raw_calibration_and_no_transition_timeout(self):
        reader = EdgeReader()
        def unchanged(_handle, _candidate):
            reader.qpc += 20_000_000_000
            return Observation(EPOCH, EPOCH - 1000, 1000, reader.qpc, reader.qpc + 1)
        reader.observe = unchanged
        with patch("v02_game_clock_reader.time.sleep"), self.assertRaises(AcquisitionError):
            reader.acquire(11, threading.Event())

    def test_identity_reuse_or_change_failclosed(self):
        for changed in (replace(IDENTITY, pid=99), replace(IDENTITY, tid=99),
                        replace(IDENTITY, created=99), replace(IDENTITY, launch_fingerprint="new"),
                        replace(IDENTITY, hwnd=99)):
            reader = EdgeReader()
            reader.native.identity.side_effect = [IDENTITY, IDENTITY, IDENTITY, changed]
            with patch("v02_game_clock_reader.time.sleep"), self.assertRaises(AcquisitionError):
                reader.acquire(11, threading.Event())
            reader.native.close.assert_called_once()

    def test_source_cancel_and_changed_candidate_at_accept(self):
        reader = EdgeReader()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(AcquisitionError):
            reader.acquire(11, cancel)
        reader = EdgeReader()
        original = reader.scan
        def changed(handle, cancel):
            value = original(handle, cancel)
            return value if reader.scan_count == 1 else Candidate(0x3000, 0x4000)
        reader.scan = changed
        with patch("v02_game_clock_reader.time.sleep"), self.assertRaises(AcquisitionError):
            reader.acquire(11, threading.Event())

    def test_slow_edge_is_not_accepted_and_next_natural_edge_can_win(self):
        reader = EdgeReader()
        original = reader.observe
        count = 0
        def delayed(handle, candidate):
            nonlocal count
            count += 1
            if count == 4:
                reader.qpc += 100_000_000
            return original(handle, candidate)
        reader.observe = delayed
        with patch("v02_game_clock_reader.time.sleep"):
            result = reader.acquire(11, threading.Event())
        self.assertNotEqual(result.server_ms, EPOCH + 10000)
        self.assertLessEqual(result.observation_bracket_ns, 25_000_000)

    def test_overlapping_or_unpaired_response_not_treated_as_low_interval(self):
        reader = EdgeReader([(0, -1000, 970, 1), (0, 8970, 970, 1), (0, 18970, 970, 10000)])
        with patch("v02_game_clock_reader.time.sleep"), self.assertRaisesRegex(AcquisitionError, "重疊"):
            reader.acquire(11, threading.Event())
        # Simultaneously changed fields provide no separately observed request edge.
        reader = EdgeReader([(0, -1000, 970, 1), (10000, 8970, 1020, 10000),
                             (20000, 18970, 1020, 10000), (30000, 28970, 1020, 10000)])
        with patch("v02_game_clock_reader.time.sleep"), self.assertRaises(AcquisitionError):
            reader.acquire(11, threading.Event())

    def test_high_response_interval_cannot_calibrate(self):
        reader = EdgeReader([(0, -1000, 970, 1),
                             (0, 8970, 970, 1), (0, 8970, 970, 200), (10000, 8970, 829, 1),
                             (10000, 18970, 829, 9799), (10000, 18970, 829, 200), (20000, 18970, 829, 1),
                             (20000, 28970, 829, 9799), (20000, 28970, 829, 200), (30000, 28970, 829, 1)])
        with patch("v02_game_clock_reader.time.sleep"), self.assertRaises(AcquisitionError):
            reader.acquire(11, threading.Event())

    def test_pending_request_beyond_ten_seconds_waits_for_real_response(self):
        reader = EdgeReader([
            (0, 0, 0, 1),
            (0, 10012, 0, 10012), (0, 10012, 0, 19), (10032, 10012, 0, 1),
            (10032, 20024, 0, 9992), (10032, 20024, 0, 29), (20054, 20024, 0, 1),
            (20054, 30036, 0, 9982), (20054, 30036, 0, 39), (30076, 30036, 0, 1),
        ])
        scripted = reader.observe
        observations = []

        def read_real_observation(handle, candidate):
            value = scripted(handle, candidate)
            pair = struct.pack("<dd", value.raw_ms, value.start_ms)
            core = struct.pack("<ddd", -28_800_000, 0, value.lag_ms)
            reader.native.read.side_effect = [pair, core, pair]
            actual = GameClockReader.observe(reader, handle, candidate)
            observations.append(actual)
            return actual

        reader.observe = read_real_observation
        with patch("v02_game_clock_reader.time.sleep"):
            result = reader.acquire(11, threading.Event())
        self.assertEqual(observations[1].raw_ms - observations[1].start_ms, -10012)
        self.assertFalse(validate_values(EPOCH, EPOCH + 10012, -28_800_000, 0, EPOCH))
        self.assertEqual(result.server_ms, EPOCH + 10032)
        self.assertEqual(result.response_interval_ms, 20)
        self.assertEqual(len(result.quality_observations), 3)
        self.assertTrue(all(row[0] >= 0 for row in result.quality_observations))

    def scanner(self, doubles):
        reader = GameClockReader(native=MagicMock(), monotonic_ns=lambda: 1,
                                 wall_ns=lambda: int(EPOCH * 1_000_000))
        reader.native.query.side_effect = lambda _h, addr: SimpleNamespace(
            BaseAddress=0x1000 if addr == 0 else 0x2000,
            RegionSize=0x1000 if addr == 0 else 0x80000000 - 0x2000,
            State=0x1000 if addr == 0 else 0x10000, Type=0x20000, Protect=4,
        )
        reader.native.read.return_value = struct.pack("<" + "d" * len(doubles), *doubles)
        reader.class_traits = MagicMock(return_value=0x3000)
        reader.u32 = MagicMock(return_value=0x4000)
        reader.observe = MagicMock()
        return reader

    def test_zero_multiple_and_unique_candidates(self):
        for values, count in (([0, 0], 0), ([EPOCH, EPOCH, EPOCH], 2), ([EPOCH, EPOCH], 1)):
            reader = self.scanner(values)
            if count != 1:
                with self.assertRaises(AcquisitionError):
                    reader.scan(99, threading.Event())
            else:
                self.assertEqual(reader.scan(99, threading.Event()).object_address, 0x1000 - 0x410)

    def test_partial_region_and_interrupted_query_cannot_claim_unique(self):
        reader = self.scanner([EPOCH, EPOCH])
        reader.native.read.side_effect = AcquisitionError("partial")
        with self.assertRaises(AcquisitionError):
            reader.scan(99, threading.Event())
        reader = self.scanner([EPOCH, EPOCH])
        first = reader.native.query(99, 0)
        reader.native.query.side_effect = [first, AcquisitionError("query failed")]
        with self.assertRaises(AcquisitionError):
            reader.scan(99, threading.Event())

    def test_recognized_object_unknown_structure_is_not_skipped(self):
        reader = self.scanner([EPOCH, EPOCH])
        reader.observe.side_effect = AcquisitionError("unknown version")
        with self.assertRaises(AcquisitionError):
            reader.scan(99, threading.Event())

    def test_second_candidate_metadata_failure_cannot_claim_unique(self):
        for reason in ("metadata query failed", "metadata partial RPM"):
            with self.subTest(reason=reason):
                reader = self.scanner([EPOCH, EPOCH, EPOCH])
                reader.class_traits.side_effect = [0x3000, AcquisitionError(reason)]
                with self.assertRaisesRegex(AcquisitionError, reason):
                    reader.scan(99, threading.Event())

    def test_only_completed_non_target_check_can_be_skipped(self):
        reader = self.scanner([EPOCH, EPOCH, EPOCH])
        reader.class_traits.side_effect = [0x3000, NonTargetObject("different class")]
        self.assertEqual(reader.scan(99, threading.Event()).object_address, 0x1000 - 0x410)

    def test_class_metadata_failures_are_not_non_target_results(self):
        native = MagicMock()
        reader = GameClockReader(native=native)
        readable = SimpleNamespace(State=0x1000, Protect=4, Type=0x1000000)
        for failing_call in ("query", "read"):
            with self.subTest(failing_call=failing_call):
                native.query.side_effect = AcquisitionError("query failed") if failing_call == "query" else None
                native.query.return_value = readable
                native.read.side_effect = AcquisitionError("partial RPM")
                with self.assertRaises(AcquisitionError) as error:
                    reader.class_traits(99, 0x1000, "MiniMapCanvas")
                self.assertNotIsInstance(error.exception, NonTargetObject)
        native.query.side_effect = None
        native.query.return_value = SimpleNamespace(State=0x10000, Protect=0, Type=0)
        with self.assertRaises(NonTargetObject):
            reader.class_traits(99, 0x1000, "MiniMapCanvas")

    def test_read_bracket_rejects_split_updates_and_bad_numeric(self):
        reader = GameClockReader(native=MagicMock(), monotonic_ns=lambda: 1_000_000,
                                 wall_ns=lambda: int(EPOCH * 1_000_000))
        reader.validate_structure = MagicMock()
        pair = struct.pack("<dd", EPOCH, EPOCH - 1000)
        core = struct.pack("<ddd", -28_800_000, 0, 970)
        changed = struct.pack("<dd", EPOCH + 10000, EPOCH - 1000)
        reader.native.read.side_effect = [pair, core, changed, pair, core, pair]
        with patch("v02_game_clock_reader.time.sleep") as retry_sleep:
            self.assertEqual(reader.observe(99, Candidate(1, 2)).raw_ms, EPOCH)
        retry_sleep.assert_called_once_with(0.001)

        reader.native.read.side_effect = [pair, core, changed] * 3
        with patch("v02_game_clock_reader.time.sleep"), self.assertRaises(AcquisitionError):
            reader.observe(99, Candidate(1, 2))
        reader.native.read.side_effect = [pair, core, pair]
        self.assertEqual(reader.observe(99, Candidate(1, 2)).raw_ms, EPOCH)


class ContinuousReader(GameClockReader):
    """Deterministic 10s natural requests with one six-second discovery scan."""
    def __init__(self):
        self.qpc = 1_000_000_000
        native = MagicMock()
        native.identity.return_value = IDENTITY
        native.open.return_value = 99
        super().__init__(native=native, monotonic_ns=lambda: self.qpc)
        self.scan_count = 0
        self.scan_seconds = 6
        self.expected = Candidate(0x1000, 0x2000)
        self.observations = 0

    def scan(self, handle, cancel):
        self.scan_count += 1
        for _ in range(self.scan_seconds):
            self.qpc += 1_000_000_000
            self._read_progress()
        return self.expected

    def _read_observation_values(self, handle, candidate):
        self.observations += 1
        self.qpc += 1_000_000
        elapsed = (self.qpc - 1_000_000_000) // 1_000_000
        start = elapsed // 10000 * 10000
        raw = (elapsed - 20) // 10000 * 10000 + 20 if elapsed >= 10020 else 0
        return Observation(EPOCH + raw, EPOCH + start, 0, self.qpc, self.qpc + 10000)

    def validate_structure(self, handle, candidate):
        pass


class ContinuousReaderTests(unittest.TestCase):
    def test_stream_docstring_matches_layered_identity_and_structure_cadence(self):
        doc = inspect.getdoc(GameClockReader.stream) or ""
        self.assertIn("cheap live token on every sample", doc)
        self.assertIn("full SourceIdentity at startup and about once per second", doc)
        self.assertIn("ABC profile at startup or when its structure key changes", doc)
        self.assertNotIn("Each later read rechecks the complete source identity", doc)

    def test_normal_four_hz_stream_uses_cheap_token_per_sample_and_full_identity_at_one_hz(self):
        class SpyNative:
            def __init__(self):
                self.full_calls = 0
                self.cheap_calls = 0

            def identity(self, _hwnd):
                self.full_calls += 1
                return IDENTITY

            def live_token(self, _hwnd, _handle):
                self.cheap_calls += 1
                return (IDENTITY.hwnd, IDENTITY.pid, IDENTITY.tid, IDENTITY.created)

            @staticmethod
            def open(_pid):
                return 99

            @staticmethod
            def close(_handle):
                return None

        class SpyReader(GameClockReader):
            def __init__(self, native):
                self.qpc = 1_000_000_000
                super().__init__(native=native, monotonic_ns=lambda: self.qpc)

            @staticmethod
            def full_scan(_handle, _cancel):
                return Candidate(0x1000, 0x2000)

            @staticmethod
            def validate_structure(_handle, _candidate):
                return None

            def _read_observation_values(self, _handle, _candidate):
                return Observation(EPOCH, EPOCH, 0, self.qpc, self.qpc + 10_000)

        native = SpyNative()
        reader = SpyReader(native)
        cancel = threading.Event()
        snapshots = []

        def sleep(seconds):
            reader.qpc += int(round(seconds * 1_000_000_000))

        def publish(item, _reason, _checked_ns):
            if item is not None:
                snapshots.append(item)
                if len(snapshots) == 8:
                    cancel.set()

        with patch("v02_game_clock_reader.time.sleep", side_effect=sleep):
            reader.stream(IDENTITY.hwnd, cancel, publish)

        self.assertEqual(len(snapshots), 8)
        self.assertGreaterEqual(native.cheap_calls, len(snapshots))
        self.assertLessEqual(native.full_calls, 7)

    def test_creation_token_reuse_faults_on_next_sample_without_waiting_for_full_identity(self):
        class ReuseNative:
            def __init__(self):
                self.reused = False
                self.full_calls = 0
                self.cheap_calls = 0

            def identity(self, _hwnd):
                self.full_calls += 1
                return IDENTITY

            def live_token(self, _hwnd, _handle):
                self.cheap_calls += 1
                created = IDENTITY.created + int(self.reused)
                return (IDENTITY.hwnd, IDENTITY.pid, IDENTITY.tid, created)

            @staticmethod
            def open(_pid):
                return 99

            @staticmethod
            def close(_handle):
                return None

        class ReuseReader(GameClockReader):
            def __init__(self, native):
                self.qpc = 1_000_000_000
                super().__init__(native=native, monotonic_ns=lambda: self.qpc)

            @staticmethod
            def full_scan(_handle, _cancel):
                return Candidate(0x1000, 0x2000)

            @staticmethod
            def validate_structure(_handle, _candidate):
                return None

            def _read_observation_values(self, _handle, _candidate):
                return Observation(EPOCH, EPOCH, 0, self.qpc, self.qpc + 10_000)

        native = ReuseNative()
        reader = ReuseReader(native)
        cancel = threading.Event()
        snapshots = []

        def sleep(seconds):
            reader.qpc += int(round(seconds * 1_000_000_000))
            if snapshots:
                native.reused = True

        def publish(item, _reason, _checked_ns):
            if item is not None:
                snapshots.append(item)
                if len(snapshots) == 3:
                    cancel.set()

        with patch("v02_game_clock_reader.time.sleep", side_effect=sleep):
            with self.assertRaises(AcquisitionError):
                reader.stream(IDENTITY.hwnd, cancel, publish)
        self.assertEqual(len(snapshots), 1)
        self.assertGreaterEqual(native.cheap_calls, 2)

    def test_multi_wrapper_trusts_reader_identity_fence_and_reuse_faults_cheaply(self):
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

        class SpyNative:
            def __init__(self):
                self.full_calls = 0
                self.cheap_calls = 0
                self.created = IDENTITY.created

            def identity(self, _hwnd):
                self.full_calls += 1
                return IDENTITY

            def live_token(self, _hwnd, _handle):
                self.cheap_calls += 1
                return (IDENTITY.hwnd, IDENTITY.pid, IDENTITY.tid, self.created)

            @staticmethod
            def open(_pid):
                return 99

            @staticmethod
            def close(_handle):
                return None

        class SpyReader(GameClockReader):
            # The override delegates acquisition to the base stream and only
            # observes callbacks, so it explicitly preserves its attestation.
            stream_identity_verified = True

            def __init__(self, native, *, reuse=False):
                self.qpc = 1_000_000_000
                self.snapshots = []
                self.full_after_first = None
                self.reuse = reuse
                super().__init__(native=native, monotonic_ns=lambda: self.qpc)

            @staticmethod
            def full_scan(_handle, _cancel):
                return Candidate(0x1000, 0x2000)

            @staticmethod
            def validate_structure(_handle, _candidate):
                return None

            def _read_observation_values(self, _handle, _candidate):
                return Observation(EPOCH, EPOCH, 0, self.qpc, self.qpc + 10_000)

            def stream(self, hwnd, cancel, publish):
                def capture(item, reason, checked_ns):
                    publish(item, reason, checked_ns)
                    if item is None:
                        return
                    self.snapshots.append(item)
                    if len(self.snapshots) == 1 and self.reuse:
                        self.full_after_first = self.native.full_calls
                        self.native.created += 1
                    if len(self.snapshots) == 8:
                        cancel.set()
                return super().stream(hwnd, cancel, capture)

        for reuse in (False, True):
            with self.subTest(reuse=reuse):
                native = SpyNative()
                reader = SpyReader(native, reuse=reuse)
                model = MultiGameClockSource(
                    lambda: reader, lambda _cancel: (),
                    monotonic_ns=lambda: reader.qpc,
                    thread_factory=InlineThread,
                )
                epoch = model.event_epoch

                def sleep(seconds):
                    reader.qpc += int(round(seconds * 1_000_000_000))

                with patch("v02_game_clock_reader.time.sleep", side_effect=sleep):
                    self.assertTrue(model._start_stream(APPROVED_SHORTCUTS[0], IDENTITY))

                if reuse:
                    self.assertEqual(len(reader.snapshots), 1)
                    self.assertGreater(model.event_epoch, epoch)
                    self.assertTrue(model.source_faulted)
                    self.assertEqual(native.full_calls, reader.full_after_first)
                    self.assertGreaterEqual(native.cheap_calls, 2)
                else:
                    self.assertEqual(len(reader.snapshots), 8)
                    self.assertEqual(model.event_epoch, epoch)
                    self.assertLessEqual(native.full_calls, 5)
                    self.assertGreaterEqual(native.cheap_calls, len(reader.snapshots))

    def test_burst_full_fences_are_bounded_and_stalled_final_fence_faults(self):
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

        class SpyNative:
            def __init__(self):
                self.full_calls = 0
                self.cheap_calls = 0

            def identity(self, _hwnd):
                self.full_calls += 1
                return IDENTITY

            def live_token(self, _hwnd, _handle):
                self.cheap_calls += 1
                return (IDENTITY.hwnd, IDENTITY.pid, IDENTITY.tid, IDENTITY.created)

            @staticmethod
            def open(_pid):
                return 99

            @staticmethod
            def close(_handle):
                return None

        class BurstReader(GameClockReader):
            stream_identity_verified = True

            def __init__(self, native, *, stall_final=False):
                self.qpc = 1_000_000_000
                self.structure_calls = 0
                self.edge_publications = 0
                self.stall_final = stall_final
                super().__init__(native=native, monotonic_ns=lambda: self.qpc)

            @staticmethod
            def full_scan(_handle, _cancel):
                return Candidate(0x1000, 0x2000)

            def validate_structure(self, _handle, _candidate):
                self.structure_calls += 1
                if self.stall_final and self.structure_calls == 4:
                    self.qpc += HEALTH_MAX_GAP_NS + 1

            def _read_observation_values(self, _handle, _candidate):
                start = EPOCH + (1_000 if self.qpc >= 1_250_000_000 else 0)
                raw = EPOCH + (1_020 if self.qpc >= 1_260_000_000 else 0)
                return Observation(raw, start, 0, self.qpc, self.qpc + 10_000)

            def stream(self, hwnd, cancel, publish):
                def capture(item, reason, checked_ns):
                    publish(item, reason, checked_ns)
                    if item is not None and item.edge is not None:
                        self.edge_publications += 1
                        cancel.set()
                return super().stream(hwnd, cancel, capture)

        for stall_final in (False, True):
            with self.subTest(stall_final=stall_final):
                native = SpyNative()
                reader = BurstReader(native, stall_final=stall_final)
                model = MultiGameClockSource(
                    lambda: reader, lambda _cancel: (),
                    monotonic_ns=lambda: reader.qpc,
                    thread_factory=InlineThread,
                )
                epoch = model.event_epoch

                def sleep(seconds):
                    reader.qpc += int(round(seconds * 1_000_000_000))

                with patch("v02_game_clock_reader.time.sleep", side_effect=sleep):
                    self.assertTrue(model._start_stream(APPROVED_SHORTCUTS[0], IDENTITY))

                if stall_final:
                    self.assertEqual(reader.edge_publications, 0)
                    self.assertGreater(model.event_epoch, epoch)
                    self.assertTrue(model.source_faulted)
                else:
                    self.assertEqual(reader.edge_publications, 1)
                    self.assertEqual(model.event_epoch, epoch)
                    self.assertEqual(native.full_calls, 4)
                    self.assertGreater(native.cheap_calls, native.full_calls)
                self.assertEqual(reader.structure_calls, 4)

    def test_prediction_renews_three_consecutive_edges_with_bounded_fast_reads(self):
        class RenewalReader(GameClockReader):
            def __init__(self):
                self.qpc = 9_750_000_000
                native = MagicMock()
                native.identity.return_value = IDENTITY
                native.open.return_value = 99
                super().__init__(native=native, monotonic_ns=lambda: self.qpc)
                self.structure_checks = []
                self.small_reads = 0
                native.read.side_effect = self.read_memory

            @staticmethod
            def full_scan(_handle, _cancel):
                return Candidate(0x1000, 0x2000)

            def validate_structure(self, _handle, _candidate):
                self.structure_checks.append(self.qpc)

            def read_memory(self, _handle, address, amount):
                request_cycle = (0 if self.qpc < 9_800_000_000 else
                                 1 + (self.qpc - 9_800_000_000) // 10_000_000_000)
                response_cycle = (0 if self.qpc < 9_820_000_000 else
                                  1 + (self.qpc - 9_820_000_000) // 10_000_000_000)
                start = EPOCH + request_cycle * 10_000
                raw = (EPOCH if response_cycle == 0
                       else EPOCH + response_cycle * 10_000 + 20)
                self.small_reads += 1
                if address == 0x1410 and amount == 16:
                    return struct.pack("<dd", raw, start)
                if address == 0x2158 and amount == 24:
                    return struct.pack("<ddd", -28_800_000.0, 0.0, 0.0)
                raise AssertionError((address, amount))

        reader = RenewalReader()
        cancel = threading.Event()
        readings = []
        edge_checks = []
        one_ms_sleeps = []

        def sleep(seconds):
            reader.qpc += int(round(seconds * 1_000_000_000))
            if seconds == 0.001:
                one_ms_sleeps.append(reader.qpc)

        def publish(item, reason, checked_ns):
            self.assertFalse(reason)
            if item is None:
                return
            readings.append(item)
            if item.edge is not None:
                self.assertFalse(item.reset_anchor)
                edge_checks.append(checked_ns)
                if len(edge_checks) == 3:
                    cancel.set()

        with patch("v02_game_clock_reader.time.sleep", side_effect=sleep):
            reader.stream(IDENTITY.hwnd, cancel, publish)

        edges = [item.edge for item in readings if item.edge is not None]
        self.assertEqual(sum(item.reset_anchor for item in readings), 1)
        self.assertEqual(len(edges), 3)
        self.assertEqual(
            [edge.server_ms for edge in edges],
            [EPOCH + 20_020, EPOCH + 30_020, EPOCH + 40_020],
        )
        self.assertEqual(edge_checks, [19_820_000_000, 29_820_000_000,
                                       39_820_000_000])
        groups = []
        for instant in one_ms_sleeps:
            if not groups or instant - groups[-1][-1] != BURST_POLL_INTERVAL_NS:
                groups.append([instant])
            else:
                groups[-1].append(instant)
        self.assertEqual(len(groups), len(edges))
        for group, checked_ns in zip(groups, edge_checks):
            burst_start_ns = group[0] - BURST_POLL_INTERVAL_NS
            burst_fences = [instant for instant in reader.structure_checks
                            if burst_start_ns <= instant <= checked_ns]
            self.assertEqual(burst_fences, [burst_start_ns, checked_ns])
        self.assertLessEqual(
            len(one_ms_sleeps),
            len(edges) * (ADAPTIVE_BURST_HARD_MAX_NS // BURST_POLL_INTERVAL_NS),
        )
        self.assertGreater(reader.small_reads, 3 * len(reader.structure_checks))

    def test_fixed_phase_miss_predicts_next_edge_without_structure_scan_per_ms(self):
        class PhaseReader(GameClockReader):
            def __init__(self):
                self.qpc = 9_750_000_000
                native = MagicMock()
                native.identity.return_value = IDENTITY
                native.open.return_value = 99
                super().__init__(native=native, monotonic_ns=lambda: self.qpc)
                self.structure_checks = []
                self.small_reads = 0
                native.read.side_effect = self.read_memory

            @staticmethod
            def full_scan(_handle, _cancel):
                return Candidate(0x1000, 0x2000)

            def validate_structure(self, _handle, _candidate):
                self.structure_checks.append(self.qpc)

            def read_memory(self, _handle, address, amount):
                if self.qpc > 20_500_000_000:
                    raise AssertionError("fixed 250 ms phase lock was not broken")
                cycle = 0 if self.qpc < 9_800_000_000 else 1 if self.qpc < 19_800_000_000 else 2
                start = EPOCH + cycle * 10_000
                if self.qpc < 9_820_000_000:
                    raw = EPOCH
                elif self.qpc < 19_900_000_000:
                    raw = EPOCH + 10_020
                else:
                    # The predicted request is observed at the final instant of
                    # its 50 ms slice; a quality-limit 100 ms response is still
                    # read at the inclusive sample deadline.
                    raw = EPOCH + 20_100
                self.small_reads += 1
                if address == 0x1410 and amount == 16:
                    return struct.pack("<dd", raw, start)
                if address == 0x2158 and amount == 24:
                    return struct.pack("<ddd", -28_800_000.0, 0.0, 0.0)
                raise AssertionError((address, amount))

        reader = PhaseReader()
        cancel = threading.Event()
        readings = []
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            reader.qpc += int(round(seconds * 1_000_000_000))

        def publish(item, reason, _checked):
            self.assertFalse(reason)
            if item is not None:
                readings.append(item)
                if getattr(item, "edge", None) is not None:
                    cancel.set()

        with patch("v02_game_clock_reader.time.sleep", side_effect=sleep):
            reader.stream(IDENTITY.hwnd, cancel, publish)

        edge = readings[-1].edge
        same_poll = next(
            item for item in readings
            if item.edge is None and item.server_ms == EPOCH + 10_020
        )
        self.assertTrue(same_poll.reset_anchor)
        self.assertIsInstance(edge, ClockSample)
        self.assertFalse(readings[-1].reset_anchor)
        self.assertEqual(edge.server_ms, EPOCH + 20_100)
        self.assertEqual(edge.response_interval_ms, EDGE_RESPONSE_MAX_MS)
        self.assertLessEqual(edge.observation_bracket_ns, 25_000_000)
        self.assertTrue(any(value == 0.001 for value in sleeps))
        burst_structure = [value for value in reader.structure_checks
                           if 19_740_000_000 <= value <= 19_910_000_000]
        self.assertEqual(len(burst_structure), 2)
        burst_polls = [value for value in sleeps if value == 0.001]
        self.assertGreaterEqual(len(burst_polls), 150)
        self.assertLessEqual(
            len(burst_polls), ADAPTIVE_BURST_SAMPLE_MAX_NS // 1_000_000,
        )
        self.assertLessEqual(sum(burst_polls), ADAPTIVE_BURST_HARD_MAX_NS / 1e9)
        self.assertGreater(reader.small_reads, 3 * len(burst_structure))

    def test_one_prediction_window_covers_full_uncertainty_response_and_slack(self):
        previous = Observation(EPOCH, EPOCH, 0, 9_750_000_000, 9_750_010_000)
        current = Observation(EPOCH + 10_020, EPOCH + 10_000, 0,
                              10_000_000_000, 10_000_010_000)
        earliest = previous.after_ns + 10_000_000_000
        latest = current.after_ns + 10_000_000_000
        start, sample_deadline, hard_deadline = predict_burst_window(previous, current)

        self.assertEqual(start, earliest)
        self.assertEqual(latest - earliest, 250_000_000)
        self.assertLessEqual(latest - earliest, PREDICTIVE_REQUEST_WINDOW_MAX_NS)
        self.assertEqual(
            sample_deadline,
            latest + EDGE_RESPONSE_MAX_MS * 1_000_000 + BURST_POLL_INTERVAL_NS,
        )
        self.assertEqual(
            hard_deadline,
            latest + EDGE_RESPONSE_MAX_MS * 1_000_000 + BURST_OBSERVATION_SLACK_NS,
        )
        self.assertLessEqual(hard_deadline - start, ADAPTIVE_BURST_HARD_MAX_NS)

        too_wide = replace(current, before_ns=current.before_ns + 22_000_000,
                           after_ns=current.after_ns + 22_000_000)
        self.assertIsNone(predict_burst_window(previous, too_wide))

    def test_272ms_slow_structure_rejects_only_prediction_and_keeps_stream(self):
        class SlowStructureReader(GameClockReader):
            def __init__(self):
                self.qpc = 1_000_000_000
                native = MagicMock()
                native.identity.return_value = IDENTITY
                native.open.return_value = 99
                super().__init__(native=native, monotonic_ns=lambda: self.qpc)
                self.structure_checks = 0

            @staticmethod
            def full_scan(_handle, _cancel):
                return Candidate(0x1000, 0x2000)

            def validate_structure(self, _handle, _candidate):
                self.structure_checks += 1
                if self.structure_checks == 2:
                    self.qpc += 22_000_000

            def _read_observation_values(self, _handle, _candidate):
                changed = self.qpc >= 1_272_000_000
                return Observation(
                    EPOCH + (1020 if changed else 0),
                    EPOCH + (1000 if changed else 0), 0,
                    self.qpc, self.qpc + 10_000,
                )

        reader = SlowStructureReader()
        cancel = threading.Event()
        readings = []
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            reader.qpc += int(round(seconds * 1_000_000_000))

        def publish(item, _reason, _checked):
            if item is not None:
                readings.append(item)
                if getattr(item, "reset_anchor", False):
                    cancel.set()

        with patch("v02_game_clock_reader.time.sleep", side_effect=sleep):
            reader.stream(IDENTITY.hwnd, cancel, publish)

        self.assertTrue(readings[-1].reset_anchor)
        self.assertIsNone(readings[-1].edge)
        self.assertEqual(reader.qpc, 1_272_000_000)
        self.assertNotIn(0.001, sleeps)

    def test_pending_request_uses_bounded_one_ms_burst_for_qualified_edge(self):
        class BurstReader(GameClockReader):
            def __init__(self):
                self.qpc = 1_000_000_000
                native = MagicMock()
                native.identity.return_value = IDENTITY
                native.open.return_value = 99
                super().__init__(native=native, monotonic_ns=lambda: self.qpc)
                self.observation_count = 0

            @staticmethod
            def full_scan(_handle, _cancel):
                return Candidate(0x1000, 0x2000)

            @staticmethod
            def validate_structure(_handle, _candidate):
                return None

            def _read_observation_values(self, _handle, _candidate):
                call = self.observation_count
                self.observation_count += 1
                if call == 0:
                    self.qpc = 1_000_000_000
                    raw, start = EPOCH, EPOCH
                elif call == 1:
                    self.qpc = 1_250_000_000
                    raw, start = EPOCH, EPOCH + 1000
                elif call <= 21:
                    burst_ms = call - 1
                    self.qpc = 1_250_000_000 + burst_ms * 1_000_000
                    raw = EPOCH + 1020 if burst_ms == 20 else EPOCH
                    start = EPOCH + 1000
                else:
                    raise AssertionError("qualified edge was not captured during bounded burst")
                return Observation(raw, start, 0, self.qpc, self.qpc + 10_000)

        reader = BurstReader()
        cancel = threading.Event()
        sleeps = []
        readings = []

        def publish(item, reason, _checked):
            self.assertFalse(reason)
            if item is not None:
                readings.append(item)
                if getattr(item, "edge", None) is not None:
                    cancel.set()

        with patch("v02_game_clock_reader.time.sleep",
                   side_effect=lambda seconds: sleeps.append(seconds)):
            reader.stream(IDENTITY.hwnd, cancel, publish)

        edge = readings[-1].edge
        self.assertIsInstance(edge, ClockSample)
        self.assertEqual(edge.server_ms, EPOCH + 1020)
        self.assertLessEqual(edge.observation_bracket_ns, 25_000_000)
        self.assertEqual(edge.response_interval_ms, 20)
        self.assertEqual(edge.anchor_ns, (1_269_000_000 + 1_270_010_000) // 2)
        burst_sleeps = [value for value in sleeps if value == 0.001]
        self.assertEqual(len(burst_sleeps), 20)
        self.assertLessEqual(sum(burst_sleeps), 0.150)
        self.assertTrue(all(not isinstance(item, ClockSample) for item in readings))

    def test_multiple_live_snapshots_reuse_validated_candidate_at_four_hz_or_less(self):
        reader = ContinuousReader()
        cancel = threading.Event()
        readings, health = [], []
        def publish(item, reason, checked):
            self.assertFalse(reason)
            if item is None:
                health.append(checked)
            else:
                readings.append(item)
                if len(readings) == 3:
                    cancel.set()
        with patch("v02_game_clock_reader.time.sleep") as sleep:
            reader.stream(11, cancel, publish)
        self.assertEqual(reader.scan_count, 1)
        self.assertEqual([row.server_ms for row in readings], [EPOCH, EPOCH, EPOCH])
        self.assertTrue(all(row.edge is None for row in readings))
        self.assertTrue(all(b - a < HEALTH_MAX_GAP_NS for a, b in zip(health, health[1:])))
        self.assertEqual(sleep.call_count, 3)
        self.assertEqual(reader.observations, 4)
        reader.native.open.assert_called_once()
        reader.native.close.assert_called_once_with(99)
        self.assertIsNone(reader._progress_hook)

    def test_cancel_during_scan_and_rpm_failure_release_handle(self):
        for fault in ("cancel", "read"):
            reader = ContinuousReader()
            cancel = threading.Event()
            if fault == "cancel":
                def publish(*_args):
                    if reader.scan_count:
                        cancel.set()
            else:
                publish = lambda *_args: None
                reader.observe = MagicMock(side_effect=AcquisitionError("RPM failed"))
            with patch("v02_game_clock_reader.time.sleep"), self.assertRaises(AcquisitionError):
                reader.stream(11, cancel, publish)
            reader.native.close.assert_called_once_with(99)

    def test_each_sample_revalidates_structure_without_full_rescan(self):
        reader = ContinuousReader()
        cancel = threading.Event()
        checks = []
        reader.validate_structure = lambda _handle, candidate: checks.append(candidate)
        def publish(item, *_args):
            if item is not None:
                cancel.set()
        with patch("v02_game_clock_reader.time.sleep"):
            reader.stream(11, cancel, publish)
        self.assertEqual(reader.scan_count, 1)
        self.assertEqual(checks, [reader.expected, reader.expected])
        reader.native.close.assert_called_once()

    def test_scan_no_progress_or_qpc_rollback_is_not_health(self):
        for delta in (HEALTH_MAX_GAP_NS + 1, -1):
            reader = ContinuousReader()
            def stalled(_handle, _cancel):
                reader.qpc += delta
                reader._read_progress()
                return reader.expected
            reader.scan = stalled
            with self.assertRaises(AcquisitionError):
                reader.stream(11, threading.Event(), lambda *_: None)
            reader.native.close.assert_called_once()

    def test_edge_tracker_retains_quality_pairing_and_clock_regression_guards(self):
        def obs(raw, start, ns, lag=0, latency=10000):
            return Observation(EPOCH + raw, EPOCH + start, lag, ns, ns + latency)
        # Synthetic feed covers rejected same-poll/unpaired and bad residual/bracket.
        for residual, bracket, expected in ((20, 1_000_000, True), (101, 1_000_000, False),
                                             (-1, 1_000_000, False), (20, 26_000_000, False)):
            tracker = EdgeTracker(IDENTITY, obs(0, 0, 1_000_000_000))
            tracker.feed(obs(0, 10000, 1_001_000_000))
            response_ns = 1_021_000_000 if bracket < 25_000_000 else 1_030_000_000
            tracker.feed(obs(0, 10000, response_ns - bracket))
            result = tracker.feed(obs(10000 + residual, 10000, response_ns))
            self.assertEqual(result is not None, expected)
        tracker = EdgeTracker(IDENTITY, obs(0, 0, 1_000_000_000))
        self.assertIsNone(tracker.feed(obs(10020, 10000, 1_010_000_000)))
        for current in (obs(0, 0, 999_999_999), obs(0, 0, 5_000_000_000),
                        obs(0, 0, 1_001_000_000, latency=20_000_001)):
            tracker = EdgeTracker(IDENTITY, obs(0, 0, 1_000_000_000))
            with self.assertRaises(AcquisitionError):
                tracker.feed(current)
        tracker = EdgeTracker(IDENTITY, obs(0, 0, 1_000_000_000))
        tracker.feed(obs(0, 10000, 1_001_000_000))
        with self.assertRaisesRegex(AcquisitionError, "重疊"):
            tracker.feed(obs(0, 20000, 1_002_000_000))

    def test_same_poll_251ms_miss_never_becomes_a_fake_request_qpc(self):
        def obs(raw, start, ns):
            return Observation(EPOCH + raw, EPOCH + start, 0, ns, ns + 10_000)

        tracker = EdgeTracker(IDENTITY, obs(0, 0, 1_000_000_000))
        # The first request and response were both missed during a 251 ms
        # observation interval.  Its observation QPC is not the request QPC.
        self.assertIsNone(tracker.feed(obs(10_020, 10_000, 1_251_000_000)))
        self.assertIsNone(tracker.last_request)

        # The next cadence is observed faithfully.  Reusing 1.251 s as the old
        # request QPC would create a 251 ms continuity error and false fault.
        for instant in (3_000_000_000, 5_000_000_000, 7_000_000_000,
                        9_000_000_000):
            self.assertIsNone(tracker.feed(obs(10_020, 10_000, instant)))
        self.assertIsNone(tracker.feed(obs(10_020, 20_000, 11_000_000_000)))
        qualified = tracker.feed(obs(20_020, 20_000, 11_020_000_000))
        self.assertIsInstance(qualified, ClockSample)
        self.assertEqual(qualified.response_interval_ms, 20)


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.raw = bytearray(512)
        self.raw[:4] = b"\x10\0\x2e\0"
        self.raw[20:36] = b"mini-map-record!"
        self.raw[60:76] = b"core-type-record"
        self.raw = bytes(self.raw)
        self.base = 0x10000
        self.pointer = {108: 300, 308: 400, 188: self.base + 20, 388: self.base + 60,
                        372: 501, 472: 502}
        self.native = MagicMock()
        self.native.read.side_effect = lambda _h, address, amount: self.raw[address - self.base:address - self.base + amount]
        self.native.query.return_value = SimpleNamespace(BaseAddress=self.base, RegionSize=len(self.raw),
                                                        State=0x1000, Type=0x20000, Protect=4)
        self.reader = GameClockReader(native=self.native)
        self.reader.u32 = lambda _h, address: self.pointer[address]
        self.reader.avm_string = lambda _h, address: {501: "SimpleCanvas", 502: "Object"}[address]
        profiles = ((20, 16, hashlib.sha256(self.raw[20:36]).hexdigest().upper(), "SimpleCanvas"),
                    (60, 16, hashlib.sha256(self.raw[60:76]).hexdigest().upper(), "Object"))
        self.patch = patch.multiple("v02_game_clock_reader", ABC_LENGTH=len(self.raw),
                                    ABC_SHA256=hashlib.sha256(self.raw).hexdigest().upper(),
                                    TRAIT_PROFILES=profiles)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_exact_live_profile_and_cache(self):
        self.reader.validate_slot_profile(99, 100, 300)
        self.native.query.assert_called_once()
        self.reader.validate_slot_profile(99, 100, 300)
        self.native.query.assert_called_once()

    def test_same_record_indices_modified_pool_or_method_rejected(self):
        changed = bytearray(self.raw)
        changed[100] = 1
        self.raw = bytes(changed)
        with self.assertRaisesRegex(AcquisitionError, "常數池或方法"):
            self.reader.validate_slot_profile(99, 100, 300)

    def test_different_abc_bases_rejected(self):
        self.pointer[388] += 1
        # Preserve the record hash to exercise the independent base-relation gate.
        original = self.native.read.side_effect
        self.native.read.side_effect = lambda h, address, amount: self.raw[60:76] if address == self.base + 61 else original(h, address, amount)
        with self.assertRaisesRegex(AcquisitionError, "來源關係"):
            self.reader.validate_slot_profile(99, 100, 300)

    def test_partial_abc_read_or_unknown_field_type_rejected(self):
        original = self.native.read.side_effect
        def partial(h, address, amount):
            if amount == len(self.raw):
                raise AcquisitionError("partial")
            return original(h, address, amount)
        self.native.read.side_effect = partial
        with self.assertRaises(AcquisitionError):
            self.reader.validate_slot_profile(99, 100, 300)
        self.native.read.side_effect = original
        changed = bytearray(self.raw)
        changed[20] ^= 1
        self.raw = bytes(changed)
        with self.assertRaisesRegex(AcquisitionError, "欄位或型別"):
            self.reader.validate_slot_profile(99, 100, 300)


class IntegrationTests(unittest.TestCase):
    def harness(self):
        from test_v02_game_clock_source import SourceHarness
        h = SourceHarness((IDENTITY,))
        h.begin()
        app = Panel()
        app.groups = [Group(name="A", master_hwnd=11), Group(name="B", master_hwnd=22)]
        app.active_group_index = Value(0)
        app.selected_hwnds = lambda: []
        app.tree = MagicMock()
        app.tree.selection.return_value = ()
        app.game_clock = h.clock
        app.game_clock_reader = h.reader
        app.game_clock_source = h.source
        app.game_time_source_text = Value("尚未校正")
        app._game_time_source_payload = None
        app.game_time_text = Value("尚未校正")
        app._game_time_text_payload = None
        app.closing_app = False
        app.source_harness = h
        return app

    def test_explicit_selection_only_remains_manual_capture_destination(self):
        app = self.harness()
        with patch.dict(NAMESPACE, {"user32": SimpleNamespace(IsWindow=lambda hwnd: hwnd in (11, 22))}):
            self.assertEqual(app.game_time_hwnd(), 11)
            app.selected_hwnds = lambda: [22]
            app.tree.selection.return_value = ("22",)
            self.assertEqual(app.game_time_hwnd(), 22)
            app.selected_hwnds = lambda: [11, 22]
            app.tree.selection.return_value = ("11", "22")
            self.assertIsNone(app.game_time_hwnd())
            app.selected_hwnds = lambda: [999]
            app.tree.selection.return_value = ("999",)
            self.assertIsNone(app.game_time_hwnd())
            app.selected_hwnds = lambda: []
            app.tree.selection.return_value = ()
            app.groups[0].master_hwnd = None
            self.assertIsNone(app.game_time_hwnd())
        self.assertEqual(app.game_time_source_token().identity, IDENTITY)

    def test_unopened_rows_do_not_bypass_single_explicit_manual_source(self):
        app = self.harness()
        with patch.dict(NAMESPACE, {"user32": SimpleNamespace(IsWindow=lambda hwnd: hwnd == 11)}):
            for selected, filtered in ((("11", "launch:2"), [11]),
                                       (("launch:2",), []),
                                       (("launch:2", "launch:3"), [])):
                with self.subTest(selected=selected):
                    app.tree.selection.return_value = selected
                    app.selected_hwnds = lambda: filtered
                    self.assertIsNone(app.game_time_hwnd())
            app.tree.selection.return_value = ()
            self.assertEqual(app.game_time_hwnd(), 11)

    def test_group_selection_master_and_sync_cannot_change_automatic_token_or_clock(self):
        app = self.harness()
        h = app.source_harness
        token = app.game_time_source_token()
        h.publish(sample())
        app.poll_game_clock_acquisition()
        original_label = app.game_time_source_text.get()
        self.assertIn("PID 22 / HWND 11", original_label)
        for index, selection in ((1, ("22",)), (0, ("11", "22")), (1, ())):
            app.active_group_index.set(index)
            app.tree.selection.return_value = selection
            app.groups[index].running = not app.groups[index].running
            app.groups[index].master_hwnd = None
            app.invalidate_game_clock_source()
            app.poll_game_clock_acquisition()
            self.assertEqual(app.game_time_source_token(), token)
            self.assertEqual(app.game_time_source_text.get(), original_label)
            self.assertIsNotNone(app.game_clock.text())
            self.assertFalse(h.source.cancel.is_set())

    def test_app_poll_never_performs_discovery_hash_identity_or_stream_on_tk(self):
        app = self.harness()
        h = app.source_harness
        h.reader.native.identity.reset_mock()
        h.discover.reset_mock()
        for _ in range(20):
            app.poll_game_clock_acquisition()
        h.reader.native.identity.assert_not_called()
        h.reader.stream.assert_not_called()
        h.discover.assert_not_called()

    def test_explicit_invalidation_clears_label_and_does_not_repeat_from_ui_events(self):
        app = self.harness()
        h = app.source_harness
        h.publish(sample())
        app.poll_game_clock_acquisition()
        app.invalidate_game_clock_source(force=True, reason="explicit fault")
        generation = h.source.generation
        self.assertIsNone(app.game_clock.sample)
        self.assertIn("尚未校正", app.game_time_source_text.get())
        for _ in range(10):
            app.invalidate_game_clock_source()
            app.poll_game_clock_acquisition()
        self.assertEqual(h.source.generation, generation)

    def test_close_cancels_and_waits_for_cleanup_without_blocking_tk(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "FlashSyncApp")
        method = next(node for node in cls.body if getattr(node, "name", "") == "on_close")
        namespace = {}
        node = ast.ClassDef(name="Closing", bases=[], keywords=[], decorator_list=[], body=[method])
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), "close", "exec"), namespace)
        app = namespace["Closing"]()
        from test_v02_game_clock_source import SourceHarness
        h = SourceHarness()
        h.begin()
        app.game_clock_source = h.source
        app.after = MagicMock()
        app.uninstall_mouse_wheel_hook = MagicMock(return_value=True)
        app.on_close()
        self.assertTrue(h.source.cancel.is_set())
        self.assertTrue(app.closing_app)
        app.after.assert_called_once()
        delay, callback = app.after.call_args.args
        self.assertEqual(delay, 25)
        self.assertTrue(callable(callback))

    def test_uncalibrated_none_prevents_existing_timed_input(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "FlashSyncApp")
        methods = [node for node in cls.body if isinstance(node, ast.FunctionDef)
                    and node.name in ("enable_timed_click", "estimated_game_time_ms",
                                      "timed_action_game_time_ms", "poll_timed_click",
                                      "_set_timed_semantic", "_timed_target_remaining_ms")]
        namespace = {
            "messagebox": MagicMock(), "DAY_MS": 86_400_000,
            "FRESHNESS_TTL_NS": 30_000_000_000,
            "TIMED_STATUS_VALUES": {"定時按下：已啟用", "定時按下：等待目標時間",
                                    "定時按下：已觸發", "定時按下：來源失效"},
            "TIMED_STATUS_ENABLED": "定時按下：已啟用",
            "TIMED_STATUS_WAITING": "定時按下：等待目標時間",
            "TIMED_STATUS_FIRED": "定時按下：已觸發",
            "TIMED_STATUS_SOURCE_INVALID": "定時按下：來源失效",
        }
        node = ast.ClassDef(name="Harness", bases=[], keywords=[], decorator_list=[], body=methods)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), "guard", "exec"), namespace)
        app = namespace["Harness"]()
        app.game_clock_source = MagicMock()
        app.game_clock_source.timed_action_time_of_day_ms.return_value = None
        app.game_clock_source.timed_source_state.return_value = "waiting"
        app.parse_target_time_ms = lambda text: 3600000
        app.timed_click_target_text = Value("01:00")
        app.timed_click_enabled = Value(True)
        app.timed_click_hwnd, app.timed_click_point = 11, (1, 1)
        app.timed_click_target_failure = lambda: None
        app.timed_click_status_text = Value("")
        app.write_log = MagicMock()
        app.schedule_timed_click_poll = MagicMock()
        app.fire_timed_click = MagicMock()
        app._timed_monotonic_ns = lambda: 1_000_000_000
        app.enable_timed_click()
        self.assertTrue(app.timed_click_enabled.get())
        self.assertIsNone(app.timed_click_deadline_ns)
        self.assertIsNone(app.timed_click_remaining_ms)
        app.fire_timed_click.assert_not_called()
        app.schedule_timed_click_poll.assert_called_once_with(250)

        app.schedule_timed_click_poll.reset_mock()
        app.timed_click_after_id = "scheduled"
        app.poll_timed_click()
        app.fire_timed_click.assert_not_called()
        app.schedule_timed_click_poll.assert_called_once_with(250)
        self.assertTrue(app.timed_click_enabled.get())
        self.assertIsNone(app.timed_click_deadline_ns)
        self.assertEqual(app.timed_click_status_text.get(), "定時按下：等待目標時間")

    def test_no_active_ocr_system_fallback(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "FlashSyncApp")
        names = {"estimated_game_time_text", "estimated_game_time_ms", "read_selected_game_time",
                 "poll_game_clock_acquisition", "system_game_time_ms"}
        text = "\n".join(ast.unparse(node) for node in cls.body if getattr(node, "name", "") in names)
        for forbidden in ("time.time", "time.localtime", "capture_game_time_image", "read_time_text_from_image",
                          "system_time_offset_ms", "flash_process_infos"):
            self.assertNotIn(forbidden, text)

    def test_unrelated_function_apis_match_fixed_batch_baseline(self):
        result = subprocess.run(
            [sys.executable, str(SOURCE.parent / "verify_v02_api_boundaries.py")],
            cwd=SOURCE.parent, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("API shape SHA256: ee7fa008654412b58fa04c43c6444d92413961469a20a53af4de943de58cecce",
                      result.stdout)


class ClockBarTests(unittest.TestCase):
    def test_pure_time_and_fixed_narrow_digit_envelope(self):
        bar = ClockBar.__new__(ClockBar)
        bar.app = SimpleNamespace(game_clock=GameClock())
        bar.rpg_font_family = "test"
        bar.floating_status_font_size = 14
        bar.floating_status_layout_cache = None
        bar.value = "尚未校正"
        measured = []
        def measure(text):
            measured.append(text)
            return sum(11 if c == "1" else 8 for c in text)
        font = SimpleNamespace(measure=measure)
        with patch("v02_game_clock_bar.tkfont.Font", return_value=font):
            first = bar.measure_floating_status_layout()
            bar.update_floating_status = MagicMock()
            bar.update("01:02:03.004")
            second = bar.measure_floating_status_layout()
            self.assertIs(first, second)
            self.assertEqual(bar.value, "01:02:03.004")
            bar.update(None)
        self.assertEqual(bar.value, "尚未校正")
        self.assertEqual(len(measured), 11)
        self.assertTrue(all("伺服器" not in text and "UTC+8" not in text for text in measured))
        self.assertLess(first["content_width"], measure("伺服器 88:88:88.888 UTC+8") + 24)
        self.assertEqual(first["content_width"], measure("11:11:11.111") + 24)

    def test_reuses_native_noactivate_drag_width_without_shared_state(self):
        app = Panel()
        app.clock_bar = None
        app.rpg_font_family = "Microsoft JhengHei UI"
        app.game_clock = GameClock()
        app.floating_status_font_size = 20
        app.floating_status_width_delta = 987
        app.floating_status_monitor_id = "status-monitor"
        app.floating_status_local_x = 111
        app.floating_status_monitors = MagicMock()
        app.save_launch_config = MagicMock()
        app.restore_from_tray = MagicMock()
        fake_tk = SimpleNamespace(Toplevel=MagicMock(), Menu=MagicMock(), Button=MagicMock(), Label=MagicMock())
        with patch("v02_game_clock_bar.tk", fake_tk), \
                patch.object(ClockBar, "update_floating_status"), \
                patch.dict(NAMESPACE, {"user32": MagicMock()}), \
                patch.object(Panel, "keep_floating_status_topmost"):
            bar = ClockBar(app, {"font_size": 12, "width_delta": 25, "monitor_id": "clock-monitor", "local_x": 333})
        self.assertEqual(bar.floating_status_settings(),
                         {"font_size": 12, "width_delta": 25, "monitor_id": "clock-monitor", "local_x": 333})
        self.assertEqual(app.floating_status_font_size, 20)
        self.assertEqual(app.floating_status_local_x, 111)
        self.assertEqual(bar.floating_status_native_rect.__func__, Panel.floating_status_native_rect)
        self.assertEqual(bar.stop_floating_status_drag.__func__, Panel.stop_floating_status_drag)
        self.assertEqual(bar.adjust_floating_status_width.__func__, Panel.adjust_floating_status_width)
        self.assertEqual(bar.floating_status_menu_thread_id, 0)
        self.assertIsNone(bar.clock_bar)
        fake_tk.Toplevel.return_value.configure.assert_called_with(bg="#ffffff")
        self.assertEqual(fake_tk.Label.call_args.kwargs["bg"], "#ffffff")
        self.assertEqual(fake_tk.Label.call_args.kwargs["fg"], "#000000")
        self.assertEqual(fake_tk.Button.call_args.kwargs["bg"], "#ffffff")
        self.assertEqual(fake_tk.Button.call_args.kwargs["fg"], "#000000")

    def test_clock_hit_excludes_input_without_mutating_status_state(self):
        app = Panel()
        app.clock_bar = SimpleNamespace(floating_status_contains_point=MagicMock(return_value=True))
        app.floating_status_window = None
        app.floating_status_menu_thread_id = 0
        self.assertTrue(app.floating_status_contains_point(-200, 1500))
        app.clock_bar.floating_status_contains_point.return_value = False
        self.assertFalse(app.floating_status_contains_point(-200, 1500))


if __name__ == "__main__":
    unittest.main()
