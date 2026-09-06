from pathlib import Path

PATCHES = (
    Path('.packaging/live_fix_cumulative_20260906.py'),
    Path('.packaging/live_fix_existing_window_reattach_20260906.py'),
)

for patch in PATCHES:
    if not patch.is_file():
        raise SystemExit(f'missing cumulative v8 repair script: {patch}')
    exec(compile(patch.read_text(encoding='utf-8'), str(patch), 'exec'), {})

print('LIVE_FIX_CUMULATIVE_V8_APPLIED prior cumulative repairs + safe existing-window reattach')
