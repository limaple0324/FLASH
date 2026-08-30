from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import MappingProxyType
from unittest import mock

from fu_reconnect_integration import EmbeddedAutomationController, InputLeaseArbiter
import flash_sync_v02
import smart_reconnect as sr
from manor_assistant import win32_api


class StandaloneIsolationTests(unittest.TestCase):
    def test_standalone_uses_dedicated_state_and_single_instance_names(self):
        self.assertEqual(flash_sync_v02.APP_DATA_DIR_NAME, "輔V0.2_自動重連獨立版")
        self.assertEqual(
            flash_sync_v02.APP_CONFIG_FILENAME,
            "sync_launch_config_reconnect_standalone.json",
        )
        self.assertNotEqual(flash_sync_v02.APP_DATA_DIR_NAME, "輔V0.2")
        self.assertNotEqual(
            flash_sync_v02.SINGLE_INSTANCE_MUTEX_NAME,
            "Local\\FlashSyncAssistantV02SingleInstance",
        )
        self.assertNotEqual(
            flash_sync_v02.SINGLE_INSTANCE_RESTORE_MESSAGE_NAME,
            "FlashSyncAssistantV02RestoreMessage",
        )

    def test_missing_config_is_copied_once_from_original_appdata_without_writeback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            original_dir = root / "輔V0.2"
            original_dir.mkdir()
            original = original_dir / "sync_launch_config.json"
            original_payload = {"groups": [{"launch_entries": [{"path": "original.lnk"}]}]}
            original_text = json.dumps(original_payload, ensure_ascii=False)
            original.write_text(original_text, encoding="utf-8")

            with mock.patch.dict(os.environ, {"APPDATA": str(root)}, clear=False), mock.patch.object(
                flash_sync_v02, "legacy_writable_dirs", return_value=[]
            ):
                target = Path(
                    flash_sync_v02.app_writable_path(flash_sync_v02.APP_CONFIG_FILENAME)
                )
                self.assertEqual(target.parent, root / "輔V0.2_自動重連獨立版")
                self.assertEqual(target.read_text(encoding="utf-8"), original_text)

                standalone_text = json.dumps({"groups": []}, ensure_ascii=False)
                target.write_text(standalone_text, encoding="utf-8")
                self.assertEqual(
                    Path(
                        flash_sync_v02.app_writable_path(
                            flash_sync_v02.APP_CONFIG_FILENAME
                        )
                    ).read_text(encoding="utf-8"),
                    standalone_text,
                )
                self.assertEqual(original.read_text(encoding="utf-8"), original_text)


