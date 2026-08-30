"""Automatic source scheduling; no game-clock validation or accuracy policy here.

Only poll/shutdown touch the model. One background job at a time owns discovery
or the stateful reader, including its finally cleanup. All events are fenced by
the full source identity and a generation; UI selection is never an input.
"""
from __future__ import annotations

from dataclasses import dataclass
import queue
import threading
import time

from v02_game_clock import HEALTH_MAX_GAP_NS, SourceIdentity
from v02_game_clock_reader import AcquisitionError

DISCOVERY_INTERVAL_NS = 5_000_000_000
FAILED_SOURCE_BACKOFF_NS = 5_000_000_000
# Waiting policy only: initial full scan (30s), the existing acquire observation
# window (45s), final full scan (30s). Progress can never renew this deadline.
FIRST_SAMPLE_TIMEOUT_NS = (30 + 45 + 30) * 1_000_000_000
RESULT_QUEUE_SIZE = 8


@dataclass(frozen=True)
class SourceToken:
    generation: int
    identity: SourceIdentity | None


def enumerate_source_windows(user, callback_type, is_flash_window, cancel, approved=None):
    """Checked, background-only enumeration, without changing the manual API."""
    windows, errors = [], []

    def visit(hwnd, _data):
        if cancel.is_set():
            return False
        try:
            hwnd = int(hwnd)
            if (user.IsWindow(hwnd) and user.IsWindowVisible(hwnd) and is_flash_window(hwnd)
                    and approved is not None and approved(hwnd)):
                windows.append(hwnd)
        except Exception:
            # Exceptions must not escape a ctypes callback and turn a partial
            # enumeration into an apparently successful empty discovery.
            errors.append(True)
            return False
        return True

    complete = user.EnumWindows(callback_type(visit), 0)
    if cancel.is_set():
        raise AcquisitionError("來源發現已取消")
    if not complete or errors:
        raise AcquisitionError("來源視窗列舉失敗")
    return tuple(dict.fromkeys(windows))


