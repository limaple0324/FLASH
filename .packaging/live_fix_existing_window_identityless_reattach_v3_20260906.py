from pathlib import Path

ROOT = Path("legacy/fu-v02-reconnect-preview")
INTEGRATION = ROOT / "fu_reconnect_integration.py"
APP = ROOT / "flash_sync_v02.py"
TEST = ROOT / "test_fu_reconnect_integration.py"

source = INTEGRATION.read_text(encoding="utf-8")

# V8 already proved whether a candidate is an explicitly allowed pre-existing
# HWND. Extend that transaction with an exact entry_id -> HWND binding so a
# shortcut that has no user=/pass= identity can still be reattached safely.
old = '''            existing_reattach_hwnds = {\n                int(hwnd)\n                for hwnd in transaction.get("existing_reattach_hwnds", set())\n            }\n            if not allow_existing_reattach:\n                existing_reattach_hwnds.clear()\n            before_processes = set()\n'''
new = '''            existing_reattach_hwnds = {\n                int(hwnd)\n                for hwnd in transaction.get("existing_reattach_hwnds", set())\n            }\n            existing_reattach_bindings = {\n                str(entry_id): int(hwnd)\n                for entry_id, hwnd in dict(\n                    transaction.get("existing_reattach_bindings", {}) or {}\n                ).items()\n                if str(entry_id)\n            }\n            if not allow_existing_reattach:\n                existing_reattach_hwnds.clear()\n                existing_reattach_bindings.clear()\n            before_processes = set()\n'''
if source.count(old) != 1:
    raise SystemExit(f"identityless-v3 parse anchor count={source.count(old)}")
source = source.replace(old, new, 1)

old = '''        except (TypeError, ValueError):\n            before_hwnds, before_processes, existing_reattach_hwnds, evidence_shape_valid = (\n                set(), set(), set(), False\n            )\n'''
new = '''        except (TypeError, ValueError):\n            before_hwnds, before_processes, existing_reattach_hwnds, existing_reattach_bindings, evidence_shape_valid = (\n                set(), set(), set(), {}, False\n            )\n'''
if source.count(old) != 1:
    raise SystemExit(f"identityless-v3 parse-failure anchor count={source.count(old)}")
source = source.replace(old, new, 1)

old = '''                rows = candidate_by_identity.get(identity, []) if identity else []\n'''
new = '''                explicit_reattach_hwnd = int(\n                    existing_reattach_bindings.get(eid, 0) or 0\n                ) if eid else 0\n                identityless_reattach = bool(\n                    allow_existing_reattach\n                    and eid\n                    and not identity\n                    and explicit_reattach_hwnd\n                    and explicit_reattach_hwnd in existing_reattach_hwnds\n                )\n                if identityless_reattach:\n                    rows = []\n                    for item in candidates:\n                        try:\n                            candidate_hwnd = int(item.get("hwnd", 0) or 0)\n                        except (TypeError, ValueError):\n                            candidate_hwnd = 0\n                        if candidate_hwnd == explicit_reattach_hwnd:\n                            rows.append(item)\n                else:\n                    rows = candidate_by_identity.get(identity, []) if identity else []\n'''
if source.count(old) != 1:
    raise SystemExit(f"identityless-v3 rows anchor count={source.count(old)}")
source = source.replace(old, new, 1)

# Do not depend on the exact V8 conditional formatting. Locate the one rejection
# line and replace its immediately preceding condition while preserving whether
# V8 made it an if or elif.
lines = source.splitlines(keepends=True)
reason_indexes = [
    i for i, line in enumerate(lines)
    if '身份不足：entry_id或identity空白' in line
]
if len(reason_indexes) != 1:
    raise SystemExit(f"identityless-v3 blank-id reason count={len(reason_indexes)}")
i = reason_indexes[0]
if i < 1:
    raise SystemExit("identityless-v3 blank-id reason has no condition")
condition = lines[i - 1]
stripped = condition.lstrip()
indent = condition[: len(condition) - len(stripped)]
if not (('eid' in stripped and 'identity' in stripped) and (stripped.startswith('if ') or stripped.startswith('elif '))):
    raise SystemExit(f"identityless-v3 unexpected blank-id condition: {stripped!r}")
keyword = 'elif' if stripped.startswith('elif ') else 'if'
replacement = [
    f'{indent}{keyword} not eid:\n',
    f'{indent}    reason = "身份不足：entry_id空白"\n',
    f'{indent}elif not identity and not identityless_reattach:\n',
    f'{indent}    reason = "身份不足：identity空白且沒有明確既有視窗接回證據"\n',
]
lines[i - 1:i + 1] = replacement
source = ''.join(lines)

# Blank identity is allowed only for the explicit exact reattach branch. Avoid
# treating all blank identities as a duplicate identity set.
needle = 'len(identities.get(identity, [])) != 1:'
lines = source.splitlines(keepends=True)
matching = [i for i, line in enumerate(lines) if needle in line]
if len(matching) != 1:
    raise SystemExit(f"identityless-v3 duplicate-identity line count={len(matching)}")
i = matching[0]
line = lines[i]
if 'identity and ' not in line:
    lines[i] = line.replace('len(identities.get(identity, [])) != 1:', 'identity and len(identities.get(identity, [])) != 1:', 1)
source = ''.join(lines)
INTEGRATION.write_text(source, encoding="utf-8", newline="\n")

app = APP.read_text(encoding="utf-8")

