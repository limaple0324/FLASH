"""Native Windows notification-area icon with a nonblocking event queue."""

from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from enum import Enum
from pathlib import Path
from queue import Empty, Queue


class SystemTrayEvent(str, Enum):
    SHOW = "show"
    HIDE = "hide"
    RESTORE = "restore"
    STOP_ALL = "stop_all"
    EXIT = "exit"


class WindowsSystemTrayBackend:
    """Own a message-only Windows tray host on a dedicated native thread."""

    WM_USER = 0x0400
    WM_TRAY = WM_USER + 1
    WM_CLOSE = 0x0010
    WM_DESTROY = 0x0002
    WM_LBUTTONUP = 0x0202
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    WM_CONTEXTMENU = 0x007B
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x0010
    LR_DEFAULTSIZE = 0x0040
    NIF_MESSAGE = 0x0001
    NIF_ICON = 0x0002
    NIF_TIP = 0x0004
    NIM_ADD = 0x0000
    NIM_DELETE = 0x0002
    MF_STRING = 0x0000
    MF_SEPARATOR = 0x0800
    TPM_RIGHTBUTTON = 0x0002
    TPM_NONOTIFY = 0x0080
    TPM_RETURNCMD = 0x0100

    def __init__(self) -> None:
        self._events: Queue[SystemTrayEvent] = Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self._start_error: Exception | None = None
        self._hwnd = 0
        self._class_name = f"FU-SystemTray-{id(self):X}"
        self._wnd_proc = None
        self._tooltip = "輔"
        self._icon_path = Path()

    @property
    def running(self) -> bool:
        return (
            self._thread is not None
            and self._thread.is_alive()
            and self._hwnd != 0
        )

    def start(self, icon_path: Path, tooltip: str) -> bool:
        if os.name != "nt":
            return False
        if self.running:
            return True
        resolved = Path(icon_path).resolve(strict=False)
        if not resolved.is_file() or resolved.suffix.casefold() != ".ico":
            return False
        self._icon_path = resolved
        self._tooltip = (
            tooltip.strip()[:127]
            if isinstance(tooltip, str) and tooltip.strip()
            else "輔"
        )
        self._ready.clear()
        self._stop_requested.clear()
        self._start_error = None
        self.poll_events()
        self._thread = threading.Thread(
            target=self._run,
            name="FU-SystemTray",
            daemon=True,
        )
        self._thread.start()
        ready = self._ready.wait(timeout=5.0)
        started = ready and self.running and self._start_error is None
        if not started:
            self.stop(timeout_seconds=2.0)
        return started

    def poll_events(self) -> tuple[SystemTrayEvent, ...]:
        events: list[SystemTrayEvent] = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except Empty:
                return tuple(events)

    def stop(self, timeout_seconds: float = 2.0) -> bool:
        self._stop_requested.set()
        hwnd = self._hwnd
        thread = self._thread
        if hwnd and os.name == "nt":
            try:
                ctypes.windll.user32.PostMessageW(
                    wintypes.HWND(hwnd),
                    self.WM_CLOSE,
                    0,
                    0,
                )
            except (AttributeError, OSError):
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout_seconds)))
        stopped = thread is None or not thread.is_alive()
        if stopped:
            self._thread = None
            self._hwnd = 0
        return stopped

    def _run(self) -> None:
        try:
            self._run_windows_loop()
        except Exception as error:
            self._start_error = error
            self._ready.set()
        finally:
            self._hwnd = 0
            self._ready.set()

    def _run_windows_loop(self) -> None:
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32
        lresult = ctypes.c_ssize_t
        wnd_proc_type = ctypes.WINFUNCTYPE(
            lresult,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = (
                ("cbSize", wintypes.UINT),
                ("style", wintypes.UINT),
                ("lpfnWndProc", wnd_proc_type),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
                ("hIconSm", wintypes.HICON),
            )

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = (
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("uTimeoutOrVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_byte * 16),
                ("hBalloonIcon", wintypes.HICON),
            )

        user32.RegisterClassExW.argtypes = (
            ctypes.POINTER(WNDCLASSEXW),
        )
        user32.RegisterClassExW.restype = wintypes.ATOM
        user32.CreateWindowExW.argtypes = (
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        )
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.DefWindowProcW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.DefWindowProcW.restype = lresult
        user32.LoadImageW.argtypes = (
            wintypes.HINSTANCE,
            wintypes.LPCWSTR,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        )
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.CreatePopupMenu.restype = wintypes.HMENU
        shell32.Shell_NotifyIconW.argtypes = (
            wintypes.DWORD,
            ctypes.POINTER(NOTIFYICONDATAW),
        )
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL

        def show_menu(hwnd: int) -> None:
            point = wintypes.POINT()
            if not user32.GetCursorPos(ctypes.byref(point)):
                return
            menu = user32.CreatePopupMenu()
            if not menu:
                return
            try:
                user32.AppendMenuW(menu, self.MF_STRING, 1, "顯示主程式")
                user32.AppendMenuW(menu, self.MF_STRING, 2, "隱藏主視窗")
                user32.AppendMenuW(menu, self.MF_STRING, 3, "恢復主視窗")
                user32.AppendMenuW(menu, self.MF_SEPARATOR, 0, None)
                user32.AppendMenuW(menu, self.MF_STRING, 4, "停止全部")
                user32.AppendMenuW(menu, self.MF_STRING, 5, "完全關閉程式")
                user32.SetForegroundWindow(wintypes.HWND(hwnd))
                command = user32.TrackPopupMenu(
                    menu,
                    (
                        self.TPM_RIGHTBUTTON
                        | self.TPM_RETURNCMD
                        | self.TPM_NONOTIFY
                    ),
                    point.x,
                    point.y,
                    0,
                    wintypes.HWND(hwnd),
                    None,
                )
                if command == 1:
                    self._events.put(SystemTrayEvent.SHOW)
                elif command == 2:
                    self._events.put(SystemTrayEvent.HIDE)
                elif command == 3:
                    self._events.put(SystemTrayEvent.RESTORE)
                elif command == 4:
                    self._events.put(SystemTrayEvent.STOP_ALL)
                elif command == 5:
                    self._events.put(SystemTrayEvent.EXIT)
            finally:
                user32.DestroyMenu(menu)

        @wnd_proc_type
        def window_proc(hwnd, message, wparam, lparam):
            if message == self.WM_TRAY:
                event = int(lparam) & 0xFFFF
                if event in {self.WM_LBUTTONUP, self.WM_LBUTTONDBLCLK}:
                    self._events.put(SystemTrayEvent.RESTORE)
                    return 0
                if event in {self.WM_RBUTTONUP, self.WM_CONTEXTMENU}:
                    show_menu(int(hwnd))
                    return 0
            if message == self.WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if message == self.WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        self._wnd_proc = window_proc
        instance = kernel32.GetModuleHandleW(None)
        window_class = WNDCLASSEXW()
        window_class.cbSize = ctypes.sizeof(WNDCLASSEXW)
        window_class.lpfnWndProc = window_proc
        window_class.hInstance = instance
        window_class.lpszClassName = self._class_name
        atom = user32.RegisterClassExW(ctypes.byref(window_class))
        if not atom:
            raise OSError("system tray window class could not be registered")
        hwnd = user32.CreateWindowExW(
            0,
            self._class_name,
            self._class_name,
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            instance,
            None,
        )
        if not hwnd:
            user32.UnregisterClassW(self._class_name, instance)
            raise OSError("system tray host window could not be created")
        self._hwnd = int(hwnd)
        icon = user32.LoadImageW(
            None,
            str(self._icon_path),
            self.IMAGE_ICON,
            0,
            0,
            self.LR_LOADFROMFILE | self.LR_DEFAULTSIZE,
        )
        if not icon:
            user32.DestroyWindow(hwnd)
            user32.UnregisterClassW(self._class_name, instance)
            raise OSError("confirmed system tray icon could not be loaded")
        tray_data = NOTIFYICONDATAW()
        tray_data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        tray_data.hWnd = hwnd
        tray_data.uID = 1
        tray_data.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
        tray_data.uCallbackMessage = self.WM_TRAY
        tray_data.hIcon = icon
        tray_data.szTip = self._tooltip
        if not shell32.Shell_NotifyIconW(
            self.NIM_ADD,
            ctypes.byref(tray_data),
        ):
            user32.DestroyIcon(icon)
            user32.DestroyWindow(hwnd)
            user32.UnregisterClassW(self._class_name, instance)
            raise OSError("system tray icon could not be added")
        self._ready.set()
        if self._stop_requested.is_set():
            user32.PostMessageW(
                wintypes.HWND(hwnd),
                self.WM_CLOSE,
                0,
                0,
            )
        try:
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        finally:
            shell32.Shell_NotifyIconW(
                self.NIM_DELETE,
                ctypes.byref(tray_data),
            )
            user32.DestroyIcon(icon)
            user32.UnregisterClassW(self._class_name, instance)


class SystemTrayController:
    """Bridge native tray events into the Tk main thread."""

    def __init__(
        self,
        window,
        backend: WindowsSystemTrayBackend,
        *,
        icon_path: Path,
        tooltip: str,
        on_stop_all,
        on_exit,
        operations_stopped: bool = True,
        poll_ms: int = 100,
    ) -> None:
        self._window = window
        self._backend = backend
        self._icon_path = Path(icon_path)
        self._tooltip = tooltip
        self._on_stop_all = on_stop_all
        self._on_exit = on_exit
        self._poll_ms = max(20, int(poll_ms))
        self._poll_id = None
        self._running = False
        self._operations_stopped = bool(operations_stopped)

    @property
    def running(self) -> bool:
        return self._running

    @property
    def operations_stopped(self) -> bool:
        return self._operations_stopped

    def mark_operations_running(self) -> None:
        self._operations_stopped = False

    def start(self) -> bool:
        if self._running:
            return True
        self._running = bool(
            self._backend.start(self._icon_path, self._tooltip)
        )
        if self._running:
            self._schedule_poll()
        return self._running

    def _schedule_poll(self) -> None:
        if self._running:
            self._poll_id = self._window.after(
                self._poll_ms,
                self.poll,
            )

    def poll(self) -> None:
        self._poll_id = None
        if not self._running:
            return
        for event in self._backend.poll_events():
            if event in (SystemTrayEvent.SHOW, SystemTrayEvent.RESTORE):
                self.restore()
            elif event is SystemTrayEvent.HIDE:
                self.hide()
            elif event is SystemTrayEvent.STOP_ALL:
                self._operations_stopped = bool(self._on_stop_all())
            elif event is SystemTrayEvent.EXIT:
                self._on_exit()
        self._schedule_poll()

    def handle_unmap(self, _event=None) -> None:
        if not self._running:
            return
        self._window.after_idle(self._hide_if_iconic)

    def _hide_if_iconic(self) -> None:
        if not self._running:
            return
        try:
            if self._window.state() == "iconic":
                self.hide()
        except Exception:
            return

    def hide(self) -> None:
        try:
            self._window.withdraw()
        except Exception:
            return

    def restore(self) -> None:
        try:
            self._window.deiconify()
            self._window.state("normal")
            self._window.lift()
            self._window.focus_force()
        except Exception:
            return

    def stop(self, timeout_seconds: float = 2.0) -> bool:
        if self._poll_id is not None:
            try:
                self._window.after_cancel(self._poll_id)
            except Exception:
                pass
            self._poll_id = None
        try:
            stopped = self._backend.stop(timeout_seconds=timeout_seconds)
        except Exception:
            stopped = False
        self._running = not stopped
        if self._running:
            self._schedule_poll()
        return stopped
