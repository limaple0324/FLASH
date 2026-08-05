"""Read-only Windows background capture probe for FLASH SP1.

This module never sends mouse or keyboard input. It uses the Windows PrintWindow
API to ask a target window to render into an off-screen bitmap, then performs a
small validity check on the captured pixels. Actual support still requires a
real target-desktop test because some legacy renderers return blank frames.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from dataclasses import dataclass
from ctypes import wintypes
from typing import Callable, Protocol


_WINDOW_STATE_MUTATION_LOCK = threading.RLock()


@dataclass(frozen=True, slots=True)
class _WindowInstanceCredential:
    """Immutable evidence that one HWND still belongs to the same window."""

    handle: int
    process_id: int
    thread_id: int
    window_class: str
    process_lifecycle_token: int


def _default_process_lifecycle_token(process_id: int) -> int | None:
    """Return the process creation FILETIME used to reject PID reuse."""

    if os.name != "nt" or process_id <= 0:
        return None
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = (
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    try:
        process_handle = kernel32.OpenProcess(
            0x1000,  # PROCESS_QUERY_LIMITED_INFORMATION
            False,
            process_id,
        )
    except OSError:
        return None
    if not process_handle:
        return None
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            process_handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        return (
            int(created.dwHighDateTime) << 32
        ) | int(created.dwLowDateTime)
    except OSError:
        return None
    finally:
        try:
            kernel32.CloseHandle(process_handle)
        except OSError:
            pass


def _raw_handle_value(handle) -> int:
    if isinstance(handle, int):
        return int(handle)
    return int(getattr(handle, "value", 0) or 0)


def _query_window_instance_credential(
    user32,
    hwnd,
    lifecycle_provider: Callable[[int], int | None],
) -> _WindowInstanceCredential | None:
    """Read fail-closed identity evidence for one live top-level window."""

    def read_identity() -> tuple[int, int, int, str] | None:
        if not user32.IsWindow(hwnd):
            return None
        process_id = wintypes.DWORD()
        thread_id = int(
            user32.GetWindowThreadProcessId(
                hwnd,
                ctypes.byref(process_id),
            )
            or 0
        )
        if thread_id <= 0 or int(process_id.value) <= 0:
            return None
        class_name = ctypes.create_unicode_buffer(256)
        class_length = int(
            user32.GetClassNameW(
                hwnd,
                class_name,
                len(class_name),
            )
            or 0
        )
        if class_length <= 0:
            return None
        return (
            _raw_handle_value(hwnd),
            int(process_id.value),
            thread_id,
            class_name.value,
        )

    try:
        identity_before = read_identity()
        if identity_before is None:
            return None
        lifecycle_before = lifecycle_provider(identity_before[1])
        identity_after = read_identity()
        if identity_after is None:
            return None
        lifecycle_after = lifecycle_provider(identity_after[1])
    except (OSError, TypeError, ValueError):
        return None
    valid_lifecycle_before = bool(
        isinstance(lifecycle_before, int)
        and not isinstance(lifecycle_before, bool)
        and lifecycle_before > 0
    )
    valid_lifecycle_after = bool(
        isinstance(lifecycle_after, int)
        and not isinstance(lifecycle_after, bool)
        and lifecycle_after > 0
    )
    if (
        not valid_lifecycle_before
        or not valid_lifecycle_after
        or identity_before != identity_after
        or lifecycle_before != lifecycle_after
    ):
        return None
    return _WindowInstanceCredential(
        handle=identity_after[0],
        process_id=identity_after[1],
        thread_id=identity_after[2],
        window_class=identity_after[3],
        process_lifecycle_token=lifecycle_after,
    )


def _same_window_instance(
    user32,
    hwnd,
    expected: _WindowInstanceCredential,
    lifecycle_provider: Callable[[int], int | None],
) -> bool:
    return (
        _query_window_instance_credential(
            user32,
            hwnd,
            lifecycle_provider,
        )
        == expected
    )


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class _WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("rcNormalPosition", wintypes.RECT),
    ]


def _configure_win32_capture_api(user32, gdi32) -> None:
    """Apply pointer-safe ctypes signatures to every Win32 capture call."""
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.IsIconic.argtypes = (wintypes.HWND,)
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowPlacement.argtypes = (
        wintypes.HWND,
        ctypes.POINTER(_WINDOWPLACEMENT),
    )
    user32.GetWindowPlacement.restype = wintypes.BOOL
    user32.GetWindowDC.argtypes = (wintypes.HWND,)
    user32.GetWindowDC.restype = wintypes.HDC
    user32.PrintWindow.argtypes = (wintypes.HWND, wintypes.HDC, wintypes.UINT)
    user32.PrintWindow.restype = wintypes.BOOL
    user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
    user32.ReleaseDC.restype = ctypes.c_int

    gdi32.CreateCompatibleDC.argtypes = (wintypes.HDC,)
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = (wintypes.HDC, ctypes.c_int, ctypes.c_int)
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = (wintypes.HDC, wintypes.HGDIOBJ)
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.GetDIBits.argtypes = (
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        wintypes.LPVOID,
        ctypes.POINTER(_BITMAPINFO),
        wintypes.UINT,
    )
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = (wintypes.HDC,)
    gdi32.DeleteDC.restype = wintypes.BOOL


@dataclass(frozen=True, slots=True)
class CaptureSample:
    width: int
    height: int
    pixels: bytes
    api_succeeded: bool


class WindowCaptureProvider(Protocol):
    def capture(self, window_handle: int) -> CaptureSample | None:
        """Capture a target window without changing focus or sending input."""


class Win32PrintWindowProvider:
    """ctypes implementation of an off-screen PrintWindow capture."""

    @staticmethod
    def _libraries():
        return ctypes.windll.user32, ctypes.windll.gdi32

    @staticmethod
    def _capture_rect(user32, hwnd) -> wintypes.RECT | None:
        if user32.IsIconic(hwnd):
            placement = _WINDOWPLACEMENT()
            placement.length = ctypes.sizeof(_WINDOWPLACEMENT)
            if not user32.GetWindowPlacement(hwnd, ctypes.byref(placement)):
                return None
            return placement.rcNormalPosition

        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return rect

    def capture(self, window_handle: int) -> CaptureSample | None:
        if os.name != "nt" or not window_handle:
            return None

        user32, gdi32 = self._libraries()
        _configure_win32_capture_api(user32, gdi32)
        hwnd = wintypes.HWND(window_handle)

        rect = self._capture_rect(user32, hwnd)
        if rect is None:
            return None

        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            return None

        window_dc = user32.GetWindowDC(hwnd)
        if not window_dc:
            return None

        memory_dc = gdi32.CreateCompatibleDC(window_dc)
        bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
        old_object = None
        bitmap_selected = False
        try:
            if not memory_dc or not bitmap:
                return None

            old_object = gdi32.SelectObject(memory_dc, bitmap)
            if not old_object:
                return None
            bitmap_selected = True

            # Legacy Flash projectors render more reliably with the documented
            # whole-window mode (flags=0); PW_RENDERFULLCONTENT can return a
            # stale pre-modal frame for background instances.
            api_succeeded = bool(user32.PrintWindow(hwnd, memory_dc, 0))
            restored_object = gdi32.SelectObject(memory_dc, old_object)
            if not restored_object:
                return None
            bitmap_selected = False

            info = _BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            info.bmiHeader.biWidth = width
            info.bmiHeader.biHeight = -height  # top-down buffer
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            info.bmiHeader.biCompression = 0  # BI_RGB

            buffer_size = width * height * 4
            buffer = (ctypes.c_ubyte * buffer_size)()
            copied = gdi32.GetDIBits(
                memory_dc,
                bitmap,
                0,
                height,
                ctypes.byref(buffer),
                ctypes.byref(info),
                0,
            )
            if copied != height:
                return None

            return CaptureSample(
                width=width,
                height=height,
                pixels=bytes(buffer),
                api_succeeded=api_succeeded,
            )
        finally:
            if bitmap_selected and old_object and memory_dc:
                if gdi32.SelectObject(memory_dc, old_object):
                    bitmap_selected = False
            if bitmap_selected:
                # A selected bitmap cannot be deleted. Releasing its memory DC
                # first makes the bitmap deletable even when restoration failed.
                if memory_dc:
                    gdi32.DeleteDC(memory_dc)
                    memory_dc = None
                if bitmap:
                    gdi32.DeleteObject(bitmap)
            else:
                if bitmap:
                    gdi32.DeleteObject(bitmap)
                if memory_dc:
                    gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(hwnd, window_dc)


class Win32RecoveringPrintWindowProvider(Win32PrintWindowProvider):
    """Refresh minimized Flash windows without taking foreground focus.

    A minimized legacy projector can keep returning its pre-modal PrintWindow
    frame. This provider briefly restores it without activation, uses the
    guarded temporary-reveal plus visible-desktop-pixel path for a fresh frame,
    and returns it to the minimized state in ``finally``. Passive PrintWindow
    pixels are never accepted by this provider.
    """

    SW_SHOWNOACTIVATE = 4
    SW_SHOWMINNOACTIVE = 7
    _window_state_lock = _WINDOW_STATE_MUTATION_LOCK

    def __init__(
        self,
        *,
        paint_settle_seconds: float = 0.75,
        fresh_capture_provider: WindowCaptureProvider | None = None,
        process_lifecycle_provider: (
            Callable[[int], int | None] | None
        ) = None,
    ) -> None:
        self._paint_settle_seconds = max(0.0, float(paint_settle_seconds))
        self._process_lifecycle_provider = (
            process_lifecycle_provider
            or _default_process_lifecycle_token
        )
        self._fresh_capture_provider = (
            fresh_capture_provider
            or Win32TemporarilyRevealedCaptureProvider(
                process_lifecycle_provider=(
                    self._process_lifecycle_provider
                )
            )
        )
        self._last_failure_stage: str | None = None

    @property
    def last_failure_stage(self) -> str | None:
        """Return one anonymous fail-closed stage from the latest capture."""

        return self._last_failure_stage

    @staticmethod
    def _window_placement(user32, hwnd) -> _WINDOWPLACEMENT | None:
        placement = _WINDOWPLACEMENT()
        placement.length = ctypes.sizeof(_WINDOWPLACEMENT)
        try:
            succeeded = user32.GetWindowPlacement(
                hwnd,
                ctypes.byref(placement),
            )
        except OSError:
            return None
        if not succeeded:
            return None
        return placement

    @staticmethod
    def _placement_signature(
        placement: _WINDOWPLACEMENT,
    ) -> tuple[int, int, int, int, int, int, int, int, int, int]:
        return (
            int(placement.flags),
            int(placement.ptMinPosition.x),
            int(placement.ptMinPosition.y),
            int(placement.ptMaxPosition.x),
            int(placement.ptMaxPosition.y),
            int(placement.rcNormalPosition.left),
            int(placement.rcNormalPosition.top),
            int(placement.rcNormalPosition.right),
            int(placement.rcNormalPosition.bottom),
            int(placement.length),
        )

    @classmethod
    def _minimized_window_was_restored(
        cls,
        user32,
        hwnd,
        *,
        process_id: int,
        minimized_rect: tuple[int, int, int, int],
        placement_signature: tuple[int, int, int, int, int, int, int, int, int, int],
        was_topmost: bool,
        expected_instance: _WindowInstanceCredential | None = None,
        lifecycle_provider: (
            Callable[[int], int | None] | None
        ) = None,
    ) -> bool:
        state = Win32TemporarilyRevealedCaptureProvider
        try:
            placement = cls._window_placement(user32, hwnd)
            if (
                expected_instance is not None
                and lifecycle_provider is not None
            ):
                instance_is_current = state._same_window_instance(
                    user32,
                    hwnd,
                    expected_instance,
                    lifecycle_provider,
                )
            else:
                instance_is_current = (
                    state._window_pid(user32, hwnd) == process_id
                )
            return bool(
                user32.IsWindow(hwnd)
                and user32.IsWindowVisible(hwnd)
                and user32.IsIconic(hwnd)
                and instance_is_current
                and state._window_rect(user32, hwnd) == minimized_rect
                and placement is not None
                and cls._placement_signature(placement)
                == placement_signature
                and state._is_topmost(user32, hwnd) is was_topmost
            )
        except OSError:
            return False

    @classmethod
    def _both_neighbor_relations_are_restored(
        cls,
        user32,
        hwnd,
        *,
        previous_handle: int,
        next_handle: int,
        previous_instance: _WindowInstanceCredential | None = None,
        next_instance: _WindowInstanceCredential | None = None,
        lifecycle_provider: (
            Callable[[int], int | None] | None
        ) = None,
    ) -> bool:
        state = Win32TemporarilyRevealedCaptureProvider
        try:
            if lifecycle_provider is not None and not (
                state._reference_instances_are_current(
                    user32,
                    previous_handle=previous_handle,
                    next_handle=next_handle,
                    previous_instance=previous_instance,
                    next_instance=next_instance,
                    lifecycle_provider=lifecycle_provider,
                )
            ):
                return False
            if state._existing_handle(user32, previous_handle):
                if state._handle_value(
                    user32.GetWindow(hwnd, state.GW_HWNDPREV)
                ) != previous_handle:
                    return False
            if state._existing_handle(user32, next_handle):
                if state._handle_value(
                    user32.GetWindow(hwnd, state.GW_HWNDNEXT)
                ) != next_handle:
                    return False
            return True
        except OSError:
            return False

    @classmethod
    def _trusted_minimized_neighbor_restoration(
        cls,
        user32,
        hwnd,
        *,
        previous_handle: int,
        next_handle: int,
        previous_instance: _WindowInstanceCredential | None,
        next_instance: _WindowInstanceCredential | None,
        lifecycle_provider: Callable[[int], int | None],
    ) -> bool:
        """Trust unchanged neighbours when Windows preserves either edge.

        Restoring or minimizing one window can reorder other live windows.
        Both original neighbour instances must still be current.  With two
        original neighbours, either exact adjacent edge is sufficient; with
        one original neighbour, that sole edge remains mandatory.
        """

        state = Win32TemporarilyRevealedCaptureProvider
        try:
            if not state._reference_instances_are_current(
                user32,
                previous_handle=previous_handle,
                next_handle=next_handle,
                previous_instance=previous_instance,
                next_instance=next_instance,
                lifecycle_provider=lifecycle_provider,
            ):
                return False
            previous_restored = bool(
                previous_handle
                and state._handle_value(
                    user32.GetWindow(hwnd, state.GW_HWNDPREV)
                )
                == previous_handle
            )
            next_restored = bool(
                next_handle
                and state._handle_value(
                    user32.GetWindow(hwnd, state.GW_HWNDNEXT)
                )
                == next_handle
            )
            # The adjacency reads above are another Win32 call boundary.  A
            # captured neighbour can be destroyed and its HWND reused after
            # the first identity barrier but before the relationship result
            # is trusted.  Revalidate both original instances immediately
            # before returning any successful restoration verdict.
            if not state._reference_instances_are_current(
                user32,
                previous_handle=previous_handle,
                next_handle=next_handle,
                previous_instance=previous_instance,
                next_instance=next_instance,
                lifecycle_provider=lifecycle_provider,
            ):
                return False
            if previous_handle and next_handle:
                return previous_restored or next_restored
            if previous_handle:
                return previous_restored
            if next_handle:
                return next_restored
            return True
        except OSError:
            return False

    def capture(self, window_handle: int) -> CaptureSample | None:
        self._last_failure_stage = None
        if os.name != "nt" or not window_handle:
            self._last_failure_stage = "platform_or_target_invalid"
            return None

        user32, _gdi32 = self._libraries()
        hwnd = wintypes.HWND(window_handle)
        state = Win32TemporarilyRevealedCaptureProvider
        state._configure(user32)
        user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.ShowWindow.restype = wintypes.BOOL
        user32.GetWindowPlacement.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(_WINDOWPLACEMENT),
        )
        user32.GetWindowPlacement.restype = wintypes.BOOL

        with self._window_state_lock:
            try:
                is_valid_minimized_window = bool(
                    user32.IsWindow(hwnd)
                    and user32.IsWindowVisible(hwnd)
                    and user32.IsIconic(hwnd)
                )
            except OSError:
                self._last_failure_stage = "window_snapshot_failed"
                return None
            if not is_valid_minimized_window:
                # The caller's earlier window snapshot is stale. This provider
                # can only prove freshness when it owns the complete minimized
                # -> no-activate restore -> capture -> minimized transition.
                self._last_failure_stage = "window_not_minimized"
                return None
            try:
                target_instance = state._window_instance_credential(
                    user32,
                    hwnd,
                    self._process_lifecycle_provider,
                )
                minimized_rect = state._window_rect(user32, hwnd)
                original_placement = self._window_placement(user32, hwnd)
                was_topmost = state._is_topmost(user32, hwnd)
                original_foreground = state._handle_value(
                    user32.GetForegroundWindow()
                )
                previous_handle = state._handle_value(
                    user32.GetWindow(hwnd, state.GW_HWNDPREV)
                )
                next_handle = state._handle_value(
                    user32.GetWindow(hwnd, state.GW_HWNDNEXT)
                )
            except OSError:
                self._last_failure_stage = "window_state_read_failed"
                return None
            if (
                target_instance is None
                or minimized_rect is None
                or original_placement is None
            ):
                self._last_failure_stage = "target_instance_incomplete"
                return None
            process_id = target_instance.process_id
            original_foreground_instance = (
                state._window_instance_credential(
                    user32,
                    wintypes.HWND(original_foreground),
                    self._process_lifecycle_provider,
                )
                if original_foreground
                else None
            )
            previous_instance = (
                state._window_instance_credential(
                    user32,
                    wintypes.HWND(previous_handle),
                    self._process_lifecycle_provider,
                )
                if previous_handle
                else None
            )
            next_instance = (
                state._window_instance_credential(
                    user32,
                    wintypes.HWND(next_handle),
                    self._process_lifecycle_provider,
                )
                if next_handle
                else None
            )
            if (
                (original_foreground and original_foreground_instance is None)
                or (previous_handle and previous_instance is None)
                or (next_handle and next_instance is None)
            ):
                self._last_failure_stage = "restore_reference_incomplete"
                return None
            placement_signature = self._placement_signature(
                original_placement
            )
            temporarily_restored = False
            restored = False
            sample: CaptureSample | None = None
            restored_rect: tuple[int, int, int, int] | None = None
            try:
                try:
                    if not state._same_window_instance(
                        user32,
                        hwnd,
                        target_instance,
                        self._process_lifecycle_provider,
                    ):
                        self._last_failure_stage = "target_instance_changed"
                        return None
                    user32.ShowWindow(hwnd, self.SW_SHOWNOACTIVATE)
                except OSError:
                    self._last_failure_stage = "temporary_restore_failed"
                    return None
                temporarily_restored = not bool(user32.IsIconic(hwnd))
                current_placement = self._window_placement(user32, hwnd)
                current_foreground = state._handle_value(
                    user32.GetForegroundWindow()
                )
                foreground_preserved = state._foreground_was_preserved(
                    user32,
                    hwnd,
                    original_foreground,
                    original_instance=original_foreground_instance,
                    lifecycle_provider=self._process_lifecycle_provider,
                )
                if (
                    current_foreground != original_foreground
                    or not foreground_preserved
                ):
                    self._last_failure_stage = "foreground_changed"
                    return None
                if (
                    not temporarily_restored
                    or not user32.IsWindow(hwnd)
                    or not user32.IsWindowVisible(hwnd)
                    or not state._same_window_instance(
                        user32,
                        hwnd,
                        target_instance,
                        self._process_lifecycle_provider,
                    )
                    or state._is_topmost(user32, hwnd) is not was_topmost
                    or current_placement is None
                    or self._placement_signature(current_placement)
                    != placement_signature
                ):
                    self._last_failure_stage = "restored_state_changed"
                    return None
                restored_rect = state._window_rect(user32, hwnd)
                if restored_rect is None:
                    self._last_failure_stage = "restored_rect_unavailable"
                    return None
                if self._paint_settle_seconds:
                    time.sleep(self._paint_settle_seconds)
                current_foreground = state._handle_value(
                    user32.GetForegroundWindow()
                )
                foreground_preserved = state._foreground_was_preserved(
                    user32,
                    hwnd,
                    original_foreground,
                    original_instance=original_foreground_instance,
                    lifecycle_provider=self._process_lifecycle_provider,
                )
                if (
                    not state._same_live_normal_window(
                        user32,
                        hwnd,
                        process_id=process_id,
                        rect=restored_rect,
                        topmost=was_topmost,
                        expected_instance=target_instance,
                        lifecycle_provider=self._process_lifecycle_provider,
                    )
                    or current_foreground != original_foreground
                    or not foreground_preserved
                ):
                    self._last_failure_stage = "pre_capture_barrier_failed"
                    return None
                try:
                    sample = self._fresh_capture_provider.capture(
                        window_handle
                    )
                except OSError:
                    sample = None
                current_foreground = state._handle_value(
                    user32.GetForegroundWindow()
                )
                foreground_preserved = state._foreground_was_preserved(
                    user32,
                    hwnd,
                    original_foreground,
                    original_instance=original_foreground_instance,
                    lifecycle_provider=self._process_lifecycle_provider,
                )
                if (
                    sample is None
                    or not sample.api_succeeded
                    or not state._same_live_normal_window(
                        user32,
                        hwnd,
                        process_id=process_id,
                        rect=restored_rect,
                        topmost=was_topmost,
                        expected_instance=target_instance,
                        lifecycle_provider=self._process_lifecycle_provider,
                    )
                    or current_foreground != original_foreground
                    or not foreground_preserved
                ):
                    sample = None
                    self._last_failure_stage = "fresh_capture_failed"
            finally:
                if (
                    temporarily_restored
                    and state._same_window_instance(
                        user32,
                        hwnd,
                        target_instance,
                        self._process_lifecycle_provider,
                    )
                ):
                    try:
                        if not user32.IsIconic(hwnd):
                            if state._same_window_instance(
                                user32,
                                hwnd,
                                target_instance,
                                self._process_lifecycle_provider,
                            ):
                                user32.ShowWindow(
                                    hwnd,
                                    self.SW_SHOWMINNOACTIVE,
                                )
                            else:
                                restored = False
                    except OSError:
                        restored = False
                    else:
                        restored = self._minimized_window_was_restored(
                            user32,
                            hwnd,
                            process_id=process_id,
                            minimized_rect=minimized_rect,
                            placement_signature=placement_signature,
                            was_topmost=was_topmost,
                            expected_instance=target_instance,
                            lifecycle_provider=self._process_lifecycle_provider,
                        )
                    if restored:
                        try:
                            if not self._both_neighbor_relations_are_restored(
                                user32,
                                hwnd,
                                previous_handle=previous_handle,
                                next_handle=next_handle,
                                previous_instance=previous_instance,
                                next_instance=next_instance,
                                lifecycle_provider=(
                                    self._process_lifecycle_provider
                                ),
                            ):
                                restore_plan = (
                                    state._restore_insert_after_for_instances(
                                        user32,
                                        hwnd,
                                        previous_handle=previous_handle,
                                        next_handle=next_handle,
                                        previous_instance=previous_instance,
                                        next_instance=next_instance,
                                        was_topmost=was_topmost,
                                        lifecycle_provider=(
                                            self._process_lifecycle_provider
                                        ),
                                    )
                                )
                                if restore_plan is None:
                                    pass
                                else:
                                    (
                                        insert_after,
                                        _verify_relation,
                                        anchor_instance,
                                    ) = restore_plan
                                    state._set_window_position_if_instances_current(
                                        user32,
                                        hwnd,
                                        insert_after=insert_after,
                                        target_instance=target_instance,
                                        previous_handle=previous_handle,
                                        next_handle=next_handle,
                                        previous_instance=previous_instance,
                                        next_instance=next_instance,
                                        anchor_instance=anchor_instance,
                                        lifecycle_provider=(
                                            self._process_lifecycle_provider
                                        ),
                                    )
                        except OSError:
                            pass
                    restored = bool(
                        restored
                        and self._minimized_window_was_restored(
                            user32,
                            hwnd,
                            process_id=process_id,
                            minimized_rect=minimized_rect,
                            placement_signature=placement_signature,
                            was_topmost=was_topmost,
                            expected_instance=target_instance,
                            lifecycle_provider=self._process_lifecycle_provider,
                        )
                        and self._trusted_minimized_neighbor_restoration(
                            user32,
                            hwnd,
                            previous_handle=previous_handle,
                            next_handle=next_handle,
                            previous_instance=previous_instance,
                            next_instance=next_instance,
                            lifecycle_provider=self._process_lifecycle_provider,
                        )
                    )
                    try:
                        foreground_preserved = (
                            state._foreground_was_preserved(
                                user32,
                                hwnd,
                                original_foreground,
                                original_instance=(
                                    original_foreground_instance
                                ),
                                lifecycle_provider=(
                                    self._process_lifecycle_provider
                                ),
                            )
                        )
                    except OSError:
                        foreground_preserved = False
                    restored = bool(restored and foreground_preserved)
                    if not restored:
                        self._last_failure_stage = "restoration_barrier_failed"
            if sample is not None and restored:
                self._last_failure_stage = None
                return sample
            if self._last_failure_stage is None:
                self._last_failure_stage = "fresh_capture_failed"
            return None


class Win32VisibleRegionCaptureProvider:
    """Capture desktop pixels only when every required game area is visible.

    This is a passive fallback for legacy Flash windows whose PrintWindow frame
    can remain on an older screen. It never activates, moves, restores, or
    sends input to the target window.
    """

    SRCCOPY = 0x00CC0020
    CAPTUREBLT = 0x40000000
    GA_ROOT = 2
    PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
    REQUIRED_VISIBLE_POINTS = (
        (0.03, 0.03),
        (0.50, 0.03),
        (0.97, 0.03),
        (0.03, 0.16),
        (0.50, 0.16),
        (0.86, 0.16),
        (0.97, 0.16),
        (0.03, 0.33),
        (0.50, 0.33),
        (0.97, 0.33),
        (0.03, 0.54),
        (0.50, 0.54),
        (0.97, 0.54),
        (0.03, 0.71),
        (0.355, 0.71),
        (0.50, 0.71),
        (0.651, 0.71),
        (0.97, 0.71),
        (0.03, 0.86),
        (0.353, 0.86),
        (0.50, 0.86),
        (0.97, 0.86),
        (0.03, 0.97),
        (0.50, 0.97),
        (0.97, 0.97),
    )

    def __init__(
        self,
        *,
        process_lifecycle_provider: (
            Callable[[int], int | None] | None
        ) = None,
    ) -> None:
        self._process_lifecycle_provider = (
            process_lifecycle_provider
            or _default_process_lifecycle_token
        )

    @staticmethod
    def _libraries():
        return ctypes.windll.user32, ctypes.windll.gdi32

    @staticmethod
    def _configure(user32, gdi32) -> None:
        _configure_win32_capture_api(user32, gdi32)
        user32.IsWindow.argtypes = (wintypes.HWND,)
        user32.IsWindow.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetDC.argtypes = (wintypes.HWND,)
        user32.GetDC.restype = wintypes.HDC
        user32.WindowFromPoint.argtypes = (wintypes.POINT,)
        user32.WindowFromPoint.restype = wintypes.HWND
        user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
        user32.GetAncestor.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        )
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetClassNameW.argtypes = (
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        )
        user32.GetClassNameW.restype = ctypes.c_int
        set_context = getattr(
            user32,
            "SetThreadDpiAwarenessContext",
            None,
        )
        if set_context is not None:
            set_context.argtypes = (ctypes.c_void_p,)
            set_context.restype = ctypes.c_void_p
        gdi32.BitBlt.argtypes = (
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.DWORD,
        )
        gdi32.BitBlt.restype = wintypes.BOOL

    @classmethod
    def _enter_dpi_context(cls, user32):
        set_context = getattr(
            user32,
            "SetThreadDpiAwarenessContext",
            None,
        )
        if set_context is None:
            return None
        try:
            return set_context(cls.PER_MONITOR_AWARE_V2)
        except OSError:
            return None

    @staticmethod
    def _leave_dpi_context(user32, previous) -> None:
        if not previous:
            return
        set_context = getattr(
            user32,
            "SetThreadDpiAwarenessContext",
            None,
        )
        if set_context is None:
            return
        try:
            set_context(ctypes.c_void_p(previous))
        except OSError:
            pass

    @classmethod
    def _required_regions_are_visible(
        cls,
        user32,
        hwnd,
        rect: wintypes.RECT,
    ) -> bool:
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        for relative_x, relative_y in cls.REQUIRED_VISIBLE_POINTS:
            point = wintypes.POINT(
                int(rect.left + width * relative_x),
                int(rect.top + height * relative_y),
            )
            found = user32.WindowFromPoint(point)
            if not found:
                return False
            root = user32.GetAncestor(found, cls.GA_ROOT)
            if int(root or 0) != int(hwnd.value or 0):
                return False
        return True

    def capture(self, window_handle: int) -> CaptureSample | None:
        if os.name != "nt" or not window_handle:
            return None
        user32, gdi32 = self._libraries()
        self._configure(user32, gdi32)
        hwnd = wintypes.HWND(window_handle)
        if (
            not user32.IsWindow(hwnd)
            or not user32.IsWindowVisible(hwnd)
            or user32.IsIconic(hwnd)
        ):
            return None
        target_instance = _query_window_instance_credential(
            user32,
            hwnd,
            self._process_lifecycle_provider,
        )
        if target_instance is None:
            return None
        previous = self._enter_dpi_context(user32)
        try:
            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return None
            width = int(rect.right - rect.left)
            height = int(rect.bottom - rect.top)
            if (
                width <= 0
                or height <= 0
                or not self._required_regions_are_visible(
                    user32,
                    hwnd,
                    rect,
                )
                or not _same_window_instance(
                    user32,
                    hwnd,
                    target_instance,
                    self._process_lifecycle_provider,
                )
            ):
                return None
            screen_dc = user32.GetDC(None)
            if not screen_dc:
                return None
            memory_dc = gdi32.CreateCompatibleDC(screen_dc)
            bitmap = gdi32.CreateCompatibleBitmap(
                screen_dc,
                width,
                height,
            )
            old_object = None
            bitmap_selected = False
            try:
                if not memory_dc or not bitmap:
                    return None
                old_object = gdi32.SelectObject(memory_dc, bitmap)
                if not old_object:
                    return None
                bitmap_selected = True
                copied_to_bitmap = bool(
                    gdi32.BitBlt(
                        memory_dc,
                        0,
                        0,
                        width,
                        height,
                        screen_dc,
                        int(rect.left),
                        int(rect.top),
                        self.SRCCOPY | self.CAPTUREBLT,
                    )
                )
                restored = gdi32.SelectObject(memory_dc, old_object)
                if not restored:
                    return None
                bitmap_selected = False
                info = _BITMAPINFO()
                info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
                info.bmiHeader.biWidth = width
                info.bmiHeader.biHeight = -height
                info.bmiHeader.biPlanes = 1
                info.bmiHeader.biBitCount = 32
                info.bmiHeader.biCompression = 0
                buffer = (ctypes.c_ubyte * (width * height * 4))()
                copied_rows = gdi32.GetDIBits(
                    memory_dc,
                    bitmap,
                    0,
                    height,
                    ctypes.byref(buffer),
                    ctypes.byref(info),
                    0,
                )
                verification_rect = wintypes.RECT()
                if (
                    copied_rows != height
                    or not user32.IsWindow(hwnd)
                    or not user32.IsWindowVisible(hwnd)
                    or user32.IsIconic(hwnd)
                    or not user32.GetWindowRect(
                        hwnd,
                        ctypes.byref(verification_rect),
                    )
                    or (
                        int(verification_rect.left),
                        int(verification_rect.top),
                        int(verification_rect.right),
                        int(verification_rect.bottom),
                    )
                    != (
                        int(rect.left),
                        int(rect.top),
                        int(rect.right),
                        int(rect.bottom),
                    )
                    or not self._required_regions_are_visible(
                        user32,
                        hwnd,
                        verification_rect,
                    )
                    or not _same_window_instance(
                        user32,
                        hwnd,
                        target_instance,
                        self._process_lifecycle_provider,
                    )
                ):
                    return None
                return CaptureSample(
                    width=width,
                    height=height,
                    pixels=bytes(buffer),
                    api_succeeded=copied_to_bitmap,
                )
            finally:
                if bitmap_selected and old_object and memory_dc:
                    if gdi32.SelectObject(memory_dc, old_object):
                        bitmap_selected = False
                if bitmap_selected:
                    if memory_dc:
                        gdi32.DeleteDC(memory_dc)
                        memory_dc = None
                    if bitmap:
                        gdi32.DeleteObject(bitmap)
                else:
                    if bitmap:
                        gdi32.DeleteObject(bitmap)
                    if memory_dc:
                        gdi32.DeleteDC(memory_dc)
                user32.ReleaseDC(None, screen_dc)
        finally:
            self._leave_dpi_context(user32, previous)


class Win32TemporarilyRevealedCaptureProvider:
    """Capture an obscured normal window through a reversible Z-order change.

    ``PrintWindow`` can return an older Flash frame, so it is never used here.
    The target is placed at the top of its existing Z-order band without
    activation, a guarded desktop capture is taken, and the original ordering
    is restored before the sample is returned. Any identity, foreground,
    position, minimized-state, or restoration race fails closed.

    This provider is intentionally separate from passive and minimized capture.
    Callers must invoke it only from the explicitly enabled obscured-window
    path.
    """

    GWL_EXSTYLE = -20
    WS_EX_TOPMOST = 0x00000008
    GW_HWNDNEXT = 2
    GW_HWNDPREV = 3
    HWND_TOP = 0
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOACTIVATE = 0x0010
    SWP_NOOWNERZORDER = 0x0200
    SWP_NOSENDCHANGING = 0x0400
    _Z_ORDER_FLAGS = (
        SWP_NOSIZE
        | SWP_NOMOVE
        | SWP_NOACTIVATE
        | SWP_NOOWNERZORDER
        | SWP_NOSENDCHANGING
    )
    _z_order_lock = _WINDOW_STATE_MUTATION_LOCK

    def __init__(
        self,
        *,
        visible_provider: WindowCaptureProvider | None = None,
        paint_settle_seconds: float = 0.05,
        process_lifecycle_provider: (
            Callable[[int], int | None] | None
        ) = None,
    ) -> None:
        self._process_lifecycle_provider = (
            process_lifecycle_provider
            or _default_process_lifecycle_token
        )
        self._visible_provider = (
            visible_provider
            or Win32VisibleRegionCaptureProvider(
                process_lifecycle_provider=(
                    self._process_lifecycle_provider
                )
            )
        )
        self._paint_settle_seconds = max(
            0.0,
            float(paint_settle_seconds),
        )

    @staticmethod
    def _libraries():
        return ctypes.windll.user32, ctypes.windll.gdi32

    @staticmethod
    def _configure(user32) -> None:
        user32.IsWindow.argtypes = (wintypes.HWND,)
        user32.IsWindow.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsIconic.argtypes = (wintypes.HWND,)
        user32.IsIconic.restype = wintypes.BOOL
        user32.GetWindowRect.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        )
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
        user32.GetWindow.restype = wintypes.HWND
        user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.GetWindowLongW.restype = wintypes.LONG
        user32.GetForegroundWindow.argtypes = ()
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        )
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetClassNameW.argtypes = (
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        )
        user32.GetClassNameW.restype = ctypes.c_int
        user32.SetWindowPos.argtypes = (
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        )
        user32.SetWindowPos.restype = wintypes.BOOL
        set_foreground = getattr(user32, "SetForegroundWindow", None)
        if set_foreground is not None:
            set_foreground.argtypes = (wintypes.HWND,)
            set_foreground.restype = wintypes.BOOL

    @staticmethod
    def _handle_value(handle) -> int:
        if isinstance(handle, int):
            return int(handle)
        value = int(getattr(handle, "value", 0) or 0)
        if value == int(ctypes.c_void_p(-1).value):
            return -1
        return value

    @classmethod
    def _window_pid(cls, user32, hwnd) -> int | None:
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(
            hwnd,
            ctypes.byref(process_id),
        )
        value = int(process_id.value)
        return value if value > 0 else None

    @classmethod
    def _window_instance_credential(
        cls,
        user32,
        hwnd,
        lifecycle_provider: Callable[[int], int | None],
    ) -> _WindowInstanceCredential | None:
        return _query_window_instance_credential(
            user32,
            hwnd,
            lifecycle_provider,
        )

    @classmethod
    def _same_window_instance(
        cls,
        user32,
        hwnd,
        expected: _WindowInstanceCredential,
        lifecycle_provider: Callable[[int], int | None],
    ) -> bool:
        return _same_window_instance(
            user32,
            hwnd,
            expected,
            lifecycle_provider,
        )

    @classmethod
    def _optional_instance_is_current(
        cls,
        user32,
        handle: int,
        expected: _WindowInstanceCredential | None,
        lifecycle_provider: Callable[[int], int | None],
    ) -> bool:
        if not handle:
            return expected is None
        if expected is None or expected.handle != handle:
            return False
        return cls._same_window_instance(
            user32,
            wintypes.HWND(handle),
            expected,
            lifecycle_provider,
        )

    @classmethod
    def _reference_instances_are_current(
        cls,
        user32,
        *,
        previous_handle: int,
        next_handle: int,
        previous_instance: _WindowInstanceCredential | None,
        next_instance: _WindowInstanceCredential | None,
        lifecycle_provider: Callable[[int], int | None],
    ) -> bool:
        return bool(
            cls._optional_instance_is_current(
                user32,
                previous_handle,
                previous_instance,
                lifecycle_provider,
            )
            and cls._optional_instance_is_current(
                user32,
                next_handle,
                next_instance,
                lifecycle_provider,
            )
        )

    @classmethod
    def _same_process_window(
        cls,
        user32,
        hwnd,
        process_id: int,
    ) -> bool:
        try:
            return bool(
                user32.IsWindow(hwnd)
                and cls._window_pid(user32, hwnd) == process_id
            )
        except OSError:
            return False

    @staticmethod
    def _window_rect(user32, hwnd) -> tuple[int, int, int, int] | None:
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return (
            int(rect.left),
            int(rect.top),
            int(rect.right),
            int(rect.bottom),
        )

    @classmethod
    def _is_topmost(cls, user32, hwnd) -> bool:
        return bool(
            int(user32.GetWindowLongW(hwnd, cls.GWL_EXSTYLE))
            & cls.WS_EX_TOPMOST
        )

    @classmethod
    def _same_live_normal_window(
        cls,
        user32,
        hwnd,
        *,
        process_id: int,
        rect: tuple[int, int, int, int],
        topmost: bool,
        expected_instance: _WindowInstanceCredential | None = None,
        lifecycle_provider: (
            Callable[[int], int | None] | None
        ) = None,
    ) -> bool:
        if expected_instance is not None and lifecycle_provider is not None:
            instance_is_current = cls._same_window_instance(
                user32,
                hwnd,
                expected_instance,
                lifecycle_provider,
            )
        else:
            instance_is_current = cls._window_pid(user32, hwnd) == process_id
        return bool(
            user32.IsWindow(hwnd)
            and user32.IsWindowVisible(hwnd)
            and not user32.IsIconic(hwnd)
            and instance_is_current
            and cls._window_rect(user32, hwnd) == rect
            and cls._is_topmost(user32, hwnd) is topmost
        )

    @classmethod
    def _existing_handle(cls, user32, handle: int) -> bool:
        return bool(handle and user32.IsWindow(wintypes.HWND(handle)))

    @classmethod
    def _restore_insert_after(
        cls,
        user32,
        hwnd,
        *,
        previous_handle: int,
        next_handle: int,
        was_topmost: bool,
    ) -> tuple[int, int | None]:
        """Return the safest insertion anchor and neighbor to verify."""

        band_top = cls.HWND_TOPMOST if was_topmost else cls.HWND_TOP
        if cls._existing_handle(user32, previous_handle):
            previous = wintypes.HWND(previous_handle)
            if cls._is_topmost(user32, previous) is was_topmost:
                return previous_handle, cls.GW_HWNDPREV
            # The immediate predecessor was in the other Z-order band. The
            # target originally sat at the top of its own band.
            return band_top, cls.GW_HWNDPREV

        if cls._existing_handle(user32, next_handle):
            following = wintypes.HWND(next_handle)
            if cls._is_topmost(user32, following) is was_topmost:
                current_previous = cls._handle_value(
                    user32.GetWindow(following, cls.GW_HWNDPREV)
                )
                if current_previous and current_previous != cls._handle_value(
                    hwnd
                ):
                    predecessor = wintypes.HWND(current_previous)
                    if cls._is_topmost(user32, predecessor) is was_topmost:
                        return current_previous, cls.GW_HWNDNEXT
                return band_top, cls.GW_HWNDNEXT

        return band_top, None

    @classmethod
    def _restore_insert_after_for_instances(
        cls,
        user32,
        hwnd,
        *,
        previous_handle: int,
        next_handle: int,
        previous_instance: _WindowInstanceCredential | None,
        next_instance: _WindowInstanceCredential | None,
        was_topmost: bool,
        lifecycle_provider: Callable[[int], int | None],
    ) -> tuple[
        int,
        int | None,
        _WindowInstanceCredential | None,
    ] | None:
        if not cls._reference_instances_are_current(
            user32,
            previous_handle=previous_handle,
            next_handle=next_handle,
            previous_instance=previous_instance,
            next_instance=next_instance,
            lifecycle_provider=lifecycle_provider,
        ):
            return None
        insert_after, verify_relation = cls._restore_insert_after(
            user32,
            hwnd,
            previous_handle=previous_handle,
            next_handle=next_handle,
            was_topmost=was_topmost,
        )
        anchor_instance = None
        if insert_after not in (
            cls.HWND_TOP,
            cls.HWND_TOPMOST,
            cls.HWND_NOTOPMOST,
        ):
            if insert_after == previous_handle:
                anchor_instance = previous_instance
            elif insert_after == next_handle:
                anchor_instance = next_instance
            else:
                anchor_instance = cls._window_instance_credential(
                    user32,
                    wintypes.HWND(insert_after),
                    lifecycle_provider,
                )
            if anchor_instance is None:
                return None
        return insert_after, verify_relation, anchor_instance

    @classmethod
    def _set_window_position_if_instances_current(
        cls,
        user32,
        hwnd,
        *,
        insert_after: int,
        target_instance: _WindowInstanceCredential,
        previous_handle: int,
        next_handle: int,
        previous_instance: _WindowInstanceCredential | None,
        next_instance: _WindowInstanceCredential | None,
        anchor_instance: _WindowInstanceCredential | None,
        lifecycle_provider: Callable[[int], int | None],
    ) -> bool:
        """Change Z-order only while target and every reference are current."""

        def all_instances_are_current() -> bool:
            if not cls._same_window_instance(
                user32,
                hwnd,
                target_instance,
                lifecycle_provider,
            ):
                return False
            if not cls._reference_instances_are_current(
                user32,
                previous_handle=previous_handle,
                next_handle=next_handle,
                previous_instance=previous_instance,
                next_instance=next_instance,
                lifecycle_provider=lifecycle_provider,
            ):
                return False
            if anchor_instance is None:
                return insert_after in (
                    cls.HWND_TOP,
                    cls.HWND_TOPMOST,
                    cls.HWND_NOTOPMOST,
                )
            return cls._same_window_instance(
                user32,
                wintypes.HWND(insert_after),
                anchor_instance,
                lifecycle_provider,
            )

        # The repeated fence catches a replacement that happens while the first
        # complete target/reference pass is being read. The final check remains
        # inside this helper immediately adjacent to the mutating Win32 call.
        if not all_instances_are_current() or not all_instances_are_current():
            return False
        return bool(
            user32.SetWindowPos(
                hwnd,
                wintypes.HWND(insert_after),
                0,
                0,
                0,
                0,
                cls._Z_ORDER_FLAGS,
            )
        )

    @classmethod
    def _z_order_was_restored(
        cls,
        user32,
        hwnd,
        *,
        previous_handle: int,
        next_handle: int,
        verify_relation: int | None,
        previous_instance: _WindowInstanceCredential | None = None,
        next_instance: _WindowInstanceCredential | None = None,
        lifecycle_provider: (
            Callable[[int], int | None] | None
        ) = None,
    ) -> bool:
        del verify_relation
        try:
            if lifecycle_provider is not None and not (
                cls._reference_instances_are_current(
                    user32,
                    previous_handle=previous_handle,
                    next_handle=next_handle,
                    previous_instance=previous_instance,
                    next_instance=next_instance,
                    lifecycle_provider=lifecycle_provider,
                )
            ):
                return False
            if cls._existing_handle(user32, previous_handle):
                if cls._handle_value(
                    user32.GetWindow(hwnd, cls.GW_HWNDPREV)
                ) != previous_handle:
                    return False
            if cls._existing_handle(user32, next_handle):
                if cls._handle_value(
                    user32.GetWindow(hwnd, cls.GW_HWNDNEXT)
                ) != next_handle:
                    return False
            return True
        except OSError:
            return False

    @classmethod
    def _foreground_was_preserved(
        cls,
        user32,
        hwnd,
        original_foreground: int,
        *,
        original_instance: _WindowInstanceCredential | None = None,
        lifecycle_provider: (
            Callable[[int], int | None] | None
        ) = None,
    ) -> bool:
        current = cls._handle_value(user32.GetForegroundWindow())
        if current == original_foreground:
            if not original_foreground:
                return original_instance is None
            return bool(
                lifecycle_provider is not None
                and original_instance is not None
                and cls._same_window_instance(
                    user32,
                    wintypes.HWND(original_foreground),
                    original_instance,
                    lifecycle_provider,
                )
            )
        target = cls._handle_value(hwnd)
        if (
            current == target
            and original_foreground
            and lifecycle_provider is not None
            and original_instance is not None
            and cls._same_window_instance(
                user32,
                wintypes.HWND(original_foreground),
                original_instance,
                lifecycle_provider,
            )
        ):
            set_foreground = getattr(user32, "SetForegroundWindow", None)
            if set_foreground is not None:
                try:
                    set_foreground(wintypes.HWND(original_foreground))
                except OSError:
                    return False
                return (
                    cls._handle_value(user32.GetForegroundWindow())
                    == original_foreground
                    and cls._same_window_instance(
                        user32,
                        wintypes.HWND(original_foreground),
                        original_instance,
                        lifecycle_provider,
                    )
                )
        # A different foreground window indicates concurrent user activity.
        # Never override it merely to recreate an old snapshot.
        return False

    def capture(self, window_handle: int) -> CaptureSample | None:
        if os.name != "nt" or not window_handle:
            return None

        user32, _gdi32 = self._libraries()
        self._configure(user32)
        hwnd = wintypes.HWND(window_handle)

        with self._z_order_lock:
            if (
                not user32.IsWindow(hwnd)
                or not user32.IsWindowVisible(hwnd)
                or user32.IsIconic(hwnd)
            ):
                return None
            target_instance = self._window_instance_credential(
                user32,
                hwnd,
                self._process_lifecycle_provider,
            )
            original_rect = self._window_rect(user32, hwnd)
            if target_instance is None or original_rect is None:
                return None
            process_id = target_instance.process_id
            was_topmost = self._is_topmost(user32, hwnd)
            original_foreground = self._handle_value(
                user32.GetForegroundWindow()
            )
            previous_handle = self._handle_value(
                user32.GetWindow(hwnd, self.GW_HWNDPREV)
            )
            next_handle = self._handle_value(
                user32.GetWindow(hwnd, self.GW_HWNDNEXT)
            )
            original_foreground_instance = (
                self._window_instance_credential(
                    user32,
                    wintypes.HWND(original_foreground),
                    self._process_lifecycle_provider,
                )
                if original_foreground
                else None
            )
            previous_instance = (
                self._window_instance_credential(
                    user32,
                    wintypes.HWND(previous_handle),
                    self._process_lifecycle_provider,
                )
                if previous_handle
                else None
            )
            next_instance = (
                self._window_instance_credential(
                    user32,
                    wintypes.HWND(next_handle),
                    self._process_lifecycle_provider,
                )
                if next_handle
                else None
            )
            if (
                (original_foreground and original_foreground_instance is None)
                or (previous_handle and previous_instance is None)
                or (next_handle and next_instance is None)
            ):
                return None
            raised = False
            restored = False
            sample: CaptureSample | None = None
            try:
                try:
                    if not self._same_window_instance(
                        user32,
                        hwnd,
                        target_instance,
                        self._process_lifecycle_provider,
                    ):
                        return None
                    raised = (
                        self._set_window_position_if_instances_current(
                            user32,
                            hwnd,
                            insert_after=self.HWND_TOPMOST,
                            target_instance=target_instance,
                            previous_handle=previous_handle,
                            next_handle=next_handle,
                            previous_instance=previous_instance,
                            next_instance=next_instance,
                            anchor_instance=None,
                            lifecycle_provider=(
                                self._process_lifecycle_provider
                            ),
                        )
                    )
                except OSError:
                    raised = False
                if not raised:
                    return None
                current_foreground = self._handle_value(
                    user32.GetForegroundWindow()
                )
                foreground_preserved = self._foreground_was_preserved(
                    user32,
                    hwnd,
                    original_foreground,
                    original_instance=original_foreground_instance,
                    lifecycle_provider=self._process_lifecycle_provider,
                )
                if (
                    current_foreground != original_foreground
                    or not foreground_preserved
                ):
                    return None
                if self._paint_settle_seconds:
                    time.sleep(self._paint_settle_seconds)
                if not self._same_live_normal_window(
                    user32,
                    hwnd,
                    process_id=process_id,
                    rect=original_rect,
                    topmost=True,
                    expected_instance=target_instance,
                    lifecycle_provider=self._process_lifecycle_provider,
                ):
                    return None
                current_foreground = self._handle_value(
                    user32.GetForegroundWindow()
                )
                foreground_preserved = self._foreground_was_preserved(
                    user32,
                    hwnd,
                    original_foreground,
                    original_instance=original_foreground_instance,
                    lifecycle_provider=self._process_lifecycle_provider,
                )
                if (
                    current_foreground != original_foreground
                    or not foreground_preserved
                ):
                    return None
                try:
                    sample = self._visible_provider.capture(window_handle)
                except OSError:
                    sample = None
                current_foreground = self._handle_value(
                    user32.GetForegroundWindow()
                )
                foreground_preserved = self._foreground_was_preserved(
                    user32,
                    hwnd,
                    original_foreground,
                    original_instance=original_foreground_instance,
                    lifecycle_provider=self._process_lifecycle_provider,
                )
                if (
                    sample is None
                    or not sample.api_succeeded
                    or not self._same_live_normal_window(
                        user32,
                        hwnd,
                        process_id=process_id,
                        rect=original_rect,
                        topmost=True,
                        expected_instance=target_instance,
                        lifecycle_provider=self._process_lifecycle_provider,
                    )
                    or current_foreground != original_foreground
                    or not foreground_preserved
                ):
                    sample = None
            finally:
                target_is_current = self._same_window_instance(
                    user32,
                    hwnd,
                    target_instance,
                    self._process_lifecycle_provider,
                )
                if raised and target_is_current:
                    demoted = False
                    try:
                        demoted = (
                            self._set_window_position_if_instances_current(
                                user32,
                                hwnd,
                                insert_after=self.HWND_NOTOPMOST,
                                target_instance=target_instance,
                                previous_handle=previous_handle,
                                next_handle=next_handle,
                                previous_instance=previous_instance,
                                next_instance=next_instance,
                                anchor_instance=None,
                                lifecycle_provider=(
                                    self._process_lifecycle_provider
                                ),
                            )
                        )
                    except OSError:
                        demoted = False
                    verify_relation = None
                    try:
                        restore_plan = (
                            self._restore_insert_after_for_instances(
                                user32,
                                hwnd,
                                previous_handle=previous_handle,
                                next_handle=next_handle,
                                previous_instance=previous_instance,
                                next_instance=next_instance,
                                was_topmost=was_topmost,
                                lifecycle_provider=(
                                    self._process_lifecycle_provider
                                ),
                            )
                        )
                        if restore_plan is None:
                            placed_at_original_level = False
                        else:
                            (
                                insert_after,
                                verify_relation,
                                anchor_instance,
                            ) = restore_plan
                            placed_at_original_level = (
                                self._set_window_position_if_instances_current(
                                    user32,
                                    hwnd,
                                    insert_after=insert_after,
                                    target_instance=target_instance,
                                    previous_handle=previous_handle,
                                    next_handle=next_handle,
                                    previous_instance=previous_instance,
                                    next_instance=next_instance,
                                    anchor_instance=anchor_instance,
                                    lifecycle_provider=(
                                        self._process_lifecycle_provider
                                    ),
                                )
                            )
                    except OSError:
                        placed_at_original_level = False
                    if (
                        not placed_at_original_level
                        and was_topmost is demoted
                    ):
                        # An original neighbour can disappear or have its HWND
                        # reused after the temporary raise.  Exact adjacency is
                        # then no longer safe to reconstruct, but leaving a
                        # normal target permanently TOPMOST is also unsafe.
                        # Restore only the verified target's original Z-order
                        # band through a special Win32 anchor; do not reference
                        # either stale neighbour.  The sample still fails below
                        # because the exact original relation was not proven.
                        try:
                            self._set_window_position_if_instances_current(
                                user32,
                                hwnd,
                                insert_after=(
                                    self.HWND_TOPMOST
                                    if was_topmost
                                    else self.HWND_NOTOPMOST
                                ),
                                target_instance=target_instance,
                                previous_handle=0,
                                next_handle=0,
                                previous_instance=None,
                                next_instance=None,
                                anchor_instance=None,
                                lifecycle_provider=(
                                    self._process_lifecycle_provider
                                ),
                            )
                        except OSError:
                            pass
                    restored = bool(
                        demoted
                        and placed_at_original_level
                        and self._same_live_normal_window(
                            user32,
                            hwnd,
                            process_id=process_id,
                            rect=original_rect,
                            topmost=was_topmost,
                            expected_instance=target_instance,
                            lifecycle_provider=(
                                self._process_lifecycle_provider
                            ),
                        )
                        and self._z_order_was_restored(
                            user32,
                            hwnd,
                            previous_handle=previous_handle,
                            next_handle=next_handle,
                            verify_relation=verify_relation,
                            previous_instance=previous_instance,
                            next_instance=next_instance,
                            lifecycle_provider=(
                                self._process_lifecycle_provider
                            ),
                        )
                    )
                    foreground_preserved = self._foreground_was_preserved(
                        user32,
                        hwnd,
                        original_foreground,
                        original_instance=original_foreground_instance,
                        lifecycle_provider=self._process_lifecycle_provider,
                    )
                    restored = bool(restored and foreground_preserved)
            return sample if restored else None


class WindowsBackgroundCaptureBackend:
    """Conservative background capability backend.

    Input probes intentionally remain undetermined. They require a user-approved,
    game-specific harmless action and are not performed by this read-only backend.
    """

    def __init__(self, provider: WindowCaptureProvider | None = None):
        self._provider = provider or Win32PrintWindowProvider()
        self.last_sample: CaptureSample | None = None

    @staticmethod
    def _looks_non_blank(sample: CaptureSample) -> bool:
        if sample.width < 2 or sample.height < 2 or len(sample.pixels) < 16:
            return False

        pixels = sample.pixels
        # Sample the buffer instead of constructing a large set for full-HD windows.
        stride = max(4, (len(pixels) // 512) // 4 * 4)
        sampled = pixels[0::stride]
        if not sampled:
            return False

        minimum = min(sampled)
        maximum = max(sampled)
        return maximum - minimum >= 8

    def probe_background_capture(self, window_handle: int) -> bool | None:
        self.last_sample = self._provider.capture(window_handle)
        if self.last_sample is None:
            return None
        if not self.last_sample.api_succeeded:
            return False
        return self._looks_non_blank(self.last_sample)

    def probe_background_input(self, window_handle: int) -> bool | None:
        return None

    def probe_minimized_input(self, window_handle: int) -> bool | None:
        return None