# User explicitly requested that the orange explanatory strip be removed.
notice = '''        automation_notice = ttk.Label(\n            automation_tools,\n            text="身份唯一且生命週期可驗證的既有視窗可安全接回；只有缺少或身份衝突的視窗才需要重新開啟。",\n            style="Status.TLabel",\n        )\n        automation_notice.pack(anchor="w", padx=10, pady=(6, 3))\n'''
if app.count(notice) != 1:
    raise SystemExit(f"identityless-v3 notice anchor count={app.count(notice)}")
app = app.replace(notice, "", 1)

# V8 has two reattach transaction construction sites. Add an immutable exact
# binding in both, keyed by the persistent LaunchEntry.entry_id.
anchor1 = '''                    "existing_reattach_entry_indexes": frozenset(\n                        int(index) for index, _entry in reattach_entries\n                    ),\n'''
insert1 = anchor1 + '''                    "existing_reattach_bindings": {\n                        str(entry.entry_id): int(existing_matches[index])\n                        for index, entry in reattach_entries\n                    },\n'''
if app.count(anchor1) != 1:
    raise SystemExit(f"identityless-v3 no-pending binding anchor count={app.count(anchor1)}")
app = app.replace(anchor1, insert1, 1)

anchor2 = '''            "existing_reattach_entry_indexes": frozenset(\n                int(index) for index, _entry in reattach_entries\n            ),\n'''
insert2 = anchor2 + '''            "existing_reattach_bindings": {\n                str(entry.entry_id): int(existing_matches[index])\n                for index, entry in reattach_entries\n            },\n'''
if app.count(anchor2) != 1:
    raise SystemExit(f"identityless-v3 mixed binding anchor count={app.count(anchor2)}")
app = app.replace(anchor2, insert2, 1)
APP.write_text(app, encoding="utf-8", newline="\n")

# Add the three cases to the test class already executed by the cumulative gate.
test = TEST.read_text(encoding="utf-8")
anchor = '''class ExistingWindowReattachTests(unittest.TestCase):\n'''
methods = '''class ExistingWindowReattachTests(unittest.TestCase):\n    @staticmethod\n    def identityless_controller():\n        ctl = object.__new__(EmbeddedAutomationController)\n        ctl.closed = False\n        ctl.stop_event = threading.Event()\n        ctl._lock = threading.RLock()\n        ctl.records = {}\n        ctl.rejections = {}\n        ctl.arbiter = InputLeaseArbiter()\n        ctl.is_window = lambda hwnd: int(hwnd) == 1001\n        ctl._start_worker = lambda _record: None\n        return ctl\n\n    @staticmethod\n    def identityless_transaction(**changes):\n        created = "2026-09-06T05:00:00+00:00"\n        tx = {\n            "transaction_id": "reattach-identityless-1",\n            "started": "2026-09-06T06:00:00+00:00",\n            "before_hwnds": {1001},\n            "before_processes": {(77, created)},\n            "process_snapshot_complete": True,\n            "allow_existing_reattach": True,\n            "existing_reattach_hwnds": {1001},\n            "existing_reattach_bindings": {"entry-a": 1001},\n        }\n        tx.update(changes)\n        return tx\n\n    @staticmethod\n    def identityless_entry():\n        return {"entry_id": "entry-a", "identity": "", "path": r"C:\\Games\\120古.lnk", "name": "120古"}\n\n    @staticmethod\n    def identityless_candidate():\n        return {"hwnd": 1001, "pid": 77, "creation_time": "2026-09-06T05:00:00+00:00", "identity": ""}\n\n    def test_explicit_existing_binding_allows_blank_account_identity(self):\n        ctl = self.identityless_controller()\n        accepted = ctl.authorize_launch_transaction(\n            [self.identityless_entry()], [self.identityless_candidate()], self.identityless_transaction()\n        )\n        self.assertEqual(accepted, {"entry-a": 1001})\n        self.assertEqual(ctl.records["entry-a"].hwnd, 1001)\n\n    def test_blank_identity_without_exact_entry_hwnd_binding_is_rejected(self):\n        ctl = self.identityless_controller()\n        accepted = ctl.authorize_launch_transaction(\n            [self.identityless_entry()], [self.identityless_candidate()],\n            self.identityless_transaction(existing_reattach_bindings={}),\n        )\n        self.assertEqual(accepted, {})\n        self.assertIn("identity空白", ctl.rejections.get("entry-a", ""))\n\n    def test_blank_identity_stays_rejected_for_normal_new_launch(self):\n        ctl = self.identityless_controller()\n        candidate = self.identityless_candidate()\n        candidate["creation_time"] = "2026-09-06T06:00:01+00:00"\n        tx = {\n            "transaction_id": "new-identityless-1",\n            "started": "2026-09-06T06:00:00+00:00",\n            "before_hwnds": set(),\n            "before_processes": set(),\n            "process_snapshot_complete": True,\n        }\n        accepted = ctl.authorize_launch_transaction([self.identityless_entry()], [candidate], tx)\n        self.assertEqual(accepted, {})\n        self.assertIn("identity空白", ctl.rejections.get("entry-a", ""))\n\n'''
if test.count(anchor) != 1:
    raise SystemExit(f"identityless-v3 test anchor count={test.count(anchor)}")
test = test.replace(anchor, methods, 1)
TEST.write_text(test, encoding="utf-8", newline="\n")

print("LIVE_FIX_APPLIED robust exact blank-identity existing-window reattach + removed orange notice")
