from pathlib import Path

TEST = Path('legacy/fu-v02-reconnect-preview/test_fu_reconnect_integration.py')
test = TEST.read_text(encoding='utf-8')
old = '''        ctl.stop_event = threading.Event()\n        ctl._lock = threading.RLock()\n        ctl.records = {}\n'''
new = '''        ctl.stop_event = threading.Event()\n        ctl._lock = threading.RLock()\n        ctl._authorization_lock = threading.RLock()\n        ctl.records = {}\n'''
if test.count(old) != 1:
    raise SystemExit(f'identityless-test authorization-lock anchor count={test.count(old)}')
test = test.replace(old, new, 1)
TEST.write_text(test, encoding='utf-8', newline='\n')
print('LIVE_FIX_TEST_APPLIED identityless reattach authorization lock fixture')
