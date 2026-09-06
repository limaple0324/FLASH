from pathlib import Path

PATCHES = (
    Path('.packaging/live_fix_cumulative_v8_20260906.py'),
    Path('.packaging/live_fix_existing_window_identityless_reattach_20260906.py'),
)

for patch in PATCHES:
    if not patch.is_file():
        raise SystemExit(f'missing cumulative v9 repair script: {patch}')
    exec(compile(patch.read_text(encoding='utf-8'), str(patch), 'exec'), {})

print('LIVE_FIX_CUMULATIVE_V9_APPLIED v8 + blank-identity exact existing-window reattach + no notice strip')
