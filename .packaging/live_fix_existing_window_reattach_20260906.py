from pathlib import Path

ROOT = Path("legacy/fu-v02-reconnect-preview")
INTEGRATION = ROOT / "fu_reconnect_integration.py"
APP = ROOT / "flash_sync_v02.py"
TEST = ROOT / "test_fu_reconnect_integration.py"

source = INTEGRATION.read_text(encoding="utf-8")

anchor = '''    def authorize_launch_transaction(\n'''
helper = '''    def managed_entry_hwnds(self) -> dict[str, int]:\n        """Return a stable snapshot of currently committed managed entry HWNDs."""\n        with self._lock:\n            return {\n                str(entry_id): int(record.hwnd)\n                for entry_id, record in self.records.items()\n                if record.ready_event.is_set()\n            }\n\n    @staticmethod\n    def _launch_transaction_candidate_reason(\n        hwnd: int,\n        pid: int,\n        created_dt: datetime,\n        started_dt: datetime,\n        before_hwnds: set[int],\n        before_processes: set[tuple[int, datetime]],\n        allow_existing_reattach: bool,\n        existing_reattach_hwnds: set[int],\n        battle_restart_request_id: str,\n    ) -> str:\n        """Validate whether one candidate is new or an explicitly proven reattach.\n\n        Reattach is intentionally narrower than ordinary live discovery: the HWND\n        must have been present in the immutable pre-launch snapshot, its exact\n        PID/creation tuple must also have been present, and the host must have\n        explicitly whitelisted that HWND after unique shortcut-identity matching.\n        """\n        existing_reattach = bool(\n            allow_existing_reattach and int(hwnd) in existing_reattach_hwnds\n        )\n        if existing_reattach:\n            if battle_restart_request_id:\n                return "既有視窗接回不可混入戰鬥重開交易"\n            if int(hwnd) not in before_hwnds:\n                return "既有視窗接回證據錯誤：窗口不在交易前快照"\n            if (int(pid), created_dt) not in before_processes:\n                return "既有視窗接回證據錯誤：程序建立時間不在交易前快照"\n            if created_dt > started_dt:\n                return "既有視窗接回證據錯誤：程序晚於接回交易建立"\n            return ""\n        if int(hwnd) in before_hwnds:\n            return "身份不足：交易前已存在的視窗"\n        if (int(pid), created_dt) in before_processes:\n            return "身份不足：交易前已存在的程序晚出視窗"\n        if created_dt < started_dt:\n            return "身份不足：程序建立時間早於本次啟動交易"\n        return ""\n\n'''
if source.count(anchor) != 1:
    raise SystemExit(f"existing-reattach helper anchor count={source.count(anchor)}")
source = source.replace(anchor, helper + anchor, 1)

old = '''        battle_restart_request_id = str(\n            transaction.get("battle_restart_request_id", "") or ""\n        )\n        evidence_shape_valid = True\n        try:\n            before_hwnds = {int(hwnd) for hwnd in transaction.get("before_hwnds", set())}\n            before_processes = set()\n'''
new = '''        battle_restart_request_id = str(\n            transaction.get("battle_restart_request_id", "") or ""\n        )\n        allow_existing_reattach = bool(\n            transaction.get("allow_existing_reattach") is True\n        )\n        evidence_shape_valid = True\n        try:\n            before_hwnds = {int(hwnd) for hwnd in transaction.get("before_hwnds", set())}\n            existing_reattach_hwnds = {\n                int(hwnd)\n                for hwnd in transaction.get("existing_reattach_hwnds", set())\n            }\n            if not allow_existing_reattach:\n                existing_reattach_hwnds.clear()\n            before_processes = set()\n'''
if source.count(old) != 1:
    raise SystemExit(f"existing-reattach transaction parse anchor count={source.count(old)}")
source = source.replace(old, new, 1)

old = '''        except (TypeError, ValueError):\n            before_hwnds, before_processes, evidence_shape_valid = set(), set(), False\n        started = str(transaction.get("started", ""))\n'''
new = '''        except (TypeError, ValueError):\n            before_hwnds, before_processes, existing_reattach_hwnds, evidence_shape_valid = (\n                set(), set(), set(), False\n            )\n        started = str(transaction.get("started", ""))\n'''
if source.count(old) != 1:
    raise SystemExit(f"existing-reattach parse failure anchor count={source.count(old)}")
source = source.replace(old, new, 1)

