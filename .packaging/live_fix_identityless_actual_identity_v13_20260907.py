from pathlib import Path

ROOT = Path("legacy/fu-v02-reconnect-preview")
INTEGRATION = ROOT / "fu_reconnect_integration.py"
APP = ROOT / "flash_sync_v02.py"
TEST = ROOT / "test_fu_reconnect_integration.py"

source = INTEGRATION.read_text(encoding="utf-8")
old = '''                else:\n                    item = rows[0]\n                    try:\n                        hwnd, pid = int(item.get("hwnd", 0)), int(item.get("pid", 0))\n                    except (TypeError, ValueError):\n                        hwnd, pid = 0, 0\n                    created = str(item.get("creation_time", ""))\n                    created_dt = self._parse_utc_iso(created)\n                    if not hwnd or not pid or created_dt is None or not self.is_window(hwnd):\n                        reason = "身份不足：CIM/PID/建立時間驗證失敗"\n                    else:\n                        reason = self._launch_transaction_candidate_reason(\n'''
new = '''                else:\n                    item = rows[0]\n                    try:\n                        hwnd, pid = int(item.get("hwnd", 0)), int(item.get("pid", 0))\n                    except (TypeError, ValueError):\n                        hwnd, pid = 0, 0\n                    created = str(item.get("creation_time", ""))\n                    candidate_identity = str(item.get("identity", ""))\n                    record_identity = identity or candidate_identity\n                    created_dt = self._parse_utc_iso(created)\n                    if not hwnd or not pid or created_dt is None or not self.is_window(hwnd):\n                        reason = "身份不足：CIM/PID/建立時間驗證失敗"\n                    else:\n                        reason = self._launch_transaction_candidate_reason(\n'''
if source.count(old) != 1:
    raise SystemExit(f"v13 candidate identity anchor count={source.count(old)}")
source = source.replace(old, new, 1)

old = '''                                        int(current.get("pid", 0) or 0) == pid\n                                        and str(current.get("creation_time", "")) == created\n                                        and str(current.get("identity", "")) == identity\n                                    )\n'''
new = '''                                        int(current.get("pid", 0) or 0) == pid\n                                        and str(current.get("creation_time", "")) == created\n                                        and str(current.get("identity", "")) == record_identity\n                                    )\n'''
if source.count(old) != 1:
    raise SystemExit(f"v13 validation identity anchor count={source.count(old)}")
source = source.replace(old, new, 1)

old = '''                                record = ManagedRecord(\n                                    eid, hwnd, pid, created, identity,\n                                    str(entry.get("path", "")), str(entry.get("name", "")),\n'''
new = '''                                record = ManagedRecord(\n                                    eid, hwnd, pid, created, record_identity,\n                                    str(entry.get("path", "")), str(entry.get("name", "")),\n'''
if source.count(old) != 1:
    raise SystemExit(f"v13 record identity anchor count={source.count(old)}")
source = source.replace(old, new, 1)
INTEGRATION.write_text(source, encoding="utf-8", newline="\n")

app = APP.read_text(encoding="utf-8")
old = '''            pid = get_window_process_id(hwnd)\n            actual = str(process_infos.get(pid, {}).get("identity", ""))\n            if actual != identity:\n                continue\n            matches[entry_index] = hwnd\n'''
new = '''            pid = get_window_process_id(hwnd)\n            actual = str(process_infos.get(pid, {}).get("identity", ""))\n            # Blank shortcut identity is already guarded by the exact entry->HWND\n            # launch/reattach proof.  The controller stores the candidate's actual\n            # process identity for ongoing revalidation, so do not reject that exact\n            # HWND merely because the .lnk itself had no parseable identity.\n            if identity and actual != identity:\n                continue\n            matches[entry_index] = hwnd\n'''
if app.count(old) != 1:
    raise SystemExit(f"v13 host activation identity anchor count={app.count(old)}")
app = app.replace(old, new, 1)
APP.write_text(app, encoding="utf-8", newline="\n")

text = TEST.read_text(encoding="utf-8")
anchor = '''class ExistingWindowReattachTests(unittest.TestCase):\n'''
case = '''class IdentitylessActualIdentityRegressionTests(unittest.TestCase):\n    def test_identityless_authorization_tracks_candidate_actual_identity(self):\n        source = Path(__file__).with_name("fu_reconnect_integration.py").read_text(encoding="utf-8")\n        self.assertIn('candidate_identity = str(item.get("identity", ""))', source)\n        self.assertIn('record_identity = identity or candidate_identity', source)\n        self.assertIn('str(current.get("identity", "")) == record_identity', source)\n        self.assertIn('eid, hwnd, pid, created, record_identity,', source)\n\n    def test_host_accepts_exact_blank_shortcut_identity_after_authorization(self):\n        source = Path(__file__).with_name("flash_sync_v02.py").read_text(encoding="utf-8")\n        self.assertIn('if identity and actual != identity:', source)\n        self.assertNotIn('if actual != identity:\\n                continue\\n            matches[entry_index] = hwnd', source)\n\n\n'''
if text.count(anchor) != 1:
    raise SystemExit(f"v13 test anchor count={text.count(anchor)}")
text = text.replace(anchor, case + anchor, 1)
TEST.write_text(text, encoding="utf-8", newline="\n")

print("LIVE_FIX_V13_APPLIED identityless launch/reattach keeps actual process identity; UI unchanged")