class AutoClockSource:
    def __init__(self, clock, reader, enumerate_windows, *,
                 monotonic_ns=time.perf_counter_ns, thread_factory=threading.Thread):
        self.clock, self.reader = clock, reader
        self.enumerate_windows = enumerate_windows
        self.now, self.thread_factory = monotonic_ns, thread_factory
        self.results = queue.Queue(maxsize=RESULT_QUEUE_SIZE)
        self.generation = 0
        self.token = SourceToken(0, None)
        self.worker = None
        self.worker_kind = None
        self.worker_token = None
        self.cancel = threading.Event()
        self.discovery_received = False
        self.candidates = ()
        self.attempts = {}
        self.sequence = 0
        self.next_discovery_ns = 0
        self.last_poll_ns = None
        self.health_ns = None
        self.first_sample_deadline_ns = None
        self.calibrated = False
        self.invalidation_seen = clock.invalidation_version
        self.closed = False
        self.status = "伺服器時間：尚未校正（自動尋找已開啟的遊戲）"

    @staticmethod
    def process_key(identity):
        # Several HWNDs in one process are not several independent clock scans.
        return identity.pid, identity.created

    def invalidate(self, reason="來源已失效"):
        now = self.now()
        if self.token.identity is not None:
            key = self.process_key(self.token.identity)
            self.attempts[key] = (self.sequence, now + FAILED_SOURCE_BACKOFF_NS)
        self.generation += 1
        self.token = SourceToken(self.generation, None)
        self.cancel.set()
        self.clock.invalidate(reason)
        self.invalidation_seen = self.clock.invalidation_version
        self.health_ns = self.first_sample_deadline_ns = None
        self.calibrated = False
        self.status = f"伺服器時間：尚未校正（{reason}）"

    def shutdown(self):
        if not self.closed:
            self.closed = True
            self.invalidate("程式正在關閉")

    def is_busy(self):
        return self.worker is not None and self.worker.is_alive()

    def _put(self, token, cancel, kind, payload, checked_ns):
        if cancel.is_set():
            return
        try:
            self.results.put_nowait((token, kind, payload, checked_ns))
        except queue.Full:
            # Never replace an error/sample with a heartbeat. Poll observes the
            # cancellation before consuming any queued event from this job.
            cancel.set()

    def _start(self, kind, identity=None):
        self.generation += 1
        token = self.token = SourceToken(self.generation, identity)
        cancel = self.cancel = threading.Event()
        self.worker_kind, self.worker_token = kind, token
        self.discovery_received = False
        now = self.now()
        if kind == "discover":
            self.next_discovery_ns = now + DISCOVERY_INTERVAL_NS
        else:
            self.sequence += 1
            self.attempts[self.process_key(identity)] = (self.sequence, 0)
            self.health_ns = now
            self.first_sample_deadline_ns = now + FIRST_SAMPLE_TIMEOUT_NS
            self.calibrated = False
            self.status = f"伺服器時間：唯讀驗證來源 HWND {identity.hwnd}，等待自然更新"

        def run():
            try:
                if cancel.is_set():
                    return
                if kind == "discover":
                    found, seen, rejected = [], set(), False
                    for hwnd in dict.fromkeys(self.enumerate_windows(cancel)):
                        if cancel.is_set():
                            return
                        try:
                            candidate = self.reader.native.identity(hwnd)
                        except AcquisitionError:
                            rejected = True
                            continue
                        key = self.process_key(candidate)
                        if key not in seen:
                            seen.add(key)
                            found.append(candidate)
                    self._put(token, cancel, "discovered", (tuple(found), rejected), self.now())
                else:
                    if self.reader.native.identity(identity.hwnd) != identity:
                        raise AcquisitionError("來源身分已改變")

                    def publish(sample, reason, checked_ns):
                        if cancel.is_set():
                            raise AcquisitionError("來源讀取已取消")
                        # Bind even pre-sample progress to the discovery identity;
                        # stream's own identity may race its initial open.
                        if self.reader.native.identity(identity.hwnd) != identity:
                            raise AcquisitionError("來源身分已改變")
                        self._put(token, cancel, "progress", (sample, reason), checked_ns)

                    self.reader.stream(identity.hwnd, cancel, publish)
                    self._put(token, cancel, "error", "來源讀取已結束", self.now())
            except AcquisitionError as error:
                self._put(token, cancel, "error", str(error), self.now())
            except Exception:
                # Fixed public reason: no paths, memory, or launch arguments.
                self._put(token, cancel, "error", "來源發現失敗" if kind == "discover"
                          else "唯讀驗證無法完成", self.now())

        self.worker = self.thread_factory(target=run, name="v02-game-clock", daemon=True)
        self.worker.start()

    def poll(self):
        if self.closed:
            return
        now = self.now()
        previous = self.last_poll_ns
        self.last_poll_ns = now
        # Gate old queued events BEFORE draining after suspend or model expiry.
        if previous is not None and not 0 <= now - previous <= HEALTH_MAX_GAP_NS:
            self.invalidate("時鐘更新中斷")
        self.clock.utc_ms()
        if self.clock.invalidation_version != self.invalidation_seen:
            self.invalidate(self.clock.reason)
        active = self.token.identity is not None
        if self.worker_token == self.token and self.cancel.is_set():
            self.invalidate("來源讀取已中斷" if active else "來源發現已中斷")
        elif active:
            if self.health_ns is None or not 0 <= now - self.health_ns <= HEALTH_MAX_GAP_NS:
                self.invalidate("來源讀取已中斷")
            elif not self.calibrated and now >= self.first_sample_deadline_ns:
                self.invalidate("初次有效校時樣本逾時")
        for _ in range(RESULT_QUEUE_SIZE):
            try:
                token, kind, payload, checked_ns = self.results.get_nowait()
            except queue.Empty:
                break
            if token != self.token or self.cancel.is_set():
                continue
            if kind == "error":
                self.invalidate(payload)
            elif kind == "discovered":
                self.discovery_received = True
                self.candidates, rejected = payload
                self.next_discovery_ns = self.now() + DISCOVERY_INTERVAL_NS
                if not self.candidates:
                    reason = "開啟的Flash未通過來源身分驗證" if rejected else "尚無已開啟的遊戲來源"
                    self.status = f"伺服器時間：尚未校正（{reason}）"
            elif kind == "progress" and token.identity is not None:
                item, reason = payload
                now = self.now()
                if (reason or not 0 <= now - checked_ns <= HEALTH_MAX_GAP_NS
                        or self.health_ns is not None and checked_ns < self.health_ns):
                    self.invalidate(reason or "來源回報已過期或逆序")
                    continue
                self.health_ns = checked_ns
                if item is not None:
                    if item.identity != token.identity:
                        self.invalidate("來源身分不符")
                    elif self.clock.accept_sample(item):
                        self.calibrated = True
                        self.status = (f"伺服器 UTC+8｜PID {item.identity.pid} / HWND {item.identity.hwnd}｜持續校正")
                    elif self.clock.sample is None:
                        self.invalidate(self.clock.reason)
        if self.is_busy():
            return
        if not self.results.empty():
            # A worker can publish its final event between the drain and the
            # is_alive check. Consume that event next tick before retiring it.
            return
        if self.worker is not None:
            if self.worker_token == self.token and not self.cancel.is_set():
                if self.worker_kind == "stream":
                    self.invalidate("來源讀取已結束")
                elif not self.discovery_received:
                    self.invalidate("來源發現已中斷")
            # is_alive(False) guarantees reader finally has finished. Never
            # join on Tk or reuse the stateful reader while it is winding down.
            self.worker = self.worker_kind = self.worker_token = None
        now = self.now()
        eligible = [item for item in self.candidates
                    if self.attempts.get(self.process_key(item), (0, 0))[1] <= now]
        eligible.sort(key=lambda item: self.attempts.get(self.process_key(item), (0, 0))[0])
        # Give an untried client its turn immediately after a failed client.
        if eligible and (self.process_key(eligible[0]) not in self.attempts
                         or now < self.next_discovery_ns):
            self._start("stream", eligible[0])
        elif now >= self.next_discovery_ns:
            self._start("discover")
        elif eligible:
            self._start("stream", eligible[0])