old = '''                    if not hwnd or not pid or created_dt is None or not self.is_window(hwnd):\n                        reason = "身份不足：CIM/PID/建立時間驗證失敗"\n                    elif hwnd in before_hwnds:\n                        reason = "身份不足：交易前已存在的視窗"\n                    elif (pid, created_dt) in before_processes:\n                        reason = "身份不足：交易前已存在的程序晚出視窗"\n                    elif created_dt < started_dt:\n                        reason = "身份不足：程序建立時間早於本次啟動交易"\n                    elif any(record.hwnd == hwnd for record in self.records.values()):\n'''
new = '''                    if not hwnd or not pid or created_dt is None or not self.is_window(hwnd):\n                        reason = "身份不足：CIM/PID/建立時間驗證失敗"\n                    else:\n                        reason = self._launch_transaction_candidate_reason(\n                            hwnd, pid, created_dt, started_dt,\n                            before_hwnds, before_processes,\n                            allow_existing_reattach, existing_reattach_hwnds,\n                            battle_restart_request_id,\n                        )\n                    if not reason and any(\n                        record.hwnd == hwnd for record in self.records.values()\n                    ):\n                        reason = "身份不足：同一原生窗口不可授權給多個啟動項"\n                    if not reason:\n'''
if source.count(old) != 1:
    raise SystemExit(f"existing-reattach candidate policy anchor count={source.count(old)}")
source = source.replace(old, new, 1)

old = '''                        reason = "身份不足：同一原生窗口不可授權給多個啟動項"\n                    else:\n                        exact_marker = input_safety.mark_target(hwnd)\n'''
new = '''                        exact_marker = input_safety.mark_target(hwnd)\n'''
if source.count(old) != 1:
    raise SystemExit(f"existing-reattach duplicate else anchor count={source.count(old)}")
source = source.replace(old, new, 1)

old = '''                "management": "固定監管" if managed else rejections.get(\n                    eid, "重啟輔後既有視窗不納管，須由輔重新開啟",\n                ),\n'''
new = '''                "management": "固定監管" if managed else rejections.get(\n                    eid, "等待安全接回；身份唯一的既有視窗可直接沿用",\n                ),\n'''
if source.count(old) != 1:
    raise SystemExit(f"existing-reattach status text anchor count={source.count(old)}")
source = source.replace(old, new, 1)
INTEGRATION.write_text(source, encoding="utf-8", newline="\n")

app = APP.read_text(encoding="utf-8")
old = '''        automation_tools = self.make_section("自動重連", True)\n        automation_columns = ("entry", "reconnect", "manor", "fishing", "state")\n'''
new = '''        automation_tools = self.make_section("自動重連", True)\n        automation_notice = ttk.Label(\n            automation_tools,\n            text="身份唯一且生命週期可驗證的既有視窗可安全接回；只有缺少或身份衝突的視窗才需要重新開啟。",\n            style="Status.TLabel",\n        )\n        automation_notice.pack(anchor="w", padx=10, pady=(6, 3))\n        automation_columns = ("entry", "reconnect", "manor", "fishing", "state")\n'''
if app.count(old) != 1:
    raise SystemExit(f"existing-reattach UI notice anchor count={app.count(old)}")
app = app.replace(old, new, 1)

