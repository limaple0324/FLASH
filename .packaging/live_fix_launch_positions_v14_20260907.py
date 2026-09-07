from pathlib import Path

ROOT = Path("legacy/fu-v02-reconnect-preview")
APP = ROOT / "flash_sync_v02.py"
TEST = ROOT / "test_fu_reconnect_integration.py"

app = APP.read_text(encoding="utf-8")

# Restore a strict UI invariant: pressing 「啟動本組」 must apply every safely
# matched window's recorded rectangle immediately.  Do not make layout depend on
# whether a later automation-management/reattach transaction is still pending.
anchor = '''    def _schedule_verified_launch_rect(\n'''
helper = '''    def _apply_verified_recorded_positions_now(\n        self,\n        group: SyncGroup,\n        entries: list[LaunchEntry],\n        matches: dict[int, int],\n        snapshot: LaunchEnvironmentSnapshot,\n    ) -> int:\n        \"\"\"Apply saved launch rectangles synchronously to snapshot-proven HWNDs.\n\n        Position restore is a launch-layout action, not an authorization decision.\n        It still requires the immutable snapshot lifecycle + exact native marker,\n        but it must not be lost when a following reattach/new-window scan cancels\n        delayed move callbacks.\n        \"\"\"\n        lifecycles = snapshot.window_lifecycles_dict()\n        exacts = snapshot.window_exact_identities_dict()\n        applied = 0\n        for index, hwnd in sorted(matches.items()):\n            index = int(index)\n            hwnd = int(hwnd)\n            if not (0 <= index < len(entries)) or not hwnd or not user32.IsWindow(hwnd):\n                continue\n            lifecycle = lifecycles.get(hwnd)\n            exact_identity = exacts.get(hwnd)\n            if (\n                lifecycle is None\n                or not isinstance(exact_identity, ExactWindowIdentity)\n                or exact_identity.lifecycle != lifecycle\n                or window_lifecycle_identity(hwnd) != lifecycle\n                or not exact_window_identity_matches(hwnd, exact_identity)\n            ):\n                continue\n            entry = entries[index]\n            try:\n                if user32.IsIconic(hwnd) or user32.IsZoomed(hwnd):\n                    user32.ShowWindow(hwnd, SW_RESTORE)\n                if set_window_recorded_rect(\n                    hwnd, entry.x, entry.y, entry.width, entry.height,\n                ):\n                    applied += 1\n            except Exception:\n                continue\n        return applied\n\n'''
if app.count(anchor) != 1:
    raise SystemExit(f"v14 helper anchor count={app.count(anchor)}")
app = app.replace(anchor, helper + anchor, 1)

old = '''        existing_matches = self._live_launch_hwnd_matches_from_snapshot(\n            group,\n            entries,\n            snapshot,\n            allow_locked_master_identity_match=True,\n        )\n        existing_moved = self._apply_launch_entry_matches_verified(\n'''
new = '''        existing_matches = self._live_launch_hwnd_matches_from_snapshot(\n            group,\n            entries,\n            snapshot,\n            allow_locked_master_identity_match=True,\n        )\n        # Apply the saved layout before any later launch/reattach scan can cancel\n        # the delayed position callbacks. This preserves the long-standing\n        # 「啟動本組 → 回到記錄位置」 behavior.\n        self._apply_verified_recorded_positions_now(\n            group, entries, existing_matches, snapshot,\n        )\n        existing_moved = self._apply_launch_entry_matches_verified(\n'''
if app.count(old) != 1:
    raise SystemExit(f"v14 existing position anchor count={app.count(old)}")
app = app.replace(old, new, 1)

old = '''        self.write_log(f"自動重連：本次嚴格身份驗證納管 {len(committed)} 個視窗。")\n        for slot, (index, hwnd) in enumerate(sorted(matches.items())):\n'''
new = '''        self.write_log(f"自動重連：本次嚴格身份驗證納管 {len(committed)} 個視窗。")\n        # Newly launched/reattached windows also jump to their recorded layout\n        # immediately after commit; delayed retries below remain as stabilization.\n        self._apply_verified_recorded_positions_now(\n            group, group.launch_entries, matches, snapshot,\n        )\n        for slot, (index, hwnd) in enumerate(sorted(matches.items())):\n'''
if app.count(old) != 1:
    raise SystemExit(f"v14 committed position anchor count={app.count(old)}")
app = app.replace(old, new, 1)
APP.write_text(app, encoding="utf-8", newline="\n")

text = TEST.read_text(encoding="utf-8")
anchor = '''class ExistingWindowReattachTests(unittest.TestCase):\n'''
case = '''class LaunchPositionRestoreRegressionTests(unittest.TestCase):\n    def test_launch_group_restores_saved_rect_before_reattach_or_rescan(self):\n        source = Path(__file__).with_name("flash_sync_v02.py").read_text(encoding="utf-8")\n        finish = source.split("    def _finish_ensure_group_launch_ready(", 1)[1].split(\n            "    def bind_existing_launch_windows_for_sync(", 1\n        )[0]\n        match_pos = finish.index("existing_matches = self._live_launch_hwnd_matches_from_snapshot(")\n        restore_pos = finish.index("self._apply_verified_recorded_positions_now(")\n        pending_pos = finish.index("pending_entries = [")\n        self.assertLess(match_pos, restore_pos)\n        self.assertLess(restore_pos, pending_pos)\n\n    def test_verified_position_restore_uses_recorded_rectangle_and_exact_snapshot_proof(self):\n        source = Path(__file__).with_name("flash_sync_v02.py").read_text(encoding="utf-8")\n        helper = source.split("    def _apply_verified_recorded_positions_now(", 1)[1].split(\n            "    def _schedule_verified_launch_rect(", 1\n        )[0]\n        self.assertIn("snapshot.window_lifecycles_dict()", helper)\n        self.assertIn("snapshot.window_exact_identities_dict()", helper)\n        self.assertIn("exact_window_identity_matches(hwnd, exact_identity)", helper)\n        self.assertIn("set_window_recorded_rect(", helper)\n        self.assertIn("entry.x, entry.y, entry.width, entry.height", helper)\n\n    def test_post_authorization_position_restore_is_immediate_and_retry_stays(self):\n        source = Path(__file__).with_name("flash_sync_v02.py").read_text(encoding="utf-8")\n        finish = source.split("    def _finish_apply_launch_authorization(", 1)[1].split(\n            "    def _apply_verified_recorded_positions_now(", 1\n        )[0]\n        self.assertIn("self._apply_verified_recorded_positions_now(", finish)\n        self.assertIn("self._schedule_verified_launch_rect(", finish)\n\n\n'''
if text.count(anchor) != 1:
    raise SystemExit(f"v14 test anchor count={text.count(anchor)}")
text = text.replace(anchor, case + anchor, 1)
TEST.write_text(text, encoding="utf-8", newline="\n")

print("LIVE_FIX_V14_APPLIED launch-group saved positions restored immediately; cumulative UI unchanged")
