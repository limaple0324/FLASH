from pathlib import Path

ROOT = Path('legacy/fu-v02-reconnect-preview')
APP = ROOT / 'flash_sync_v02.py'
TEST = ROOT / 'test_fu_reconnect_integration.py'

app = APP.read_text(encoding='utf-8')
old = '        self.required_sections = {"組別啟動設定", "同步視窗"}\n'
new = '        self.required_sections = set()\n'
if app.count(old) != 1:
    raise SystemExit(f'v11 section visibility anchor count={app.count(old)}')
app = app.replace(old, new, 1)
APP.write_text(app, encoding='utf-8', newline='\n')

test = TEST.read_text(encoding='utf-8')
old_assert = '        self.assertIn(\'self.required_sections = {"組別啟動設定", "同步視窗"}\', source)\n'
new_assert = '        self.assertIn(\'self.required_sections = set()\', source)\n'
if test.count(old_assert) != 1:
    raise SystemExit(f'v11 required-section regression anchor count={test.count(old_assert)}')
test = test.replace(old_assert, new_assert, 1)

anchor = 'class StrictRegistryTests(unittest.TestCase):\n'
case = '''class SectionVisibilityRegressionTests(unittest.TestCase):\n    def test_group_launch_and_sync_sections_are_user_toggleable_not_forced_visible(self):\n        source = Path(__file__).with_name("flash_sync_v02.py").read_text(encoding="utf-8")\n        self.assertIn('self.required_sections = set()', source)\n        self.assertIn('make_section("組別啟動設定",', source)\n        self.assertIn('make_section("同步視窗", True)', source)\n        self.assertNotIn('self.required_sections = {"組別啟動設定"', source)\n        self.assertNotIn('self.required_sections = {"同步視窗"', source)\n\n\n'''
if test.count(anchor) != 1:
    raise SystemExit(f'v11 section regression class anchor count={test.count(anchor)}')
test = test.replace(anchor, case + anchor, 1)
TEST.write_text(test, encoding='utf-8', newline='\n')

print('LIVE_FIX_V11_APPLIED restored user-toggleable section visibility; no forced group/sync section')
