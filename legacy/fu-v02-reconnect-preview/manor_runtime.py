from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import threading
import time
from typing import Callable

from manor_assistant.models import CROP_BY_KEY, CROP_OPTIONS, Profile, RunResult
from manor_assistant.vision import VisionEngine
from manor_assistant.workflow import ManorWorkflow
from runtime_paths import APP_DATA_DIR


STATUS_PATH = APP_DATA_DIR / "manor_status.json"
PROGRESS_PATH = APP_DATA_DIR / "manor_progress.json"
SUCCESS_INTERVAL = timedelta(minutes=60)
RETRY_INTERVAL = timedelta(minutes=3)

_active_lock = threading.RLock()
_active_hwnds: set[int] = set()


def is_hwnd_active(hwnd: int) -> bool:
    with _active_lock:
        return int(hwnd or 0) in _active_hwnds


def _set_hwnd_active(hwnd: int, active: bool) -> None:
    with _active_lock:
        if active:
            _active_hwnds.add(int(hwnd))
        else:
            _active_hwnds.discard(int(hwnd))


def profile_key(hwnd: int, binding: dict) -> str:
    key = str(binding.get("profile_key", "") or "").strip()
    if key:
        return key
    shortcut = str(binding.get("shortcut_path", "") or "").strip().casefold()
    return shortcut or f"hwnd:{int(hwnd)}"


