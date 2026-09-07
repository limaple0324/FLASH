from pathlib import Path

PATCHES = (
    Path('.packaging/live_fix_cumulative_20260906.py'),
    Path('.packaging/live_fix_existing_window_reattach_20260906.py'),
    Path('.packaging/live_fix_existing_window_reattach_test_import_20260906.py'),
    Path('.packaging/live_fix_existing_window_identityless_reattach_v3_20260906.py'),
    Path('.packaging/live_fix_identityless_reattach_helper_20260906.py'),
    Path('.packaging/live_fix_identityless_sync_lifecycle_v11_20260906.py'),
    Path('.packaging/live_fix_identityless_new_launch_v12_20260907.py'),
    Path('.packaging/live_fix_v12_regression_attach_20260907.py'),
    Path('.packaging/live_fix_identityless_actual_identity_v13_20260907.py'),
    Path('.packaging/live_fix_launch_positions_v14_20260907.py'),
    Path('.packaging/live_fix_remove_clock_bar_v15_20260907.py'),
)

for patch in PATCHES:
    if not patch.is_file():
        raise SystemExit(f'missing cumulative repair script: {patch}')
    exec(compile(patch.read_text(encoding='utf-8'), str(patch), 'exec'), {})

print('LIVE_FIX_CUMULATIVE_V15_APPLIED v14 + removed standalone time display stays removed; no other UI changed')
