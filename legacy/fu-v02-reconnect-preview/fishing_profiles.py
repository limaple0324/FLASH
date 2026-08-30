from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Iterable

from runtime_paths import USER_DATA_DIR


FISHING_PROFILES_PATH = USER_DATA_DIR / "fishing_profiles.json"
_LINK_RE = re.compile(r"\[@N\|([^|\]\r\n]+)\|([^\]\r\n]+)\]")
_LOCK = threading.RLock()
_CACHE_MTIME_NS = -1
_CACHE: dict = {}

BUILTIN_FISHING_PROFILES = (
    {
        "id": "builtin-level-1",
        "name": "一級魚",
        # 一級有三組訊息。程式會把每一個非空白行視為一組，依序嘗試
        # A → B → C；不會把十個連結塞在同一行造成換行與誤點風險。
        "message": (
            "[@N|1189|11][@N|1190|12][@N|1191|13]\n"
            "[@N|1192|14][@N|1193|15][@N|1194|16]\n"
            "[@N|1195|17][@N|1196|18][@N|1197|19]"
        ),
        "wait_seconds": 10.0,
        "success_text": "正在釣魚",
        "built_in": True,
    },
    {
        "id": "builtin-level-2",
        "name": "二級魚",
        "message": "[@N|1199|21][@N|1202|22][@N|1203|23][@N|1204|24]",
        "wait_seconds": 10.0,
        "success_text": "正在釣魚",
        "built_in": True,
    },
    {
        "id": "builtin-level-3",
        "name": "三級魚",
        "message": "[@N|1205|31][@N|1206|32][@N|1207|33]",
        "wait_seconds": 10.0,
        "success_text": "正在釣魚",
        "built_in": True,
    },
    {
        "id": "builtin-level-4",
        "name": "四級魚",
        "message": "[@N|1208|41][@N|1209|42][@N|1210|43][@N|1211|44]",
        "wait_seconds": 10.0,
        "success_text": "正在釣魚",
        "built_in": True,
    },
    {
        "id": "builtin-level-5",
        "name": "五級魚",
        # 使用者已確認原本重複的 1212 是誤植；正確只有 1212～1215。
        "message": "[@N|1212|51][@N|1213|52][@N|1214|53][@N|1215|54]",
        "wait_seconds": 10.0,
        "success_text": "正在釣魚",
        "built_in": True,
    },
    {
        "id": "builtin-level-6",
        "name": "六級魚",
        "message": "[@N|1216|61][@N|1217|62][@N|1218|63]",
        "wait_seconds": 10.0,
        "success_text": "正在釣魚",
        "built_in": True,
    },
    {
        "id": "builtin-level-7",
        "name": "七級魚",
        "message": "[@N|1596|71][@N|1597|72][@N|1598|73]",
        "wait_seconds": 10.0,
        "success_text": "正在釣魚",
        "built_in": True,
    },
)
BUILTIN_PROFILE_BY_ID = {item["id"]: item for item in BUILTIN_FISHING_PROFILES}
# 舊自我檢查與外部程式可能仍引用這個名稱；保留為二級相容別名。
DEFAULT_FISHING_PROFILE = dict(BUILTIN_PROFILE_BY_ID["builtin-level-2"])


def parse_message_links(message: str) -> list[dict]:
    """Parse the exact Flash chat-link syntax without guessing malformed input."""
    out = []
    for match in _LINK_RE.finditer(str(message or "")):
        target_id = str(match.group(1) or "").strip()
        label = str(match.group(2) or "").strip()
        if not target_id or not label:
            continue
        out.append({"target_id": target_id, "label": label})
    return out


def message_groups(profile_or_message: object) -> list[str]:
    """Return non-empty message groups; one line is one independently sent group."""
    if isinstance(profile_or_message, dict):
        value = profile_or_message.get("message", "")
    else:
        value = profile_or_message
    return [line.strip() for line in re.split(r"[\r\n]+", str(value or "")) if line.strip()]


def profile_group_link_counts(profile: dict) -> list[int]:
    return [len(parse_message_links(group)) for group in message_groups(profile)]


def validate_profile(name: str, message: str) -> tuple[dict | None, str]:
    clean_name = str(name or "").strip()
    clean_message = str(message or "").strip()
    if not clean_name:
        return None, "名稱不能留空。"
    if len(clean_name) > 40:
        return None, "名稱最多 40 個字。"
    if not clean_message:
        return None, "發送字串不能留空。"
    if len(clean_message) > 1500:
        return None, "發送字串過長（最多 1500 個字元）。"
    groups = message_groups(clean_message)
    if not groups:
        return None, "找不到有效座標；格式必須是 [@N|編號|顯示文字]。"
    if len(groups) > 8:
        return None, "單一設定最多 8 組發送字串。"
    total_links = 0
    for index, group in enumerate(groups, start=1):
        links = parse_message_links(group)
        if not links:
            return None, f"第 {index} 組找不到有效座標。"
        if len(links) > 6:
            return None, f"第 {index} 組超過 6 個座標；請換行拆組，避免聊天換行後誤點。"
        # Every non-whitespace character must belong to a link token. This
        # prevents an accidental free-form chat message from being sent.
        remainder = _LINK_RE.sub("", group)
        if remainder.strip():
            return None, f"第 {index} 組只能包含連續的 [@N|編號|顯示文字]。"
        total_links += len(links)
    if total_links > 24:
        return None, "單一設定全部訊息合計最多 24 個座標，避免無限誤點。"
    return {
        "name": clean_name,
        "message": clean_message,
        "wait_seconds": 10.0,
        "success_text": "正在釣魚",
    }, ""