class StrictRegistryTests(unittest.TestCase):
    START = "2026-08-30T04:00:00+00:00"
    CREATED = "2026-08-30T04:00:01+00:00"

    def tx(self, txid="tx", *, started=None, before=()):
        return MappingProxyType({
            "transaction_id": txid,
            "started": started or self.START,
            "before_hwnds": frozenset(),
            "process_snapshot_complete": True,
            "before_processes": frozenset(before),
        })

    def controller(self, path: Path):
        ctl = EmbeddedAutomationController(path, lambda hwnd: int(hwnd) in {101, 102, 103})
        ctl._start_worker = lambda _record: None
        self.addCleanup(ctl.stop)
        return ctl

    def test_only_unique_identity_delta_is_managed(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = self.controller(Path(td) / "settings.json")
            accepted = ctl.authorize_launch_transaction(
                [{"entry_id": "a", "identity": "id-a", "path": "a.lnk", "name": "A"}],
                [
                    {"hwnd": 101, "pid": 1, "creation_time": self.CREATED, "identity": "id-a"},
                    {"hwnd": 102, "pid": 2, "creation_time": self.CREATED, "identity": "external"},
                    {"hwnd": 103, "pid": 3, "creation_time": self.CREATED, "identity": "external2"},
                ],
                self.tx(),
            )
            self.assertEqual(accepted, {"a": 101})
            self.assertEqual(set(ctl.binding_loader()), {101})

    def test_blank_duplicate_multiple_and_cim_failure_are_rejected(self):
        cases = [
            ([{"entry_id": "a", "identity": ""}], [{"hwnd": 101, "pid": 1, "creation_time": "t", "identity": ""}]),
            ([{"entry_id": "a", "identity": "same"}, {"entry_id": "b", "identity": "same"}], [{"hwnd": 101, "pid": 1, "creation_time": "t", "identity": "same"}]),
            ([{"entry_id": "a", "identity": "x"}], [{"hwnd": 101, "pid": 1, "creation_time": "t", "identity": "x"}, {"hwnd": 102, "pid": 2, "creation_time": "t2", "identity": "x"}]),
            ([{"entry_id": "a", "identity": "x"}], [{"hwnd": 101, "pid": 1, "creation_time": "", "identity": "x"}]),
        ]
        for entries, candidates in cases:
            with self.subTest(entries=entries, candidates=candidates), tempfile.TemporaryDirectory() as td:
                ctl = self.controller(Path(td) / "settings.json")
                self.assertEqual(ctl.authorize_launch_transaction(entries, candidates, self.tx()), {})
                self.assertEqual(ctl.binding_loader(), {})

    def test_preexisting_pid_late_hwnd_rejected_but_new_process_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = self.controller(Path(td) / "settings.json")
            entry = [{"entry_id": "a", "identity": "id-a", "path": "a", "name": "A", "command_marker": "token=abc"}]
            old_time = "2026-08-30T04:00:00+00:00"
            old = [{"hwnd": 101, "pid": 7, "creation_time": old_time, "identity": "id-a", "command_line": "flash token=abc"}]
            tx = self.tx(started="2026-08-30T04:01:00+00:00", before={(7, old_time)})
            self.assertEqual(ctl.authorize_launch_transaction(entry, old, tx), {})
            new = [{"hwnd": 102, "pid": 8, "creation_time": "2026-08-30T04:01:01+00:00", "identity": "id-a", "command_line": "flash token=abc"}]
            self.assertEqual(ctl.authorize_launch_transaction(entry, new, tx), {"a": 102})

    def test_same_position_is_irrelevant_and_restart_does_not_restore_registry(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            ctl = self.controller(path)
            ctl.update_setting("stable-entry", manor_enabled=True, fishing_profile_id="builtin-level-5")
            ctl.authorize_launch_transaction(
                [{"entry_id": "stable-entry", "identity": "x", "x": 1, "y": 1}],
                [{"hwnd": 101, "pid": 5, "creation_time": self.CREATED, "identity": "x", "x": 999, "y": 999}],
                self.tx(),
            )
            self.assertEqual(set(ctl.binding_loader()), {101})
            ctl.stop()
            fresh = self.controller(path)
            self.assertEqual(fresh.binding_loader(), {})
            self.assertTrue(fresh.setting("stable-entry")["manor_enabled"])
            row = fresh.status_rows([{"entry_id": "stable-entry", "name": "A"}])[0]
            self.assertIn("重啟輔後既有視窗不納管", row["management"])

    def test_three_flash_allowlist_one_means_worker_manor_status_one(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = self.controller(Path(td) / "settings.json")
            ctl.authorize_launch_transaction(
                [{"entry_id": "a", "identity": "a"}],
                [{"hwnd": 101, "pid": 1, "creation_time": self.CREATED, "identity": "a"},
                 {"hwnd": 102, "pid": 2, "creation_time": self.CREATED, "identity": "b"},
                 {"hwnd": 103, "pid": 3, "creation_time": self.CREATED, "identity": "c"}],
                self.tx(),
            )
            self.assertEqual(len(ctl.binding_loader()), 1)
            self.assertEqual(sum(r["managed"] for r in ctl.status_rows([
                {"entry_id": "a", "name": "A"}, {"entry_id": "b", "name": "B"}, {"entry_id": "c", "name": "C"}
            ])), 1)

    def test_runtime_pid_creation_identity_mismatch_revokes_allowlist(self):
        with tempfile.TemporaryDirectory() as td:
            current = {"pid": 1, "creation_time": self.CREATED, "identity": "id-a"}
            ctl = EmbeddedAutomationController(
                Path(td) / "settings.json", lambda hwnd: int(hwnd) == 101,
                lambda _hwnd: dict(current),
            )
            ctl._start_worker = lambda _record: None
            self.addCleanup(ctl.stop)
            ctl.authorize_launch_transaction(
                [{"entry_id": "a", "identity": "id-a", "path": "a", "name": "A"}],
                [{"hwnd": 101, "pid": 1, "creation_time": self.CREATED, "identity": "id-a"}],
                self.tx(),
            )
            self.assertEqual(set(ctl.binding_loader()), {101})
            current["creation_time"] = "reused"
            ctl._validated_at[101] = 0.0
            self.assertEqual(ctl.binding_loader(), {})
            self.assertIn("重新驗證失敗", ctl.rejections["a"])

    def test_settings_keyed_by_entry_id_survive_reorder_and_fishing_off_retains_level(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            ctl = self.controller(path)
            ctl.update_setting("a", fishing_enabled=True, fishing_profile_id="builtin-level-6")
            ctl.update_setting("b", manor_enabled=True, manor_quantity=7)
            ctl.update_setting("a", fishing_enabled=False)
            ctl.stop()
            fresh = self.controller(path)
            reordered = [fresh.setting(x) for x in ("b", "a")]
            self.assertEqual(reordered[0]["manor_quantity"], 7)
            self.assertFalse(reordered[1]["fishing_enabled"])
            self.assertEqual(reordered[1]["fishing_profile_id"], "builtin-level-6")

    def test_live_worker_gets_settings_immediately_and_fishing_off_retains_profile(self):
        class FakeWorker:
            def __init__(self): self.applied = []
            def is_alive(self): return True
            def apply_binding(self, binding, announce=False): self.applied.append(dict(binding))
            def join(self, timeout=None): pass
        with tempfile.TemporaryDirectory() as td:
            ctl = self.controller(Path(td) / "settings.json")
            ctl.authorize_launch_transaction(
                [{"entry_id": "a", "identity": "id-a", "path": "a", "name": "A"}],
                [{"hwnd": 101, "pid": 1, "creation_time": self.CREATED, "identity": "id-a"}],
                self.tx(),
            )
            fake = FakeWorker(); ctl.workers[101] = fake
            ctl.update_setting("a", fishing_enabled=True, fishing_profile_id="builtin-level-5")
            self.assertTrue(fake.applied[-1]["fishing_enabled"])
            ctl.update_setting("a", fishing_enabled=False)
            self.assertFalse(fake.applied[-1]["fishing_enabled"])
            self.assertEqual(fake.applied[-1]["fishing_profile_id"], "builtin-level-5")

    def test_revoke_blocks_new_input_and_stops_worker_and_manor(self):
        class FakeWorker:
            def __init__(self): self.joined = 0
            def is_alive(self): return True
            def join(self, timeout=None): self.joined += 1
        class FakeManager:
            _thread = None
            def __init__(self): self.joined = 0
            def join(self, timeout=None): self.joined += 1
        with tempfile.TemporaryDirectory() as td:
            current = {"pid": 1, "creation_time": self.CREATED, "identity": "id-a"}
            ctl = EmbeddedAutomationController(Path(td) / "s.json", lambda _h: True, lambda _h: dict(current))
            ctl._start_worker = lambda _r: None
            self.addCleanup(ctl.stop)
            ctl.authorize_launch_transaction(
                [{"entry_id": "a", "identity": "id-a", "path": "a", "name": "A"}],
                [{"hwnd": 101, "pid": 1, "creation_time": self.CREATED, "identity": "id-a"}],
                self.tx(),
            )
            worker, manager, stop = FakeWorker(), FakeManager(), threading.Event()
            ctl.workers[101] = worker; ctl.worker_stops["a"] = stop; ctl.manors["a"] = (manager, stop)
            current["identity"] = "reused"; ctl._validated_at[101] = 0
            self.assertEqual(ctl.binding_loader(), {})
            self.assertTrue(stop.is_set()); self.assertEqual(worker.joined, 1); self.assertEqual(manager.joined, 1)
            self.assertIsNone(ctl.arbiter.acquire(101, "automation:later", wait=False))

    def test_independent_monitor_revokes_blocked_manor_without_binding_loader(self):
        class FakeWorker:
            def is_alive(self): return True
            def join(self, timeout=None): pass
        class BlockedManor:
            def __init__(self, stop):
                self.stop = stop
                self.cancelled = threading.Event()
                self._thread = threading.Thread(target=self.run, daemon=True)
                self._thread.start()
            def run(self):
                self.stop.wait()
                self.cancelled.set()
            def join(self, timeout=None): self._thread.join(timeout)

        with tempfile.TemporaryDirectory() as td:
            current = {"pid": 1, "creation_time": self.CREATED, "identity": "id-a"}
            ctl = EmbeddedAutomationController(
                Path(td) / "s.json", lambda _h: True, lambda _h: dict(current), monitor_interval=.05,
            )
            ctl._start_worker = lambda _r: None
            self.addCleanup(ctl.stop)
            ctl.authorize_launch_transaction(
                [{"entry_id": "a", "identity": "id-a", "path": "a", "name": "A"}],
                [{"hwnd": 101, "pid": 1, "creation_time": self.CREATED, "identity": "id-a"}],
                self.tx(),
            )
            stop = threading.Event(); manor = BlockedManor(stop)
            ctl.workers[101] = FakeWorker(); ctl.worker_stops["a"] = stop; ctl.manors["a"] = (manor, stop)
            current["creation_time"] = "2026-08-30T04:00:02+00:00"
            deadline = time.monotonic() + .8
            while "a" in ctl.records and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertNotIn("a", ctl.records)
            self.assertTrue(stop.is_set()); self.assertTrue(manor.cancelled.wait(.2))
            later_inputs = 0
            token = ctl.arbiter.acquire(101, "automation:later", wait=False)
            if token is not None:
                later_inputs += 1
            self.assertEqual(later_inputs, 0)

    def test_transaction_evidence_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = self.controller(Path(td) / "s.json")
            entry = [{"entry_id": "a", "identity": "id-a"}]
            candidate = [{"hwnd": 101, "pid": 1, "creation_time": self.CREATED, "identity": "id-a"}]
            for evidence in (None, {}, {"transaction_id": "x"}, {
                "transaction_id": "x", "started": self.START, "before_processes": frozenset()
            }):
                with self.subTest(evidence=evidence):
                    self.assertEqual(ctl.authorize_launch_transaction(entry, candidate, evidence), {})

    def test_transaction_rejects_hwnd_that_was_already_in_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = self.controller(Path(td) / "s.json")
            evidence = dict(self.tx())
            evidence["before_hwnds"] = frozenset({101})
            got = ctl.authorize_launch_transaction(
                [{"entry_id": "a", "identity": "id-a"}],
                [{"hwnd": 101, "pid": 1, "creation_time": self.CREATED, "identity": "id-a"}],
                evidence,
            )
            self.assertEqual(got, {})

    def test_utc_iso_creation_boundaries_and_localized_formats(self):
        self.assertIsNone(EmbeddedAutomationController._parse_utc_iso("2026/8/30 下午 12:01:00"))
        self.assertIsNone(EmbeddedAutomationController._parse_utc_iso("08/30/2026 12:01:00 PM"))
        entry = [{"entry_id": "a", "identity": "id-a"}]
        cases = [
            ("2026-08-30T03:59:59.999999+00:00", False),
            (self.START, True),
            ("2026-08-30T12:00:00+08:00", True),
            (self.CREATED, True),
        ]
        for index, (created, expected) in enumerate(cases):
            with self.subTest(created=created), tempfile.TemporaryDirectory() as td:
                ctl = self.controller(Path(td) / "s.json")
                got = ctl.authorize_launch_transaction(
                    entry, [{"hwnd": 101, "pid": index + 1, "creation_time": created, "identity": "id-a"}],
                    self.tx(txid=f"tx-{index}"),
                )
                self.assertEqual(bool(got), expected)

    def test_overlapping_same_group_callbacks_keep_their_own_transaction(self):
        app = object.__new__(flash_sync_v02.FlashSyncApp)
        app.groups = [flash_sync_v02.SyncGroup("g")]
        app.closing_app = False; app.launch_wait_after_ids = {}
        seen = []
        app.bind_launched_windows_to_group = lambda gi, entries, windows, tx, start_after_ready=False: seen.append((tx["transaction_id"], list(windows)))
        old_enum = flash_sync_v02.enumerate_flash_windows
        try:
            flash_sync_v02.enumerate_flash_windows = lambda: [101]
            tx1, tx2 = self.tx("first"), self.tx("second")
            # Simulate the later transaction completing first.
            app.wait_for_launched_windows(0, [(0, object())], set(), 0, tx2)
            app.wait_for_launched_windows(0, [(0, object())], set(), 0, tx1)
            self.assertEqual([row[0] for row in seen], ["second", "first"])
        finally:
            flash_sync_v02.enumerate_flash_windows = old_enum

    def test_production_creation_date_command_is_invariant_utc_iso(self):
        source = inspect.getsource(flash_sync_v02.flash_process_infos)
        self.assertIn('.ToUniversalTime().ToString("o", [System.Globalization.CultureInfo]::InvariantCulture)', source)
        launch = inspect.getsource(flash_sync_v02.FlashSyncApp.ensure_group_launch_ready)
        self.assertIn("datetime.now(timezone.utc).isoformat()", launch)

    def test_process_snapshot_distinguishes_failure_from_successful_empty(self):
        original = flash_sync_v02.run_powershell_json
        try:
            flash_sync_v02.run_powershell_json = lambda *_a, **_k: None
            self.assertEqual(flash_sync_v02.flash_process_infos(), ({}, False))
            flash_sync_v02.run_powershell_json = lambda *_a, **_k: []
            infos, complete = flash_sync_v02.flash_process_infos()
            self.assertEqual(infos, {}); self.assertTrue(complete)
            with tempfile.TemporaryDirectory() as td:
                ctl = self.controller(Path(td) / "s.json")
                tx = dict(self.tx()); tx["process_snapshot_complete"] = complete
                got = ctl.authorize_launch_transaction(
                    [{"entry_id": "a", "identity": "id-a"}],
                    [{"hwnd": 101, "pid": 9, "creation_time": self.CREATED, "identity": "id-a"}], tx,
                )
                self.assertEqual(got, {"a": 101})
        finally:
            flash_sync_v02.run_powershell_json = original

    def test_failed_snapshot_rejects_preexisting_process_late_hwnd(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = self.controller(Path(td) / "s.json")
            tx = dict(self.tx(before={(7, self.CREATED)}))
            tx["process_snapshot_complete"] = False
            got = ctl.authorize_launch_transaction(
                [{"entry_id": "a", "identity": "id-a"}],
                [{"hwnd": 101, "pid": 7, "creation_time": self.CREATED, "identity": "id-a"}], tx,
            )
            self.assertEqual(got, {})

    def test_close_cancels_all_launch_transactions_and_saved_callbacks_are_inert(self):
        app = object.__new__(flash_sync_v02.FlashSyncApp)
        app.groups = [flash_sync_v02.SyncGroup("g")]
        app.closing_app = False; app.launch_wait_after_ids = {}
        callbacks, cancelled, effects = {}, [], {"authorization": 0, "input": 0}
        def after(_delay, callback):
            aid = f"after-{len(callbacks) + 1}"; callbacks[aid] = callback; return aid
        app.after = after
        app.after_cancel = lambda aid: cancelled.append(aid)
        app.bind_launched_windows_to_group = lambda *_a, **_k: effects.__setitem__("authorization", effects["authorization"] + 1)
        old_enum = flash_sync_v02.enumerate_flash_windows
        try:
            flash_sync_v02.enumerate_flash_windows = lambda: []
            app.wait_for_launched_windows(0, [(0, object())], set(), 0, self.tx("tx-1"))
            app.wait_for_launched_windows(0, [(0, object())], set(), 0, self.tx("tx-2"))
            saved = list(callbacks.values())
            self.assertEqual(set(app.launch_wait_after_ids), {"tx-1", "tx-2"})
            app.closing_app = True
            app.cancel_launch_waits()
            for callback in saved:
                callback()
            self.assertEqual(app.launch_wait_after_ids, {})
            self.assertEqual(len(cancelled), 2)
            self.assertEqual(effects, {"authorization": 0, "input": 0})
        finally:
            flash_sync_v02.enumerate_flash_windows = old_enum

        with tempfile.TemporaryDirectory() as td:
            ctl = self.controller(Path(td) / "s.json")
            ctl.stop()
            got = ctl.authorize_launch_transaction(
                [{"entry_id": "a", "identity": "id-a"}],
                [{"hwnd": 101, "pid": 1, "creation_time": self.CREATED, "identity": "id-a"}], self.tx(),
            )
            self.assertEqual(got, {})
            self.assertEqual(ctl.workers, {}); self.assertEqual(ctl.manors, {})


class ArbiterTests(unittest.TestCase):
    def test_sync_priority_no_interleave_timeout_and_stop_release(self):
        arb = InputLeaseArbiter(timeout=0.08)
        auto = arb.acquire(1, "automation:1", wait=False)
        self.assertIsNotNone(auto)
        result = []

        def sync_waiter():
            token = arb.acquire(1, "sync:mouse", wait=True, timeout=0.5)
            result.append(token)
            if token:
                time.sleep(0.02)
                arb.release(token)

        thread = threading.Thread(target=sync_waiter)
        thread.start()
        time.sleep(0.02)
        self.assertIsNone(arb.acquire(1, "automation:2", wait=False))
        arb.release(auto)
        thread.join(1)
        self.assertTrue(result and result[0])
        stale = arb.acquire(2, "automation:stale", wait=False)
        time.sleep(0.10)
        self.assertIsNotNone(arb.acquire(2, "sync:mouse", wait=False))
        arb.release_all()
        self.assertFalse(arb.valid(stale))

    def test_manor_move_is_arbitrated(self):
        original = win32_api.post_move
        sent = []
        try:
            win32_api.post_move = lambda hwnd, x, y: sent.append((hwnd, x, y)) or True
            with tempfile.TemporaryDirectory() as td:
                ctl = EmbeddedAutomationController(Path(td) / "s.json", lambda _h: True)
                self.addCleanup(ctl.stop)
                token = ctl.arbiter.acquire(101, "sync:mouse", wait=False)
                self.assertFalse(win32_api.post_move(101, 5, 6)); self.assertEqual(sent, [])
                ctl.arbiter.release(token)
                self.assertTrue(win32_api.post_move(101, 5, 6)); self.assertEqual(len(sent), 1)
        finally:
            win32_api.post_move = original

    def test_delayed_sync_callback_is_cancelled_by_stop_generation(self):
        app = object.__new__(flash_sync_v02.FlashSyncApp)
        group = flash_sync_v02.SyncGroup("g", running=True)
        app.groups = [group]; app._sync_generation = {0: 1}; app._sync_timers = {}
        app._sync_mouse_leases = {}; app._sync_keyboard_leases = {}; app.closing_app = False
        app.poll_after_id = None; app.worker_errors = __import__('queue').SimpleQueue()
        app.update_sync_state_text = lambda: None; app.any_group_running = lambda: False
        app.uninstall_mouse_wheel_hook = lambda: None; app.write_log = lambda _x: None
        class A: pass
        app.automation = A(); app.automation.arbiter = InputLeaseArbiter()
        fired = []
        app.run_sync_action(80, lambda: fired.append(1), 0)
        app.stop_sync(0)
        time.sleep(.12)
        self.assertEqual(fired, []); self.assertEqual(app._sync_timers, {})

    def test_repeated_mouse_down_has_one_lease_and_single_up_releases(self):
        app = object.__new__(flash_sync_v02.FlashSyncApp)
        app.closing_app = False; app._sync_mouse_leases = {}; app._sync_keyboard_leases = {}
        class A: pass
        app.automation = A(); app.automation.arbiter = InputLeaseArbiter()
        app.adjusted_click_point = lambda _g, _h, x, y: (True, x, y)
        group = flash_sync_v02.SyncGroup("g")
        old_child, old_user32 = flash_sync_v02.child_at_client_point, flash_sync_v02.user32
        class U:
            def PostMessageW(self, *args): return True
        try:
            flash_sync_v02.child_at_client_point = lambda hwnd, x, y: (hwnd, x, y)
            flash_sync_v02.user32 = U()
            down = flash_sync_v02.MouseMirrorEvent(0, flash_sync_v02.WM_LBUTTONDOWN, 1, 2)
            up = flash_sync_v02.MouseMirrorEvent(0, flash_sync_v02.WM_LBUTTONUP, 1, 2)
            app.post_mouse_event_to_follower(group, 101, down)
            first = app._sync_mouse_leases[101]
            app.post_mouse_event_to_follower(group, 101, down)
            self.assertIs(app._sync_mouse_leases[101], first)
            app.post_mouse_event_to_follower(group, 101, up)
            self.assertNotIn(101, app._sync_mouse_leases)
            later = app.automation.arbiter.acquire(101, "automation:later", wait=False)
            self.assertIsNotNone(later)
        finally:
            flash_sync_v02.child_at_client_point, flash_sync_v02.user32 = old_child, old_user32


class PolicyAndUiTests(unittest.TestCase):
    def test_physical_entry_points_are_fail_fast(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = EmbeddedAutomationController(Path(td) / "settings.json", lambda _h: False)
            self.addCleanup(ctl.stop)
            for fn in (sr.WIO.click_foreground_physical, sr.WIO.send_chat_message_foreground_physical, sr.WIO.press_enter_foreground_physical):
                with self.assertRaises(RuntimeError):
                    fn(1, 1, 1)

    def test_ui_has_section_notice_and_only_manor_fishing_edit_controls(self):
        source = inspect.getsource(flash_sync_v02.FlashSyncApp._build_ui)
        self.assertIn('make_section("自動重連"', source)
        self.assertIn("重啟輔後既有視窗不納管", source)
        self.assertNotIn("automation_reconnect_enabled", source)
        self.assertIn("automation_manor_enabled", source)
        self.assertIn("automation_fishing_enabled", source)

    def test_no_smart_main_or_discovery_connected(self):
        source = inspect.getsource(EmbeddedAutomationController)
        self.assertNotIn("sr.main", source)
        self.assertNotIn("enum_game_windows", source)
        self.assertNotIn("auto_rebind", source)

    def test_strict_authorization_is_only_at_new_launch_transaction_boundary(self):
        launch_source = inspect.getsource(flash_sync_v02.FlashSyncApp.bind_launched_windows_to_group)
        self.assertEqual(launch_source.count("authorize_launch_transaction"), 1)
        for method in (
            flash_sync_v02.FlashSyncApp.live_launch_hwnd_matches,
            flash_sync_v02.FlashSyncApp.add_identity_matched_launch_hwnds,
            flash_sync_v02.FlashSyncApp.add_position_matched_launch_hwnds,
            flash_sync_v02.FlashSyncApp.bind_existing_launch_windows_for_sync,
        ):
            self.assertNotIn("authorize_launch_transaction", inspect.getsource(method))


if __name__ == "__main__":
    unittest.main()
