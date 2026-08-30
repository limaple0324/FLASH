from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import threading
import time
from typing import Callable

from .config_store import ConfigStore, LOG_DIR
from .models import AppConfig, Profile, RuntimeState, RunResult
from .vision import VisionEngine
from .win32_api import is_window
from .workflow import ManorWorkflow, WindowResolver
from user_activity_guard import USER_ACTIVITY_GUARD


EventCallback = Callable[[str, object], None]


class ProfileScheduler:
    def __init__(
        self,
        config: AppConfig,
        store: ConfigStore,
        events: EventCallback,
    ) -> None:
        self.config = config
        self.store = store
        self.events = events
        self.vision = VisionEngine()
        self.states: dict[str, RuntimeState] = {
            profile.id: RuntimeState(profile.id) for profile in config.profiles
        }
        self._lock = threading.RLock()
        self._cancelled = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.running = False
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.log_path = LOG_DIR / f"run-{datetime.now():%Y%m%d}.log"
        self.resolver = WindowResolver(self._profile_bound, self.log)
        self.workflow = ManorWorkflow(self.vision, self.log)

    def _profile_name(self, profile_id: str) -> str:
        if profile_id == "system":
            return "系統"
        for profile in self.config.profiles:
            if profile.id == profile_id:
                return profile.shortcut_name or profile.id[:8]
        return profile_id[:8]

    def log(self, profile_id: str, message: str) -> None:
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [{self._profile_name(profile_id)}] {message}"
        try:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass
        self.events("log", line)

    def _profile_bound(self, _profile: Profile) -> None:
        with self._lock:
            self.store.save(self.config)
        self.events("profiles", None)

    def sync_profiles(self) -> None:
        with self._lock:
            current = {profile.id for profile in self.config.profiles}
            for profile_id in list(self.states):
                if profile_id not in current:
                    del self.states[profile_id]
            for profile in self.config.profiles:
                self.states.setdefault(profile.id, RuntimeState(profile.id))
        self._wake.set()

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            if self._thread is not None and self._thread.is_alive():
                self.log("system", "上一個工作仍在安全停止中，請稍候再開始")
                return
            self.running = True
            cancelled = threading.Event()
            self._cancelled = cancelled
            now = datetime.now()
            for profile in self.config.profiles:
                state = self.states.setdefault(profile.id, RuntimeState(profile.id))
                state.paused = False
                state.running = False
                state.next_due = now if profile.enabled else None
                state.status = "準備立即執行" if profile.enabled else "已停用"
            self._thread = threading.Thread(
                target=self._worker,
                args=(cancelled,),
                name="manor-scheduler",
                daemon=True,
            )
            self._thread.start()
        self.log("system", "全域排程已開始；已啟用角色立即執行第一次")
        self.events("running", True)

    def stop(self) -> None:
        with self._lock:
            if not self.running:
                return
            self.running = False
            self._cancelled.set()
            self._wake.set()
            for state in self.states.values():
                if not state.running:
                    state.status = "已停止"
                    state.next_due = None
        self.log("system", "全域排程已停止")
        self.events("running", False)
        self.events("states", None)

    def retry_now(self, profile_id: str) -> None:
        with self._lock:
            state = self.states.setdefault(profile_id, RuntimeState(profile_id))
            state.paused = False
            state.next_due = datetime.now()
            state.status = "準備立即重試"
        if not self.running:
            self.start()
        self._wake.set()
        self.events("states", None)

    def set_enabled(self, profile_id: str, enabled: bool) -> None:
        with self._lock:
            profile = next(
                (item for item in self.config.profiles if item.id == profile_id), None
            )
            if profile is None:
                return
            profile.enabled = enabled
            state = self.states.setdefault(profile_id, RuntimeState(profile_id))
            if enabled:
                state.paused = False
                state.status = "準備立即執行" if self.running else "待機"
                state.next_due = datetime.now() if self.running else None
            else:
                state.paused = False
                state.status = "已停用"
                state.next_due = None
            self.store.save(self.config)
        self._wake.set()
        self.events("profiles", None)
        self.events("states", None)

    def snapshot(self) -> tuple[bool, dict[str, RuntimeState]]:
        with self._lock:
            copied = {
                key: RuntimeState(
                    profile_id=value.profile_id,
                    status=value.status,
                    next_due=value.next_due,
                    last_success=value.last_success,
                    paused=value.paused,
                    running=value.running,
                )
                for key, value in self.states.items()
            }
            return self.running, copied

    def _set_result(self, profile: Profile, result: RunResult) -> None:
        now = datetime.now()
        with self._lock:
            state = self.states.setdefault(profile.id, RuntimeState(profile.id))
            state.running = False
            state.status = result.message
            if result.kind == "success":
                state.last_success = now
                state.next_due = now + timedelta(minutes=self.config.interval_minutes)
            elif result.kind == "retry":
                state.next_due = now + timedelta(minutes=self.config.retry_minutes)
            elif result.kind == "pause":
                state.paused = True
                state.next_due = None
            else:
                state.next_due = None
        self.log(profile.id, result.message)
        self.events("states", None)

    def _assigned_hwnds(self, excluded_profile_id: str) -> set[int]:
        return {
            profile.window_hwnd
            for profile in self.config.profiles
            if profile.id != excluded_profile_id and is_window(profile.window_hwnd)
        }

    def _next_due_profile(self) -> Profile | None:
        now = datetime.now()
        with self._lock:
            for profile in self.config.profiles:
                state = self.states.setdefault(profile.id, RuntimeState(profile.id))
                if (
                    profile.enabled
                    and not state.paused
                    and not state.running
                    and state.next_due is not None
                    and state.next_due <= now
                ):
                    state.running = True
                    state.status = "執行中"
                    return profile
        return None

    def _worker(self, cancelled: threading.Event) -> None:
        while not cancelled.is_set():
            profile = self._next_due_profile()
            if profile is None:
                self._wake.wait(0.5)
                self._wake.clear()
                continue

            self.events("states", None)
            self.log(profile.id, f"開始執行：{profile.crop.label}，最多新種 {profile.quantity} 格")
            hwnd, resolver_result = self.resolver.ensure_window(
                profile,
                self._assigned_hwnds(profile.id),
                cancelled,
            )
            if resolver_result is not None:
                self._set_result(profile, resolver_result)
                continue
            if hwnd is None:
                self._set_result(profile, RunResult.retry("找不到遊戲視窗，3 分鐘後重試"))
                continue
            waiting_logged = False
            while not cancelled.is_set():
                remaining = USER_ACTIVITY_GUARD.remaining(hwnd)
                if remaining <= 0.0:
                    break
                with self._lock:
                    state = self.states.setdefault(profile.id, RuntimeState(profile.id))
                    state.status = f"使用者操作中，剩餘約 {int(remaining + 0.999)} 秒"
                if not waiting_logged:
                    self.log(profile.id, "偵測到使用者正在操作此視窗；莊園等待最後操作後 3 分鐘，其他視窗繼續")
                    waiting_logged = True
                self.events("states", None)
                cancelled.wait(min(0.5, remaining))
            if cancelled.is_set():
                break
            if waiting_logged:
                self.log(profile.id, "已連續 3 分鐘沒有使用者輸入；恢復莊園流程")
            result = self.workflow.run(profile, hwnd, cancelled)
            self._set_result(profile, result)

        with self._lock:
            for state in self.states.values():
                state.running = False
                if not self.running:
                    state.status = "已停止"
                    state.next_due = None
        self.events("states", None)
