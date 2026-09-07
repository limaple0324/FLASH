from pathlib import Path

TEST = Path('legacy/fu-v02-reconnect-preview/test_fu_reconnect_integration.py')
text = TEST.read_text(encoding='utf-8')
old_first = 'class IdentitylessNewLaunchTests(unittest.TestCase):\n'
old_second = '\n\nclass ExistingWindowReattachTests(unittest.TestCase):\n'
if text.count(old_first) != 1 or text.count(old_second) != 1:
    raise SystemExit(
        f'v12 regression attach anchors first={text.count(old_first)} second={text.count(old_second)}'
    )
text = text.replace(old_first, 'class ExistingWindowReattachTests(unittest.TestCase):\n', 1)
text = text.replace(old_second, '\n', 1)
TEST.write_text(text, encoding='utf-8', newline='\n')
print('LIVE_FIX_V12_REGRESSION_ATTACHED identityless-new-launch tests run with ExistingWindowReattachTests')
