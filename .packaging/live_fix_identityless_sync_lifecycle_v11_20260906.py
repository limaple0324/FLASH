from pathlib import Path

ROOT = Path('legacy/fu-v02-reconnect-preview')
APP = ROOT / 'flash_sync_v02.py'
TEST = ROOT / 'test_fu_reconnect_integration.py'

app = APP.read_text(encoding='utf-8')

# Preserve the exact lifecycle proof obtained when an already-open Flash window
# is safely reattached.  The normal sync path can then validate the same HWND
# without requiring the window to be reopened.  This changes no UI/section
# visibility and restores no buttons.
anchor = '''        self.write_log(f"自動重連：本次嚴格身份驗證納管 {len(committed)} 個視窗。")\n'''
insert = '''        for index, hwnd in sorted(matches.items()):\n            if entry_identities.get(int(index), ""):\n                continue\n            lifecycle = snapshot_lifecycles.get(int(hwnd))\n            if lifecycle is None or not (0 <= int(index) < len(group.launch_entries)):\n                continue\n            entry_id = str(group.launch_entries[int(index)].entry_id)\n            if entry_id:\n                group.explicit_launch_bindings[entry_id] = (int(hwnd), lifecycle)\n\n''' + anchor
if app.count(anchor) != 1:
    raise SystemExit(f'v11 lifecycle proof anchor count={app.count(anchor)}')
app = app.replace(anchor, insert, 1)
APP.write_text(app, encoding='utf-8', newline='\n')

test = TEST.read_text(encoding='utf-8')
anchor_test = 'class ExistingWindowReattachTests(unittest.TestCase):\n'
method = '''    def test_identityless_reattach_keeps_lifecycle_proof_for_existing_sync_path_without_ui_restore(self):\n        source = Path(__file__).with_name("flash_sync_v02.py").read_text(encoding="utf-8")\n        self.assertIn('group.explicit_launch_bindings[entry_id] = (int(hwnd), lifecycle)', source)\n        self.assertNotIn('text="開始同步", command=self.toggle_current_sync', source)\n        self.assertNotIn('def toggle_current_sync(', source)\n        self.assertNotIn('self.required_sections = {"組別啟動設定", "同步視窗"}', source)\n\n'''
if test.count(anchor_test) != 1:
    raise SystemExit(f'v11 ExistingWindowReattachTests anchor count={test.count(anchor_test)}')
test = test.replace(anchor_test, anchor_test + method, 1)
TEST.write_text(test, encoding='utf-8', newline='\n')

print('LIVE_FIX_V11_APPLIED identityless sync lifecycle proof only; UI unchanged')
