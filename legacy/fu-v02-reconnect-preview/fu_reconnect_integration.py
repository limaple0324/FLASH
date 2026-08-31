"""Strict, embedded test23 automation for the Fu V0.2 preview.

This adapter deliberately has no window discovery path.  A window can enter the
allow-list only through :meth:`authorize_launch_transaction`, which is called by
Fu immediately after it launches shortcuts and sees the delta HWNDs.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import threading
import time
from typing import Callable, Iterable

import smart_reconnect as sr
import manor_runtime

LOG = logging.getLogger("fu.reconnect")


class LeaseBusy(RuntimeError):
    pass


@dataclass(frozen=True)
class LeaseToken:
    hwnd: int
    owner: str
    serial: int


class InputLeaseArbiter:
    """Per-HWND non-interleaving input leases; sync owners have priority."""

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = max(0.05, float(timeout))
        self._cv = threading.Condition(threading.RLock())
        self._held: dict[int, dict] = {}
        self._serial = 0
        self._sync_waiters: dict[int, int] = {}
        self._blocked: set[int] = set()

    def _reap(self, hwnd: int, now: float) -> None:
        held = self._held.get(hwnd)
        if held and now - float(held["touched"]) > self.timeout:
            self._held.pop(hwnd, None)
            self._cv.notify_all()

    def acquire(self, hwnd: int, owner: str, *, wait: bool, timeout: float = 2.0) -> LeaseToken | None:
        hwnd, owner = int(hwnd), str(owner)
        sync = owner.startswith("sync")
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cv:
            if hwnd in self._blocked:
                return None
            if sync:
                self._sync_waiters[hwnd] = self._sync_waiters.get(hwnd, 0) + 1
            try:
                while True:
                    now = time.monotonic()
                    self._reap(hwnd, now)
                    held = self._held.get(hwnd)
                    if held is None and (sync or not self._sync_waiters.get(hwnd, 0)):
                        self._serial += 1
                        token = LeaseToken(hwnd, owner, self._serial)
                        self._held[hwnd] = {"token": token, "touched": now, "depth": 1}
                        return token
                    if held and held["token"].owner == owner:
                        held["touched"] = now
                        held["depth"] = int(held.get("depth", 1)) + 1
                        return held["token"]
                    if not wait or now >= deadline:
                        return None
                    self._cv.wait(min(0.05, deadline - now))
            finally:
                if sync:
                    left = self._sync_waiters.get(hwnd, 1) - 1
                    if left > 0:
                        self._sync_waiters[hwnd] = left
                    else:
                        self._sync_waiters.pop(hwnd, None)

    def valid(self, token: LeaseToken | None) -> bool:
        if token is None:
            return False
        with self._cv:
            self._reap(token.hwnd, time.monotonic())
            held = self._held.get(token.hwnd)
            return bool(held and held["token"] == token)

    def touch(self, token: LeaseToken) -> bool:
        with self._cv:
            if not self.valid(token):
                return False
            self._held[token.hwnd]["touched"] = time.monotonic()
            return True

    def release(self, token: LeaseToken | None) -> None:
        if token is None:
            return
        with self._cv:
            held = self._held.get(token.hwnd)
            if held and held["token"] == token:
                depth = int(held.get("depth", 1)) - 1
                if depth > 0:
                    held["depth"] = depth
                    held["touched"] = time.monotonic()
                else:
                    self._held.pop(token.hwnd, None)
                    self._cv.notify_all()

    def release_hwnd(self, hwnd: int) -> None:
        with self._cv:
            self._held.pop(int(hwnd), None)
            self._sync_waiters.pop(int(hwnd), None)
            self._cv.notify_all()

    def block_hwnd(self, hwnd: int) -> None:
        with self._cv:
            self._blocked.add(int(hwnd))
            self._held.pop(int(hwnd), None)
            self._sync_waiters.pop(int(hwnd), None)
            self._cv.notify_all()

    def unblock_hwnd(self, hwnd: int) -> None:
        with self._cv:
            self._blocked.discard(int(hwnd))

    def release_all(self) -> None:
        with self._cv:
            self._held.clear()
            self._sync_waiters.clear()
            self._cv.notify_all()

    @contextmanager
    def lease(self, hwnd: int, owner: str, *, wait: bool, timeout: float = 2.0):
        token = self.acquire(hwnd, owner, wait=wait, timeout=timeout)
        if token is None:
            raise LeaseBusy(f"{hwnd} busy")
        try:
            yield token
        finally:
            self.release(token)


@dataclass
class ManagedRecord:
    entry_id: str
    hwnd: int
    pid: int
    creation_time: str
    identity: str
    shortcut_path: str
    shortcut_name: str


class EmbeddedAutomationController:
    """Owns test23 workers only for strict managed-launch records."""

    def __init__(
        self,
        config_path: Path,
        is_window: Callable[[int], bool],
        record_validator: Callable[[int], dict] | None = None,
        monitor_interval: float = 3.0,
    ) -> None:
        self.config_path = Path(config_path)
        self.is_window = is_window
        self.record_validator = record_validator
        self.monitor_interval = max(0.05, float(monitor_interval))
        self.arbiter = InputLeaseArbiter()
        self.records: dict[str, ManagedRecord] = {}
        self.settings: dict[str, dict] = {}
        self.rejections: dict[str, str] = {}
        self.workers: dict[int, sr.GameWorker] = {}
        self.worker_stops: dict[str, threading.Event] = {}
        self.manors: dict[str, tuple[manor_runtime.ManorManager, threading.Event]] = {}
        self.stop_event = threading.Event()
        self.closed = False
        self.pause_event = threading.Event()
        self._lock = threading.RLock()
        self._validated_at: dict[int, float] = {}
        self._load_settings()
        self._install_policy()
        self._monitor_thread = threading.Thread(target=self._monitor_loop, name="fu-strict-identity-monitor", daemon=True)
        self._monitor_thread.start()

    @staticmethod
    def _parse_utc_iso(value: object) -> datetime | None:
        text = str(value or "")
        if "T" not in text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)

    def _monitor_loop(self) -> None:
        while not self.stop_event.wait(self.monitor_interval):
            self._validate_all_records()

    def _validate_all_records(self) -> None:
        if self.record_validator is None:
            return
        pending = []
        with self._lock:
            for eid, record in list(self.records.items()):
                valid = self.is_window(record.hwnd)
                try:
                    current = dict(self.record_validator(record.hwnd) or {}) if valid else {}
                    valid = valid and (
                        int(current.get("pid", 0) or 0) == record.pid
                        and str(current.get("creation_time", "")) == record.creation_time
                        and str(current.get("identity", "")) == record.identity
                    )
                except Exception:
                    valid = False
                if not valid:
                    pending.append(self._begin_revoke(eid, record, "身份不足：獨立監看PID／建立時間／identity驗證失敗"))
        for worker, manor_pair in pending:
            self._finish_revoke(worker, manor_pair)

    def _load_settings(self) -> None:
        try:
            obj = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            obj = {}
        raw = obj.get("entries", {}) if isinstance(obj, dict) else {}
        if isinstance(raw, dict):
            self.settings = {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}

    def _save_settings(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix(".tmp")
        payload = {"version": 1, "registry_policy": "session-only-strict", "entries": self.settings}
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.config_path)

    def _install_policy(self) -> None:
        # Physical fallback cannot be enabled in the embedded product.
        sr.FOREGROUND_PHYSICAL_FALLBACK = False
        sr.CONFIG["允許前景實體輸入備援"] = False

        def denied(*_args, **_kwargs):
            raise RuntimeError("整合政策禁止前景實體輸入")

        sr.WIO.click_foreground_physical = denied
        sr.WIO.send_chat_message_foreground_physical = denied
        sr.WIO.press_enter_foreground_physical = denied
        sr.load_window_bindings = self.binding_loader

        # Every test23 input method is leased. Nested WIO calls reuse the same
        # owner on the worker thread and therefore remain one atomic call.
        for name in ("click", "click_root", "click_active", "click_interactive", "click_mode",
                     "click_target_dpi_context", "send_chat_message_background", "press_enter"):
            original = getattr(sr.WIO, name, None)
            if not callable(original) or getattr(original, "_fu_leased", False):
                continue

            def make_wrapper(fn):
                def wrapped(hwnd, *args, **kwargs):
                    token = self.arbiter.acquire(int(hwnd), f"automation:{threading.get_ident()}", wait=False)
                    if token is None:
                        return False
                    try:
                        return fn(hwnd, *args, **kwargs)
                    finally:
                        self.arbiter.release(token)
                wrapped._fu_leased = True
                return wrapped
            setattr(sr.WIO, name, make_wrapper(original))

        original_post_click = manor_runtime.ManorWorkflow.__module__  # keep import side effect explicit
        del original_post_click
        from manor_assistant import win32_api
        for method_name in ("post_click", "post_move"):
            raw_method = getattr(win32_api, method_name)
            if getattr(raw_method, "_fu_leased", False):
                continue
            def manor_input(hwnd, *args, _raw=raw_method, **kwargs):
                token = self.arbiter.acquire(int(hwnd), f"automation:{threading.get_ident()}", wait=False)
                if token is None:
                    return False
                try:
                    return _raw(hwnd, *args, **kwargs)
                finally:
                    self.arbiter.release(token)
            manor_input._fu_leased = True
            setattr(win32_api, method_name, manor_input)

    def setting(self, entry_id: str) -> dict:
        row = self.settings.setdefault(str(entry_id), {})
        row.setdefault("manor_enabled", False)
        row.setdefault("manor_crop_key", "normal_rock")
        row.setdefault("manor_quantity", 16)
        row.setdefault("fishing_enabled", False)
        row.setdefault("fishing_profile_id", "")
        return row

    def update_setting(self, entry_id: str, **changes) -> None:
        row = self.setting(entry_id)
        for key in ("manor_enabled", "manor_crop_key", "manor_quantity", "fishing_enabled", "fishing_profile_id"):
            if key in changes:
                row[key] = changes[key]
        self._save_settings()
        record = self.records.get(str(entry_id))
        worker = self.workers.get(record.hwnd) if record else None
        if worker is not None and worker.is_alive():
            worker.apply_binding(self.binding_loader().get(record.hwnd), announce=False)

    def authorize_launch_transaction(
        self, entries: Iterable[dict], candidates: Iterable[dict], transaction: dict | None = None
    ) -> dict[str, int]:
        """Strict identity-only authorization. No order/position fallback."""
        if self.closed or self.stop_event.is_set():
            return {}
        entries = [dict(x) for x in entries]
        candidates = [dict(x) for x in candidates]
        identities: dict[str, list[dict]] = {}
        for entry in entries:
            identities.setdefault(str(entry.get("identity", "")), []).append(entry)
        candidate_by_identity: dict[str, list[dict]] = {}
        for item in candidates:
            candidate_by_identity.setdefault(str(item.get("identity", "")), []).append(item)
        accepted: dict[str, int] = {}
        try:
            transaction = dict(transaction or {})
        except (TypeError, ValueError):
            transaction = {}
        transaction_id = str(transaction.get("transaction_id", ""))
        evidence_shape_valid = True
        try:
            before_hwnds = {int(hwnd) for hwnd in transaction.get("before_hwnds", set())}
            before_processes = set()
            for pid, created in transaction.get("before_processes", set()):
                created_dt = self._parse_utc_iso(created)
                if created_dt is None:
                    evidence_shape_valid = False
                    continue
                before_processes.add((int(pid), created_dt))
        except (TypeError, ValueError):
            before_hwnds, before_processes, evidence_shape_valid = set(), set(), False
        started = str(transaction.get("started", ""))
        started_dt = self._parse_utc_iso(started)
        with self._lock:
            if self.closed or self.stop_event.is_set():
                return {}
            for entry in entries:
                eid = str(entry.get("entry_id", ""))
                identity = str(entry.get("identity", ""))
                reason = ""
                rows = candidate_by_identity.get(identity, []) if identity else []
                if not eid or not identity:
                    reason = "身份不足：entry_id或identity空白"
                elif len(identities.get(identity, [])) != 1:
                    reason = "身份不足：啟動項identity重複"
                elif len(rows) != 1:
                    reason = "身份不足：新視窗不是唯一identity候選"
                elif (
                    not transaction_id
                    or started_dt is None
                    or "before_hwnds" not in transaction
                    or "before_processes" not in transaction
                    or transaction.get("process_snapshot_complete") is not True
                    or not evidence_shape_valid
                ):
                    reason = "身份不足：啟動交易證據缺失或格式錯誤"
                else:
                    item = rows[0]
                    try:
                        hwnd, pid = int(item.get("hwnd", 0)), int(item.get("pid", 0))
                    except (TypeError, ValueError):
                        hwnd, pid = 0, 0
                    created = str(item.get("creation_time", ""))
                    created_dt = self._parse_utc_iso(created)
                    if not hwnd or not pid or created_dt is None or not self.is_window(hwnd):
                        reason = "身份不足：CIM/PID/建立時間驗證失敗"
                    elif hwnd in before_hwnds:
                        reason = "身份不足：交易前已存在的視窗"
                    elif (pid, created_dt) in before_processes:
                        reason = "身份不足：交易前已存在的程序晚出視窗"
                    elif created_dt < started_dt:
                        reason = "身份不足：程序建立時間早於本次啟動交易"
                    else:
                        self.records[eid] = ManagedRecord(
                            eid, hwnd, pid, created, identity,
                            str(entry.get("path", "")), str(entry.get("name", "")),
                        )
                        self.rejections.pop(eid, None)
                        self.arbiter.unblock_hwnd(hwnd)
                        accepted[eid] = hwnd
                        self._start_worker(self.records[eid])
                if reason:
                    self.rejections[eid or str(entry.get("path", ""))] = reason
            return accepted

    def _start_worker(self, record: ManagedRecord) -> None:
        if self.closed or self.stop_event.is_set():
            return
        existing = self.workers.get(record.hwnd)
        if existing is not None and existing.is_alive():
            return
        if existing is not None:
            self.workers.pop(record.hwnd, None)
        if not self.is_window(record.hwnd):
            return
        profile = dict(sr.DEFAULT_CONFIG["捷徑設定"][0])
        profile.update({"名稱": record.shortcut_name, "捷徑路徑": record.shortcut_path, "優先角色": ""})
        local_stop = threading.Event()
        self.worker_stops[record.entry_id] = local_stop
        worker = sr.GameWorker(record.hwnd, profile, local_stop, self.pause_event)
        self.workers[record.hwnd] = worker
        worker.start()
        manager = manor_runtime.ManorManager(
            local_stop,
            lambda eid=record.entry_id: self.binding_loader(entry_id=eid),
            LOG,
            progress_path=manor_runtime.PROGRESS_PATH.with_name(f"manor_progress_{record.entry_id}.json"),
        )
        self.manors[record.entry_id] = (manager, local_stop)
        manager.start()

    def _begin_revoke(self, eid: str, record: ManagedRecord, reason: str):
        self.arbiter.block_hwnd(record.hwnd)
        self.records.pop(eid, None)
        self.rejections[eid] = reason
        stop = self.worker_stops.pop(eid, None)
        if stop is not None:
            stop.set()
        worker = self.workers.pop(record.hwnd, None)
        manor_pair = self.manors.pop(eid, None)
        if manor_pair is not None:
            _manager, manor_stop = manor_pair
            manor_stop.set()
        return worker, manor_pair

    @staticmethod
    def _finish_revoke(worker, manor_pair) -> None:
        current = threading.current_thread()
        if worker is not None and worker is not current:
            worker.join(timeout=1.0)
        if manor_pair is not None:
            manager, _event = manor_pair
            thread = getattr(manager, "_thread", None)
            if thread is not current:
                manager.join(timeout=1.0)

    def binding_loader(self, entry_id: str | None = None) -> dict[int, dict]:
        result: dict[int, dict] = {}
        joins = []
        with self._lock:
            for eid, record in list(self.records.items()):
                if entry_id is not None and eid != str(entry_id):
                    continue
                valid = self.is_window(record.hwnd)
                now = time.monotonic()
                if valid and self.record_validator is not None and now - self._validated_at.get(record.hwnd, 0.0) >= 3.0:
                    self._validated_at[record.hwnd] = now
                    try:
                        current = dict(self.record_validator(record.hwnd) or {})
                        valid = (
                            int(current.get("pid", 0) or 0) == record.pid
                            and str(current.get("creation_time", "")) == record.creation_time
                            and str(current.get("identity", "")) == record.identity
                        )
                    except Exception:
                        valid = False
                if not valid:
                    joins.append(self._begin_revoke(eid, record, "身份不足：執行中PID／建立時間／identity重新驗證失敗"))
                    continue
                row = dict(self.setting(eid))
                row.update({
                    "entry_id": eid, "profile_key": eid, "pid": record.pid,
                    "shortcut_path": record.shortcut_path, "shortcut_name": record.shortcut_name,
                    "reconnect_enabled": True,
                })
                result[record.hwnd] = row
        for worker, manor_pair in joins:
            self._finish_revoke(worker, manor_pair)
        return result

    def status_rows(self, entries: Iterable[dict]) -> list[dict]:
        rows = []
        for entry in entries:
            eid = str(entry.get("entry_id", ""))
            record = self.records.get(eid)
            worker = self.workers.get(record.hwnd) if record else None
            setting = self.setting(eid)
            rows.append({
                "entry_id": eid,
                "name": str(entry.get("name", "")),
                "managed": bool(record and self.is_window(record.hwnd)),
                "management": "固定監管" if record and self.is_window(record.hwnd) else self.rejections.get(eid, "重啟輔後既有視窗不納管，須由輔重新開啟"),
                "manor_enabled": bool(setting.get("manor_enabled", False)),
                "manor_crop_key": str(setting.get("manor_crop_key", "normal_rock")),
                "manor_quantity": int(setting.get("manor_quantity", 16) or 16),
                "fishing_enabled": bool(setting.get("fishing_enabled", False)),
                "fishing_profile_id": str(setting.get("fishing_profile_id", "")),
                "state": str(getattr(worker, "last_event", "等待由輔啟動")),
            })
        return rows

    def stop(self) -> None:
        with self._lock:
            self.closed = True
            self.stop_event.set()
        for event in list(self.worker_stops.values()):
            event.set()
        self.arbiter.release_all()
        for worker in list(self.workers.values()):
            worker.join(timeout=1.0)
        for manager, event in list(self.manors.values()):
            event.set()
            manager.join(timeout=1.0)
        if getattr(self, "_monitor_thread", None) is not threading.current_thread():
            self._monitor_thread.join(timeout=1.0)
