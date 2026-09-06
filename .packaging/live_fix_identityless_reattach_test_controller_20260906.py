from pathlib import Path

TEST = Path('legacy/fu-v02-reconnect-preview/test_fu_reconnect_integration.py')
test = TEST.read_text(encoding='utf-8')
old = '''    @staticmethod\n    def identityless_controller():\n        ctl = object.__new__(EmbeddedAutomationController)\n        ctl.closed = False\n        ctl.stop_event = threading.Event()\n        ctl._lock = threading.RLock()\n        ctl._authorization_lock = threading.RLock()\n        ctl.records = {}\n        ctl.rejections = {}\n        ctl.arbiter = InputLeaseArbiter()\n        ctl.is_window = lambda hwnd: int(hwnd) == 1001\n        ctl._start_worker = lambda _record: None\n        return ctl\n'''
new = '''    @staticmethod\n    def identityless_controller():\n        ctl = EmbeddedAutomationController(\n            Path(tempfile.gettempdir()) / "fu_identityless_reattach_test_settings.json",\n            lambda hwnd: int(hwnd) == 1001,\n            monitor_interval=3600.0,\n        )\n        ctl._start_worker = lambda _record: None\n        return ctl\n'''
if test.count(old) != 1:
    raise SystemExit(f'identityless-test full-controller anchor count={test.count(old)}')
test = test.replace(old, new, 1)
TEST.write_text(test, encoding='utf-8', newline='\n')
print('LIVE_FIX_TEST_APPLIED fully initialized identityless reattach controller fixture')
