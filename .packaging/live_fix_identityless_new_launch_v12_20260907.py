from pathlib import Path

ROOT = Path("legacy/fu-v02-reconnect-preview")
INTEGRATION = ROOT / "fu_reconnect_integration.py"
APP = ROOT / "flash_sync_v02.py"
TEST = ROOT / "test_fu_reconnect_integration.py"

source = INTEGRATION.read_text(encoding="utf-8")
anchor = '''    def authorize_launch_transaction(\n'''
helper = '''    @staticmethod\n    def _identityless_new_launch_hwnd(\n        entry_id: str,\n        identity: str,\n        allow_identityless_new_launch: bool,\n        identityless_new_launch_bindings: dict[str, int],\n    ) -> int:\n        """Return the host-proven HWND for a blank-identity window just launched by Fu.\n\n        The host may supply this mapping only at the launch-transaction boundary,\n        after excluding all pre-existing HWNDs. The normal lifecycle checks below\n        still prove PID/creation time and reject any pre-transaction process.\n        """\n        if identity or not allow_identityless_new_launch or not entry_id:\n            return 0\n        try:\n            return int(identityless_new_launch_bindings.get(str(entry_id), 0) or 0)\n        except (TypeError, ValueError):\n            return 0\n\n'''
if source.count(anchor) != 1:
    raise SystemExit(f"v12 authorize anchor count={source.count(anchor)}")
source = source.replace(anchor, helper + anchor, 1)

old = '''        allow_existing_reattach = bool(\n            transaction.get("allow_existing_reattach") is True\n        )\n        evidence_shape_valid = True\n'''
new = '''        allow_existing_reattach = bool(\n            transaction.get("allow_existing_reattach") is True\n        )\n        allow_identityless_new_launch = bool(\n            transaction.get("allow_identityless_new_launch") is True\n        )\n        evidence_shape_valid = True\n'''
if source.count(old) != 1:
    raise SystemExit(f"v12 transaction flag anchor count={source.count(old)}")
source = source.replace(old, new, 1)

old = '''            existing_reattach_bindings = {\n                str(entry_id): int(hwnd)\n                for entry_id, hwnd in dict(\n                    transaction.get("existing_reattach_bindings", {}) or {}\n                ).items()\n                if str(entry_id)\n            }\n            if not allow_existing_reattach:\n                existing_reattach_hwnds.clear()\n                existing_reattach_bindings.clear()\n            before_processes = set()\n'''
new = '''            existing_reattach_bindings = {\n                str(entry_id): int(hwnd)\n                for entry_id, hwnd in dict(\n                    transaction.get("existing_reattach_bindings", {}) or {}\n                ).items()\n                if str(entry_id)\n            }\n            identityless_new_launch_bindings = {\n                str(entry_id): int(hwnd)\n                for entry_id, hwnd in dict(\n                    transaction.get("identityless_new_launch_bindings", {}) or {}\n                ).items()\n                if str(entry_id)\n            }\n            if not allow_existing_reattach:\n                existing_reattach_hwnds.clear()\n                existing_reattach_bindings.clear()\n            if not allow_identityless_new_launch:\n                identityless_new_launch_bindings.clear()\n            before_processes = set()\n'''
if source.count(old) != 1:
    raise SystemExit(f"v12 binding parse anchor count={source.count(old)}")
source = source.replace(old, new, 1)

old = '''        except (TypeError, ValueError):\n            before_hwnds, before_processes, existing_reattach_hwnds, existing_reattach_bindings, evidence_shape_valid = (\n                set(), set(), set(), {}, False\n            )\n'''
new = '''        except (TypeError, ValueError):\n            before_hwnds, before_processes, existing_reattach_hwnds, existing_reattach_bindings, identityless_new_launch_bindings, evidence_shape_valid = (\n                set(), set(), set(), {}, {}, False\n            )\n'''
if source.count(old) != 1:
    raise SystemExit(f"v12 parse failure anchor count={source.count(old)}")
