# -*- coding: utf-8 -*-
"""Process-wide Windows mouse-wheel hook with a strict owner-thread boundary."""
from __future__ import annotations

import ctypes
import queue
import threading
from dataclasses import dataclass
from ctypes import wintypes
from typing import Callable, Protocol


WH_MOUSE_LL = 14
HC_ACTION = 0
WM_MOUSEWHEEL = 0x020A
WM_QUIT = 0x0012
PM_NOREMOVE = 0x0000
LRESULT = ctypes.c_ssize_t


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
        ("lPrivate", wintypes.DWORD),
    ]


MSG_ABI_BY_POINTER_SIZE = {
    4: (32, {"hwnd": 0, "message": 4, "wParam": 8, "lParam": 12,
             "time": 16, "pt": 20, "lPrivate": 28}),
    8: (48, {"hwnd": 0, "message": 8, "wParam": 16, "lParam": 24,
             "time": 32, "pt": 36, "lPrivate": 44}),
}
_MSG_SIZE, _MSG_OFFSETS = MSG_ABI_BY_POINTER_SIZE[ctypes.sizeof(ctypes.c_void_p)]
assert ctypes.sizeof(MSG) == _MSG_SIZE
assert {name: getattr(MSG, name).offset for name in _MSG_OFFSETS} == _MSG_OFFSETS