old = '''        pending_entries = [\n            (index, entry)\n            for index, entry in enumerate(entries)\n            if index not in existing_matches\n        ]\n        if not pending_entries:\n            if group is self.current_group():\n                self.refresh_group_ui()\n            self.write_log(f"{group.name}已套用 {existing_moved} 個已綁定視窗位置。")\n            if start_after_ready:\n                self.schedule_verified_sync_start(\n                    group_index,\n                    group,\n                    int(snapshot.generation),\n                    launch_entries_signature(group.launch_entries),\n                    100,\n                )\n            else:\n                self.__dict__.setdefault(\n                    "_launch_inflight_group_tokens", set(),\n                ).discard(group_token)\n            return\n        specs = snapshot.specs_dict()\n'''
new = '''        pending_entries = [\n            (index, entry)\n            for index, entry in enumerate(entries)\n            if index not in existing_matches\n        ]\n        managed_hwnds = self.automation.managed_entry_hwnds()\n        reattach_entries = [\n            (index, entry)\n            for index, entry in enumerate(entries)\n            if index in existing_matches\n            and int(managed_hwnds.get(str(entry.entry_id), 0) or 0)\n            != int(existing_matches[index])\n        ]\n        if not pending_entries:\n            if group is self.current_group():\n                self.refresh_group_ui()\n            self.write_log(f"{group.name}已套用 {existing_moved} 個已綁定視窗位置。")\n            if reattach_entries:\n                before = set(snapshot.windows)\n                before_infos = snapshot.process_infos_dict()\n                transaction = MappingProxyType({\n                    "transaction_id": uuid.uuid4().hex,\n                    "started": datetime.now(timezone.utc).isoformat(),\n                    "launch_generation": int(snapshot.generation),\n                    "group_token": id(group),\n                    "entry_signature": launch_entries_signature(group.launch_entries),\n                    "before_hwnds": frozenset(before),\n                    "process_snapshot_complete": True,\n                    "before_processes": frozenset({\n                        (int(pid), str(info.get("creation_time", "")))\n                        for pid, info in before_infos.items()\n                    }),\n                    "allow_existing_reattach": True,\n                    "existing_reattach_hwnds": frozenset(\n                        int(existing_matches[index])\n                        for index, _entry in reattach_entries\n                    ),\n                    "existing_reattach_entry_indexes": frozenset(\n                        int(index) for index, _entry in reattach_entries\n                    ),\n                })\n                self.write_log(\n                    f"{group.name}找到 {len(reattach_entries)} 個身份唯一的既有視窗，正在安全接回固定監管。"\n                )\n                self._finish_bind_launched_windows_to_group(\n                    group_index,\n                    reattach_entries,\n                    [int(existing_matches[index]) for index, _entry in reattach_entries],\n                    transaction,\n                    start_after_ready,\n                    snapshot,\n                )\n                return\n            if start_after_ready:\n                self.schedule_verified_sync_start(\n                    group_index,\n                    group,\n                    int(snapshot.generation),\n                    launch_entries_signature(group.launch_entries),\n                    100,\n                )\n            else:\n                self.__dict__.setdefault(\n                    "_launch_inflight_group_tokens", set(),\n                ).discard(group_token)\n            return\n        specs = snapshot.specs_dict()\n'''
if app.count(old) != 1:
    raise SystemExit(f"existing-reattach no-pending anchor count={app.count(old)}")
app = app.replace(old, new, 1)

old = '''            "before_processes": frozenset({\n                (int(pid), str(info.get("creation_time", "")))\n                for pid, info in before_infos.items()\n            }),\n        })\n'''
new = '''            "before_processes": frozenset({\n                (int(pid), str(info.get("creation_time", "")))\n                for pid, info in before_infos.items()\n            }),\n            "allow_existing_reattach": bool(reattach_entries),\n            "existing_reattach_hwnds": frozenset(\n                int(existing_matches[index])\n                for index, _entry in reattach_entries\n            ),\n            "existing_reattach_entry_indexes": frozenset(\n                int(index) for index, _entry in reattach_entries\n            ),\n        })\n'''
start = app.find('    def _finish_ensure_group_launch_ready(')
end = app.find('    def bind_existing_launch_windows_for_sync(', start)
segment = app[start:end]
if segment.count(old) != 1:
    raise SystemExit(f"existing-reattach launch transaction anchor count={segment.count(old)}")
segment = segment.replace(old, new, 1)
app = app[:start] + segment + app[end:]

old = '''        before_hwnds = {\n            int(hwnd) for hwnd in (transaction or {}).get("before_hwnds", ())\n        }\n        # Use the completed snapshot, not the older wait-loop capture.  A second\n        # same-identity window may appear while this scan is still running.\n        windows = [\n            int(hwnd) for hwnd in snapshot_windows\n            if int(hwnd) not in before_hwnds\n            and int(hwnd) not in blocked_hwnds\n'''
new = '''        before_hwnds = {\n            int(hwnd) for hwnd in (transaction or {}).get("before_hwnds", ())\n        }\n        allow_existing_reattach = bool(\n            (transaction or {}).get("allow_existing_reattach") is True\n            and not str((transaction or {}).get("battle_restart_request_id", "") or "")\n        )\n        existing_reattach_hwnds = {\n            int(hwnd)\n            for hwnd in (transaction or {}).get("existing_reattach_hwnds", ())\n        } if allow_existing_reattach else set()\n        existing_reattach_indexes = {\n            int(index)\n            for index in (transaction or {}).get("existing_reattach_entry_indexes", ())\n        } if allow_existing_reattach else set()\n        if existing_reattach_indexes:\n            combined = {int(index): entry for index, entry in entries}\n            for index in existing_reattach_indexes:\n                if 0 <= index < len(group.launch_entries):\n                    combined[index] = group.launch_entries[index]\n            entries = sorted(combined.items())\n        # Use the completed snapshot, not the older wait-loop capture.  A second\n        # same-identity window may appear while this scan is still running.\n        windows = [\n            int(hwnd) for hwnd in snapshot_windows\n            if (\n                int(hwnd) not in before_hwnds\n                or int(hwnd) in existing_reattach_hwnds\n            )\n            and int(hwnd) not in blocked_hwnds\n'''
if app.count(old) != 1:
    raise SystemExit(f"existing-reattach final snapshot candidate anchor count={app.count(old)}")