source = source.replace(old, new, 1)

old = '''                identityless_reattach = bool(explicit_reattach_hwnd)\n                if identityless_reattach:\n                    rows = []\n                    for item in candidates:\n                        try:\n                            candidate_hwnd = int(item.get("hwnd", 0) or 0)\n                        except (TypeError, ValueError):\n                            candidate_hwnd = 0\n                        if candidate_hwnd == explicit_reattach_hwnd:\n                            rows.append(item)\n                else:\n                    rows = candidate_by_identity.get(identity, []) if identity else []\n'''
new = '''                identityless_reattach = bool(explicit_reattach_hwnd)\n                identityless_new_launch_hwnd = self._identityless_new_launch_hwnd(\n                    eid, identity, allow_identityless_new_launch,\n                    identityless_new_launch_bindings,\n                )\n                identityless_new_launch = bool(identityless_new_launch_hwnd)\n                if identityless_reattach or identityless_new_launch:\n                    exact_hwnd = explicit_reattach_hwnd or identityless_new_launch_hwnd\n                    rows = []\n                    for item in candidates:\n                        try:\n                            candidate_hwnd = int(item.get("hwnd", 0) or 0)\n                        except (TypeError, ValueError):\n                            candidate_hwnd = 0\n                        if candidate_hwnd == exact_hwnd:\n                            rows.append(item)\n                else:\n                    rows = candidate_by_identity.get(identity, []) if identity else []\n'''
if source.count(old) != 1:
    raise SystemExit(f"v12 identityless candidate anchor count={source.count(old)}")
source = source.replace(old, new, 1)

old = '''                elif not identity and not identityless_reattach:\n                    reason = "身份不足：identity空白且沒有明確既有視窗接回證據"\n'''
new = '''                elif not identity and not identityless_reattach and not identityless_new_launch:\n                    reason = "身份不足：identity空白且沒有明確啟動／接回綁定證據"\n'''
if source.count(old) != 1:
    raise SystemExit(f"v12 blank identity policy anchor count={source.count(old)}")
source = source.replace(old, new, 1)
INTEGRATION.write_text(source, encoding="utf-8", newline="\n")

app = APP.read_text(encoding="utf-8")
anchor = '''        def validate_snapshot_candidate(hwnd: int) -> dict:\n'''
insert = '''        # Blank shortcut identity is common in the user's .lnk files.  For windows\n        # created by this exact Fu launch transaction, derive the same deterministic\n        # entry->HWND mapping the host uses, but only when every remaining new Flash\n        # window can be accounted for one-to-one. Extra/ambiguous windows fail closed.\n        authorization_transaction = dict(transaction or {})\n        entry_identity_counts: dict[str, int] = {}\n        candidate_identity_hwnds: dict[str, list[int]] = {}\n        for identity in entry_identities.values():\n            if identity:\n                entry_identity_counts[identity] = entry_identity_counts.get(identity, 0) + 1\n        for row in strict_candidates:\n            identity = str(row.get("identity", ""))\n            if not identity:\n                continue\n            try:\n                candidate_hwnd = int(row.get("hwnd", 0) or 0)\n            except (TypeError, ValueError):\n                candidate_hwnd = 0\n            if candidate_hwnd:\n                candidate_identity_hwnds.setdefault(identity, []).append(candidate_hwnd)\n        reserved_identity_hwnds: set[int] = set()\n        for entry_index, _entry in entries:\n            identity = entry_identities.get(entry_index, "")\n            hwnds = candidate_identity_hwnds.get(identity, []) if identity else []\n            if (\n                identity\n                and entry_identity_counts.get(identity) == 1\n                and len(hwnds) == 1\n            ):\n                reserved_identity_hwnds.add(int(hwnds[0]))\n        new_blank_entries = [\n            (entry_index, entry)\n            for entry_index, entry in entries\n            if not entry_identities.get(entry_index, "")\n            and int(entry_index) not in existing_reattach_indexes\n        ]\n        remaining_new_hwnds = sorted(\n            (\n                int(hwnd) for hwnd in windows\n                if int(hwnd) not in reserved_identity_hwnds\n                and int(hwnd) not in existing_reattach_hwnds\n            ),\n            key=self.window_sort_key,\n        )\n        if new_blank_entries and len(remaining_new_hwnds) == len(new_blank_entries):\n            blank_bindings = {\n                str(entry.entry_id): int(hwnd)\n                for (_entry_index, entry), hwnd in zip(\n                    new_blank_entries, remaining_new_hwnds,\n                )\n                if str(entry.entry_id)\n            }\n            if len(blank_bindings) == len(new_blank_entries):\n                authorization_transaction["allow_identityless_new_launch"] = True\n                authorization_transaction["identityless_new_launch_bindings"] = blank_bindings\n\n'''
if app.count(anchor) != 1:
    raise SystemExit(f"v12 host mapping anchor count={app.count(anchor)}")