NativeCallback = ctypes.WINFUNCTYPE(
    LRESULT,
    ctypes.c_int,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


@dataclass(frozen=True, slots=True)
class WheelHookEvent:
    x: int
    y: int
    delta: int
    timestamp: int
    generation: int


@dataclass(frozen=True, slots=True)
class WheelHookError:
    generation: int
    message: str


class WheelNativeApi(Protocol):
    def make_callback(self, callback: Callable[[int, int, int], int]): ...
    def install(self, callback): ...
    def call_next(self, n_code: int, w_param: int, l_param: int) -> int: ...
    def current_thread_id(self) -> int: ...
    def ensure_message_queue(self) -> None: ...
    def get_message(self) -> int: ...
    def dispatch_message(self) -> None: ...
    def post_quit(self, thread_id: int) -> bool: ...
    def unhook(self, hook) -> bool: ...


class Win32WheelNativeApi:
    def __init__(self) -> None:
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        self.message = MSG()
        self.user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int,
            NativeCallback,
            wintypes.HINSTANCE,
            wintypes.DWORD,
        ]
        self.user32.SetWindowsHookExW.restype = wintypes.HHOOK
        self.user32.CallNextHookEx.argtypes = [
            wintypes.HHOOK,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self.user32.CallNextHookEx.restype = LRESULT
        self.user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        self.user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        self.user32.GetMessageW.argtypes = [
            ctypes.POINTER(MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self.user32.GetMessageW.restype = wintypes.BOOL
        self.user32.PeekMessageW.argtypes = [
            ctypes.POINTER(MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self.user32.PeekMessageW.restype = wintypes.BOOL
        self.user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
        self.user32.TranslateMessage.restype = wintypes.BOOL
        self.user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
        self.user32.DispatchMessageW.restype = LRESULT
        self.user32.PostThreadMessageW.argtypes = [
            wintypes.DWORD,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self.user32.PostThreadMessageW.restype = wintypes.BOOL
        self.kernel32.GetCurrentThreadId.argtypes = []
        self.kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self.kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    def make_callback(self, callback: Callable[[int, int, int], int]):
        return NativeCallback(callback)

    def install(self, callback):
        module_handle = self.kernel32.GetModuleHandleW(None)
        return self.user32.SetWindowsHookExW(WH_MOUSE_LL, callback, module_handle, 0)

    def call_next(self, n_code: int, w_param: int, l_param: int) -> int:
        return int(self.user32.CallNextHookEx(None, n_code, w_param, l_param))

    def current_thread_id(self) -> int:
        return int(self.kernel32.GetCurrentThreadId())

    def ensure_message_queue(self) -> None:
        self.user32.PeekMessageW(ctypes.byref(self.message), None, 0, 0, PM_NOREMOVE)

    def get_message(self) -> int:
        return int(self.user32.GetMessageW(ctypes.byref(self.message), None, 0, 0))

    def dispatch_message(self) -> None:
        self.user32.TranslateMessage(ctypes.byref(self.message))
        self.user32.DispatchMessageW(ctypes.byref(self.message))

    def post_quit(self, thread_id: int) -> bool:
        return bool(self.user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0))

    def unhook(self, hook) -> bool:
        return bool(self.user32.UnhookWindowsHookEx(hook))


class WheelHookService:
    """Own a WH_MOUSE_LL hook and its message pump on one dedicated thread."""

    def __init__(
        self,
        native: WheelNativeApi | None = None,
        *,
        install_timeout: float = 0.75,
    ) -> None:
        self.events: queue.SimpleQueue[WheelHookEvent] = queue.SimpleQueue()
        self.errors: queue.SimpleQueue[WheelHookError] = queue.SimpleQueue()
        self._native = native if native is not None else Win32WheelNativeApi()
        self._install_timeout = max(0.01, float(install_timeout))
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._hook = None
        self._callback = None
        self._generation = 0
        self._closing = True
        self._started = threading.Event()
        self._installed = False
        self._start_cancel: threading.Event | None = None

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def active(self) -> bool:
        with self._lock:
            return bool(self._installed and self._thread and self._thread.is_alive())

    @property
    def closing(self) -> bool:
        with self._lock:
            return self._closing

    def start(self) -> bool:
        with self._lock:
            if self._installed and self._thread and self._thread.is_alive():
                return True
            if self._thread and self._thread.is_alive():
                return False
            if self._hook is not None or self._callback is not None:
                self.errors.put(WheelHookError(self._generation, "前一次滾輪監聽尚未安全解除。"))
                return False
            self._generation += 1
            generation = self._generation
            self._closing = False
            self._installed = False
            self._started = threading.Event()
            start_cancel = threading.Event()
            self._start_cancel = start_cancel
            thread = threading.Thread(
                target=self._owner_main,
                args=(generation, start_cancel),
                name="V02WheelHookOwner",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        if not self._started.wait(self._install_timeout):
            start_cancel.set()
            self.errors.put(WheelHookError(generation, "滾輪監聽啟動逾時。"))
            self.stop(timeout=self._install_timeout)
            return False
        return self.active

    def stop(self, *, timeout: float = 0.35) -> bool:
        with self._lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                self._closing = True
                self._installed = False
                self._thread = None
                return self._hook is None and self._callback is None and self._thread_id == 0
            if not self._closing:
                self._closing = True
                self._generation += 1
            start_cancel = self._start_cancel
            if start_cancel is not None:
                start_cancel.set()
            thread_id = self._thread_id
            generation = self._generation
        if thread_id and not self._native.post_quit(thread_id):
            self.errors.put(WheelHookError(generation, "無法通知滾輪監聽執行緒停止。"))
        thread.join(max(0.0, float(timeout)))
        if thread.is_alive():
            return False
        with self._lock:
            clean = self._hook is None and self._callback is None and self._thread_id == 0
            if clean and self._thread is thread:
                self._thread = None
            return clean

    def _make_native_callback(self, generation: int):
        native = self._native
        events = self.events

        def callback(n_code: int, w_param: int, l_param: int) -> int:
            if n_code == HC_ACTION and int(w_param) == WM_MOUSEWHEEL:
                info = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                delta = ctypes.c_short((int(info.mouseData) >> 16) & 0xFFFF).value
                events.put(WheelHookEvent(int(info.pt.x), int(info.pt.y), int(delta), int(info.time), generation))
            return native.call_next(n_code, w_param, l_param)

        return self._native.make_callback(callback)

    def _start_is_cancelled(
        self,
        generation: int,
        start_cancel: threading.Event,
    ) -> bool:
        with self._lock:
            return bool(
                start_cancel.is_set()
                or self._start_cancel is not start_cancel
                or self._generation != generation
                or self._closing
            )

    def _owner_main(self, generation: int, start_cancel: threading.Event) -> None:
        hook = None
        try:
            thread_id = self._native.current_thread_id()
            self._native.ensure_message_queue()
            if self._start_is_cancelled(generation, start_cancel):
                return
            callback = self._make_native_callback(generation)
            with self._lock:
                self._thread_id = thread_id
                self._callback = callback
            if self._start_is_cancelled(generation, start_cancel):
                return
            hook = self._native.install(callback)
            with self._lock:
                self._hook = hook
                self._installed = bool(hook)
            self._started.set()
            if not hook:
                self.errors.put(WheelHookError(generation, "滾輪同步常駐監聽啟動失敗。"))
                return
            if self._start_is_cancelled(generation, start_cancel):
                return
            while True:
                result = self._native.get_message()
                if result == 0:
                    break
                if result < 0:
                    raise OSError("GetMessageW failed")
                self._native.dispatch_message()
        except BaseException as exc:
            self.errors.put(WheelHookError(generation, f"滾輪監聽執行緒失敗：{exc}"))
            self._started.set()
        finally:
            unhooked = not hook
            if hook:
                unhook_error = "滾輪監聽解除失敗。"
                for attempt in range(3):
                    try:
                        unhooked = self._native.unhook(hook)
                    except BaseException as exc:
                        unhook_error = f"滾輪監聽解除失敗：{exc}"
                    if unhooked:
                        break
                    if attempt < 2:
                        threading.Event().wait(0.01)
                if not unhooked:
                    self.errors.put(WheelHookError(generation, unhook_error))
            with self._lock:
                self._installed = False
                self._thread_id = 0
                if unhooked:
                    self._hook = None
                    self._callback = None
                if self._start_cancel is start_cancel:
                    self._start_cancel = None
                self._closing = True
            self._started.set()


_PROCESS_SERVICE_LOCK = threading.Lock()
_PROCESS_SERVICE: WheelHookService | None = None


def get_process_wheel_hook_service() -> WheelHookService:
    global _PROCESS_SERVICE
    with _PROCESS_SERVICE_LOCK:
        if _PROCESS_SERVICE is None:
            _PROCESS_SERVICE = WheelHookService()
        return _PROCESS_SERVICE
