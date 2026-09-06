from pathlib import Path

ROOT = Path("legacy/fu-v02-reconnect-preview")
BASE_PATCH = Path(".packaging/live_fix_runtime_init_20260906.py")
INTEGRATION = ROOT / "fu_reconnect_integration.py"
APP = ROOT / "flash_sync_v02.py"
TEST = ROOT / "test_fu_reconnect_integration.py"

# Always apply the already-verified live repairs first.  This keeps every later
# repair cumulative instead of rebuilding from the exact source and accidentally
# dropping an earlier fix.
if not BASE_PATCH.is_file():
    raise SystemExit("missing cumulative base repair script")
exec(compile(BASE_PATCH.read_text(encoding="utf-8"), str(BASE_PATCH), "exec"), {})

# Add a narrow activity query for adding NEW launch entries.  The existing
# has_active_work() deliberately remains strict because settings import/rewrite
# must still be blocked while any fixed monitoring record exists.
source = INTEGRATION.read_text(encoding="utf-8")
anchor = '''    def has_active_work(self) -> bool:\n'''
helper = '''    def has_launch_add_blocking_work(self) -> bool:\n        """Whether adding a new launch entry could collide with live automation.\n\n        Passive fixed monitoring is intentionally not a blocker.  A worker that\n        is only waiting out the three-minute user-activity guard is also passive.\n        Actual reconnect flow, startup login probing, fishing, manor work, battle\n        restart, retirement, or a provisional managed record remains fail-closed.\n        """\n        with self._lock:\n            if self.closed:\n                return False\n            if (\n                self.manors\n                or self._retirements\n                or self._battle_restarts\n                or self._retirement_finish_threads\n            ):\n                return True\n            records = tuple(self.records.values())\n            workers = tuple(self.workers.values())\n\n        for record in records:\n            try:\n                if not record.ready_event.is_set():\n                    return True\n            except Exception:\n                return True\n\n        try:\n            now = float(self._monotonic_clock())\n        except Exception:\n            return True\n        for worker in workers:\n            try:\n                if float(getattr(worker, "flow_started_at", 0.0) or 0.0) > 0.0:\n                    return True\n                if str(getattr(worker, "state", "監看")) != "監看":\n                    return True\n                if bool(getattr(worker, "fishing_profile", None)):\n                    return True\n                if bool(getattr(worker, "reconnect_enabled", False)) and now <= float(\n                    getattr(worker, "startup_login_probe_until", 0.0) or 0.0\n                ):\n                    return True\n            except Exception:\n                return True\n        return False\n\n'''
if source.count(anchor) != 1:
    raise SystemExit(f"launch-add controller anchor count={source.count(anchor)}")
source = source.replace(anchor, helper + anchor, 1)
INTEGRATION.write_text(source, encoding="utf-8", newline="\n")

app = APP.read_text(encoding="utf-8")
anchor = '''\ndef launch_flow_in_progress(owner) -> bool:\n'''
helper = '''\ndef launch_add_busy(owner) -> bool:\n    """Narrow busy gate for appending launch files.\n\n    All local sync/launch/input transactions still block.  Embedded automation\n    may explicitly report that only passive fixed monitoring remains; older or\n    failed automation implementations fall back to the original strict gate.\n    """\n    state = owner.__dict__\n    automation = state.get("automation")\n    if automation is None:\n        automation_busy = False\n    else:\n        checker = getattr(automation, "has_launch_add_blocking_work", None)\n        if not callable(checker):\n            return launch_structure_busy(owner)\n        try:\n            automation_busy = bool(checker())\n        except Exception:\n            automation_busy = True\n\n    timers = state.get("_sync_timers", {})\n    relogin_after_ids = state.get("relogin_after_ids", {})\n    timed_enabled = state.get("timed_click_enabled")\n    try:\n        timed_click_busy = bool(timed_enabled is not None and timed_enabled.get())\n    except Exception:\n        timed_click_busy = True\n    return bool(\n        any(getattr(group, "running", False) for group in state.get("groups", ()))\n        or state.get("_wheel_start_intents")\n        or state.get("pending_sync_start_groups")\n        or state.get("launch_wait_after_ids")\n        or state.get("_launch_scan_requests")\n        or state.get("_launch_authorization_requests")\n        or state.get("_battle_restart_flows")\n        or state.get("capture_custom_input")\n        or state.get("capture_after_id")\n        or state.get("capture_follower_click")\n        or state.get("capture_follower_after_id")\n        or state.get("capture_window_group_index") is not None\n        or any(bool(items) for items in timers.values())\n        or state.get("_sync_mouse_leases")\n        or state.get("_sync_keyboard_leases")\n        or state.get("_sync_mouse_release_targets")\n        or state.get("_sync_keyboard_release_targets")\n        or state.get("_pending_mouse_releases")\n        or bool(\n            state.get("sync_action_scheduler")\n            and state["sync_action_scheduler"].pending()\n        )\n        or any(bool(items) for items in relogin_after_ids.values())\n        or state.get("relogin_resume_groups")\n        or state.get("autoclick_running")\n        or state.get("_timed_click_batch")\n        or timed_click_busy\n        or automation_busy\n    )\n\n'''
if app.count(anchor) != 1:
    raise SystemExit(f"launch-add UI helper anchor count={app.count(anchor)}")
