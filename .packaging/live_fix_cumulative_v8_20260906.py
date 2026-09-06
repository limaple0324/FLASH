from pathlib import Path

PATCHES = (
    Path('.packaging/live_fix_cumulative_20260906.py'),
    Path('.packaging/live_fix_existing_window_reattach_20260906.py'),
    Path('.packaging/live_fix_existing_window_reattach_test_import_20260906.py'),
    Path('.packaging/live_fix_existing_window_identityless_reattach_v3_20260906.py'),
    Path('.packaging/live_fix_identityless_reattach_helper_20260906.py'),
    Path('.packaging/live_fix_sync_window_restore_v10_compat_20260906.py'),
    Path('.packaging/live_fix_sync_window_restore_v10_20260906.py'),
)

for patch in PATCHES:
    if not patch.is_file():
        raise SystemExit(f'missing cumulative repair script: {patch}')
    exec(compile(patch.read_text(encoding='utf-8'), str(patch), 'exec'), {})

print('LIVE_FIX_CUMULATIVE_V10_APPLIED v9 + sync-window UI restored + identityless lifecycle proof retained')