class ManorManager:
    """Run the original manor workflow on SmartReconnect's existing bindings."""

    def __init__(
        self,
        stop_event: threading.Event,
        binding_loader: Callable[[], dict[int, dict]],
        logger,
        progress_path: Path | None = None,
    ) -> None:
        self.stop_event = stop_event
        self.binding_loader = binding_loader
        self.logger = logger
        self._thread: threading.Thread | None = None
        self._states: dict[str, dict] = {}
        self._workflow: ManorWorkflow | None = None
        self.progress_path = progress_path or PROGRESS_PATH
        self._load_progress()

    @staticmethod
    def _parse_datetime(value) -> datetime | None:
        try:
            return datetime.fromisoformat(str(value)) if value else None
        except (TypeError, ValueError):
            return None

    def _load_progress(self) -> None:
        """Restore role scheduling so Stop -> Start continues the unfinished batch."""
        try:
            obj = json.loads(self.progress_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            return
        rows = obj.get("profiles", {}) if isinstance(obj, dict) else {}
        if not isinstance(rows, dict):
            return
        now = datetime.now()
        for key, raw in rows.items():
            if not isinstance(raw, dict):
                continue
            was_running = bool(raw.get("running", False))
            self._states[str(key)] = {
                "status": "上次中斷，準備續跑未完成角色" if was_running else str(raw.get("status", "待機")),
                "next_due": now if was_running else self._parse_datetime(raw.get("next_due")),
                "last_success": self._parse_datetime(raw.get("last_success")),
                "paused": bool(raw.get("paused", False)) and not was_running,
                "running": False,
                "retry_token": float(raw.get("retry_token", 0.0) or 0.0),
            }

    def _save_progress(self) -> None:
        rows = {}
        for key, state in self._states.items():
            rows[str(key)] = {
                "status": str(state.get("status", "")),
                "next_due": state.get("next_due").isoformat(timespec="seconds") if state.get("next_due") else "",
                "last_success": state.get("last_success").isoformat(timespec="seconds") if state.get("last_success") else "",
                "paused": bool(state.get("paused", False)),
                "running": bool(state.get("running", False)),
                "retry_token": float(state.get("retry_token", 0.0) or 0.0),
            }
        payload = {"version": 1, "updated_at": time.time(), "profiles": rows}
        try:
            self.progress_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.progress_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.progress_path)
        except OSError:
            pass

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="integrated-manor", daemon=True)
        self._thread.start()

    def join(self, timeout: float = 1.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _log(self, profile_id: str, message: str) -> None:
        self.logger.info("[莊園:%s] %s", profile_id[:12], message)

    @staticmethod
    def _enabled(binding: dict) -> bool:
        return bool(binding.get("manor_enabled", False))

    def _ensure_state(self, key: str, binding: dict) -> dict:
        state = self._states.get(key)
        if state is None:
            enabled = self._enabled(binding)
            state = {
                "status": "準備立即執行" if enabled else "已停用",
                "next_due": datetime.now() if enabled else None,
                "last_success": None,
                "paused": False,
                "running": False,
                "retry_token": float(binding.get("manor_retry_token", 0.0) or 0.0),
            }
            self._states[key] = state
            self._save_progress()
        return state

    def _sync_binding(self, key: str, binding: dict) -> dict:
        state = self._ensure_state(key, binding)
        enabled = self._enabled(binding)
        retry_token = float(binding.get("manor_retry_token", 0.0) or 0.0)
        if retry_token > float(state.get("retry_token", 0.0) or 0.0):
            state["retry_token"] = retry_token
            state["paused"] = False
            state["next_due"] = datetime.now()
            state["status"] = "準備立即重試"
        if not enabled and not state["running"]:
            state["paused"] = False
            state["next_due"] = None
            state["status"] = "已停用"
        elif enabled and state["status"] == "已停用":
            state["next_due"] = datetime.now()
            state["status"] = "準備立即執行"
        return state

    def _profile(self, hwnd: int, binding: dict, key: str) -> Profile:
        crop_key = str(binding.get("manor_crop_key", CROP_OPTIONS[0].key) or CROP_OPTIONS[0].key)
        if crop_key not in CROP_BY_KEY:
            crop_key = CROP_OPTIONS[0].key
        quantity = max(1, min(16, int(binding.get("manor_quantity", 16) or 16)))
        return Profile(
            id=key,
            shortcut_path=str(binding.get("shortcut_path", "") or ""),
            shortcut_name=str(binding.get("shortcut_name", "") or binding.get("preferred_role", "") or key[:8]),
            crop_key=crop_key,
            quantity=quantity,
            enabled=True,
            window_hwnd=int(hwnd),
            window_pid=int(binding.get("pid", 0) or 0),
            window_title=str(binding.get("window_title", "") or ""),
        )

    def _apply_result(self, state: dict, result: RunResult) -> None:
        now = datetime.now()
        state["running"] = False
        state["status"] = result.message
        if result.kind == "success":
            state["last_success"] = now
            state["next_due"] = now + SUCCESS_INTERVAL
        elif result.kind == "retry":
            state["next_due"] = now + RETRY_INTERVAL
        elif result.kind == "pause":
            state["paused"] = True
            state["next_due"] = None
        else:
            # A user Stop is an interruption, not completion. Resume this exact
            # role on the next Start instead of restarting the whole role list.
            state["next_due"] = now
            state["status"] = "已中斷；下次開始將續跑此角色"
        self._save_progress()

    def _write_status(self, bindings: dict[int, dict]) -> None:
        rows = {}
        for hwnd, binding in bindings.items():
            key = profile_key(hwnd, binding)
            state = self._states.get(key)
            if state is None:
                continue
            rows[str(int(hwnd))] = {
                "profile_key": key,
                "enabled": self._enabled(binding),
                "status": str(state["status"]),
                "next_due": state["next_due"].isoformat(timespec="seconds") if state["next_due"] else "",
                "last_success": state["last_success"].isoformat(timespec="seconds") if state["last_success"] else "",
                "paused": bool(state["paused"]),
                "running": bool(state["running"]),
            }
        payload = {"updated_at": time.time(), "profiles": rows}
        try:
            STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary = STATUS_PATH.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, STATUS_PATH)
        except OSError:
            pass

    def _run(self) -> None:
        try:
            self._workflow = ManorWorkflow(VisionEngine(), self._log)
            self.logger.info("莊園模組已載入；已啟用角色會立即執行第一次。")
        except Exception:
            self.logger.exception("莊園模組初始化失敗；自動重連仍繼續運作。")
            return

        last_status = 0.0
        while not self.stop_event.is_set():
            try:
                bindings = self.binding_loader()
            except Exception:
                bindings = {}

            selected: tuple[int, dict, str, dict] | None = None
            now = datetime.now()
            for hwnd, binding in bindings.items():
                key = profile_key(hwnd, binding)
                state = self._sync_binding(key, binding)
                if (
                    selected is None
                    and self._enabled(binding)
                    and not state["paused"]
                    and not state["running"]
                    and state["next_due"] is not None
                    and state["next_due"] <= now
                ):
                    selected = (int(hwnd), binding, key, state)

            if selected is not None:
                hwnd, binding, key, state = selected
                profile = self._profile(hwnd, binding, key)
                state["running"] = True
                state["status"] = "執行中"
                self._save_progress()
                self._write_status(bindings)
                self.logger.info("[莊園:%s] 開始執行：%s，最多新種 %d 格", profile.shortcut_name, profile.crop.label, profile.quantity)
                _set_hwnd_active(hwnd, True)
                try:
                    result = self._workflow.run(profile, hwnd, self.stop_event)
                finally:
                    _set_hwnd_active(hwnd, False)
                self._apply_result(state, result)
                self.logger.info("[莊園:%s] %s", profile.shortcut_name, result.message)
                self._write_status(bindings)
                continue

            if time.monotonic() - last_status >= 1.0:
                self._write_status(bindings)
                last_status = time.monotonic()
            self.stop_event.wait(0.35)

        self._save_progress()
        self._write_status({})
