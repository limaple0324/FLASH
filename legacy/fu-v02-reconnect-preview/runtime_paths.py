from __future__ import annotations

import os
import json
import shutil
import sys
import time
from pathlib import Path

try:
    import msvcrt
except Exception:
    msvcrt = None


APP_NAME = "SmartReconnect"
APP_VERSION = "11.8.4-manor-test23"
IS_FROZEN = bool(getattr(sys, "frozen", False))
SOURCE_DIR = Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", SOURCE_DIR)) if IS_FROZEN else SOURCE_DIR
LOCAL_APP_DATA = Path(
    os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
)
USER_DATA_DIR = LOCAL_APP_DATA / APP_NAME
APP_DATA_DIR = USER_DATA_DIR if IS_FROZEN else SOURCE_DIR

_SENSITIVE_PERSISTED_KEYS = {
    "process_command_line", "process_identity", "command_line", "command_marker",
    "username", "password", "account_identity", "launch_identity",
}


def initialize_app_data() -> None:
    """Create writable state and seed defaults without copying versioned assets."""
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (APP_DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
    if not IS_FROZEN:
        return
    for name in ("config.json", "bindings.json"):
        source = RESOURCE_DIR / name
        target = APP_DATA_DIR / name
        if source.is_file() and not target.exists():
            shutil.copyfile(source, target)


def sanitized_record(value: object) -> dict:
    """Return a binding/profile record safe for persistence.

    Raw launch/account material and process-lifetime identities must never be
    written to user data. Legacy variants are removed recursively.
    """
    if not isinstance(value, dict):
        return {}
    result, _changed = _remove_sensitive_fields(value)
    return result if isinstance(result, dict) else {}


def _remove_sensitive_fields(value: object) -> tuple[object, bool]:
    changed = False
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if str(key).casefold() in _SENSITIVE_PERSISTED_KEYS:
                changed = True
                continue
            nested, nested_changed = _remove_sensitive_fields(item)
            cleaned[key] = nested
            changed = changed or nested_changed
        return cleaned, changed
    if isinstance(value, list):
        cleaned_list = []
        for item in value:
            nested, nested_changed = _remove_sensitive_fields(item)
            cleaned_list.append(nested)
            changed = changed or nested_changed
        return cleaned_list, changed
    return value, False


def scrub_legacy_sensitive_data(timeout: float = 2.5) -> None:
    """Atomically remove raw process command lines written by older releases."""
    lock_path = USER_DATA_DIR / "bindings.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "a+b")
    locked = False
    try:
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        if msvcrt is not None:
            deadline = time.monotonic() + max(0.2, timeout)
            while time.monotonic() < deadline:
                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    time.sleep(0.025)
            if not locked:
                return
        for name in ("bindings.json", "identity_profiles.json", "automation_settings.json",
                     "sync_launch_config.json", "config.json"):
            path = USER_DATA_DIR / name
            if not path.is_file():
                continue
            try:
                original = json.loads(path.read_text(encoding="utf-8-sig"))
                cleaned, changed = _remove_sensitive_fields(original)
                if not changed:
                    continue
                temporary = path.with_suffix(path.suffix + ".privacy.tmp")
                temporary.write_text(
                    json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                os.replace(temporary, path)
            except Exception:
                continue
    finally:
        if locked:
            try:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        lock_file.close()


initialize_app_data()
scrub_legacy_sensitive_data()
