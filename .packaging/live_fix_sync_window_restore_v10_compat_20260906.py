from pathlib import Path

PATCH = Path('.packaging/live_fix_sync_window_restore_v10_20260906.py')
text = PATCH.read_text(encoding='utf-8')
old = 'anchor_commit = \'\'\'        self.write_log(f"自動重連：本次嚴格身份驗證納管 {len(committed)} 個新視窗。")\\n\'\'\''
new = 'anchor_commit = \'\'\'        self.write_log(f"自動重連：本次嚴格身份驗證納管 {len(committed)} 個視窗。")\\n\'\'\''
if text.count(old) != 1:
    raise SystemExit(f'v10 compatibility anchor count={text.count(old)}')
PATCH.write_text(text.replace(old, new, 1), encoding='utf-8', newline='\n')
print('LIVE_FIX_V10_COMPAT_APPLIED post-v9 reattach log wording')