def _normalize_profile(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    valid, _error = validate_profile(value.get("name", ""), value.get("message", ""))
    if valid is None:
        return None
    profile_id = str(value.get("id", "") or "").strip()
    if not profile_id:
        profile_id = "user-" + uuid.uuid4().hex
    valid.update({
        "id": profile_id[:96],
        # The current product contract fixes the verification delay and text.
        # Keep the fields persisted so a future UI can expose them safely.
        "wait_seconds": 10.0,
        "success_text": "正在釣魚",
        "built_in": bool(value.get("built_in", False)),
    })
    return valid


def _default_payload() -> dict:
    return {
        "version": 2,
        "updated_at": time.time(),
        "profiles": [dict(item) for item in BUILTIN_FISHING_PROFILES],
    }


def _write_payload(payload: dict) -> None:
    FISHING_PROFILES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = FISHING_PROFILES_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, FISHING_PROFILES_PATH)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _load_uncached() -> dict:
    if not FISHING_PROFILES_PATH.exists():
        payload = _default_payload()
        _write_payload(payload)
        return payload
    try:
        raw = json.loads(FISHING_PROFILES_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        # Do not overwrite a damaged user file. Return the built-in profile in
        # memory and let the UI report/save explicitly on the next edit.
        return _default_payload()
    values = raw.get("profiles", []) if isinstance(raw, dict) else []
    custom_profiles = []
    seen = set()
    for value in values if isinstance(values, list) else []:
        profile = _normalize_profile(value)
        if profile is None or profile["id"] in seen:
            continue
        seen.add(profile["id"])
        if profile["id"] not in BUILTIN_PROFILE_BY_ID:
            custom_profiles.append(profile)
    profiles = [dict(item) for item in BUILTIN_FISHING_PROFILES] + custom_profiles
    return {
        "version": 2,
        "updated_at": float(raw.get("updated_at", 0) or 0) if isinstance(raw, dict) else 0.0,
        "profiles": profiles,
    }


def load_profiles(force: bool = False) -> list[dict]:
    global _CACHE_MTIME_NS, _CACHE
    with _LOCK:
        try:
            mtime_ns = FISHING_PROFILES_PATH.stat().st_mtime_ns
        except Exception:
            mtime_ns = -1
        if force or not _CACHE or mtime_ns != _CACHE_MTIME_NS:
            _CACHE = _load_uncached()
            try:
                _CACHE_MTIME_NS = FISHING_PROFILES_PATH.stat().st_mtime_ns
            except Exception:
                _CACHE_MTIME_NS = -1
        return [dict(item) for item in _CACHE.get("profiles", []) if isinstance(item, dict)]


def profile_by_id(profile_id: str) -> dict | None:
    wanted = str(profile_id or "").strip()
    if not wanted:
        return None
    for profile in load_profiles():
        if str(profile.get("id", "")) == wanted:
            return profile
    return None


def save_profiles(profiles: Iterable[dict]) -> list[dict]:
    global _CACHE_MTIME_NS, _CACHE
    custom_clean = []
    seen = set()
    for value in profiles:
        profile = _normalize_profile(value)
        if profile is None or profile["id"] in seen:
            continue
        seen.add(profile["id"])
        if profile["id"] not in BUILTIN_PROFILE_BY_ID:
            custom_clean.append(profile)
    clean = [dict(item) for item in BUILTIN_FISHING_PROFILES] + custom_clean
    payload = {"version": 2, "updated_at": time.time(), "profiles": clean}
    with _LOCK:
        _write_payload(payload)
        _CACHE = payload
        try:
            _CACHE_MTIME_NS = FISHING_PROFILES_PATH.stat().st_mtime_ns
        except Exception:
            _CACHE_MTIME_NS = -1
    return [dict(item) for item in clean]


def add_profile(name: str, message: str) -> tuple[dict | None, str]:
    valid, error = validate_profile(name, message)
    if valid is None:
        return None, error
    profiles = load_profiles(force=True)
    if any(str(item.get("name", "")).casefold() == valid["name"].casefold() for item in profiles):
        return None, "已有相同名稱的釣魚設定。"
    valid.update({"id": "user-" + uuid.uuid4().hex, "built_in": False})
    profiles.append(valid)
    save_profiles(profiles)
    return dict(valid), ""


def update_profile(profile_id: str, name: str, message: str) -> tuple[dict | None, str]:
    wanted = str(profile_id or "")
    valid, error = validate_profile(name, message)
    if valid is None:
        return None, error
    profiles = load_profiles(force=True)
    current = next((p for p in profiles if str(p.get("id", "")) == wanted), None)
    if current is None:
        return None, "找不到要編輯的釣魚設定。"
    if bool(current.get("built_in", False)):
        return None, "內建設定不可直接修改；請新增一份自訂設定。"
    if any(
        str(item.get("id", "")) != wanted
        and str(item.get("name", "")).casefold() == valid["name"].casefold()
        for item in profiles
    ):
        return None, "已有相同名稱的釣魚設定。"
    valid.update({"id": wanted, "built_in": False})
    replaced = [valid if str(item.get("id", "")) == wanted else item for item in profiles]
    save_profiles(replaced)
    return dict(valid), ""


def delete_profile(profile_id: str) -> tuple[bool, str]:
    wanted = str(profile_id or "")
    profiles = load_profiles(force=True)
    current = next((p for p in profiles if str(p.get("id", "")) == wanted), None)
    if current is None:
        return False, "找不到要刪除的釣魚設定。"
    if bool(current.get("built_in", False)):
        return False, "內建設定不能刪除。"
    save_profiles([p for p in profiles if str(p.get("id", "")) != wanted])
    return True, ""