app = app.replace(old, new, 1)

old = '''        self.write_log(f"自動重連：本次嚴格身份驗證納管 {len(committed)} 個新視窗。")\n'''
new = '''        self.write_log(f"自動重連：本次嚴格身份驗證納管 {len(committed)} 個視窗。")\n'''
if app.count(old) != 1:
    raise SystemExit(f"existing-reattach commit log anchor count={app.count(old)}")
app = app.replace(old, new, 1)
old = '''        self.write_log(f"{group.name}已套用 {assigned} 個新開啟的 Flash 視窗位置。")\n'''
new = '''        self.write_log(f"{group.name}已套用 {assigned} 個 Flash 視窗位置。")\n'''
if app.count(old) != 1:
    raise SystemExit(f"existing-reattach position log anchor count={app.count(old)}")
app = app.replace(old, new, 1)
APP.write_text(app, encoding="utf-8", newline="\n")

test = TEST.read_text(encoding="utf-8")
anchor = '''class StrictRegistryTests(unittest.TestCase):\n'''
case = '''class ExistingWindowReattachTests(unittest.TestCase):\n    def test_preexisting_candidate_requires_explicit_snapshot_whitelist(self):\n        started = datetime(2026, 9, 6, 6, 0, tzinfo=timezone.utc)\n        created = datetime(2026, 9, 6, 5, 0, tzinfo=timezone.utc)\n        before_hwnds = {1001}\n        before_processes = {(77, created)}\n        reason = EmbeddedAutomationController._launch_transaction_candidate_reason(\n            1001, 77, created, started, before_hwnds, before_processes,\n            True, {1001}, "",\n        )\n        self.assertEqual(reason, "")\n        refused = EmbeddedAutomationController._launch_transaction_candidate_reason(\n            1001, 77, created, started, before_hwnds, before_processes,\n            False, set(), "",\n        )\n        self.assertEqual(refused, "身份不足：交易前已存在的視窗")\n\n    def test_reattach_rejects_missing_process_proof_and_battle_restart_mix(self):\n        started = datetime(2026, 9, 6, 6, 0, tzinfo=timezone.utc)\n        created = datetime(2026, 9, 6, 5, 0, tzinfo=timezone.utc)\n        self.assertIn(\n            "程序建立時間不在交易前快照",\n            EmbeddedAutomationController._launch_transaction_candidate_reason(\n                1001, 77, created, started, {1001}, set(), True, {1001}, "",\n            ),\n        )\n        self.assertEqual(\n            EmbeddedAutomationController._launch_transaction_candidate_reason(\n                1001, 77, created, started, {1001}, {(77, created)},\n                True, {1001}, "battle-1",\n            ),\n            "既有視窗接回不可混入戰鬥重開交易",\n        )\n\n    def test_normal_new_candidate_policy_is_unchanged(self):\n        started = datetime(2026, 9, 6, 6, 0, tzinfo=timezone.utc)\n        created = datetime(2026, 9, 6, 6, 0, 1, tzinfo=timezone.utc)\n        self.assertEqual(\n            EmbeddedAutomationController._launch_transaction_candidate_reason(\n                2002, 88, created, started, {1001}, set(), False, set(), "",\n            ),\n            "",\n        )\n\n    def test_ui_routes_existing_unique_windows_into_same_strict_authorization(self):\n        source = (Path(__file__).parent / "flash_sync_v02.py").read_text(encoding="utf-8")\n        self.assertIn('"allow_existing_reattach": True', source)\n        self.assertIn('"existing_reattach_hwnds": frozenset(', source)\n        self.assertIn('"existing_reattach_entry_indexes": frozenset(', source)\n        self.assertIn('self.automation.managed_entry_hwnds()', source)\n        self.assertIn('身份唯一的既有視窗，正在安全接回固定監管', source)\n        self.assertIn('or int(hwnd) in existing_reattach_hwnds', source)\n        self.assertNotIn('重啟輔後既有視窗不納管', source)\n\n\n'''
if test.count(anchor) != 1:
    raise SystemExit(f"existing-reattach test anchor count={test.count(anchor)}")
test = test.replace(anchor, case + anchor, 1)
TEST.write_text(test, encoding="utf-8", newline="\n")

print("LIVE_FIX_APPLIED safe existing-window reattach without reopening Flash")