app = app.replace(anchor, helper + anchor, 1)

old = '''    def add_launch_files(self) -> None:\n        group = self.current_group()\n        if launch_structure_busy(self):\n            messagebox.showwarning(\n                "目前無法加入",\n                "請先停止所有同步、啟動等待與自動重連動作，再加入啟動檔案。",\n            )\n            return\n'''
new = '''    def add_launch_files(self) -> None:\n        group = self.current_group()\n        if launch_add_busy(self):\n            messagebox.showwarning(\n                "目前無法加入",\n                "請先停止同步或等待啟動／正在執行的自動化交易完成，再加入啟動檔案。"\n                "固定監管與使用者操作等待不會阻止加入。",\n            )\n            return\n'''
if app.count(old) != 1:
    raise SystemExit(f"add-launch initial gate anchor count={app.count(old)}")
app = app.replace(old, new, 1)
old = '''        if launch_structure_busy(self) or self.current_group() is not group:\n'''
new = '''        if launch_add_busy(self) or self.current_group() is not group:\n'''
if app.count(old) != 1:
    raise SystemExit(f"add-launch recheck anchor count={app.count(old)}")
app = app.replace(old, new, 1)
APP.write_text(app, encoding="utf-8", newline="\n")

# Regression coverage is deliberately cumulative: verify the new narrow gate,
# and prove that the old strict gate still blocks settings/structural replacement.
test = TEST.read_text(encoding="utf-8")
anchor = '''class StrictRegistryTests(unittest.TestCase):\n'''
case = '''class LiveLaunchAddSafetyTests(unittest.TestCase):\n    @staticmethod\n    def controller_stub(worker=None):\n        ctl = object.__new__(EmbeddedAutomationController)\n        ctl._lock = threading.RLock()\n        ctl.closed = False\n        ctl.manors = {}\n        ctl._retirements = []\n        ctl._battle_restarts = {}\n        ctl._retirement_finish_threads = set()\n        ctl.records = {}\n        ctl.workers = {} if worker is None else {1: worker}\n        ctl._monotonic_clock = lambda: 100.0\n        return ctl\n\n    @staticmethod\n    def passive_worker(**changes):\n        values = dict(\n            flow_started_at=0.0,\n            state="監看",\n            fishing_profile=None,\n            reconnect_enabled=False,\n            startup_login_probe_until=999.0,\n            user_activity_paused=True,\n        )\n        values.update(changes)\n        return SimpleNamespace(**values)\n\n    def test_passive_fixed_monitoring_and_user_guard_allow_only_launch_add(self):\n        ctl = self.controller_stub(self.passive_worker())\n        owner = SimpleNamespace(automation=ctl)\n        self.assertFalse(ctl.has_launch_add_blocking_work())\n        self.assertFalse(flash_sync_v02.launch_add_busy(owner))\n        self.assertTrue(flash_sync_v02.launch_structure_busy(owner))\n\n    def test_idle_enabled_reconnect_after_startup_probe_is_not_a_launch_add_blocker(self):\n        ctl = self.controller_stub(\n            self.passive_worker(reconnect_enabled=True, startup_login_probe_until=50.0)\n        )\n        self.assertFalse(ctl.has_launch_add_blocking_work())\n\n    def test_actual_reconnect_fishing_and_startup_probe_still_block_launch_add(self):\n        cases = (\n            self.passive_worker(\n                reconnect_enabled=True, state="等待登入畫面", flow_started_at=99.0\n            ),\n            self.passive_worker(fishing_profile={"name": "五級魚"}),\n            self.passive_worker(\n                reconnect_enabled=True, startup_login_probe_until=101.0\n            ),\n        )\n        for worker in cases:\n            with self.subTest(worker=worker):\n                ctl = self.controller_stub(worker)\n                self.assertTrue(ctl.has_launch_add_blocking_work())\n                self.assertTrue(flash_sync_v02.launch_add_busy(SimpleNamespace(automation=ctl)))\n\n    def test_provisional_managed_record_still_blocks_launch_add(self):\n        ctl = self.controller_stub()\n        ready = threading.Event()\n        ctl.records = {"entry": SimpleNamespace(ready_event=ready)}\n        self.assertTrue(ctl.has_launch_add_blocking_work())\n        ready.set()\n        self.assertFalse(ctl.has_launch_add_blocking_work())\n\n\n'''
if test.count(anchor) != 1:
    raise SystemExit(f"launch-add regression anchor count={test.count(anchor)}")
test = test.replace(anchor, case + anchor, 1)
TEST.write_text(test, encoding="utf-8", newline="\n")

print("LIVE_FIX_CUMULATIVE_APPLIED runtime-init + no-fly relocation + safe launch-file add")
