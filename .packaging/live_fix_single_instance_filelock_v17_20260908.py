from pathlib import Path

ROOT = Path('legacy/fu-v02-reconnect-preview')
APP = ROOT / 'flash_sync_v02.py'
TEST = ROOT / 'test_fu_reconnect_integration.py'

app = APP.read_text(encoding='utf-8')

old_import = '''import json\nimport os\nimport queue\n'''
new_import = '''import json\nimport os\nimport msvcrt\nimport queue\n'''
if app.count(old_import) != 1:
    raise SystemExit(f'v17 msvcrt import anchor count={app.count(old_import)}')
app = app.replace(old_import, new_import, 1)

old_global = '''SINGLE_INSTANCE_MUTEX_HANDLE = None\nPROCESS_QUERY_LIMITED_INFORMATION = 0x1000\n'''
new_global = '''SINGLE_INSTANCE_MUTEX_HANDLE = None\nSINGLE_INSTANCE_FILE_HANDLE = None\nPROCESS_QUERY_LIMITED_INFORMATION = 0x1000\n'''
if app.count(old_global) != 1:
    raise SystemExit(f'v17 singleton global anchor count={app.count(old_global)}')
app = app.replace(old_global, new_global, 1)

old_acquire = '''def acquire_single_instance_lock() -> bool:\n    global SINGLE_INSTANCE_MUTEX_HANDLE\n    handle = kernel32.CreateMutexW(None, True, SINGLE_INSTANCE_MUTEX_NAME)\n    if not handle:\n        return True\n    if int(kernel32.GetLastError()) == ERROR_ALREADY_EXISTS:\n        kernel32.CloseHandle(handle)\n        return False\n    SINGLE_INSTANCE_MUTEX_HANDLE = handle\n    return True\n'''
new_acquire = '''def acquire_single_instance_lock() -> bool:\n    global SINGLE_INSTANCE_MUTEX_HANDLE, SINGLE_INSTANCE_FILE_HANDLE\n\n    # The named mutex alone proved insufficient with the one-file Windows build.\n    # Hold a real OS byte-range file lock for the whole UI lifetime. Windows\n    # releases this lock automatically if the process crashes, so no stale lock\n    # can permanently block the next launch.\n    base = (\n        os.environ.get("LOCALAPPDATA")\n        or os.environ.get("APPDATA")\n        or os.environ.get("TEMP")\n        or os.path.expanduser("~")\n    )\n    lock_dir = os.path.join(base, APP_DATA_DIR_NAME)\n    try:\n        os.makedirs(lock_dir, exist_ok=True)\n        lock_file = open(os.path.join(lock_dir, "fu_single_instance.lock"), "a+b")\n        lock_file.seek(0, os.SEEK_END)\n        if lock_file.tell() < 1:\n            lock_file.write(b"\\0")\n            lock_file.flush()\n        lock_file.seek(0)\n        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)\n    except OSError:\n        try:\n            lock_file.close()\n        except Exception:\n            pass\n        return False\n    except Exception:\n        try:\n            lock_file.close()\n        except Exception:\n            pass\n        return False\n\n    SINGLE_INSTANCE_FILE_HANDLE = lock_file\n\n    # Compatibility guard for V15/V16 and older builds that do not own the new\n    # file lock. Their hidden Tk main window still exists while they are in the\n    # tray, so do not allow this process to create another UI beside it.\n    if find_existing_fu_instance_path():\n        release_single_instance_lock()\n        return False\n\n    # Keep the original mutex as a secondary guard/restore contract.\n    handle = kernel32.CreateMutexW(None, True, SINGLE_INSTANCE_MUTEX_NAME)\n    if handle and int(kernel32.GetLastError()) == ERROR_ALREADY_EXISTS:\n        kernel32.CloseHandle(handle)\n        release_single_instance_lock()\n        return False\n    if handle:\n        SINGLE_INSTANCE_MUTEX_HANDLE = handle\n    return True\n'''
if app.count(old_acquire) != 1:
    raise SystemExit(f'v17 acquire anchor count={app.count(old_acquire)}')
app = app.replace(old_acquire, new_acquire, 1)

old_release = '''def release_single_instance_lock() -> None:\n    global SINGLE_INSTANCE_MUTEX_HANDLE\n    if SINGLE_INSTANCE_MUTEX_HANDLE:\n        try:\n            kernel32.CloseHandle(SINGLE_INSTANCE_MUTEX_HANDLE)\n        except Exception:\n            pass\n        SINGLE_INSTANCE_MUTEX_HANDLE = None\n'''
new_release = '''def release_single_instance_lock() -> None:\n    global SINGLE_INSTANCE_MUTEX_HANDLE, SINGLE_INSTANCE_FILE_HANDLE\n    if SINGLE_INSTANCE_MUTEX_HANDLE:\n        try:\n            kernel32.CloseHandle(SINGLE_INSTANCE_MUTEX_HANDLE)\n        except Exception:\n            pass\n        SINGLE_INSTANCE_MUTEX_HANDLE = None\n    if SINGLE_INSTANCE_FILE_HANDLE is not None:\n        try:\n            SINGLE_INSTANCE_FILE_HANDLE.seek(0)\n            msvcrt.locking(SINGLE_INSTANCE_FILE_HANDLE.fileno(), msvcrt.LK_UNLCK, 1)\n        except Exception:\n            pass\n        try:\n            SINGLE_INSTANCE_FILE_HANDLE.close()\n        except Exception:\n            pass\n        SINGLE_INSTANCE_FILE_HANDLE = None\n'''
if app.count(old_release) != 1:
    raise SystemExit(f'v17 release anchor count={app.count(old_release)}')
app = app.replace(old_release, new_release, 1)
APP.write_text(app, encoding='utf-8', newline='\n')

text = TEST.read_text(encoding='utf-8')
anchor = '''class ExistingWindowReattachTests(unittest.TestCase):\n'''
case = '''class SingleInstanceFileLockRegressionTests(unittest.TestCase):\n    def test_single_instance_uses_os_file_lock_and_legacy_window_guard(self):\n        source = Path(__file__).with_name("flash_sync_v02.py").read_text(encoding="utf-8")\n        self.assertIn("import msvcrt", source)\n        self.assertIn("SINGLE_INSTANCE_FILE_HANDLE = None", source)\n        self.assertIn("msvcrt.LK_NBLCK", source)\n        self.assertIn("fu_single_instance.lock", source)\n        self.assertIn("if find_existing_fu_instance_path():", source)\n        self.assertIn("msvcrt.LK_UNLCK", source)\n\n    def test_single_instance_does_not_change_product_features(self):\n        source = Path(__file__).with_name("flash_sync_v02.py").read_text(encoding="utf-8")\n        self.assertIn("self.create_floating_status_window()", source)\n        self.assertIn("QuickCodeFloatingControl(", source)\n        self.assertNotIn("        self.create_clock_bar()\\n", source)\n\n\n'''
if text.count(anchor) != 1:
    raise SystemExit(f'v17 regression anchor count={text.count(anchor)}')
text = text.replace(anchor, case + anchor, 1)
TEST.write_text(text, encoding='utf-8', newline='\n')

print('LIVE_FIX_V17_APPLIED robust file-lock singleton + legacy hidden-window guard only')