app = app.replace(anchor, insert + anchor, 1)

old = '''            transaction,\n            validate_snapshot_candidate,\n'''
new = '''            authorization_transaction,\n            validate_snapshot_candidate,\n'''
pos = app.find('        self.request_launch_authorization(')
pos2 = app.find(old, pos)
if pos < 0 or pos2 < 0:
    raise SystemExit("v12 request authorization transaction anchor missing")
app = app[:pos2] + app[pos2:].replace(old, new, 1)

old = '''            if (\n                not identity\n                or identity_counts.get(identity) != 1\n                or candidate_counts.get(identity) != 1\n                or not hwnd\n'''
new = '''            if (\n                (\n                    identity\n                    and (\n                        identity_counts.get(identity) != 1\n                        or candidate_counts.get(identity) != 1\n                    )\n                )\n                or not hwnd\n'''
if app.count(old) != 1:
    raise SystemExit(f"v12 host activation anchor count={app.count(old)}")
app = app.replace(old, new, 1)
APP.write_text(app, encoding="utf-8", newline="\n")

text = TEST.read_text(encoding="utf-8")
anchor = '''class ExistingWindowReattachTests(unittest.TestCase):\n'''
case = '''class IdentitylessNewLaunchTests(unittest.TestCase):\n    def test_blank_identity_new_launch_requires_explicit_host_binding(self):\n        helper = EmbeddedAutomationController._identityless_new_launch_hwnd\n        self.assertEqual(helper("entry-a", "", True, {"entry-a": 1001}), 1001)\n        self.assertEqual(helper("entry-a", "", False, {"entry-a": 1001}), 0)\n        self.assertEqual(helper("entry-a", "identity", True, {"entry-a": 1001}), 0)\n        self.assertEqual(helper("entry-a", "", True, {}), 0)\n\n    def test_host_supplies_blank_identity_binding_only_for_exact_new_window_set(self):\n        source = Path(__file__).with_name("flash_sync_v02.py").read_text(encoding="utf-8")\n        self.assertIn('authorization_transaction["allow_identityless_new_launch"] = True', source)\n        self.assertIn('authorization_transaction["identityless_new_launch_bindings"] = blank_bindings', source)\n        self.assertIn('len(remaining_new_hwnds) == len(new_blank_entries)', source)\n        self.assertIn('and int(hwnd) not in existing_reattach_hwnds', source)\n        self.assertNotIn('self.required_sections = {"組別啟動設定", "同步視窗"}', source)\n\n\n'''
if text.count(anchor) != 1:
    raise SystemExit(f"v12 test anchor count={text.count(anchor)}")
text = text.replace(anchor, case + anchor, 1)
TEST.write_text(text, encoding="utf-8", newline="\n")

print("LIVE_FIX_V12_APPLIED blank-identity windows launched by Fu become strict managed records; UI unchanged")
