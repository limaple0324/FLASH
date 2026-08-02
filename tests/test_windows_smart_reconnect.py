import ctypes
import json
import threading
import time
from dataclasses import dataclass, replace
from ctypes import wintypes
from pathlib import Path

import pytest

from adapters.game_screen_recognizer import (
    CHARACTER_ENTER_CLICK_POINT,
    CharacterSelectionCandidate,
    ScreenRecognition,
)
from adapters.windows_background_capture import (
    CaptureSample,
    Win32PrintWindowProvider,
    Win32RecoveringPrintWindowProvider,
    Win32TemporarilyRevealedCaptureProvider,
)
from adapters.windows_battle_restart import BattleRestartResult
from adapters.windows_smart_reconnect import (
    MouseClickResult,
    ReconnectRuntimeStateStore,
    RegisteredReconnectRole,
    Win32MouseMessageBackend,
    WindowInstanceToken,
    WindowsSmartReconnectController,
)
from adapters.windows_window import WindowInfo
from core.reconnect_policy import ReconnectScreenState
from core.sp1_boundaries import ReconnectState
from domain.character import CharacterImportance
from services.game_operation_gate import GameOperationGate
from services.group_launch_service import GroupLaunchPlan, GroupLaunchTarget
from services.reconnect_failure_status_service import (
    ReconnectFailureStatusService,
)
from services.smart_reconnect_capture_settings_service import (
    SmartReconnectCaptureSettings,
)
from services.target_window_contract_service import ResolvedTargetWindows


def make_window(
    handle,
    *,
    process_id=None,
    fingerprint=None,
    minimized=False,
    thread_id=None,
    process_lifecycle_token=None,
):
    return WindowInfo(
        handle=handle,
        title="Adobe Flash Player 11",
        visible=True,
        minimized=minimized,
        rect=(0, 0, 900, 600),
        process_id=process_id if process_id is not None else handle + 100,
        window_class="ShockwaveFlash",
        launch_fingerprint=(
            fingerprint if fingerprint is not None else f"{handle:064x}"
        ),
        thread_id=(
            thread_id if thread_id is not None else handle + 1000
        ),
        process_lifecycle_token=(
            process_lifecycle_token
            if process_lifecycle_token is not None
            else handle + 10000
        ),
    )


class FakeWindowBackend:
    def __init__(self, windows):
        self.windows = list(windows)

    def list_windows(self):
        return list(self.windows)

    def foreground_handle(self):
        return self.windows[0].handle if self.windows else None

    def top_window_at(self, _x, _y):
        return None


class FullyVisibleWindowBackend(FakeWindowBackend):
    def top_window_at(self, _x, _y):
        return self.windows[0].handle if self.windows else None


class ObscuredWindowBackend(FakeWindowBackend):
    def top_window_at(self, _x, _y):
        return 999999


class FakeCaptureProvider:
    def __init__(self, states):
        self.states = dict(states)
        self.calls = []

    def capture(self, handle):
        self.calls.append(handle)
        marker = self.states.get(handle, 255)
        if marker is None:
            return None
        return CaptureSample(
            width=2,
            height=2,
            pixels=bytes([marker, 0, 0, 255] * 4),
            api_succeeded=True,
        )


class FakeRecognizer:
    def __init__(self, states, points=None, battle_markers=()):
        self.states = dict(states)
        self.points = dict(points or {})
        self.battle_markers = set(battle_markers)

    def recognize_capture(self, sample):
        marker = sample.pixels[0] if sample is not None else 255
        state = self.states.get(marker, ReconnectScreenState.UNKNOWN)
        return ScreenRecognition(
            state=state,
            score=0.0 if state is not ReconnectScreenState.UNKNOWN else None,
            click_point=self.points.get(marker),
            reference_name=state.value,
            battle_context=marker in self.battle_markers,
        )


class FakeMouseBackend:
    def __init__(
        self,
        *,
        invalid=(),
        unresponsive=(),
        fail=(),
        click_results=(),
    ):
        self.invalid = set(invalid)
        self.unresponsive = set(unresponsive)
        self.fail = set(fail)
        self.clicks = []
        self.expected_process_ids = []
        self.instance_tokens = []
        self.click_results = list(click_results)

    def is_window(self, handle):
        return handle not in self.invalid

    def probe_responsive(self, handle, _timeout_ms):
        return handle not in self.unresponsive

    def click_relative(
        self,
        handle,
        point,
        expected_process_id,
        instance_token,
    ):
        self.clicks.append((handle, point))
        self.expected_process_ids.append(expected_process_id)
        self.instance_tokens.append(instance_token)
        if self.click_results:
            return self.click_results.pop(0)
        if handle in self.fail:
            return MouseClickResult(
                False,
                True,
                False,
                "mouse_click_failed",
            )
        return MouseClickResult(True, True, False, None)


class FakeWin32Function:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


def win32_handle_value(handle):
    if isinstance(handle, int):
        return handle
    value = int(getattr(handle, "value", 0) or 0)
    for signed in (-1, -2):
        if value == int(ctypes.c_void_p(signed).value):
            return signed
    return value


class FakeWin32MouseApi:
    def __init__(
        self,
        *,
        minimized=False,
        message_results=None,
        restore_succeeds=True,
        minimize_succeeds=True,
        z_restore_succeeds=True,
    ):
        self.target = 123
        self.expected_process_id = 456
        self.process_ids = {
            700: 70,
            300: 30,
            self.target: self.expected_process_id,
            400: 40,
            888: 88,
        }
        self.thread_ids = {
            handle: process_id + 1000
            for handle, process_id in self.process_ids.items()
        }
        self.window_classes = {
            handle: "ShockwaveFlash"
            for handle in self.process_ids
        }
        self.process_lifecycle_token = 987654321
        self.visible = {handle: True for handle in self.process_ids}
        self.minimized = {
            handle: False for handle in self.process_ids
        }
        self.minimized[self.target] = minimized
        self.rects = {
            handle: (10, 20, 910, 620)
            for handle in self.process_ids
        }
        self.normal_rect = (10, 20, 910, 620)
        self.client_rect = (0, 0, 900, 600)
        self.foreground = 700
        self.z_order = [700, 300, self.target, 400]
        self.topmost = set()
        self.restore_succeeds = restore_succeeds
        self.minimize_succeeds = minimize_succeeds
        self.z_restore_succeeds = z_restore_succeeds
        self.message_results = {
            message: list(results)
            for message, results in (message_results or {}).items()
        }
        self.show_calls = []
        self.position_calls = []
        self.foreground_calls = []
        self.message_calls = []
        self.processed_messages = []
        self.after_message = None
        self.after_restore = None
        self.after_client_rect = None
        self.after_next_neighbor = None
        self.IsWindow = FakeWin32Function(
            lambda handle: win32_handle_value(handle)
            in self.process_ids
        )
        self.IsWindowVisible = FakeWin32Function(
            lambda handle: self.visible.get(
                win32_handle_value(handle),
                False,
            )
        )
        self.IsIconic = FakeWin32Function(
            lambda handle: self.minimized.get(
                win32_handle_value(handle),
                False,
            )
        )
        self.GetWindowRect = FakeWin32Function(self._get_window_rect)
        self.GetWindow = FakeWin32Function(self._get_window)
        self.GetWindowLongW = FakeWin32Function(
            lambda handle, _index: (
                Win32TemporarilyRevealedCaptureProvider.WS_EX_TOPMOST
                if win32_handle_value(handle) in self.topmost
                else 0
            )
        )
        self.GetForegroundWindow = FakeWin32Function(
            lambda: self.foreground
        )
        self.GetWindowThreadProcessId = FakeWin32Function(
            self._get_window_thread_process_id
        )
        self.GetClassNameW = FakeWin32Function(self._get_class_name)
        self.GetWindowPlacement = FakeWin32Function(
            self._get_window_placement
        )
        self.GetClientRect = FakeWin32Function(self._get_client_rect)
        self.SetWindowPos = FakeWin32Function(self._set_window_pos)
        self.SetForegroundWindow = FakeWin32Function(
            self._set_foreground_window
        )
        self.ShowWindow = FakeWin32Function(self._show_window)
        self.SendMessageTimeoutW = FakeWin32Function(
            self._send_message_timeout
        )

    def _get_window_rect(self, handle, pointer):
        rect = self.rects.get(win32_handle_value(handle))
        if rect is None:
            return False
        target = pointer._obj
        target.left, target.top, target.right, target.bottom = rect
        return True

    def _get_client_rect(self, handle, pointer):
        if win32_handle_value(handle) not in self.process_ids:
            return False
        target = pointer._obj
        target.left, target.top, target.right, target.bottom = (
            self.client_rect
        )
        if self.after_client_rect is not None:
            callback = self.after_client_rect
            self.after_client_rect = None
            callback(self)
        return True

    def _get_window(self, handle, command):
        value = win32_handle_value(handle)
        if value not in self.z_order:
            return 0
        index = self.z_order.index(value)
        if (
            command
            == Win32TemporarilyRevealedCaptureProvider.GW_HWNDPREV
        ):
            return self.z_order[index - 1] if index > 0 else 0
        if command == Win32TemporarilyRevealedCaptureProvider.GW_HWNDNEXT:
            result = (
                self.z_order[index + 1]
                if index + 1 < len(self.z_order)
                else 0
            )
            if self.after_next_neighbor is not None:
                callback = self.after_next_neighbor
                self.after_next_neighbor = None
                callback(self)
            return result
        return 0

    def _get_window_thread_process_id(self, handle, process_pointer):
        value = win32_handle_value(handle)
        process_id = self.process_ids.get(value, 0)
        process_pointer._obj.value = process_id
        return self.thread_ids.get(value, 0) if process_id else 0

    def _get_class_name(self, handle, buffer, capacity):
        value = self.window_classes.get(win32_handle_value(handle), "")
        if not value or capacity <= len(value):
            return 0
        buffer.value = value
        return len(value)

    def instance_token(self):
        return WindowInstanceToken(
            self.target,
            self.expected_process_id,
            self.thread_ids[self.target],
            self.window_classes[self.target],
            self.rects[self.target],
            self.minimized[self.target],
            self.process_lifecycle_token,
        )

    def _get_window_placement(self, handle, pointer):
        if win32_handle_value(handle) not in self.process_ids:
            return False
        placement = pointer._obj
        placement.flags = 0
        placement.showCmd = (
            2
            if self.minimized.get(
                win32_handle_value(handle),
                False,
            )
            else 1
        )
        placement.ptMinPosition.x = -1
        placement.ptMinPosition.y = -1
        placement.ptMaxPosition.x = -1
        placement.ptMaxPosition.y = -1
        (
            placement.rcNormalPosition.left,
            placement.rcNormalPosition.top,
            placement.rcNormalPosition.right,
            placement.rcNormalPosition.bottom,
        ) = self.normal_rect
        return True

    def _show_window(self, handle, command):
        value = win32_handle_value(handle)
        self.show_calls.append((value, command))
        if command == Win32MouseMessageBackend.SW_SHOWNOACTIVATE:
            if self.restore_succeeds:
                self.minimized[value] = False
                self.z_order.remove(value)
                self.z_order.insert(0, value)
                if self.after_restore is not None:
                    callback = self.after_restore
                    self.after_restore = None
                    callback(self)
        elif command == Win32MouseMessageBackend.SW_SHOWMINNOACTIVE:
            if self.minimize_succeeds:
                self.minimized[value] = True
                self.z_order.remove(value)
                self.z_order.append(value)
        return True

    def _set_window_pos(
        self,
        handle,
        insert_after,
        x,
        y,
        width,
        height,
        flags,
    ):
        value = win32_handle_value(handle)
        anchor = win32_handle_value(insert_after)
        self.position_calls.append(
            (value, anchor, x, y, width, height, flags)
        )
        if not self.z_restore_succeeds or value not in self.z_order:
            return False
        self.z_order.remove(value)
        if anchor in self.z_order:
            self.z_order.insert(self.z_order.index(anchor) + 1, value)
        elif anchor == Win32TemporarilyRevealedCaptureProvider.HWND_TOP:
            normal_index = next(
                (
                    index
                    for index, existing in enumerate(self.z_order)
                    if existing not in self.topmost
                ),
                len(self.z_order),
            )
            self.z_order.insert(normal_index, value)
            self.topmost.discard(value)
        elif (
            anchor
            == Win32TemporarilyRevealedCaptureProvider.HWND_TOPMOST
        ):
            self.z_order.insert(0, value)
            self.topmost.add(value)
        else:
            return False
        return True

    def _set_foreground_window(self, handle):
        value = win32_handle_value(handle)
        self.foreground_calls.append(value)
        if value not in self.process_ids:
            return False
        self.foreground = value
        return True

    def _send_message_timeout(
        self,
        handle,
        message,
        wparam,
        lparam,
        flags,
        timeout_ms,
        result_pointer,
    ):
        value = win32_handle_value(handle)
        self.message_calls.append(
            (value, message, wparam, lparam, flags, timeout_ms)
        )
        outcomes = self.message_results.get(message)
        succeeded = outcomes.pop(0) if outcomes else True
        if succeeded:
            self.processed_messages.append(message)
            result_pointer._obj.value = 1
        if self.after_message is not None:
            self.after_message(self, message)
        return succeeded


class FakeWin32KernelApi:
    def __init__(self, user32):
        self.user32 = user32
        self.process_handle = 9001
        self.process_alive = True
        self.open_calls = []
        self.close_calls = []
        self.OpenProcess = FakeWin32Function(self._open_process)
        self.GetProcessTimes = FakeWin32Function(
            self._get_process_times
        )
        self.WaitForSingleObject = FakeWin32Function(
            self._wait_for_single_object
        )
        self.CloseHandle = FakeWin32Function(self._close_handle)

    def _open_process(self, access, inherit, process_id):
        self.open_calls.append((access, inherit, process_id))
        if process_id != self.user32.expected_process_id:
            return 0
        return self.process_handle

    def _get_process_times(
        self,
        process_handle,
        created_pointer,
        exited_pointer,
        kernel_pointer,
        user_pointer,
    ):
        if process_handle != self.process_handle:
            return False
        token = self.user32.process_lifecycle_token
        created_pointer._obj.dwLowDateTime = token & 0xFFFFFFFF
        created_pointer._obj.dwHighDateTime = token >> 32
        exited_pointer._obj.dwLowDateTime = 0
        exited_pointer._obj.dwHighDateTime = 0
        kernel_pointer._obj.dwLowDateTime = 0
        kernel_pointer._obj.dwHighDateTime = 0
        user_pointer._obj.dwLowDateTime = 0
        user_pointer._obj.dwHighDateTime = 0
        return True

    def _wait_for_single_object(self, process_handle, _timeout):
        if (
            process_handle == self.process_handle
            and self.process_alive
        ):
            return Win32MouseMessageBackend.WAIT_TIMEOUT
        return 0

    def _close_handle(self, process_handle):
        self.close_calls.append(process_handle)
        return True


def win32_mouse_backend(api, monkeypatch):
    backend = Win32MouseMessageBackend()
    kernel32 = FakeWin32KernelApi(api)
    monkeypatch.setattr(backend, "_user32", lambda: api)
    monkeypatch.setattr(backend, "_kernel32", lambda: kernel32)
    monkeypatch.setattr(
        backend,
        "MINIMIZED_PAINT_SETTLE_SECONDS",
        0,
    )
    backend.fake_kernel32 = kernel32
    return backend


def message_numbers(api):
    return [call[1] for call in api.message_calls]


def test_win32_mouse_uses_confirmed_synchronous_messages_for_normal_window(
    monkeypatch,
):
    api = FakeWin32MouseApi(minimized=False)
    backend = win32_mouse_backend(api, monkeypatch)

    result = backend.click_relative(
        api.target,
        (0.5, 0.5),
        api.expected_process_id,
        api.instance_token(),
    )
    assert result == MouseClickResult(True, True, False, None)

    assert message_numbers(api) == [
        backend.WM_MOUSEMOVE,
        backend.WM_LBUTTONDOWN,
        backend.WM_LBUTTONUP,
    ]
    assert api.processed_messages == message_numbers(api)
    assert api.show_calls == []
    assert not hasattr(api, "PostMessageW")
    assert backend.fake_kernel32.close_calls == [
        backend.fake_kernel32.process_handle
    ]
    for _handle, _message, _wparam, _lparam, flags, timeout in (
        api.message_calls
    ):
        assert flags == (
            backend.SMTO_BLOCK
            | backend.SMTO_ABORTIFHUNG
            | backend.SMTO_ERRORONEXIT
        )
        assert timeout == backend.MESSAGE_TIMEOUT_MS


@pytest.mark.parametrize("expected_process_id", [0, -1, 999])
def test_win32_mouse_rejects_missing_or_mismatched_process_identity(
    monkeypatch,
    expected_process_id,
):
    api = FakeWin32MouseApi(minimized=False)
    backend = win32_mouse_backend(api, monkeypatch)

    result = backend.click_relative(
        api.target,
        (0.5, 0.5),
        expected_process_id,
        api.instance_token(),
    )
    assert result.delivered is False
    assert result.delivery_uncertain is False
    assert api.show_calls == []
    assert api.message_calls == []


def test_win32_mouse_minimized_click_restores_full_window_state(
    monkeypatch,
):
    api = FakeWin32MouseApi(minimized=True)
    backend = win32_mouse_backend(api, monkeypatch)
    original_order = list(api.z_order)
    original_rect = api.rects[api.target]
    original_normal_rect = api.normal_rect

    result = backend.click_relative(
        api.target,
        (0.25, 0.75),
        api.expected_process_id,
        api.instance_token(),
    )
    assert result == MouseClickResult(True, True, False, None)

    assert api.minimized[api.target] is True
    assert api.foreground == 700
    assert api.foreground_calls == []
    assert api.rects[api.target] == original_rect
    assert api.normal_rect == original_normal_rect
    assert api.z_order == original_order
    assert api.show_calls == [
        (api.target, backend.SW_SHOWNOACTIVATE),
        (api.target, backend.SW_SHOWMINNOACTIVE),
    ]
    assert len(api.position_calls) == 1
    assert api.position_calls[0][1] == 300
    assert (
        backend._window_state_lock
        is Win32RecoveringPrintWindowProvider._window_state_lock
    )


def test_win32_mouse_preserves_minimized_topmost_band(monkeypatch):
    api = FakeWin32MouseApi(minimized=True)
    api.z_order = [300, api.target, 700, 400]
    api.topmost = {300, api.target}
    backend = win32_mouse_backend(api, monkeypatch)
    original_order = list(api.z_order)

    result = backend.click_relative(
        api.target,
        (0.5, 0.5),
        api.expected_process_id,
        api.instance_token(),
    )
    assert result == MouseClickResult(True, True, False, None)

    assert api.minimized[api.target] is True
    assert api.target in api.topmost
    assert api.z_order == original_order


def test_win32_mouse_rejects_pid_reuse_before_minimized_restore(
    monkeypatch,
):
    api = FakeWin32MouseApi(minimized=True)
    api.after_next_neighbor = lambda current: current.process_ids.__setitem__(
        current.target,
        999,
    )
    backend = win32_mouse_backend(api, monkeypatch)

    result = backend.click_relative(
        api.target,
        (0.5, 0.5),
        api.expected_process_id,
        api.instance_token(),
    )
    assert result.delivered is False
    assert result.delivery_uncertain is False

    assert api.show_calls == []
    assert api.message_calls == []
    assert api.process_ids[api.target] == 999


def test_win32_mouse_rejects_pid_reuse_during_restore_wait(
    monkeypatch,
):
    api = FakeWin32MouseApi(minimized=True)
    backend = win32_mouse_backend(api, monkeypatch)
    monkeypatch.setattr(
        "adapters.windows_smart_reconnect.time.sleep",
        lambda _seconds: api.process_ids.__setitem__(api.target, 999),
    )
    monkeypatch.setattr(
        backend,
        "MINIMIZED_PAINT_SETTLE_SECONDS",
        0.01,
    )

    result = backend.click_relative(
        api.target,
        (0.5, 0.5),
        api.expected_process_id,
        api.instance_token(),
    )
    assert result.delivered is False
    assert result.restored is False
    assert result.delivery_uncertain is False

    assert api.show_calls == [
        (api.target, backend.SW_SHOWNOACTIVATE),
    ]
    assert api.message_calls == []
    assert api.position_calls == []


def test_win32_mouse_rejects_failed_temporary_restore(monkeypatch):
    api = FakeWin32MouseApi(
        minimized=True,
        restore_succeeds=False,
    )
    backend = win32_mouse_backend(api, monkeypatch)

    result = backend.click_relative(
        api.target,
        (0.5, 0.5),
        api.expected_process_id,
        api.instance_token(),
    )
    assert result.delivered is False
    assert result.restored is True
    assert result.delivery_uncertain is False

    assert api.minimized[api.target] is True
    assert api.show_calls == [
        (api.target, backend.SW_SHOWNOACTIVATE),
    ]
    assert api.message_calls == []
    assert api.position_calls == []


@pytest.mark.parametrize(
    ("race_message", "expected_messages"),
    [
        (
            Win32MouseMessageBackend.WM_MOUSEMOVE,
            [Win32MouseMessageBackend.WM_MOUSEMOVE],
        ),
        (
            Win32MouseMessageBackend.WM_LBUTTONDOWN,
            [
                Win32MouseMessageBackend.WM_MOUSEMOVE,
                Win32MouseMessageBackend.WM_LBUTTONDOWN,
            ],
        ),
        (
            Win32MouseMessageBackend.WM_LBUTTONUP,
            [
                Win32MouseMessageBackend.WM_MOUSEMOVE,
                Win32MouseMessageBackend.WM_LBUTTONDOWN,
                Win32MouseMessageBackend.WM_LBUTTONUP,
            ],
        ),
    ],
)
def test_win32_mouse_never_sends_segments_to_reused_handle(
    monkeypatch,
    race_message,
    expected_messages,
):
    api = FakeWin32MouseApi(minimized=False)

    def replace_after_message(current, message):
        if message == race_message:
            current.process_ids[current.target] = 999

    api.after_message = replace_after_message
    backend = win32_mouse_backend(api, monkeypatch)

    result = backend.click_relative(
        api.target,
        (0.5, 0.5),
        api.expected_process_id,
        api.instance_token(),
    )
    assert result.delivered is False
    assert message_numbers(api) == expected_messages
    assert api.process_ids[api.target] == 999


@pytest.mark.parametrize(
    "changed_evidence",
    [
        "thread",
        "class",
        "lifecycle",
        "process_handle",
        "rect",
        "minimized",
    ],
)
def test_win32_mouse_fails_closed_when_instance_evidence_changes(
    monkeypatch,
    changed_evidence,
):
    api = FakeWin32MouseApi(minimized=False)
    backend = win32_mouse_backend(api, monkeypatch)

    def change_after_move(current, message):
        if message != Win32MouseMessageBackend.WM_MOUSEMOVE:
            return
        if changed_evidence == "thread":
            current.thread_ids[current.target] += 1
        elif changed_evidence == "class":
            current.window_classes[current.target] = "ReplacementWindow"
        elif changed_evidence == "lifecycle":
            current.process_lifecycle_token += 1
        elif changed_evidence == "process_handle":
            backend.fake_kernel32.process_alive = False
        elif changed_evidence == "rect":
            current.rects[current.target] = (20, 30, 920, 630)
        else:
            current.minimized[current.target] = True

    api.after_message = change_after_move

    result = backend.click_relative(
        api.target,
        (0.5, 0.5),
        api.expected_process_id,
        api.instance_token(),
    )

    assert result.delivered is False
    assert result.delivery_uncertain is False
    assert message_numbers(api) == [backend.WM_MOUSEMOVE]


def test_win32_mouse_rechecks_pid_after_coordinate_lookup(monkeypatch):
    api = FakeWin32MouseApi(minimized=False)
    api.after_client_rect = lambda current: current.process_ids.__setitem__(
        current.target,
        999,
    )
    backend = win32_mouse_backend(api, monkeypatch)

    result = backend.click_relative(
        api.target,
        (0.5, 0.5),
        api.expected_process_id,
        api.instance_token(),
    )
    assert result.delivered is False
    assert api.message_calls == []


def test_win32_mouse_rechecks_pid_immediately_before_finally_restore(
    monkeypatch,
):
    api = FakeWin32MouseApi(minimized=True)

    def arm_finally_reuse(current, message):
        if message == Win32MouseMessageBackend.WM_LBUTTONUP:
            current.after_next_neighbor = (
                lambda target: target.process_ids.__setitem__(
                    target.target,
                    999,
                )
            )

    api.after_message = arm_finally_reuse
    backend = win32_mouse_backend(api, monkeypatch)

    result = backend.click_relative(
        api.target,
        (0.5, 0.5),
        api.expected_process_id,
        api.instance_token(),
    )
    assert result.delivered is True
    assert result.restored is False
    assert result.failure_code == "input_window_restore_failed"

    assert api.process_ids[api.target] == 999
    assert api.show_calls == [
        (api.target, backend.SW_SHOWNOACTIVATE),
    ]
    assert api.position_calls == []


@pytest.mark.parametrize(
    "race_kind",
    ["foreground", "position", "layer", "manual_restore"],
)
def test_win32_mouse_does_not_overwrite_concurrent_user_state(
    monkeypatch,
    race_kind,
):
    api = FakeWin32MouseApi(minimized=True)
    backend = win32_mouse_backend(api, monkeypatch)

    def user_changes_state(_seconds):
        if race_kind == "foreground":
            api.foreground = 888
        elif race_kind == "position":
            api.rects[api.target] = (30, 40, 930, 640)
            api.normal_rect = api.rects[api.target]
        elif race_kind == "layer":
            api.z_order.remove(api.target)
            api.z_order.append(api.target)
        else:
            api.foreground = api.target

    monkeypatch.setattr(
        "adapters.windows_smart_reconnect.time.sleep",
        user_changes_state,
    )
    monkeypatch.setattr(
        backend,
        "MINIMIZED_PAINT_SETTLE_SECONDS",
        0.01,
    )

    result = backend.click_relative(
        api.target,
        (0.5, 0.5),
        api.expected_process_id,
        api.instance_token(),
    )
    assert result.delivered is False
    assert result.restored is False

    assert api.message_calls == []
    assert api.show_calls == [
        (api.target, backend.SW_SHOWNOACTIVATE),
    ]
    assert api.position_calls == []
    assert api.minimized[api.target] is False
    if race_kind == "foreground":
        assert api.foreground == 888
    elif race_kind == "position":
        assert api.rects[api.target] == (30, 40, 930, 640)
    elif race_kind == "layer":
        assert api.z_order[-1] == api.target
    else:
        assert api.foreground == api.target


@pytest.mark.parametrize(
    (
        "failed_message",
        "expected_messages",
        "expected_uncertain",
        "expected_failure_code",
    ),
    [
        (
            Win32MouseMessageBackend.WM_MOUSEMOVE,
            [Win32MouseMessageBackend.WM_MOUSEMOVE],
            False,
            "mouse_move_delivery_failed",
        ),
        (
            Win32MouseMessageBackend.WM_LBUTTONDOWN,
            [
                Win32MouseMessageBackend.WM_MOUSEMOVE,
                Win32MouseMessageBackend.WM_LBUTTONDOWN,
                Win32MouseMessageBackend.WM_LBUTTONUP,
            ],
            True,
            "mouse_down_delivery_uncertain",
        ),
    ],
)
def test_win32_mouse_timeout_releases_every_attempted_down(
    monkeypatch,
    failed_message,
    expected_messages,
    expected_uncertain,
    expected_failure_code,
):
    api = FakeWin32MouseApi(
        message_results={failed_message: [False]},
    )
    backend = win32_mouse_backend(api, monkeypatch)

    result = backend.click_relative(
        api.target,
        (0.5, 0.5),
        api.expected_process_id,
        api.instance_token(),
    )
    assert result.delivered is False
    assert result.delivery_uncertain is expected_uncertain
    assert result.failure_code == expected_failure_code
    assert message_numbers(api) == expected_messages


def test_win32_mouse_never_releases_to_replacement_after_down_timeout(
    monkeypatch,
):
    api = FakeWin32MouseApi(
        message_results={
            Win32MouseMessageBackend.WM_LBUTTONDOWN: [False],
        },
    )

    def replace_after_down(current, message):
        if message == Win32MouseMessageBackend.WM_LBUTTONDOWN:
            current.thread_ids[current.target] += 1
            current.window_classes[current.target] = "ReplacementWindow"

    api.after_message = replace_after_down
    backend = win32_mouse_backend(api, monkeypatch)

    result = backend.click_relative(
        api.target,
        (0.5, 0.5),
        api.expected_process_id,
        api.instance_token(),
    )

    assert result == MouseClickResult(
        False,
        True,
        True,
        "mouse_down_delivery_uncertain",
    )
    assert message_numbers(api) == [
        backend.WM_MOUSEMOVE,
        backend.WM_LBUTTONDOWN,
    ]


def test_win32_mouse_releases_same_instance_before_reporting_state_race(
    monkeypatch,
):
    api = FakeWin32MouseApi(minimized=False)

    def move_window_after_down(current, message):
        if message == Win32MouseMessageBackend.WM_LBUTTONDOWN:
            current.rects[current.target] = (20, 30, 920, 630)

    api.after_message = move_window_after_down
    backend = win32_mouse_backend(api, monkeypatch)

    result = backend.click_relative(
        api.target,
        (0.5, 0.5),
        api.expected_process_id,
        api.instance_token(),
    )

    assert result == MouseClickResult(
        False,
        True,
        True,
        "input_window_state_changed_during_click",
    )
    assert message_numbers(api) == [
        backend.WM_MOUSEMOVE,
        backend.WM_LBUTTONDOWN,
        backend.WM_LBUTTONUP,
    ]


def test_win32_mouse_retries_up_after_confirmed_down(monkeypatch):
    api = FakeWin32MouseApi(
        message_results={
            Win32MouseMessageBackend.WM_LBUTTONUP: [False, True],
        },
    )
    backend = win32_mouse_backend(api, monkeypatch)

    result = backend.click_relative(
        api.target,
        (0.5, 0.5),
        api.expected_process_id,
        api.instance_token(),
    )
    assert result == MouseClickResult(True, True, False, None)
    assert message_numbers(api) == [
        backend.WM_MOUSEMOVE,
        backend.WM_LBUTTONDOWN,
        backend.WM_LBUTTONUP,
        backend.WM_LBUTTONUP,
    ]


def test_win32_mouse_up_compensation_is_bounded(monkeypatch):
    api = FakeWin32MouseApi(
        message_results={
            Win32MouseMessageBackend.WM_LBUTTONUP: [
                False,
                False,
                False,
            ],
        },
    )
    backend = win32_mouse_backend(api, monkeypatch)

    result = backend.click_relative(
        api.target,
        (0.5, 0.5),
        api.expected_process_id,
        api.instance_token(),
    )
    assert result == MouseClickResult(
        False,
        True,
        True,
        "mouse_up_delivery_uncertain",
    )
    assert message_numbers(api).count(backend.WM_LBUTTONUP) == (
        1 + backend.UP_COMPENSATION_ATTEMPTS
    )


def test_win32_mouse_reports_failure_when_reminimize_fails(monkeypatch):
    api = FakeWin32MouseApi(
        minimized=True,
        minimize_succeeds=False,
    )
    backend = win32_mouse_backend(api, monkeypatch)

    result = backend.click_relative(
        api.target,
        (0.5, 0.5),
        api.expected_process_id,
        api.instance_token(),
    )
    assert result == MouseClickResult(
        True,
        False,
        False,
        "input_window_restore_failed",
    )
    assert api.processed_messages == [
        backend.WM_MOUSEMOVE,
        backend.WM_LBUTTONDOWN,
        backend.WM_LBUTTONUP,
    ]
    assert api.minimized[api.target] is False


def test_win32_mouse_reports_failure_when_z_order_restore_fails(
    monkeypatch,
):
    api = FakeWin32MouseApi(
        minimized=True,
        z_restore_succeeds=False,
    )
    backend = win32_mouse_backend(api, monkeypatch)

    result = backend.click_relative(
        api.target,
        (0.5, 0.5),
        api.expected_process_id,
        api.instance_token(),
    )
    assert result == MouseClickResult(
        True,
        False,
        False,
        "input_window_restore_failed",
    )
    assert api.minimized[api.target] is True
    assert len(api.position_calls) == 1
    assert api.z_order != [700, 300, api.target, 400]


@pytest.mark.parametrize(
    "changed_evidence",
    ["process", "thread", "class", "lifecycle"],
)
def test_win32_mouse_rechecks_instance_after_reminimize_before_z_restore(
    monkeypatch,
    changed_evidence,
):
    api = FakeWin32MouseApi(minimized=True)
    original_show = api._show_window

    def show_then_replace(handle, command):
        result = original_show(handle, command)
        if command == Win32MouseMessageBackend.SW_SHOWMINNOACTIVE:
            if changed_evidence == "process":
                api.process_ids[api.target] = 999
            elif changed_evidence == "thread":
                api.thread_ids[api.target] += 1
            elif changed_evidence == "class":
                api.window_classes[api.target] = "ReplacementWindow"
            else:
                api.process_lifecycle_token += 1
        return result

    api.ShowWindow = FakeWin32Function(show_then_replace)
    backend = win32_mouse_backend(api, monkeypatch)

    result = backend.click_relative(
        api.target,
        (0.5, 0.5),
        api.expected_process_id,
        api.instance_token(),
    )

    assert result == MouseClickResult(
        True,
        False,
        False,
        "input_window_restore_failed",
    )
    assert api.position_calls == []


class FakeBattleRestarter:
    def __init__(self, *, succeeds=True, failure_code="battle_restart_failed"):
        self.succeeds = succeeds
        self.failure_code = failure_code
        self.calls = []
        self.reopen_calls = []

    def restart(self, window, target):
        self.calls.append((window, target))
        return BattleRestartResult(
            self.succeeds,
            None if self.succeeds else self.failure_code,
        )

    def reopen_missing(self, target, candidate_windows):
        self.reopen_calls.append((target, tuple(candidate_windows)))
        return BattleRestartResult(
            self.succeeds,
            None if self.succeeds else self.failure_code,
            shortcut_open_requested=self.succeeds,
        )


@dataclass
class Fixture:
    controller: WindowsSmartReconnectController
    capture: FakeCaptureProvider
    mouse: FakeMouseBackend


def make_controller(
    screen_states,
    *,
    windows=None,
    expected_windows=2,
    points=None,
    mouse=None,
    clock=None,
    state_path=None,
    battle_markers=(),
    battle_restarter=None,
    group_launch_plan=None,
    failure_status_service=None,
    failure_record_callback=None,
    target_windows_provider=None,
    visible_capture_provider=None,
    obscured_capture_provider=None,
    active_refresh_capture_provider=None,
    primary_capture_is_trusted=True,
    operation_gate=None,
    window_backend=None,
    require_expected_window_count=True,
):
    if clock is None:
        default_time = [-5.0]

        def clock():
            default_time[0] += 5.0
            return default_time[0]

    windows = windows or [make_window(1), make_window(2)]
    capture = FakeCaptureProvider(
        {window.handle: marker for window, marker in zip(windows, screen_states)}
    )
    recognizer = FakeRecognizer(
        {
            1: ReconnectScreenState.CONNECTED,
            2: ReconnectScreenState.DISCONNECTED,
            3: ReconnectScreenState.LOGIN_START,
            4: ReconnectScreenState.LINE_SELECTION,
            5: ReconnectScreenState.CHARACTER_SELECTION,
            6: ReconnectScreenState.POST_LOGIN_ACTIVITY,
            7: ReconnectScreenState.POST_LOGIN_RECOMMENDATION,
            8: ReconnectScreenState.RECONNECTING,
            9: ReconnectScreenState.FORCE_LOGIN_TIMEOUT,
            10: ReconnectScreenState.CONNECTED,
            11: ReconnectScreenState.CONNECTED,
            12: ReconnectScreenState.CONNECTED,
            255: ReconnectScreenState.UNKNOWN,
        },
        points
        or {
            2: (0.5, 0.5),
            3: (0.5, 0.8),
            4: (0.5, 0.3),
            5: (0.35, 0.85),
            6: (0.86, 0.12),
            7: (0.81, 0.18),
            9: (0.5, 0.57),
        },
        battle_markers=battle_markers,
    )
    mouse = mouse or FakeMouseBackend()
    controller = WindowsSmartReconnectController(
            expected_windows=expected_windows,
            title_keywords=("Adobe Flash Player",),
            window_backend=window_backend or FakeWindowBackend(windows),
            capture_provider=capture,
            visible_capture_provider=visible_capture_provider,
            obscured_capture_provider=obscured_capture_provider,
            active_refresh_capture_provider=active_refresh_capture_provider,
            primary_capture_is_trusted=primary_capture_is_trusted,
            recognizer=recognizer,
            mouse_backend=mouse,
            monotonic_clock=clock,
            state_path=state_path,
            execution_enabled=True,
            require_expected_window_count=require_expected_window_count,
            battle_restarter=battle_restarter,
            failure_status_service=failure_status_service,
            failure_record_callback=failure_record_callback,
            target_windows_provider=target_windows_provider,
            operation_gate=operation_gate,
        )
    if group_launch_plan is not None:
        controller.set_group_launch_plan(group_launch_plan)
    return Fixture(
        controller=controller,
        capture=capture,
        mouse=mouse,
    )


def test_passive_observation_uses_explicit_ungrouped_candidates():
    grouped = make_window(1, fingerprint="a" * 64)
    ungrouped = make_window(2, fingerprint="b" * 64)
    fixture = make_controller(
        [1],
        windows=[grouped],
        expected_windows=1,
    )
    fixture.capture.states[ungrouped.handle] = 1

    observed = fixture.controller.observe_screen_states(
        (ungrouped.launch_fingerprint,),
        candidate_windows=(ungrouped,),
    )

    assert observed == {
        ungrouped.launch_fingerprint: ReconnectScreenState.CONNECTED,
    }
    assert fixture.mouse.clicks == []


def make_group_plan(tmp_path, windows, group_name="current"):
    return GroupLaunchPlan(
        group_name,
        targets=tuple(
            GroupLaunchTarget(
                index,
                f"{group_name}-{index}",
                tmp_path / f"{group_name}-{index}.lnk",
                window.launch_fingerprint,
            )
            for index, window in enumerate(windows, start=1)
        ),
    )


def complete_with_fresh_connected_frames(
    fixture: Fixture,
    *,
    handle: int = 1,
    now: list[float] | None = None,
) -> None:
    for marker in (10, 11, 12):
        if now is not None:
            now[0] += 2.0
        fixture.capture.states[handle] = marker
        fixture.controller.reconnect()


def test_read_only_check_detects_reconnect_need_without_clicking():
    fixture = make_controller([1, 2])

    first = fixture.controller.check_connection()
    result = fixture.controller.check_connection()

    assert first.code == "reconnect.waiting"
    assert result.success is False
    assert result.code == "reconnect.required"
    assert fixture.controller.state is ReconnectState.DISCONNECTED
    assert fixture.mouse.clicks == []
    assert result.details["connected_windows"] == 1
    assert result.details["actionable_windows"] == 1
    assert fixture.controller.reconnecting_fingerprints() == frozenset()
    assert result.details["captured_pixels_persisted"] is False


def test_selected_group_identity_excludes_other_open_flash_windows():
    windows = [make_window(1), make_window(2), make_window(3)]
    selected = {
        windows[0].launch_fingerprint,
        windows[2].launch_fingerprint,
    }
    fixture = make_controller(
        [2, 1, 4],
        windows=windows,
    )
    fixture.controller.set_expected_windows(2)
    fixture.controller.set_allowed_fingerprints(selected)

    first = fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert first.code == "reconnect.waiting"
    assert result.success is True
    assert result.details["discovered_windows"] == 2
    assert fixture.capture.calls == [1, 3, 1, 3, 1]
    assert fixture.mouse.clicks == [
        (1, (0.5, 0.5)),
    ]


def test_selected_group_missing_identity_does_not_block_confirmed_open_role():
    windows = [make_window(1), make_window(2)]
    fixture = make_controller([2, 1], windows=windows)
    fixture.controller.set_allowed_fingerprints(
        {
            windows[0].launch_fingerprint,
            "f" * 64,
        }
    )

    first = fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert first.code == "reconnect.waiting"
    assert result.success is True
    assert result.code == "reconnect.progressed"
    assert result.details["expected_windows"] == 2
    assert result.details["discovered_windows"] == 1
    assert result.details["all_connected"] is False
    assert result.details["failure_codes"] == []
    assert fixture.capture.calls == [1, 1, 1]
    assert fixture.mouse.clicks == [(1, (0.5, 0.5))]
    assert fixture.mouse.expected_process_ids == [windows[0].process_id]
    assert fixture.mouse.instance_tokens == [
        WindowInstanceToken.from_window(windows[0])
    ]


def test_selected_group_missing_identity_prevents_false_connected_result():
    windows = [make_window(1), make_window(2)]
    fixture = make_controller([1, 2], windows=windows)
    fixture.controller.set_allowed_fingerprints(
        {
            windows[0].launch_fingerprint,
            "f" * 64,
        }
    )

    result = fixture.controller.reconnect()

    assert result.success is False
    assert result.code == "reconnect.waiting"
    assert result.details["all_connected"] is False
    assert result.details["expected_windows"] == 2
    assert result.details["discovered_windows"] == 1
    assert result.details["source_missing_windows"] == 1
    assert fixture.capture.calls == [1]
    assert fixture.mouse.clicks == []


def test_isolated_target_source_failure_prevents_false_connected_result():
    windows = [make_window(1), make_window(2)]
    selected = {
        windows[0].launch_fingerprint,
        windows[1].launch_fingerprint,
    }
    fixture = make_controller(
        [1],
        windows=[windows[0]],
        expected_windows=2,
        target_windows_provider=lambda: ResolvedTargetWindows(
            (windows[0],),
            ("window_identity_duplicate",),
        ),
    )
    fixture.controller.set_allowed_fingerprints(selected)

    result = fixture.controller.reconnect()

    assert result.success is False
    assert result.code == "reconnect.waiting"
    assert result.details["all_connected"] is False
    assert result.details["connected_windows"] == 1
    assert "window_identity_duplicate" in result.details["failure_codes"]
    assert fixture.mouse.clicks == []


def test_offline_target_source_failure_prevents_false_connected_result():
    windows = [make_window(1)]
    fixture = make_controller(
        [1],
        windows=windows,
        expected_windows=2,
        target_windows_provider=lambda: ResolvedTargetWindows(
            tuple(windows),
            ("window_offline",),
        ),
    )
    fixture.controller.set_allowed_fingerprints(
        {
            windows[0].launch_fingerprint,
            "f" * 64,
        }
    )

    result = fixture.controller.reconnect()

    assert result.success is False
    assert result.code == "reconnect.waiting"
    assert result.details["all_connected"] is False
    assert result.details["connected_windows"] == 1
    assert "window_offline" in result.details["failure_codes"]
    assert result.details["next_check_seconds"] == 5
    assert fixture.mouse.clicks == []


@pytest.mark.parametrize(
    ("failure_codes", "block_missing"),
    (
        (("window_offline",), False),
        ((), True),
    ),
)
def test_scoped_source_failure_revokes_only_affected_connected_evidence(
    failure_codes,
    block_missing,
):
    windows = [make_window(1), make_window(2)]
    selected = frozenset(
        window.launch_fingerprint
        for window in windows
    )
    provider_state = {
        "value": ResolvedTargetWindows(tuple(windows)),
    }
    fixture = make_controller(
        [1, 1],
        windows=windows,
        expected_windows=2,
        target_windows_provider=lambda: provider_state["value"],
    )
    fixture.controller.set_allowed_fingerprints(selected)
    connected = fixture.controller.reconnect()
    assert connected.code == "reconnect.connected"
    assert set(fixture.controller._trusted_connected_evidence) == selected

    affected = windows[1].launch_fingerprint
    provider_state["value"] = ResolvedTargetWindows(
        (windows[0],),
        failure_codes,
        frozenset({affected}) if block_missing else frozenset(),
    )

    result = fixture.controller.reconnect()

    assert result.details["all_connected"] is False
    assert fixture.controller.role_screen_states() == {
        windows[0].launch_fingerprint: ReconnectScreenState.CONNECTED,
        affected: ReconnectScreenState.UNKNOWN,
    }
    assert set(fixture.controller._trusted_connected_evidence) == {
        windows[0].launch_fingerprint
    }
    assert fixture.mouse.clicks == []


def test_scoped_source_subset_without_failure_revokes_missing_evidence():
    windows = [make_window(1), make_window(2)]
    selected = frozenset(
        window.launch_fingerprint
        for window in windows
    )
    provider_state = {
        "value": ResolvedTargetWindows(tuple(windows)),
    }
    fixture = make_controller(
        [1, 1],
        windows=windows,
        expected_windows=2,
        target_windows_provider=lambda: provider_state["value"],
    )
    fixture.controller.set_allowed_fingerprints(selected)
    connected = fixture.controller.reconnect()
    assert connected.code == "reconnect.connected"
    assert set(fixture.controller._trusted_connected_evidence) == selected

    missing = windows[1].launch_fingerprint
    provider_state["value"] = ResolvedTargetWindows((windows[0],))

    result = fixture.controller.reconnect()

    assert result.details["all_connected"] is False
    assert result.details["source_missing_windows"] == 1
    assert fixture.controller.role_screen_states() == {
        windows[0].launch_fingerprint: ReconnectScreenState.CONNECTED,
        missing: ReconnectScreenState.UNKNOWN,
    }
    assert set(fixture.controller._trusted_connected_evidence) == {
        windows[0].launch_fingerprint
    }
    assert fixture.mouse.clicks == []


def test_source_subset_final_publish_removes_late_connected_evidence(
    monkeypatch,
):
    windows = [make_window(1), make_window(2)]
    selected = frozenset(
        window.launch_fingerprint
        for window in windows
    )
    provider_state = {
        "value": ResolvedTargetWindows(tuple(windows)),
    }
    fixture = make_controller(
        [1, 1],
        windows=windows,
        expected_windows=2,
        target_windows_provider=lambda: provider_state["value"],
    )
    fixture.controller.set_allowed_fingerprints(selected)
    connected = fixture.controller.reconnect()
    assert connected.code == "reconnect.connected"

    missing = windows[1].launch_fingerprint
    stale_evidence = fixture.controller._trusted_connected_evidence[missing]
    original_revoke = fixture.controller._revoke_source_failure_evidence

    def revoke_then_restore(fingerprints):
        original_revoke(fingerprints)
        if missing in fingerprints:
            with fixture.controller._screen_state_lock:
                fixture.controller._trusted_connected_evidence[missing] = (
                    stale_evidence
                )

    monkeypatch.setattr(
        fixture.controller,
        "_revoke_source_failure_evidence",
        revoke_then_restore,
    )
    provider_state["value"] = ResolvedTargetWindows((windows[0],))

    result = fixture.controller.reconnect()

    assert result.details["all_connected"] is False
    assert result.details["source_missing_windows"] == 1
    assert missing not in fixture.controller._trusted_connected_evidence
    assert fixture.controller.role_screen_states()[missing] == (
        ReconnectScreenState.UNKNOWN
    )


def test_observation_rejects_connected_state_after_source_generation_change(
    monkeypatch,
):
    window = make_window(1)
    fixture = make_controller([1], windows=[window], expected_windows=1)
    fingerprint = window.launch_fingerprint
    original_capture = fixture.controller._capture_and_recognize

    def capture_then_revoke(window_arg, fingerprint_arg):
        result = original_capture(window_arg, fingerprint_arg)
        fixture.controller._revoke_source_failure_evidence(
            frozenset({fingerprint_arg})
        )
        return result

    monkeypatch.setattr(
        fixture.controller,
        "_capture_and_recognize",
        capture_then_revoke,
    )

    observed = fixture.controller.observe_screen_states({fingerprint})

    assert observed == {
        fingerprint: ReconnectScreenState.UNKNOWN,
    }
    assert fingerprint not in fixture.controller._trusted_connected_evidence


def test_isolated_target_source_failure_does_not_block_safe_disconnected_role():
    windows = [make_window(1), make_window(2)]
    selected = {
        windows[0].launch_fingerprint,
        windows[1].launch_fingerprint,
    }
    fixture = make_controller(
        [2],
        windows=[windows[0]],
        expected_windows=2,
        target_windows_provider=lambda: ResolvedTargetWindows(
            (windows[0],),
            ("unidentified_candidate_window",),
        ),
    )
    fixture.controller.set_allowed_fingerprints(selected)

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.success is True
    assert result.code == "reconnect.progressed_with_isolation"
    assert result.details["clicked_windows"] == 1
    assert "unidentified_candidate_window" in result.details["failure_codes"]
    assert fixture.mouse.clicks == [(1, (0.5, 0.5))]


def test_unscoped_incomplete_window_set_still_fails_before_capture():
    windows = [make_window(1)]
    fixture = make_controller([2], windows=windows, expected_windows=2)

    result = fixture.controller.reconnect()

    assert result.success is False
    assert "window_count_mismatch" in result.details["failure_codes"]
    assert fixture.capture.calls == []
    assert fixture.mouse.clicks == []


def test_global_reconnect_handles_a_confirmed_disconnect_without_a_group():
    window = make_window(1)
    fixture = make_controller(
        [2],
        windows=[window],
        expected_windows=14,
        require_expected_window_count=False,
    )

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert "window_count_mismatch" not in result.details["failure_codes"]
    assert fixture.capture.calls == [
        window.handle,
        window.handle,
        window.handle,
    ]
    assert fixture.mouse.clicks == [(window.handle, (0.5, 0.5))]


def test_global_reconnect_does_not_act_on_manual_login_without_disconnect():
    window = make_window(1)
    fixture = make_controller(
        [3],
        windows=[window],
        expected_windows=14,
        require_expected_window_count=False,
    )

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["actionable_windows"] == 0
    assert fixture.mouse.clicks == []


def test_global_reconnect_enters_only_the_game_selected_role_after_disconnect():
    window = make_window(1)
    capture = FakeCaptureProvider({window.handle: 5})
    mouse = FakeMouseBackend()

    class SelectedRoleRecognizer:
        def recognize_capture(self, _sample):
            return ScreenRecognition(
                state=ReconnectScreenState.CHARACTER_SELECTION,
                score=0.0,
                click_point=(0.3, 0.8),
                reference_name="character_selection",
                character_slot_selected=True,
            )

    controller = WindowsSmartReconnectController(
        expected_windows=14,
        title_keywords=("Adobe Flash Player",),
        window_backend=FakeWindowBackend([window]),
        capture_provider=capture,
        recognizer=SelectedRoleRecognizer(),
        mouse_backend=mouse,
        execution_enabled=True,
        primary_capture_is_trusted=True,
        require_expected_window_count=False,
    )
    controller._pending_reconnect_fingerprints.add(window.launch_fingerprint)

    controller.reconnect()
    result = controller.reconnect()

    assert result.details["clicked_windows"] == 1
    assert mouse.clicks == [(window.handle, CHARACTER_ENTER_CLICK_POINT)]


def test_global_reconnect_selects_the_unique_highest_level_after_disconnect():
    window = make_window(1)
    capture = FakeCaptureProvider({window.handle: 5})
    mouse = FakeMouseBackend()
    candidates = (
        CharacterSelectionCandidate(
            120,
            CharacterImportance.PRIMARY,
            0,
            False,
            (0.355, 0.706),
        ),
        CharacterSelectionCandidate(
            160,
            CharacterImportance.SECONDARY,
            2,
            False,
            (0.651, 0.706),
        ),
    )

    class HighestLevelRecognizer:
        def recognize_capture(self, _sample):
            return ScreenRecognition(
                state=ReconnectScreenState.CHARACTER_SELECTION,
                score=0.0,
                click_point=candidates[0].click_point,
                reference_name="character_selection",
                character_candidates=candidates,
            )

    controller = WindowsSmartReconnectController(
        expected_windows=14,
        title_keywords=("Adobe Flash Player",),
        window_backend=FakeWindowBackend([window]),
        capture_provider=capture,
        recognizer=HighestLevelRecognizer(),
        mouse_backend=mouse,
        execution_enabled=True,
        primary_capture_is_trusted=True,
        require_expected_window_count=False,
    )
    controller._pending_reconnect_fingerprints.add(window.launch_fingerprint)

    controller.reconnect()
    result = controller.reconnect()

    assert result.details["clicked_windows"] == 1
    assert mouse.clicks == [(window.handle, candidates[1].click_point)]


def test_global_reconnect_uses_the_main_role_for_a_highest_level_tie():
    window = make_window(1)
    capture = FakeCaptureProvider({window.handle: 5})
    mouse = FakeMouseBackend()
    candidates = (
        CharacterSelectionCandidate(
            160,
            CharacterImportance.SECONDARY,
            0,
            False,
            (0.355, 0.706),
        ),
        CharacterSelectionCandidate(
            160,
            CharacterImportance.PRIMARY,
            1,
            False,
            (0.500, 0.706),
        ),
    )

    class TiedLevelRecognizer:
        def recognize_capture(self, _sample):
            return ScreenRecognition(
                state=ReconnectScreenState.CHARACTER_SELECTION,
                score=0.0,
                click_point=candidates[0].click_point,
                reference_name="character_selection",
                character_candidates=candidates,
            )

    controller = WindowsSmartReconnectController(
        expected_windows=14,
        title_keywords=("Adobe Flash Player",),
        window_backend=FakeWindowBackend([window]),
        capture_provider=capture,
        recognizer=TiedLevelRecognizer(),
        mouse_backend=mouse,
        execution_enabled=True,
        primary_capture_is_trusted=True,
        require_expected_window_count=False,
    )
    controller._pending_reconnect_fingerprints.add(window.launch_fingerprint)

    controller.reconnect()
    result = controller.reconnect()

    assert result.details["clicked_windows"] == 1
    assert mouse.clicks == [(window.handle, candidates[1].click_point)]


def test_global_reconnect_uses_the_saved_main_role_for_a_highest_level_tie():
    window = make_window(1)
    capture = FakeCaptureProvider({window.handle: 5})
    mouse = FakeMouseBackend()
    candidates = (
        CharacterSelectionCandidate(
            160,
            None,
            0,
            False,
            (0.355, 0.706),
            identity="角色乙",
        ),
        CharacterSelectionCandidate(
            160,
            None,
            1,
            False,
            (0.500, 0.706),
            identity="角色甲",
        ),
    )

    class TiedLevelRecognizer:
        def recognize_capture(self, _sample):
            return ScreenRecognition(
                state=ReconnectScreenState.CHARACTER_SELECTION,
                score=0.0,
                click_point=candidates[0].click_point,
                reference_name="character_selection",
                character_candidates=candidates,
            )

    controller = WindowsSmartReconnectController(
        expected_windows=14,
        title_keywords=("Adobe Flash Player",),
        window_backend=FakeWindowBackend([window]),
        capture_provider=capture,
        recognizer=TiedLevelRecognizer(),
        mouse_backend=mouse,
        execution_enabled=True,
        primary_capture_is_trusted=True,
        require_expected_window_count=False,
        registered_role_provider=lambda: (
            RegisteredReconnectRole(
                "角色乙",
                CharacterImportance.SECONDARY,
            ),
            RegisteredReconnectRole(
                "角色甲",
                CharacterImportance.PRIMARY,
            ),
        ),
    )
    controller._pending_reconnect_fingerprints.add(window.launch_fingerprint)

    controller.reconnect()
    result = controller.reconnect()

    assert result.details["clicked_windows"] == 1
    assert mouse.clicks == [(window.handle, candidates[1].click_point)]


@pytest.mark.parametrize("unsafe_group", ("missing", "duplicate"))
def test_group_failure_early_return_revokes_old_connected_evidence(
    unsafe_group,
):
    window = make_window(1)
    fixture = make_controller(
        [1],
        windows=[window],
        expected_windows=1,
    )
    connected = fixture.controller.reconnect()
    assert connected.code == "reconnect.connected"
    assert fixture.controller._trusted_connected_evidence

    if unsafe_group == "missing":
        fixture.controller._window_backend.windows = []
    else:
        fixture.controller._window_backend.windows = [
            window,
            make_window(
                2,
                fingerprint=window.launch_fingerprint,
            ),
        ]

    result = fixture.controller.reconnect()

    assert result.code == "reconnect.waiting"
    assert fixture.controller.role_screen_states() == {
        window.launch_fingerprint: ReconnectScreenState.UNKNOWN
    }
    assert fixture.controller._trusted_connected_evidence == {}
    assert fixture.mouse.clicks == []


def test_new_controller_starts_with_execution_hard_disabled():
    windows = [make_window(1), make_window(2)]
    capture = FakeCaptureProvider({1: 2, 2: 1})
    mouse = FakeMouseBackend()
    controller = WindowsSmartReconnectController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=FakeWindowBackend(windows),
        capture_provider=capture,
        primary_capture_is_trusted=True,
        recognizer=FakeRecognizer(
            {
                1: ReconnectScreenState.CONNECTED,
                2: ReconnectScreenState.DISCONNECTED,
            },
            {2: (0.5, 0.5)},
        ),
        mouse_backend=mouse,
    )

    result = controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert mouse.clicks == []


def test_reconnect_does_not_advance_a_login_state_without_disconnect_session():
    fixture = make_controller([2, 4])

    first = fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert first.code == "reconnect.waiting"
    assert result.success is True
    assert result.code == "reconnect.progressed"
    assert fixture.controller.state is ReconnectState.RECONNECTING
    assert fixture.mouse.clicks == [
        (1, (0.5, 0.5)),
    ]
    assert result.details["clicked_windows"] == 1
    assert result.details["next_check_seconds"] == 5


def test_disconnect_context_uses_force_login_instead_of_start_game():
    fixture = make_controller([2, 1])

    fixture.controller.reconnect()
    first = fixture.controller.reconnect()
    fixture.capture.states[1] = 3
    fixture.controller.reconnect()
    second = fixture.controller.reconnect()

    assert first.code == "reconnect.progressed"
    assert second.code == "reconnect.progressed"
    assert fixture.mouse.clicks == [
        (1, (0.5, 0.5)),
        (1, (0.505, 0.856)),
    ]
    assert second.details["state_counts"] == {
        "connected": 1,
        "force_login_start": 1,
    }
    assert second.details["next_check_seconds"] == 10


def test_battle_disconnect_restarts_exact_target_without_clicking(tmp_path):
    windows = [make_window(1), make_window(2)]
    first_shortcut = tmp_path / "first.lnk"
    second_shortcut = tmp_path / "second.lnk"
    plan = GroupLaunchPlan(
        "120",
        targets=(
            GroupLaunchTarget(
                1,
                "120古",
                first_shortcut,
                windows[0].launch_fingerprint,
            ),
            GroupLaunchTarget(
                2,
                "120靈",
                second_shortcut,
                windows[1].launch_fingerprint,
            ),
        ),
    )
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [2, 1],
        windows=windows,
        battle_markers={2},
        battle_restarter=restarter,
        group_launch_plan=plan,
    )

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.success is True
    assert result.details["restarted_windows"] == 1
    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []
    assert restarter.calls == [(windows[0], plan.targets[0])]
    assert result.details["next_check_seconds"] == 2


def test_battle_disconnect_without_unique_target_waits_one_minute():
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [2, 1],
        battle_markers={2},
        battle_restarter=restarter,
    )

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.success is False
    assert "battle_restart_identity_unresolved" in result.details["failure_codes"]
    assert result.details["restarted_windows"] == 0
    assert result.details["clicked_windows"] == 0
    assert result.details["next_check_seconds"] == 60
    assert fixture.mouse.clicks == []
    assert restarter.calls == []


def test_battle_failure_keeps_one_named_status_until_connected(tmp_path):
    windows = [make_window(1), make_window(2)]
    plan = GroupLaunchPlan(
        "120",
        targets=(
            GroupLaunchTarget(
                1,
                "120古",
                tmp_path / "first.lnk",
                windows[0].launch_fingerprint,
            ),
            GroupLaunchTarget(
                2,
                "120靈",
                tmp_path / "second.lnk",
                windows[1].launch_fingerprint,
            ),
        ),
    )
    statuses = ReconnectFailureStatusService()
    restarter = FakeBattleRestarter(succeeds=False)
    fixture = make_controller(
        [2, 1],
        windows=windows,
        battle_markers={2},
        battle_restarter=restarter,
        group_launch_plan=plan,
        failure_status_service=statuses,
    )

    fixture.controller.reconnect()
    fixture.controller.reconnect()
    fixture.controller.reconnect()
    assert statuses.messages() == ("120古－重連失敗",)

    complete_with_fresh_connected_frames(fixture)
    assert statuses.messages() == ()


def test_unknown_battle_identity_uses_group_unknown_status():
    statuses = ReconnectFailureStatusService()
    fixture = make_controller(
        [2, 1],
        battle_markers={2},
        battle_restarter=FakeBattleRestarter(),
        failure_status_service=statuses,
    )

    fixture.controller.reconnect()
    fixture.controller.reconnect()

    assert statuses.messages() == (
        "目前組別中的未知角色－重連失敗",
    )


def test_missing_reopen_retries_immediately_without_touching_other_roles(
    tmp_path,
):
    now = [0.0]
    windows = [make_window(1), make_window(2)]
    plan = GroupLaunchPlan(
        "120",
        targets=(
            GroupLaunchTarget(
                1,
                "120古",
                tmp_path / "first.lnk",
                windows[0].launch_fingerprint,
            ),
            GroupLaunchTarget(
                2,
                "120靈",
                tmp_path / "second.lnk",
                windows[1].launch_fingerprint,
            ),
        ),
    )
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [2, 1],
        windows=windows,
        clock=lambda: now[0],
        battle_markers={2},
        battle_restarter=restarter,
        group_launch_plan=plan,
        failure_status_service=ReconnectFailureStatusService(),
    )

    fixture.controller.reconnect()
    first = fixture.controller.reconnect()
    assert first.details["restarted_windows"] == 1
    fixture.controller._window_backend.windows = [windows[1]]

    now[0] = 1.0
    before = fixture.controller.reconnect()
    now[0] = 2.0
    retry = fixture.controller.reconnect()
    now[0] = 3.0
    no_duplicate = fixture.controller.reconnect()
    now[0] = 4.0
    next_retry = fixture.controller.reconnect()

    assert before.details["restarted_windows"] == 0
    assert retry.details["restarted_windows"] == 1
    assert no_duplicate.details["restarted_windows"] == 0
    assert next_retry.details["restarted_windows"] == 1
    assert len(restarter.reopen_calls) == 2


def test_failed_battle_restart_retries_same_role_after_progress_interval(
    tmp_path,
):
    now = [0.0]
    windows = [make_window(1), make_window(2)]
    plan = GroupLaunchPlan(
        "120",
        targets=(
            GroupLaunchTarget(
                1,
                "120古",
                tmp_path / "first.lnk",
                windows[0].launch_fingerprint,
            ),
            GroupLaunchTarget(
                2,
                "120靈",
                tmp_path / "second.lnk",
                windows[1].launch_fingerprint,
            ),
        ),
    )
    restarter = FakeBattleRestarter(succeeds=False)
    fixture = make_controller(
        [2, 1],
        windows=windows,
        clock=lambda: now[0],
        battle_markers={2},
        battle_restarter=restarter,
        group_launch_plan=plan,
        failure_status_service=ReconnectFailureStatusService(),
    )

    fixture.controller.reconnect()
    first = fixture.controller.reconnect()
    assert len(restarter.calls) == 2
    now[0] = 1.0
    before = fixture.controller.reconnect()
    assert len(restarter.calls) == 2
    now[0] = 2.0
    retry = fixture.controller.reconnect()

    assert first.details["next_check_seconds"] == 2
    assert before.details["restarted_windows"] == 0
    assert retry.details["next_check_seconds"] == 2
    assert len(restarter.calls) == 4
    assert all(call[0].handle == windows[0].handle for call in restarter.calls)


def test_each_known_role_failure_records_then_restarts_only_that_role(
    tmp_path,
):
    windows = [make_window(1), make_window(2)]
    plan = GroupLaunchPlan(
        "120",
        targets=tuple(
            GroupLaunchTarget(
                index,
                name,
                tmp_path / f"{index}.lnk",
                window.launch_fingerprint,
            )
            for index, (name, window) in enumerate(
                (("120古", windows[0]), ("120靈", windows[1])),
                start=1,
            )
        ),
    )
    restarter = FakeBattleRestarter(succeeds=False)
    records = []
    fixture = make_controller(
        [1, 1],
        windows=windows,
        battle_restarter=restarter,
        group_launch_plan=plan,
        failure_status_service=ReconnectFailureStatusService(),
        failure_record_callback=lambda role, detail: records.append(
            (role, detail)
        ),
    )

    fixture.controller._report_reconnect_failure(
        windows[0].launch_fingerprint
    )
    fixture.controller._report_reconnect_failure(
        windows[0].launch_fingerprint
    )

    assert [call[0].handle for call in restarter.calls] == [1, 1]
    assert all(call[0].handle != 2 for call in restarter.calls)
    assert records[0] == ("120古", "重連失敗")
    assert records[2] == ("120古", "重連失敗")
    assert len(records) == 4


def test_known_role_failure_restarts_without_status_service(tmp_path):
    windows = [make_window(1), make_window(2)]
    plan = make_group_plan(tmp_path, windows, "120")
    restarter = FakeBattleRestarter(succeeds=False)
    fixture = make_controller(
        [1, 1],
        windows=windows,
        battle_restarter=restarter,
        group_launch_plan=plan,
    )

    fixture.controller._report_reconnect_failure(
        windows[0].launch_fingerprint
    )

    assert restarter.calls == [(windows[0], plan.targets[0])]
    assert restarter.reopen_calls == []


def test_target_provider_failure_blocks_direct_role_restart(tmp_path):
    window = make_window(1)
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [1],
        windows=[window],
        expected_windows=1,
        battle_restarter=restarter,
        group_launch_plan=make_group_plan(tmp_path, [window], "120"),
        target_windows_provider=lambda: (_ for _ in ()).throw(
            RuntimeError("target source failed")
        ),
    )

    fixture.controller._report_reconnect_failure(window.launch_fingerprint)

    assert restarter.calls == []
    assert restarter.reopen_calls == []


def test_target_provider_failure_blocks_pending_role_reopen(tmp_path):
    window = make_window(1)
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [1],
        windows=[window],
        expected_windows=1,
        battle_restarter=restarter,
        group_launch_plan=make_group_plan(tmp_path, [window], "120"),
        target_windows_provider=lambda: (_ for _ in ()).throw(
            RuntimeError("target source failed")
        ),
    )
    fixture.controller._pending_reopen_fingerprints.add(
        window.launch_fingerprint
    )
    fixture.controller._reopen_retry_after[window.launch_fingerprint] = 0.0

    result = fixture.controller.reconnect()

    assert "target_window_provider_failed" in result.details[
        "failure_codes"
    ]
    assert restarter.calls == []
    assert restarter.reopen_calls == []


def test_failure_report_does_not_restart_after_capture_settings_change(
    tmp_path,
):
    window = make_window(1)
    restarter = FakeBattleRestarter()
    failures = ReconnectFailureStatusService()
    fixture = make_controller(
        [1],
        windows=[window],
        expected_windows=1,
        battle_restarter=restarter,
        group_launch_plan=make_group_plan(tmp_path, [window], "120"),
        failure_status_service=failures,
    )
    fixture.controller.reconnect()
    _, old_revision = fixture.controller._capture_settings_snapshot()
    fixture.controller.set_capture_settings(
        SmartReconnectCaptureSettings(
            visible=True,
            obscured=True,
            minimized=False,
        )
    )

    fixture.controller._report_reconnect_failure(
        window.launch_fingerprint,
        expected_capture_settings_revision=old_revision,
        capture_route="visible",
    )

    assert failures.has(f"role:{window.launch_fingerprint}") is True
    assert restarter.calls == []
    assert restarter.reopen_calls == []


def test_same_screen_action_is_not_repeated_before_one_minute_retry():
    now = [0.0]
    fixture = make_controller([2, 1], clock=lambda: now[0])

    observed = fixture.controller.reconnect()
    confirmed = fixture.controller.reconnect()
    now[0] = 5.0
    first = fixture.controller.reconnect()
    now[0] = 64.0
    before_deadline = fixture.controller.reconnect()
    now[0] = 65.0
    second = fixture.controller.reconnect()

    assert observed.details["clicked_windows"] == 0
    assert confirmed.details["clicked_windows"] == 0
    assert first.details["clicked_windows"] == 1
    assert before_deadline.details["clicked_windows"] == 0
    assert second.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [
        (1, (0.5, 0.5)),
        (1, (0.5, 0.5)),
    ]


def test_confirm_force_login_and_followup_obey_five_ten_ten_waits():
    now = [100.0]
    fixture = make_controller([2, 1], clock=lambda: now[0])

    first_disconnect = fixture.controller.reconnect()
    confirmed_disconnect = fixture.controller.reconnect()
    now[0] = 104.0
    before_confirm = fixture.controller.reconnect()
    now[0] = 105.0
    confirm = fixture.controller.reconnect()

    fixture.capture.states[1] = 3
    now[0] = 110.0
    first_force = fixture.controller.reconnect()
    now[0] = 112.0
    confirmed_force = fixture.controller.reconnect()
    now[0] = 114.0
    before_force = fixture.controller.reconnect()
    now[0] = 115.0
    force = fixture.controller.reconnect()

    fixture.capture.states[1] = 4
    now[0] = 124.0
    before_followup = fixture.controller.reconnect()
    now[0] = 125.0
    followup = fixture.controller.reconnect()

    assert first_disconnect.details["clicked_windows"] == 0
    assert confirmed_disconnect.details["clicked_windows"] == 0
    assert before_confirm.details["clicked_windows"] == 0
    assert confirm.details["clicked_windows"] == 1
    assert first_force.details["clicked_windows"] == 0
    assert confirmed_force.details["clicked_windows"] == 0
    assert before_force.details["clicked_windows"] == 0
    assert force.details["clicked_windows"] == 1
    assert before_followup.details["clicked_windows"] == 0
    assert followup.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [
        (1, (0.5, 0.5)),
        (1, (0.505, 0.856)),
        (1, (0.5, 0.327)),
    ]


def test_same_group_disconnect_context_and_pause_survive_restart(tmp_path):
    state_path = tmp_path / "smart_reconnect_state.json"
    now = [1000.0]
    windows = [make_window(1), make_window(2)]
    plan = make_group_plan(tmp_path, windows, "same-group")
    first = make_controller(
        [2, 1],
        windows=windows,
        clock=lambda: now[0],
        state_path=state_path,
        group_launch_plan=plan,
    )

    first.controller.reconnect()
    now[0] = 1005.0
    first.controller.reconnect()
    assert first.mouse.clicks == [(1, (0.5, 0.5))]

    now[0] = 1006.0
    second = make_controller(
        [3, 1],
        windows=windows,
        clock=lambda: now[0],
        state_path=state_path,
        group_launch_plan=make_group_plan(
            tmp_path,
            windows,
            "same-group",
        ),
    )
    second.controller.reconnect()
    before_deadline = second.controller.reconnect()

    assert before_deadline.details["state_counts"] == {
        "connected": 1,
        "force_login_start": 1,
    }
    assert before_deadline.details["clicked_windows"] == 0
    assert second.mouse.clicks == []

    now[0] = 1015.0
    after_deadline = second.controller.reconnect()

    assert after_deadline.details["clicked_windows"] == 1
    assert second.mouse.clicks == [(1, (0.505, 0.856))]


@pytest.mark.parametrize("legacy_version", range(1, 6))
def test_legacy_reconnect_authority_is_cleared_before_controller_can_act(
    tmp_path,
    legacy_version,
):
    state_path = tmp_path / "smart_reconnect_state.json"
    fingerprint = make_window(1).launch_fingerprint
    state_path.write_text(
        json.dumps(
            {
                "version": legacy_version,
                "pending_fingerprints": [fingerprint],
                "active_fingerprints": [fingerprint],
                "active_until": {fingerprint: 9_999_999_999},
                "retry_after": {
                    fingerprint: {
                        "state": "force_login_start",
                        "retry_at": 9_999_999_999,
                    }
                },
                "pending_reopen_fingerprints": [fingerprint],
                "reopen_retry_after": {fingerprint: 9_999_999_999},
                "terminal_ready_after": {fingerprint: 0},
                "flow_pause_until": {fingerprint: 9_999_999_999},
                "scope_token": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    fixture = make_controller([4, 1], state_path=state_path)

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["actionable_windows"] == 0
    assert fixture.controller.reconnecting_fingerprints() == frozenset()
    assert fixture.mouse.clicks == []
    migrated = json.loads(state_path.read_text(encoding="utf-8"))
    assert migrated["version"] == ReconnectRuntimeStateStore.VERSION
    assert migrated["scope_token"] is None
    assert migrated["pending_fingerprints"] == []
    assert migrated["active_fingerprints"] == []
    assert migrated["flow_pause_until"] == {}
    assert fingerprint not in state_path.read_text(encoding="utf-8")


def test_timeout_flow_pause_survives_controller_restart(tmp_path):
    state_path = tmp_path / "smart_reconnect_state.json"
    now = [1000.0]
    windows = [make_window(1), make_window(2)]
    plan = make_group_plan(tmp_path, windows, "timeout-group")
    first = make_controller(
        [9, 1],
        windows=windows,
        clock=lambda: now[0],
        state_path=state_path,
        group_launch_plan=plan,
    )
    fingerprint = windows[0].launch_fingerprint
    first.controller._pending_reconnect_fingerprints.add(fingerprint)

    first.controller.reconnect()
    first.controller.reconnect()
    assert first.mouse.clicks == [(1, (0.5, 0.57))]

    now[0] = 1001.0
    second = make_controller(
        [3, 1],
        windows=windows,
        clock=lambda: now[0],
        state_path=state_path,
        group_launch_plan=make_group_plan(
            tmp_path,
            windows,
            "timeout-group",
        ),
    )
    second.controller.reconnect()
    after_restart = second.controller.reconnect()

    assert after_restart.details["clicked_windows"] == 0
    assert second.mouse.clicks == []

    now[0] = 1059.0
    before_deadline = second.controller.reconnect()
    now[0] = 1060.0
    after_deadline = second.controller.reconnect()

    assert before_deadline.details["clicked_windows"] == 0
    assert after_deadline.details["clicked_windows"] == 1
    assert second.mouse.clicks == [(1, (0.505, 0.856))]


def test_popup_automation_context_survives_controller_restart(tmp_path):
    state_path = tmp_path / "smart_reconnect_state.json"
    now = [1000.0]
    windows = [make_window(1), make_window(2)]
    plan = make_group_plan(tmp_path, windows, "popup-group")
    first = make_controller(
        [2, 1],
        windows=windows,
        clock=lambda: now[0],
        state_path=state_path,
        group_launch_plan=plan,
    )

    first.controller.reconnect()
    now[0] = 1005.0
    first.controller.reconnect()
    now[0] = 1015.0
    second = make_controller(
        [6, 1],
        windows=windows,
        clock=lambda: now[0],
        state_path=state_path,
        group_launch_plan=make_group_plan(
            tmp_path,
            windows,
            "popup-group",
        ),
    )
    second.controller.reconnect()
    result = second.controller.reconnect()

    assert result.code == "reconnect.progressed"
    assert second.mouse.clicks == [(1, (0.86, 0.12))]


def test_connected_screen_revokes_popup_and_manual_login_authorization(tmp_path):
    state_path = tmp_path / "smart_reconnect_state.json"
    now = [1000.0]
    first = make_controller(
        [2, 1],
        clock=lambda: now[0],
        state_path=state_path,
    )
    first.controller.reconnect()
    first.controller.reconnect()

    now[0] = 1008.0
    connected = make_controller(
        [10, 1],
        clock=lambda: now[0],
        state_path=state_path,
    )
    complete_with_fresh_connected_frames(
        connected,
        now=now,
    )

    now[0] = 1020.0
    popup = make_controller(
        [7, 1],
        clock=lambda: now[0],
        state_path=state_path,
    )
    popup.controller.reconnect()
    result = popup.controller.reconnect()

    assert result.code == "reconnect.waiting"
    assert result.details["actionable_windows"] == 0
    assert popup.controller.reconnecting_fingerprints() == frozenset()
    assert popup.mouse.clicks == []


def test_delayed_popup_context_expires_and_restores_player_control(tmp_path):
    state_path = tmp_path / "smart_reconnect_state.json"
    now = [1000.0]
    first = make_controller(
        [3, 1],
        clock=lambda: now[0],
        state_path=state_path,
    )
    first.controller.reconnect()

    now[0] = 1181.0
    expired = make_controller(
        [7, 1],
        clock=lambda: now[0],
        state_path=state_path,
    )
    result = expired.controller.reconnect()

    assert result.code == "reconnect.waiting"
    assert result.details["actionable_windows"] == 0
    assert expired.mouse.clicks == []


def test_completed_context_is_removed_without_waiting_for_expiration(tmp_path):
    state_path = tmp_path / "smart_reconnect_state.json"
    now = [1000.0]
    first = make_controller(
        [2, 1],
        clock=lambda: now[0],
        state_path=state_path,
    )

    first.controller.reconnect()
    first.controller.reconnect()
    now[0] = 1008.0
    complete_with_fresh_connected_frames(
        first,
        now=now,
    )
    assert first.controller.reconnecting_fingerprints() == frozenset()

    now[0] = 1181.0
    assert first.controller.reconnecting_fingerprints() == frozenset()
    restored = make_controller(
        [1, 1],
        clock=lambda: now[0],
        state_path=state_path,
    )
    assert restored.controller.reconnecting_fingerprints() == frozenset()


def test_changed_screen_allows_next_action_without_waiting_one_minute():
    fixture = make_controller([2, 1])

    fixture.controller.reconnect()
    fixture.controller.reconnect()
    fixture.capture.states[1] = 3
    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [
        (1, (0.5, 0.5)),
        (1, (0.505, 0.856)),
    ]


def test_character_selection_confirms_exact_role_before_entering_game(tmp_path):
    windows = [make_window(1), make_window(2)]
    capture = FakeCaptureProvider({1: 5, 2: 1})
    mouse = FakeMouseBackend()

    class CharacterSequenceRecognizer:
        def __init__(self):
            self.selected = False

        def recognize_capture(self, sample):
            if sample.pixels[0] == 1:
                return ScreenRecognition(
                    state=ReconnectScreenState.CONNECTED,
                    score=0.0,
                    click_point=None,
                    reference_name="connected",
                )
            return ScreenRecognition(
                state=ReconnectScreenState.CHARACTER_SELECTION,
                score=0.0,
                click_point=(
                    (0.353, 0.854)
                    if self.selected
                    else (0.651, 0.706)
                ),
                reference_name="character_selection",
                character_level=160,
                character_slot_index=2,
                character_slot_selected=self.selected,
                character_identity="160主",
            )

    recognizer = CharacterSequenceRecognizer()
    controller = WindowsSmartReconnectController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=FakeWindowBackend(windows),
        capture_provider=capture,
        primary_capture_is_trusted=True,
        recognizer=recognizer,
        mouse_backend=mouse,
        execution_enabled=True,
    )
    controller.set_group_launch_plan(
        GroupLaunchPlan(
            "160",
            targets=(
                GroupLaunchTarget(
                    1,
                    "160主",
                    tmp_path / "160.lnk",
                    windows[0].launch_fingerprint,
                ),
                GroupLaunchTarget(
                    2,
                    "160副",
                    tmp_path / "160-2.lnk",
                    windows[1].launch_fingerprint,
                ),
            ),
        )
    )
    controller._pending_reconnect_fingerprints.add(
        windows[0].launch_fingerprint
    )

    controller.reconnect()
    first = controller.reconnect()
    recognizer.selected = True
    controller.reconnect()
    second = controller.reconnect()

    assert first.details["clicked_windows"] == 1
    assert second.details["clicked_windows"] == 1
    assert mouse.clicks == [
        (1, (0.651, 0.706)),
        (1, (0.353, 0.854)),
    ]


def test_character_selection_never_clicks_a_different_role_level(tmp_path):
    windows = [make_window(1), make_window(2)]
    capture = FakeCaptureProvider({1: 5, 2: 1})
    mouse = FakeMouseBackend()

    class MismatchedCharacterRecognizer:
        def recognize_capture(self, sample):
            if sample.pixels[0] == 1:
                return ScreenRecognition(
                    state=ReconnectScreenState.CONNECTED,
                    score=0.0,
                    click_point=None,
                    reference_name="connected",
                )
            return ScreenRecognition(
                state=ReconnectScreenState.CHARACTER_SELECTION,
                score=0.0,
                click_point=(0.651, 0.706),
                reference_name="character_selection",
                character_level=160,
                character_slot_index=2,
                character_slot_selected=False,
            )

    controller = WindowsSmartReconnectController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=FakeWindowBackend(windows),
        capture_provider=capture,
        primary_capture_is_trusted=True,
        recognizer=MismatchedCharacterRecognizer(),
        mouse_backend=mouse,
        execution_enabled=True,
    )
    controller.set_group_launch_plan(
        GroupLaunchPlan(
            "120",
            targets=(
                GroupLaunchTarget(
                    1,
                    "120古",
                    tmp_path / "120.lnk",
                    windows[0].launch_fingerprint,
                ),
                GroupLaunchTarget(
                    2,
                    "120靈",
                    tmp_path / "120-2.lnk",
                    windows[1].launch_fingerprint,
                ),
            ),
        )
    )
    controller._pending_reconnect_fingerprints.add(
        windows[0].launch_fingerprint
    )

    controller.reconnect()
    result = controller.reconnect()

    assert result.details["actionable_windows"] == 0
    assert mouse.clicks == []


def test_manual_activity_popup_is_recognized_but_not_closed():
    fixture = make_controller([6, 1])

    result = fixture.controller.reconnect()

    assert result.success is False
    assert result.code == "reconnect.waiting"
    assert result.details["state_counts"] == {
        "connected": 1,
        "post_login_activity": 1,
    }
    assert result.details["actionable_windows"] == 0
    assert fixture.mouse.clicks == []


def test_popup_is_closed_only_inside_controller_started_login_flow():
    fixture = make_controller([2, 1])

    fixture.controller.reconnect()
    fixture.controller.reconnect()
    fixture.capture.states[1] = 3
    fixture.controller.reconnect()
    fixture.controller.reconnect()
    fixture.capture.states[1] = 6
    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.success is True
    assert result.code == "reconnect.progressed"
    assert fixture.mouse.clicks == [
        (1, (0.5, 0.5)),
        (1, (0.505, 0.856)),
        (1, (0.86, 0.12)),
    ]


def test_fresh_login_screen_never_takes_over_player_login():
    fixture = make_controller([3, 1])

    first = fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert first.code == "reconnect.waiting"
    assert result.code == "reconnect.waiting"
    assert fixture.mouse.clicks == []
    assert result.details["actionable_windows"] == 0
    assert result.details["state_counts"] == {
        "connected": 1,
        "login_start": 1,
    }


def test_fresh_line_and_character_screens_never_take_over_player_login():
    line = make_controller([4, 1])
    character = make_controller([5, 1])

    line.controller.reconnect()
    line_result = line.controller.reconnect()
    character.controller.reconnect()
    character_result = character.controller.reconnect()

    assert line_result.details["actionable_windows"] == 0
    assert character_result.details["actionable_windows"] == 0
    assert line.mouse.clicks == []
    assert character.mouse.clicks == []


def test_one_transient_disconnect_frame_does_not_start_automation():
    fixture = make_controller([2, 1])

    first = fixture.controller.reconnect()
    fixture.capture.states[1] = 1
    second = fixture.controller.reconnect()

    assert first.details["actionable_windows"] == 0
    assert second.code == "reconnect.connected"
    assert fixture.controller.reconnecting_fingerprints() == frozenset()
    assert fixture.mouse.clicks == []


def test_unknown_peer_blocks_disconnected_batch_without_clicking():
    fixture = make_controller([2, 255])

    result = fixture.controller.reconnect()

    assert result.details["unknown_windows"] == 1
    assert result.details["actionable_windows"] == 0
    assert result.details["next_check_seconds"] == 2
    assert fixture.mouse.clicks == []


def test_unknown_peer_is_never_operated_during_a_known_reconnect_session():
    fixture = make_controller([2, 255])
    fixture.controller.reconnect()
    fixture.controller.reconnect()
    fixture.capture.states[1] = 3

    result = fixture.controller.reconnect()

    assert result.details["state_counts"] == {
        "force_login_start": 1,
        "unknown": 1,
    }
    assert result.details["actionable_windows"] == 0
    assert result.details["next_check_seconds"] == 2
    assert fixture.mouse.clicks == [(1, (0.5, 0.5))]


def test_line_selection_keeps_default_when_dialog_indicator_changes():
    fixture = make_controller([4, 1])
    fingerprint = make_window(1).launch_fingerprint
    fixture.controller._pending_reconnect_fingerprints.add(fingerprint)

    fixture.controller.reconnect()
    fixture.controller._recognizer.points[4] = (0.5, 0.4)
    changed = fixture.controller.reconnect()
    confirmed = fixture.controller.reconnect()

    assert changed.details["clicked_windows"] == 1
    assert confirmed.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == [(1, (0.5, 0.327))]


def test_real_controller_uses_only_non_disruptive_background_capture():
    controller = WindowsSmartReconnectController.for_real_windows(
        reference_dir=Path("assets") / "reconnect_reference",
        expected_windows=1,
    )

    assert isinstance(controller._capture_provider, Win32PrintWindowProvider)
    assert type(controller._capture_provider) is Win32PrintWindowProvider
    assert controller._obscured_capture_provider is None
    assert controller._active_refresh_capture_provider is None
    assert controller._primary_capture_is_trusted is True
    assert controller._primary_capture_is_fresh_without_visibility is True


def test_line_selection_keeps_the_saved_default_instead_of_dialog_hint(
    tmp_path,
):
    state_path = tmp_path / "reconnect-state.json"
    fixture = make_controller(
        [4, 1],
        points={4: (0.5, 0.665)},
        state_path=state_path,
    )
    fingerprint = make_window(1).launch_fingerprint
    fixture.controller._pending_reconnect_fingerprints.add(fingerprint)

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [(1, (0.5, 0.327))]
    saved = ReconnectRuntimeStateStore(state_path).load()
    assert saved.preferred_line_numbers == {fingerprint: 1}


def test_fresh_visible_capture_wins_without_running_stale_primary_capture():
    windows = [make_window(1), make_window(2)]
    primary = FakeCaptureProvider({1: 2, 2: 1})
    visible = FakeCaptureProvider({1: 1, 2: 1})
    recognizer = FakeRecognizer(
        {
            1: ReconnectScreenState.CONNECTED,
            2: ReconnectScreenState.DISCONNECTED,
            255: ReconnectScreenState.UNKNOWN,
        }
    )
    controller = WindowsSmartReconnectController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=FakeWindowBackend(windows),
        capture_provider=primary,
        visible_capture_provider=visible,
        recognizer=recognizer,
        mouse_backend=FakeMouseBackend(),
        execution_enabled=True,
    )

    result = controller.reconnect()

    assert result.code == "reconnect.connected"
    assert result.details["state_counts"] == {"connected": 2}
    assert primary.calls == []
    assert visible.calls == [1, 2]


def test_visible_unknown_fails_closed_instead_of_using_stale_disconnect():
    windows = [make_window(1), make_window(2)]
    primary = FakeCaptureProvider({1: 2, 2: 1})
    visible = FakeCaptureProvider({1: 255, 2: 1})
    recognizer = FakeRecognizer(
        {
            1: ReconnectScreenState.CONNECTED,
            2: ReconnectScreenState.DISCONNECTED,
            255: ReconnectScreenState.UNKNOWN,
        },
        points={2: (0.5, 0.5)},
    )
    mouse = FakeMouseBackend()
    controller = WindowsSmartReconnectController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=FakeWindowBackend(windows),
        capture_provider=primary,
        visible_capture_provider=visible,
        recognizer=recognizer,
        mouse_backend=mouse,
        execution_enabled=True,
    )

    controller.reconnect()
    result = controller.reconnect()

    assert result.details["state_counts"] == {
        "connected": 1,
        "unknown": 1,
    }
    assert result.details["clicked_windows"] == 0
    assert mouse.clicks == []
    assert primary.calls == []


def test_obscured_window_never_trusts_stale_background_disconnect():
    windows = [make_window(1), make_window(2)]
    primary = FakeCaptureProvider({1: 2, 2: 1})
    visible = FakeCaptureProvider({1: None, 2: 1})
    active_refresh = FakeCaptureProvider({1: 1})
    recognizer = FakeRecognizer(
        {
            1: ReconnectScreenState.CONNECTED,
            2: ReconnectScreenState.DISCONNECTED,
            255: ReconnectScreenState.UNKNOWN,
        },
        points={2: (0.5, 0.5)},
    )
    mouse = FakeMouseBackend()
    controller = WindowsSmartReconnectController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=ObscuredWindowBackend(windows),
        capture_provider=primary,
        visible_capture_provider=visible,
        active_refresh_capture_provider=active_refresh,
        recognizer=recognizer,
        mouse_backend=mouse,
        execution_enabled=True,
    )

    controller.reconnect()
    result = controller.reconnect()

    assert result.details["state_counts"] == {
        "connected": 1,
        "unknown": 1,
    }
    assert result.details["clicked_windows"] == 0
    assert primary.calls == [1, 1]
    assert active_refresh.calls == []
    assert mouse.clicks == []


def test_disabled_visible_check_never_captures_or_reports_online():
    windows = [make_window(1)]
    fixture = make_controller(
        [1],
        windows=windows,
        expected_windows=1,
        window_backend=FullyVisibleWindowBackend(windows),
    )
    fixture.controller.set_capture_settings(
        SmartReconnectCaptureSettings(
            visible=False,
            obscured=True,
            minimized=True,
        )
    )

    result = fixture.controller.reconnect()

    assert fixture.capture.calls == []
    assert result.details["connected_windows"] == 0
    assert result.details["unknown_windows"] == 1
    assert result.details["state_counts"] == {"check_disabled": 1}
    assert result.details["failure_codes"] == ["capture_mode_disabled"]
    assert result.details["all_connected"] is False
    assert fixture.mouse.clicks == []
    assert fixture.controller.role_screen_states() == {
        windows[0].launch_fingerprint: (
            ReconnectScreenState.CHECK_DISABLED
        )
    }


def test_disabling_capture_mode_revokes_states_by_last_trusted_route():
    visible_window = make_window(1)
    minimized_window = make_window(2, minimized=True)
    active_refresh = FakeCaptureProvider({2: 1})
    fixture = make_controller(
        [1, 1],
        windows=[visible_window, minimized_window],
        expected_windows=2,
        active_refresh_capture_provider=active_refresh,
    )
    fixture.controller.reconnect()
    assert fixture.controller.role_screen_states() == {
        visible_window.launch_fingerprint: ReconnectScreenState.CONNECTED,
        minimized_window.launch_fingerprint: ReconnectScreenState.CONNECTED,
    }

    fixture.controller.set_capture_settings(
        SmartReconnectCaptureSettings(
            visible=True,
            obscured=True,
            minimized=False,
        )
    )

    assert fixture.controller.role_screen_states() == {
        visible_window.launch_fingerprint: ReconnectScreenState.UNKNOWN,
        minimized_window.launch_fingerprint: (
            ReconnectScreenState.CHECK_DISABLED
        ),
    }


def test_disabled_obscured_check_never_calls_obscured_or_primary_provider():
    window = make_window(1)
    visible = FakeCaptureProvider({1: None})
    obscured = FakeCaptureProvider({1: 2})
    fixture = make_controller(
        [2],
        windows=[window],
        expected_windows=1,
        visible_capture_provider=visible,
        obscured_capture_provider=obscured,
        primary_capture_is_trusted=False,
        window_backend=ObscuredWindowBackend([window]),
    )
    fixture.controller.set_capture_settings(
        SmartReconnectCaptureSettings(
            visible=True,
            obscured=False,
            minimized=True,
        )
    )

    result = fixture.controller.reconnect()

    assert visible.calls == [1]
    assert obscured.calls == []
    assert fixture.capture.calls == []
    assert result.details["unknown_windows"] == 1
    assert result.details["state_counts"] == {"check_disabled": 1}
    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []


def test_enabled_obscured_check_uses_only_fresh_obscured_provider():
    window = make_window(1)
    visible = FakeCaptureProvider({1: None})
    obscured = FakeCaptureProvider({1: 2})
    fixture = make_controller(
        [1],
        windows=[window],
        expected_windows=1,
        visible_capture_provider=visible,
        obscured_capture_provider=obscured,
        primary_capture_is_trusted=False,
        window_backend=ObscuredWindowBackend([window]),
    )

    first = fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert first.details["clicked_windows"] == 0
    assert result.details["clicked_windows"] == 1
    assert visible.calls == [1, 1, 1]
    assert obscured.calls == [1, 1, 1]
    assert fixture.capture.calls == []
    assert fixture.mouse.clicks == [(1, (0.5, 0.5))]


def test_passive_observation_never_temporarily_reveals_obscured_window():
    window = make_window(1)
    visible = FakeCaptureProvider({1: None})
    obscured = FakeCaptureProvider({1: 2})
    fixture = make_controller(
        [1],
        windows=[window],
        expected_windows=1,
        visible_capture_provider=visible,
        obscured_capture_provider=obscured,
        primary_capture_is_trusted=False,
        window_backend=ObscuredWindowBackend([window]),
    )

    observed = fixture.controller.observe_screen_states(
        [window.launch_fingerprint]
    )

    assert observed == {
        window.launch_fingerprint: ReconnectScreenState.UNKNOWN
    }
    assert visible.calls == [1]
    assert obscured.calls == []
    assert fixture.capture.calls == []
    assert fixture.mouse.clicks == []


def test_recent_active_obscured_connected_evidence_allows_deferred_checks():
    window = make_window(1)
    visible = FakeCaptureProvider({1: None})
    obscured = FakeCaptureProvider({1: 1})
    now = [100.0]
    fixture = make_controller(
        [1],
        windows=[window],
        expected_windows=1,
        clock=lambda: now[0],
        visible_capture_provider=visible,
        obscured_capture_provider=obscured,
        primary_capture_is_trusted=False,
        window_backend=ObscuredWindowBackend([window]),
    )

    active = fixture.controller.reconnect()
    now[0] += 1.0
    ready_check = fixture.controller.observe_screen_states(
        [window.launch_fingerprint]
    )
    delivery_check = fixture.controller.observe_screen_states(
        [window.launch_fingerprint]
    )

    assert active.code == "reconnect.connected"
    assert ready_check == {
        window.launch_fingerprint: ReconnectScreenState.CONNECTED
    }
    assert delivery_check == ready_check
    assert obscured.calls == [1]
    assert fixture.capture.calls == []
    assert fixture.mouse.clicks == []


def test_recent_active_minimized_connected_evidence_allows_deferred_checks():
    window = make_window(1, minimized=True)
    active_refresh = FakeCaptureProvider({1: 1})
    now = [100.0]
    fixture = make_controller(
        [1],
        windows=[window],
        expected_windows=1,
        clock=lambda: now[0],
        active_refresh_capture_provider=active_refresh,
    )

    active = fixture.controller.reconnect()
    now[0] += 1.0
    ready_check = fixture.controller.observe_screen_states(
        [window.launch_fingerprint]
    )
    delivery_check = fixture.controller.observe_screen_states(
        [window.launch_fingerprint]
    )

    assert active.code == "reconnect.connected"
    assert ready_check == {
        window.launch_fingerprint: ReconnectScreenState.CONNECTED
    }
    assert delivery_check == ready_check
    assert active_refresh.calls == [1]
    assert fixture.mouse.clicks == []


def test_unknown_passive_observation_revokes_old_connected_evidence():
    window = make_window(1)

    class ToggleVisibilityBackend(FakeWindowBackend):
        obscured = True

        def top_window_at(self, _x, _y):
            return 999999 if self.obscured else self.windows[0].handle

    backend = ToggleVisibilityBackend([window])
    visible = FakeCaptureProvider({1: None})
    obscured = FakeCaptureProvider({1: 1})
    fixture = make_controller(
        [1],
        windows=[window],
        expected_windows=1,
        visible_capture_provider=visible,
        obscured_capture_provider=obscured,
        primary_capture_is_trusted=False,
        window_backend=backend,
    )

    active = fixture.controller.reconnect()
    backend.obscured = False
    failed_visible = fixture.controller.observe_screen_states(
        [window.launch_fingerprint]
    )
    backend.obscured = True
    obscured_again = fixture.controller.observe_screen_states(
        [window.launch_fingerprint]
    )

    assert active.code == "reconnect.connected"
    assert failed_visible == {
        window.launch_fingerprint: ReconnectScreenState.UNKNOWN
    }
    assert obscured_again == {
        window.launch_fingerprint: ReconnectScreenState.UNKNOWN
    }
    assert fixture.controller._trusted_connected_evidence == {}


def test_missing_passive_target_replaces_cached_connected_state_with_unknown():
    window = make_window(1)
    backend = FakeWindowBackend([window])
    fixture = make_controller(
        [1],
        windows=[window],
        expected_windows=1,
        window_backend=backend,
    )
    fixture.controller.reconnect()
    backend.windows = []

    observed = fixture.controller.observe_screen_states(
        [window.launch_fingerprint]
    )

    assert observed == {
        window.launch_fingerprint: ReconnectScreenState.UNKNOWN
    }
    assert fixture.controller.role_screen_states() == observed
    assert fixture.controller._trusted_connected_evidence == {}


def test_replaced_obscured_instance_cannot_reuse_connected_evidence():
    window = make_window(1)
    backend = ObscuredWindowBackend([window])
    visible = FakeCaptureProvider({1: None})
    obscured = FakeCaptureProvider({1: 1})
    now = [100.0]
    fixture = make_controller(
        [1],
        windows=[window],
        expected_windows=1,
        clock=lambda: now[0],
        visible_capture_provider=visible,
        obscured_capture_provider=obscured,
        primary_capture_is_trusted=False,
        window_backend=backend,
    )

    active = fixture.controller.reconnect()
    backend.windows = [
        replace(
            window,
            process_lifecycle_token=window.process_lifecycle_token + 1,
        )
    ]
    now[0] += 1.0
    observed = fixture.controller.observe_screen_states(
        [window.launch_fingerprint]
    )

    assert active.code == "reconnect.connected"
    assert observed == {
        window.launch_fingerprint: ReconnectScreenState.UNKNOWN
    }
    assert obscured.calls == [1]
    assert fixture.mouse.clicks == []


def test_passive_and_active_obscured_disabled_results_are_consistent():
    window = make_window(1)
    visible = FakeCaptureProvider({1: None})
    obscured = FakeCaptureProvider({1: 2})
    fixture = make_controller(
        [2],
        windows=[window],
        expected_windows=1,
        visible_capture_provider=visible,
        obscured_capture_provider=obscured,
        primary_capture_is_trusted=False,
        window_backend=ObscuredWindowBackend([window]),
    )
    fixture.controller.set_capture_settings(
        SmartReconnectCaptureSettings(
            visible=True,
            obscured=False,
            minimized=True,
        )
    )

    observed = fixture.controller.observe_screen_states(
        [window.launch_fingerprint]
    )
    active = fixture.controller.reconnect()

    assert observed == {
        window.launch_fingerprint: ReconnectScreenState.CHECK_DISABLED
    }
    assert active.details["state_counts"] == {"check_disabled": 1}
    assert fixture.controller.role_screen_states() == observed
    assert obscured.calls == []
    assert fixture.capture.calls == []
    assert fixture.mouse.clicks == []


def test_passive_observation_cannot_restore_state_revoked_after_last_snapshot(
    monkeypatch,
):
    window = make_window(1)
    fixture = make_controller(
        [1],
        windows=[window],
        expected_windows=1,
        window_backend=FullyVisibleWindowBackend([window]),
    )
    original_snapshot = fixture.controller._capture_settings_snapshot
    snapshot_calls = 0

    def final_snapshot_then_disable_visible():
        nonlocal snapshot_calls
        snapshot_calls += 1
        snapshot = original_snapshot()
        if snapshot_calls == 2:
            fixture.controller.set_capture_settings(
                SmartReconnectCaptureSettings(
                    visible=False,
                    obscured=True,
                    minimized=True,
                )
            )
        return snapshot

    monkeypatch.setattr(
        fixture.controller,
        "_capture_settings_snapshot",
        final_snapshot_then_disable_visible,
    )

    observed = fixture.controller.observe_screen_states(
        [window.launch_fingerprint]
    )

    assert observed == {
        window.launch_fingerprint: ReconnectScreenState.CHECK_DISABLED
    }
    assert fixture.controller.role_screen_states() == observed
    assert fixture.mouse.clicks == []


def test_force_login_progress_waits_ten_seconds_without_clicking():
    fixture = make_controller([8, 1])

    result = fixture.controller.reconnect()

    assert result.success is False
    assert result.code == "reconnect.waiting"
    assert fixture.mouse.clicks == []
    assert result.details["next_check_seconds"] == 10


def test_force_login_timeout_confirms_yes_then_waits_before_retry():
    fixture = make_controller([9, 1])
    fingerprint = make_window(1).launch_fingerprint
    fixture.controller._pending_reconnect_fingerprints.add(fingerprint)

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [(1, (0.5, 0.57))]
    assert result.details["next_check_seconds"] == 60


def test_manual_force_login_timeout_is_observed_without_clicking():
    fixture = make_controller([9, 1])

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["actionable_windows"] == 0
    assert fixture.mouse.clicks == []


def test_one_unresponsive_window_is_isolated_without_redirecting_other_click():
    mouse = FakeMouseBackend(unresponsive={1})
    fixture = make_controller([2, 2], mouse=mouse)

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.success is True
    assert result.code == "reconnect.progressed_with_isolation"
    assert fixture.mouse.clicks == [(2, (0.5, 0.5))]
    assert result.details["clicked_windows"] == 1
    assert "input_target_unresponsive" in result.details["failure_codes"]


def test_group_identity_failure_aborts_before_capture_or_click():
    windows = [
        make_window(1, fingerprint="a" * 64),
        make_window(2, fingerprint="a" * 64),
    ]
    fixture = make_controller([2, 3], windows=windows)

    result = fixture.controller.reconnect()

    assert result.success is False
    assert result.code == "reconnect.waiting"
    assert fixture.capture.calls == []
    assert fixture.mouse.clicks == []
    assert "fingerprint_missing_or_duplicate" in result.details["failure_codes"]
    assert result.details["validated_windows"] == 0


def test_connected_gameplay_revokes_session_before_later_manual_login():
    fixture = make_controller([2, 1])

    fixture.controller.reconnect()
    fixture.controller.reconnect()
    assert fixture.mouse.clicks == [(1, (0.5, 0.5))]

    complete_with_fresh_connected_frames(fixture)
    assert fixture.controller.reconnecting_fingerprints() == frozenset()

    fixture.capture.states[1] = 3
    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["actionable_windows"] == 0
    assert fixture.mouse.clicks == [(1, (0.5, 0.5))]


def test_same_level_without_unique_role_identity_never_selects_character(
    tmp_path,
):
    windows = [make_window(1), make_window(2)]
    capture = FakeCaptureProvider({1: 5, 2: 1})
    mouse = FakeMouseBackend()

    class LevelOnlyRecognizer:
        def recognize_capture(self, sample):
            if sample.pixels[0] == 1:
                return ScreenRecognition(
                    state=ReconnectScreenState.CONNECTED,
                    score=0.0,
                    click_point=None,
                    reference_name="connected",
                )
            return ScreenRecognition(
                state=ReconnectScreenState.CHARACTER_SELECTION,
                score=0.0,
                click_point=(0.651, 0.706),
                reference_name="character_selection",
                character_level=160,
                character_slot_index=2,
                character_slot_selected=False,
            )

    controller = WindowsSmartReconnectController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=FakeWindowBackend(windows),
        capture_provider=capture,
        primary_capture_is_trusted=True,
        recognizer=LevelOnlyRecognizer(),
        mouse_backend=mouse,
        execution_enabled=True,
    )
    controller.set_group_launch_plan(
        GroupLaunchPlan(
            "160",
            targets=(
                GroupLaunchTarget(
                    1,
                    "160主",
                    tmp_path / "160-main.lnk",
                    windows[0].launch_fingerprint,
                ),
                GroupLaunchTarget(
                    2,
                    "160副",
                    tmp_path / "160-secondary.lnk",
                    windows[1].launch_fingerprint,
                ),
            ),
        )
    )
    controller._pending_reconnect_fingerprints.add(
        windows[0].launch_fingerprint
    )

    controller.reconnect()
    result = controller.reconnect()

    assert result.details["actionable_windows"] == 0
    assert mouse.clicks == []


def test_known_shortcut_role_selects_its_unique_level_instead_of_leftmost(
    tmp_path,
):
    windows = [make_window(1), make_window(2)]
    capture = FakeCaptureProvider({1: 5, 2: 1})
    mouse = FakeMouseBackend()
    candidates = (
        CharacterSelectionCandidate(
            160,
            CharacterImportance.PRIMARY,
            2,
            False,
            (0.651, 0.706),
        ),
        CharacterSelectionCandidate(
            120,
            CharacterImportance.PRIMARY,
            1,
            False,
            (0.500, 0.706),
        ),
    )

    class CandidateRecognizer:
        def recognize_capture(self, sample):
            if sample.pixels[0] == 1:
                return ScreenRecognition(
                    state=ReconnectScreenState.CONNECTED,
                    score=0.0,
                    click_point=None,
                    reference_name="connected",
                )
            return ScreenRecognition(
                state=ReconnectScreenState.CHARACTER_SELECTION,
                score=0.0,
                click_point=candidates[0].click_point,
                reference_name="character_selection",
                character_level=candidates[0].level,
                character_importance=candidates[0].importance,
                character_slot_index=candidates[0].slot_index,
                character_slot_selected=False,
                character_candidates=candidates,
            )

    controller = WindowsSmartReconnectController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=FakeWindowBackend(windows),
        capture_provider=capture,
        primary_capture_is_trusted=True,
        recognizer=CandidateRecognizer(),
        mouse_backend=mouse,
        execution_enabled=True,
    )
    controller.set_group_launch_plan(
        GroupLaunchPlan(
            "混合",
            targets=(
                GroupLaunchTarget(
                    1,
                    "120古",
                    tmp_path / "120古.lnk",
                    windows[0].launch_fingerprint,
                ),
                GroupLaunchTarget(
                    2,
                    "160靈",
                    tmp_path / "160靈.lnk",
                    windows[1].launch_fingerprint,
                ),
            ),
        )
    )
    controller._pending_reconnect_fingerprints.add(
        windows[0].launch_fingerprint
    )

    controller.reconnect()
    result = controller.reconnect()

    assert result.details["clicked_windows"] == 1
    assert mouse.clicks == [(1, (0.500, 0.706))]


def test_user_reported_selection_requires_registered_role_and_retains_match(
    tmp_path,
):
    windows = [make_window(1), make_window(2)]

    class CandidateRecognizer:
        def __init__(self, candidates):
            self.candidates = candidates

        def recognize_capture(self, sample):
            if sample.pixels[0] == 1:
                return ScreenRecognition(
                    state=ReconnectScreenState.CONNECTED,
                    score=0.0,
                    click_point=None,
                    reference_name="connected",
                )
            first = self.candidates[0]
            return ScreenRecognition(
                state=ReconnectScreenState.CHARACTER_SELECTION,
                score=0.0,
                click_point=first.click_point,
                reference_name="character_selection",
                character_level=first.level,
                character_slot_index=first.slot_index,
                character_slot_selected=first.selected,
                character_candidates=self.candidates,
            )

    def controller_for(candidates):
        capture = FakeCaptureProvider({1: 5, 2: 1})
        mouse = FakeMouseBackend()
        controller = WindowsSmartReconnectController(
            expected_windows=2,
            title_keywords=("Adobe Flash Player",),
            window_backend=FakeWindowBackend(windows),
            capture_provider=capture,
            primary_capture_is_trusted=True,
            recognizer=CandidateRecognizer(candidates),
            mouse_backend=mouse,
            execution_enabled=True,
        )
        controller.set_group_launch_plan(
            GroupLaunchPlan(
                "120",
                targets=(
                    GroupLaunchTarget(
                        1,
                        "120古",
                        tmp_path / "120古.lnk",
                        windows[0].launch_fingerprint,
                        registered_level=120,
                    ),
                    GroupLaunchTarget(
                        2,
                        "120靈",
                        tmp_path / "120靈.lnk",
                        windows[1].launch_fingerprint,
                        registered_level=120,
                    ),
                ),
            )
        )
        controller._pending_reconnect_fingerprints.add(
            windows[0].launch_fingerprint
        )
        return controller, mouse

    left_candidates = (
        CharacterSelectionCandidate(
            level=None,
            importance=None,
            slot_index=0,
            selected=True,
            click_point=(0.353, 0.854),
            digit_count=2,
        ),
        CharacterSelectionCandidate(
            level=100,
            importance=None,
            slot_index=1,
            selected=False,
            click_point=(0.500, 0.706),
            digit_count=3,
        ),
        CharacterSelectionCandidate(
            level=None,
            importance=None,
            slot_index=2,
            selected=False,
            click_point=(0.651, 0.706),
            digit_count=2,
        ),
    )
    left, left_mouse = controller_for(left_candidates)
    left.reconnect()
    left_result = left.reconnect()

    assert left_result.details["actionable_windows"] == 0
    assert left_result.details["clicked_windows"] == 0
    assert left_mouse.clicks == []

    right_candidates = (
        CharacterSelectionCandidate(
            level=120,
            importance=None,
            slot_index=0,
            selected=False,
            click_point=(0.355, 0.706),
            digit_count=3,
        ),
        CharacterSelectionCandidate(
            level=None,
            importance=None,
            slot_index=1,
            selected=False,
            click_point=(0.500, 0.706),
            digit_count=2,
        ),
        CharacterSelectionCandidate(
            level=120,
            importance=None,
            slot_index=2,
            selected=True,
            click_point=(0.353, 0.854),
            digit_count=3,
        ),
    )
    right, right_mouse = controller_for(right_candidates)
    right.reconnect()
    right_result = right.reconnect()

    assert right_result.details["clicked_windows"] == 1
    assert right_mouse.clicks == [(1, (0.353, 0.854))]


def test_duplicate_target_level_never_guesses_a_character_slot(tmp_path):
    windows = [make_window(1), make_window(2)]
    capture = FakeCaptureProvider({1: 5, 2: 1})
    mouse = FakeMouseBackend()
    candidates = (
        CharacterSelectionCandidate(
            120,
            CharacterImportance.PRIMARY,
            0,
            False,
            (0.355, 0.706),
        ),
        CharacterSelectionCandidate(
            120,
            CharacterImportance.PRIMARY,
            1,
            False,
            (0.500, 0.706),
        ),
    )

    class AmbiguousRecognizer:
        def recognize_capture(self, sample):
            if sample.pixels[0] == 1:
                return ScreenRecognition(
                    state=ReconnectScreenState.CONNECTED,
                    score=0.0,
                    click_point=None,
                    reference_name="connected",
                )
            return ScreenRecognition(
                state=ReconnectScreenState.CHARACTER_SELECTION,
                score=0.0,
                click_point=candidates[0].click_point,
                reference_name="character_selection",
                character_level=120,
                character_importance=CharacterImportance.PRIMARY,
                character_slot_index=0,
                character_slot_selected=False,
                character_candidates=candidates,
            )

    controller = WindowsSmartReconnectController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=FakeWindowBackend(windows),
        capture_provider=capture,
        primary_capture_is_trusted=True,
        recognizer=AmbiguousRecognizer(),
        mouse_backend=mouse,
        execution_enabled=True,
    )
    controller.set_group_launch_plan(
        GroupLaunchPlan(
            "120",
            targets=(
                GroupLaunchTarget(
                    1,
                    "120古",
                    tmp_path / "120古.lnk",
                    windows[0].launch_fingerprint,
                ),
                GroupLaunchTarget(
                    2,
                    "120靈",
                    tmp_path / "120靈.lnk",
                    windows[1].launch_fingerprint,
                ),
            ),
        )
    )
    controller._pending_reconnect_fingerprints.add(
        windows[0].launch_fingerprint
    )

    controller.reconnect()
    result = controller.reconnect()

    assert result.details["actionable_windows"] == 0
    assert mouse.clicks == []


def test_stop_gate_is_rechecked_after_preflight_before_click(monkeypatch):
    fixture = make_controller([2, 1])
    fixture.controller.reconnect()
    calls = iter((True, False))
    monkeypatch.setattr(
        fixture.controller,
        "_execution_allowed",
        lambda: next(calls, False),
    )

    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []


def test_capture_settings_change_during_scan_revokes_confirmed_click(
    monkeypatch,
):
    fixture = make_controller([2, 1])
    fingerprint = make_window(1).launch_fingerprint
    fixture.controller.reconnect()
    original = fixture.controller._action_is_confirmed

    def confirm_then_change_settings(target, recognition):
        confirmed = original(target, recognition)
        if confirmed:
            fixture.controller.set_capture_settings(
                SmartReconnectCaptureSettings(
                    visible=True,
                    obscured=True,
                    minimized=False,
                )
            )
        return confirmed

    monkeypatch.setattr(
        fixture.controller,
        "_action_is_confirmed",
        confirm_then_change_settings,
    )

    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []
    assert fingerprint not in fixture.controller.reconnecting_fingerprints()
    assert fingerprint not in fixture.controller._action_retry_after
    assert fixture.controller._action_confirmations == {}


def test_capture_settings_change_after_final_snapshot_cannot_republish_session(
    monkeypatch,
):
    fixture = make_controller([2, 1])
    fingerprint = make_window(1).launch_fingerprint
    fixture.controller.reconnect()
    original_snapshot = fixture.controller._capture_settings_snapshot
    snapshot_calls = 0

    def final_snapshot_then_change_settings():
        nonlocal snapshot_calls
        snapshot_calls += 1
        snapshot = original_snapshot()
        if snapshot_calls == fixture.controller.expected_windows + 1:
            fixture.controller.set_capture_settings(
                SmartReconnectCaptureSettings(
                    visible=True,
                    obscured=True,
                    minimized=False,
                )
            )
        return snapshot

    monkeypatch.setattr(
        fixture.controller,
        "_capture_settings_snapshot",
        final_snapshot_then_change_settings,
    )

    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []
    assert fingerprint not in fixture.controller.reconnecting_fingerprints()
    assert fingerprint not in fixture.controller._pending_reconnect_fingerprints
    assert fixture.controller._action_confirmations == {}


def test_capture_settings_change_during_result_build_revokes_returned_status():
    window = make_window(1)
    fixture = make_controller(
        [1],
        windows=[window],
        expected_windows=1,
        window_backend=FullyVisibleWindowBackend([window]),
    )
    original_policy = fixture.controller._policy

    class PolicyHook:
        def __init__(self):
            self.calls = 0

        def __getattr__(self, name):
            return getattr(original_policy, name)

        def decide(self, state):
            self.calls += 1
            if self.calls == 2:
                fixture.controller.set_capture_settings(
                    SmartReconnectCaptureSettings(
                        visible=False,
                        obscured=True,
                        minimized=True,
                    )
                )
            return original_policy.decide(state)

    fixture.controller._policy = PolicyHook()

    result = fixture.controller.reconnect()

    assert result.code == "reconnect.waiting"
    assert result.details["all_connected"] is False
    assert result.details["connected_windows"] == 0
    assert result.details["state_counts"] == {"check_disabled": 1}
    assert fixture.controller.role_screen_states() == {
        window.launch_fingerprint: ReconnectScreenState.CHECK_DISABLED
    }
    assert fixture.controller.state is ReconnectState.FAILED


def test_stop_gate_is_rechecked_immediately_before_battle_restart(
    tmp_path,
    monkeypatch,
):
    windows = [make_window(1), make_window(2)]
    restarter = FakeBattleRestarter()
    plan = GroupLaunchPlan(
        "120",
        targets=(
            GroupLaunchTarget(
                1,
                "120古",
                tmp_path / "first.lnk",
                windows[0].launch_fingerprint,
            ),
            GroupLaunchTarget(
                2,
                "120靈",
                tmp_path / "second.lnk",
                windows[1].launch_fingerprint,
            ),
        ),
    )
    fixture = make_controller(
        [2, 1],
        windows=windows,
        battle_markers={2},
        battle_restarter=restarter,
        group_launch_plan=plan,
    )
    fixture.controller.reconnect()
    calls = iter((True, False))
    monkeypatch.setattr(
        fixture.controller,
        "_execution_allowed",
        lambda: next(calls, False),
    )

    result = fixture.controller.reconnect()

    assert result.details["restarted_windows"] == 0
    assert restarter.calls == []


def test_capture_settings_change_during_scan_revokes_battle_restart(
    tmp_path,
    monkeypatch,
):
    windows = [make_window(1), make_window(2)]
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [2, 1],
        windows=windows,
        battle_markers={2},
        battle_restarter=restarter,
        group_launch_plan=make_group_plan(tmp_path, windows, "120"),
    )
    fingerprint = windows[0].launch_fingerprint
    fixture.controller.reconnect()
    original = fixture.controller._action_is_confirmed

    def confirm_then_change_settings(target, recognition):
        confirmed = original(target, recognition)
        if confirmed:
            fixture.controller.set_capture_settings(
                SmartReconnectCaptureSettings(
                    visible=True,
                    obscured=True,
                    minimized=False,
                )
            )
        return confirmed

    monkeypatch.setattr(
        fixture.controller,
        "_action_is_confirmed",
        confirm_then_change_settings,
    )

    result = fixture.controller.reconnect()

    assert result.details["restarted_windows"] == 0
    assert restarter.calls == []
    assert fingerprint not in fixture.controller._pending_reopen_fingerprints
    assert fingerprint not in fixture.controller._active_automation_fingerprints
    assert fingerprint not in fixture.controller.reconnecting_fingerprints()


def test_stop_gate_is_rechecked_immediately_before_missing_role_reopen(
    tmp_path,
    monkeypatch,
):
    windows = [make_window(1), make_window(2)]
    restarter = FakeBattleRestarter()
    plan = GroupLaunchPlan(
        "120",
        targets=(
            GroupLaunchTarget(
                1,
                "120古",
                tmp_path / "first.lnk",
                windows[0].launch_fingerprint,
            ),
            GroupLaunchTarget(
                2,
                "120靈",
                tmp_path / "second.lnk",
                windows[1].launch_fingerprint,
            ),
        ),
    )
    fixture = make_controller(
        [1],
        windows=[windows[1]],
        battle_restarter=restarter,
        group_launch_plan=plan,
    )
    fingerprint = windows[0].launch_fingerprint
    fixture.controller._pending_reopen_fingerprints.add(fingerprint)
    fixture.controller._reopen_retry_after[fingerprint] = 0.0
    calls = iter((True, False))
    monkeypatch.setattr(
        fixture.controller,
        "_execution_allowed",
        lambda: next(calls, False),
    )

    fixture.controller.reconnect()

    assert restarter.reopen_calls == []


def test_capture_settings_change_revokes_pending_reopen(
    tmp_path,
    monkeypatch,
):
    windows = [make_window(1), make_window(2)]
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [1],
        windows=[windows[1]],
        expected_windows=2,
        battle_restarter=restarter,
        group_launch_plan=make_group_plan(tmp_path, windows, "120"),
    )
    fingerprint = windows[0].launch_fingerprint
    fixture.controller._pending_reopen_fingerprints.add(fingerprint)
    fixture.controller._reopen_retry_after[fingerprint] = 0.0
    changed = [False]

    def change_settings_before_reopen():
        if not changed[0]:
            changed[0] = True
            fixture.controller.set_capture_settings(
                SmartReconnectCaptureSettings(
                    visible=True,
                    obscured=True,
                    minimized=False,
                )
            )
        return True

    monkeypatch.setattr(
        fixture.controller,
        "_execution_allowed",
        change_settings_before_reopen,
    )

    result = fixture.controller.reconnect()

    assert result.details["restarted_windows"] == 0
    assert restarter.reopen_calls == []
    assert fingerprint not in fixture.controller._pending_reopen_fingerprints
    assert fingerprint not in fixture.controller._reopen_retry_after
    assert fingerprint not in fixture.controller.reconnecting_fingerprints()


def test_temporarily_missing_role_clears_only_its_first_frame_confirmation():
    windows = [make_window(1), make_window(2)]
    fixture = make_controller([2, 1], windows=windows)
    fixture.controller.set_allowed_fingerprints(
        {window.launch_fingerprint for window in windows}
    )

    first = fixture.controller.reconnect()
    assert first.details["actionable_windows"] == 0
    fixture.controller._window_backend.windows = [windows[1]]
    missing = fixture.controller.reconnect()
    assert missing.code == "reconnect.waiting"
    assert missing.details["all_connected"] is False
    assert missing.details["source_missing_windows"] == 1
    fixture.controller._window_backend.windows = windows
    restored = fixture.controller.reconnect()

    assert restored.details["actionable_windows"] == 0
    assert fixture.mouse.clicks == []
    confirmed = fixture.controller.reconnect()
    assert confirmed.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [(1, (0.5, 0.5))]


def test_temporarily_missing_role_keeps_its_transition_wait_deadline():
    windows = [make_window(1), make_window(2)]
    current_time = [0.0]
    fixture = make_controller(
        [3, 1],
        windows=windows,
        clock=lambda: current_time[0],
    )
    fingerprint = windows[0].launch_fingerprint
    fixture.controller._pending_reconnect_fingerprints.add(fingerprint)
    fixture.controller._flow_pause_until[fingerprint] = 10.0

    fixture.controller._window_backend.windows = [windows[1]]
    current_time[0] = 2.0
    fixture.controller.reconnect()

    assert fixture.controller._flow_pause_until[fingerprint] == 10.0

    fixture.controller._window_backend.windows = windows
    current_time[0] = 5.0
    assert fixture.controller._action_wait_seconds(
        fingerprint,
        ReconnectScreenState.LOGIN_START,
        current_time[0],
    ) == 5


def test_failed_missing_role_reopen_does_not_block_open_disconnected_role(
    tmp_path,
):
    windows = [make_window(1), make_window(2)]
    plan = GroupLaunchPlan(
        "120",
        targets=(
            GroupLaunchTarget(
                1,
                "120-first",
                tmp_path / "first.lnk",
                windows[0].launch_fingerprint,
            ),
            GroupLaunchTarget(
                2,
                "120-second",
                tmp_path / "second.lnk",
                windows[1].launch_fingerprint,
            ),
        ),
    )
    restarter = FakeBattleRestarter(succeeds=False)
    fixture = make_controller(
        [2],
        windows=[windows[1]],
        expected_windows=2,
        battle_restarter=restarter,
        group_launch_plan=plan,
    )
    fixture.controller._pending_reopen_fingerprints.add(
        windows[0].launch_fingerprint
    )
    fixture.controller._reopen_retry_after[windows[0].launch_fingerprint] = 0.0

    first = fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert first.code == "reconnect.waiting"
    assert result.success is True
    assert result.code == "reconnect.progressed_with_isolation"
    assert "battle_restart_failed" in result.details["failure_codes"]
    assert fixture.capture.calls == [2, 2, 2]
    assert fixture.mouse.clicks == [(2, (0.5, 0.5))]


def test_pending_missing_reopen_target_never_reports_all_connected(tmp_path):
    now = [0.0]
    windows = [make_window(1), make_window(2)]
    plan = GroupLaunchPlan(
        "120",
        targets=(
            GroupLaunchTarget(
                1,
                "120-first",
                tmp_path / "first.lnk",
                windows[0].launch_fingerprint,
            ),
            GroupLaunchTarget(
                2,
                "120-second",
                tmp_path / "second.lnk",
                windows[1].launch_fingerprint,
            ),
        ),
    )
    fixture = make_controller(
        [1],
        windows=[windows[1]],
        expected_windows=2,
        clock=lambda: now[0],
        battle_restarter=FakeBattleRestarter(),
        group_launch_plan=plan,
    )
    missing = windows[0].launch_fingerprint
    fixture.controller._pending_reopen_fingerprints.add(missing)
    fixture.controller._reopen_retry_after[missing] = 30.0

    result = fixture.controller.reconnect()

    assert result.success is False
    assert result.code == "reconnect.waiting"
    assert result.details["all_connected"] is False
    assert result.details["connected_windows"] == 1
    assert result.details["next_check_seconds"] == 30
    assert "reconnect_target_missing" in result.details["failure_codes"]
    assert fixture.mouse.clicks == []


def test_pending_reopen_keeps_incomplete_appeared_instance(tmp_path):
    windows = [make_window(1), make_window(2)]
    incomplete = replace(windows[0], thread_id=0)
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [1, 1],
        windows=[incomplete, windows[1]],
        expected_windows=2,
        battle_restarter=restarter,
        group_launch_plan=make_group_plan(tmp_path, windows, "120"),
    )
    missing = windows[0].launch_fingerprint
    fixture.controller._pending_reopen_fingerprints.add(missing)
    fixture.controller._reopen_retry_after[missing] = 0.0

    result = fixture.controller.reconnect()

    assert result.code == "reconnect.waiting"
    assert "window_instance_incomplete" in result.details["failure_codes"]
    assert missing in fixture.controller._pending_reopen_fingerprints
    assert fixture.controller._reopen_retry_after[missing] == 0.0
    assert restarter.reopen_calls == []
    assert fixture.capture.calls == []
    assert fixture.mouse.clicks == []


def test_pending_reopen_requires_unique_complete_instance_to_clear(tmp_path):
    windows = [make_window(1), make_window(2)]
    incomplete_same_identity = replace(windows[0], handle=3, thread_id=0)
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [1, 1, 1],
        windows=[windows[0], incomplete_same_identity, windows[1]],
        expected_windows=2,
        battle_restarter=restarter,
        group_launch_plan=make_group_plan(tmp_path, windows, "120"),
    )
    missing = windows[0].launch_fingerprint
    fixture.controller._pending_reopen_fingerprints.add(missing)
    fixture.controller._reopen_retry_after[missing] = 0.0

    result = fixture.controller.reconnect()

    assert result.code == "reconnect.waiting"
    assert "window_instance_incomplete" in result.details["failure_codes"]
    assert missing in fixture.controller._pending_reopen_fingerprints
    assert fixture.controller._reopen_retry_after[missing] == 0.0
    assert restarter.reopen_calls == []
    assert fixture.mouse.clicks == []


def test_pending_reopen_requires_non_duplicate_complete_instance_to_clear(
    tmp_path,
):
    windows = [make_window(1), make_window(2)]
    duplicate_same_identity = replace(windows[0], handle=3, process_id=103)
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [1, 1, 1],
        windows=[windows[0], duplicate_same_identity, windows[1]],
        expected_windows=2,
        battle_restarter=restarter,
        group_launch_plan=make_group_plan(tmp_path, windows, "120"),
    )
    missing = windows[0].launch_fingerprint
    fixture.controller._pending_reopen_fingerprints.add(missing)
    fixture.controller._reopen_retry_after[missing] = 0.0

    result = fixture.controller.reconnect()

    assert result.code == "reconnect.waiting"
    assert "fingerprint_missing_or_duplicate" in (
        result.details["failure_codes"]
    )
    assert missing in fixture.controller._pending_reopen_fingerprints
    assert fixture.controller._reopen_retry_after[missing] == 0.0
    assert restarter.reopen_calls == []
    assert fixture.mouse.clicks == []


def test_duplicate_identity_never_triggers_missing_role_reopen(tmp_path):
    now = [0.0]
    windows = [make_window(1), make_window(2)]
    plan = GroupLaunchPlan(
        "120",
        targets=(
            GroupLaunchTarget(
                1,
                "120-first",
                tmp_path / "first.lnk",
                windows[0].launch_fingerprint,
            ),
            GroupLaunchTarget(
                2,
                "120-second",
                tmp_path / "second.lnk",
                windows[1].launch_fingerprint,
            ),
        ),
    )
    restarter = FakeBattleRestarter()
    blocked = windows[0].launch_fingerprint
    fixture = make_controller(
        [1],
        windows=[windows[1]],
        expected_windows=2,
        clock=lambda: now[0],
        battle_restarter=restarter,
        group_launch_plan=plan,
        target_windows_provider=lambda: ResolvedTargetWindows(
            (windows[1],),
            ("window_identity_duplicate",),
            frozenset({blocked}),
        ),
    )
    fixture.controller._pending_reopen_fingerprints.add(blocked)
    fixture.controller._reopen_retry_after[blocked] = 0.0

    result = fixture.controller.reconnect()
    fixture.controller._report_reconnect_failure(blocked)

    assert result.code == "reconnect.waiting"
    assert "battle_reopen_identity_unsafe" in result.details["failure_codes"]
    assert restarter.calls == []
    assert restarter.reopen_calls == []
    assert fixture.mouse.clicks == []


def test_switching_group_revokes_old_sessions_and_monitors_open_new_role(
    tmp_path,
):
    first_group = [make_window(1), make_window(2)]
    second_group = [make_window(3), make_window(4)]
    first_plan = GroupLaunchPlan(
        "first",
        targets=tuple(
            GroupLaunchTarget(
                index,
                f"first-{index}",
                tmp_path / f"first-{index}.lnk",
                window.launch_fingerprint,
            )
            for index, window in enumerate(first_group, start=1)
        ),
    )
    second_plan = GroupLaunchPlan(
        "second",
        targets=tuple(
            GroupLaunchTarget(
                index,
                f"second-{index}",
                tmp_path / f"second-{index}.lnk",
                window.launch_fingerprint,
            )
            for index, window in enumerate(second_group, start=1)
        ),
    )
    fixture = make_controller(
        [2, 1],
        windows=first_group,
        expected_windows=2,
        group_launch_plan=first_plan,
    )
    fixture.controller._pending_reconnect_fingerprints.add(
        first_group[0].launch_fingerprint
    )
    fixture.controller._active_automation_fingerprints.add(
        first_group[0].launch_fingerprint
    )
    fixture.controller._window_backend.windows = [second_group[0]]
    fixture.capture.states[second_group[0].handle] = 2

    fixture.controller.set_group_launch_plan(second_plan)
    first = fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert first.code == "reconnect.waiting"
    assert result.code == "reconnect.progressed"
    assert fixture.controller.reconnecting_fingerprints() == frozenset(
        {second_group[0].launch_fingerprint}
    )
    assert fixture.capture.calls == [3, 3, 3]
    assert fixture.mouse.clicks == [(3, (0.5, 0.5))]


def test_restart_with_different_group_revokes_shared_role_authority(
    tmp_path,
):
    state_path = tmp_path / "smart_reconnect_state.json"
    now = [1000.0]
    windows = [make_window(1), make_window(2)]
    first = make_controller(
        [2, 1],
        windows=windows,
        expected_windows=2,
        clock=lambda: now[0],
        state_path=state_path,
        group_launch_plan=make_group_plan(tmp_path, windows, "first"),
    )
    fingerprint = windows[0].launch_fingerprint
    first.controller.reconnect()
    now[0] = 1005.0
    first.controller.reconnect()

    assert first.controller.reconnecting_fingerprints() == frozenset(
        {fingerprint}
    )
    assert first.mouse.clicks == [(1, (0.5, 0.5))]

    now[0] = 1006.0
    second = make_controller(
        [3, 1],
        windows=windows,
        expected_windows=2,
        clock=lambda: now[0],
        state_path=state_path,
        group_launch_plan=make_group_plan(tmp_path, windows, "second"),
    )
    second.controller.reconnect()
    result = second.controller.reconnect()

    assert result.code == "reconnect.waiting"
    assert result.details["actionable_windows"] == 0
    assert second.controller.reconnecting_fingerprints() == frozenset()
    assert second.controller._action_confirmations == {}
    assert second.mouse.clicks == []
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    second_plan = make_group_plan(tmp_path, windows, "second")
    assert persisted["scope_token"] == second.controller._group_scope_token(
        second_plan
    )
    assert persisted["pending_fingerprints"] == []
    assert persisted["active_fingerprints"] == []
    assert persisted["flow_pause_until"] == {}


def test_runtime_state_writes_serialize_revocation_after_older_snapshot(
    tmp_path,
):
    state_path = tmp_path / "smart_reconnect_state.json"
    windows = [make_window(1), make_window(2)]
    first_plan = make_group_plan(tmp_path, windows, "first")
    second_plan = make_group_plan(tmp_path, windows, "second")
    fixture = make_controller(
        [1, 1],
        windows=windows,
        state_path=state_path,
        group_launch_plan=first_plan,
    )
    fingerprint = windows[0].launch_fingerprint
    fixture.controller._pending_reconnect_fingerprints.add(fingerprint)
    fixture.controller._publish_reconnecting_fingerprints()

    store = fixture.controller._runtime_state_store
    real_save = store.save
    old_snapshot_entered = threading.Event()
    release_old_snapshot = threading.Event()
    save_count_lock = threading.Lock()
    save_count = 0

    def blocking_first_save(state):
        nonlocal save_count
        with save_count_lock:
            save_count += 1
            current = save_count
        if current == 1:
            old_snapshot_entered.set()
            release_old_snapshot.wait(2)
        return real_save(state)

    store.save = blocking_first_save
    old_results = []
    old_writer = threading.Thread(
        target=lambda: old_results.append(
            fixture.controller._persist_runtime_state()
        )
    )
    revoker = threading.Thread(
        target=lambda: fixture.controller.set_group_launch_plan(
            second_plan
        )
    )

    old_writer.start()
    assert old_snapshot_entered.wait(1) is True
    revoker.start()
    deadline = time.monotonic() + 1
    while (
        fixture.controller.reconnecting_fingerprints()
        and time.monotonic() < deadline
    ):
        time.sleep(0.005)
    in_memory_revoked = (
        fixture.controller.reconnecting_fingerprints() == frozenset()
    )
    release_old_snapshot.set()
    old_writer.join(1)
    revoker.join(1)

    assert in_memory_revoked is True
    assert old_writer.is_alive() is False
    assert revoker.is_alive() is False
    assert old_results == [True]
    restored = ReconnectRuntimeStateStore(state_path).load()
    assert restored.pending_fingerprints == set()
    assert restored.active_fingerprints == set()
    assert restored.scope_token == fixture.controller._group_scope_token(
        second_plan
    )


def test_active_operation_gate_does_not_block_connected_read_scan():
    gate = GameOperationGate()
    fixture = make_controller([1, 1], operation_gate=gate)
    active_sync = gate.acquire("keyboard-sync")
    results = []
    completed = threading.Event()

    def run_connected_scan():
        results.append(fixture.controller.reconnect())
        completed.set()

    worker = threading.Thread(target=run_connected_scan)
    worker.start()
    finished_while_gate_active = completed.wait(0.5)
    active_sync.release()
    worker.join(1)

    assert finished_while_gate_active is True
    assert worker.is_alive() is False
    assert results[0].code == "reconnect.connected"
    assert fixture.capture.calls == [1, 2]


def test_reconnecting_snapshot_does_not_wait_for_long_scan_lock():
    fixture = make_controller([1, 1])
    lock_held = threading.Event()
    release_lock = threading.Event()

    def hold_scan_lock():
        with fixture.controller._scan_lock:
            lock_held.set()
            release_lock.wait(2)

    worker = threading.Thread(target=hold_scan_lock)
    worker.start()
    assert lock_held.wait(1) is True

    started = time.monotonic()
    snapshot = fixture.controller.reconnecting_fingerprints()
    elapsed = time.monotonic() - started
    release_lock.set()
    worker.join(1)

    assert snapshot == frozenset()
    assert elapsed < 0.1
    assert worker.is_alive() is False


def test_busy_operation_gate_does_not_charge_retry_or_deliver_click():
    gate = GameOperationGate()
    fixture = make_controller([2, 1], operation_gate=gate)
    fingerprint = make_window(1).launch_fingerprint
    active_sync = gate.acquire("keyboard-sync")

    fixture.controller.reconnect()
    busy = fixture.controller.reconnect()

    assert busy.details["clicked_windows"] == 0
    assert fingerprint not in fixture.controller._action_retry_after
    assert fixture.mouse.clicks == []

    active_sync.release()
    fixture.controller.reconnect()
    recovered = fixture.controller.reconnect()

    assert recovered.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [(1, (0.5, 0.5))]


def test_window_preflight_failure_clears_double_frame_confirmation():
    mouse = FakeMouseBackend(invalid={1})
    fixture = make_controller([2, 1], mouse=mouse)

    fixture.controller.reconnect()
    attempted = fixture.controller.reconnect()
    assert "input_target_invalid" in attempted.details["failure_codes"]
    mouse.invalid.clear()
    restored = fixture.controller.reconnect()

    assert restored.details["actionable_windows"] == 0
    assert fixture.mouse.clicks == []


def test_click_preflight_rejects_reused_handle_with_different_process():
    windows = [make_window(1), make_window(2)]
    backend = FakeWindowBackend(windows)
    current_time = [-5.0]

    def clock():
        current_time[0] += 5.0
        return current_time[0]

    class ReusingCapture(FakeCaptureProvider):
        def capture(self, handle):
            sample = super().capture(handle)
            if handle == 1 and self.calls.count(1) == 2:
                backend.windows = [
                    make_window(
                        1,
                        process_id=9999,
                        fingerprint=windows[0].launch_fingerprint,
                    ),
                    windows[1],
                ]
            return sample

    capture = ReusingCapture({1: 2, 2: 1})
    mouse = FakeMouseBackend()
    controller = WindowsSmartReconnectController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=backend,
        capture_provider=capture,
        primary_capture_is_trusted=True,
        recognizer=FakeRecognizer(
            {
                1: ReconnectScreenState.CONNECTED,
                2: ReconnectScreenState.DISCONNECTED,
            },
            points={2: (0.5, 0.5)},
        ),
        mouse_backend=mouse,
        monotonic_clock=clock,
        execution_enabled=True,
    )

    controller.reconnect()
    result = controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert "input_target_changed_before_delivery" in (
        result.details["failure_codes"]
    )
    assert mouse.clicks == []


@pytest.mark.parametrize(
    "window",
    [
        make_window(1, thread_id=0),
        make_window(1, process_lifecycle_token=0),
    ],
)
def test_click_preflight_requires_complete_instance_token(window):
    windows = [window, make_window(2)]
    fixture = make_controller([2, 1], windows=windows)

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert "window_instance_incomplete" in (
        result.details["failure_codes"]
    )
    assert fixture.capture.calls == []
    assert fixture.mouse.clicks == []


def test_click_delivery_rechecks_identity_after_fresh_recapture():
    windows = [make_window(1), make_window(2)]
    backend = FakeWindowBackend(windows)
    current_time = [-5.0]

    def clock():
        current_time[0] += 5.0
        return current_time[0]

    class ReusingAfterFreshCapture(FakeCaptureProvider):
        def capture(self, handle):
            sample = super().capture(handle)
            if handle == 1 and self.calls.count(1) == 3:
                backend.windows = [
                    make_window(
                        1,
                        process_id=9999,
                        fingerprint=windows[0].launch_fingerprint,
                    ),
                    windows[1],
                ]
            return sample

    capture = ReusingAfterFreshCapture({1: 2, 2: 1})
    mouse = FakeMouseBackend()
    controller = WindowsSmartReconnectController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=backend,
        capture_provider=capture,
        primary_capture_is_trusted=True,
        recognizer=FakeRecognizer(
            {
                1: ReconnectScreenState.CONNECTED,
                2: ReconnectScreenState.DISCONNECTED,
            },
            points={2: (0.5, 0.5)},
        ),
        mouse_backend=mouse,
        monotonic_clock=clock,
        execution_enabled=True,
    )

    controller.reconnect()
    result = controller.reconnect()

    assert capture.calls.count(1) == 3
    assert result.details["clicked_windows"] == 0
    assert "input_target_changed_before_delivery" in (
        result.details["failure_codes"]
    )
    assert mouse.clicks == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("thread_id", 9999),
        ("window_class", "ReplacementWindow"),
        ("rect", (20, 30, 920, 630)),
        ("minimized", True),
        ("process_lifecycle_token", 999999),
    ],
)
def test_click_delivery_binds_fresh_recognition_to_full_instance_token(
    field,
    value,
):
    windows = [make_window(1), make_window(2)]
    backend = FakeWindowBackend(windows)

    class RebuildingAfterFreshCapture(FakeCaptureProvider):
        def capture(self, handle):
            sample = super().capture(handle)
            if handle == 1 and self.calls.count(1) == 3:
                backend.windows = [
                    replace(windows[0], **{field: value}),
                    windows[1],
                ]
            return sample

    capture = RebuildingAfterFreshCapture({1: 2, 2: 1})
    mouse = FakeMouseBackend()
    controller = WindowsSmartReconnectController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=backend,
        capture_provider=capture,
        primary_capture_is_trusted=True,
        recognizer=FakeRecognizer(
            {
                1: ReconnectScreenState.CONNECTED,
                2: ReconnectScreenState.DISCONNECTED,
            },
            points={2: (0.5, 0.5)},
        ),
        mouse_backend=mouse,
        monotonic_clock=iter((0.0, 5.0, 10.0, 15.0)).__next__,
        execution_enabled=True,
    )

    controller.reconnect()
    result = controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert "input_target_changed_before_delivery" in (
        result.details["failure_codes"]
    )
    assert mouse.clicks == []


def test_click_preflight_rejects_screen_that_changed_after_scan():
    windows = [make_window(1), make_window(2)]
    current_time = [-5.0]

    def clock():
        current_time[0] += 5.0
        return current_time[0]

    class TransitioningCapture:
        def __init__(self):
            self.calls = []
            self.first_markers = iter((2, 2, 3))

        def capture(self, handle):
            self.calls.append(handle)
            marker = next(self.first_markers) if handle == 1 else 1
            return CaptureSample(
                width=2,
                height=2,
                pixels=bytes([marker, 0, 0, 255] * 4),
                api_succeeded=True,
            )

    capture = TransitioningCapture()
    mouse = FakeMouseBackend()
    controller = WindowsSmartReconnectController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=FakeWindowBackend(windows),
        capture_provider=capture,
        primary_capture_is_trusted=True,
        recognizer=FakeRecognizer(
            {
                1: ReconnectScreenState.CONNECTED,
                2: ReconnectScreenState.DISCONNECTED,
                3: ReconnectScreenState.LOGIN_START,
            },
            points={2: (0.5, 0.5), 3: (0.5, 0.8)},
        ),
        mouse_backend=mouse,
        monotonic_clock=clock,
        execution_enabled=True,
    )

    controller.reconnect()
    result = controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert "input_target_changed_before_delivery" in (
        result.details["failure_codes"]
    )
    assert mouse.clicks == []


def test_transition_wait_starts_when_click_actually_succeeds():
    current_time = [0.0]
    windows = [make_window(1), make_window(2)]

    class TimestampMouse(FakeMouseBackend):
        def click_relative(
            self,
            handle,
            point,
            expected_process_id,
            instance_token,
        ):
            current_time[0] = 100.0
            return super().click_relative(
                handle,
                point,
                expected_process_id,
                instance_token,
            )

    mouse = TimestampMouse()
    fixture = make_controller(
        [2, 1],
        windows=windows,
        clock=lambda: current_time[0],
        mouse=mouse,
    )
    fingerprint = windows[0].launch_fingerprint

    fixture.controller.reconnect()
    current_time[0] = 5.0
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 1
    assert fixture.controller._flow_pause_until[fingerprint] == 110.0
    assert fixture.controller._action_wait_seconds(
        fingerprint,
        ReconnectScreenState.LOGIN_START,
        100.0,
    ) == 10


def test_delivered_click_with_restore_failure_advances_without_reclicking():
    current_time = [100.0]
    windows = [make_window(1), make_window(2)]
    mouse = FakeMouseBackend(
        click_results=(
            MouseClickResult(
                True,
                False,
                False,
                None,
            ),
        )
    )
    fixture = make_controller(
        [2, 1],
        windows=windows,
        clock=lambda: current_time[0],
        mouse=mouse,
    )
    fingerprint = windows[0].launch_fingerprint

    fixture.controller.reconnect()
    current_time[0] = 105.0
    delivered = fixture.controller.reconnect()

    assert delivered.details["clicked_windows"] == 1
    assert "input_window_restore_failed" in (
        delivered.details["failure_codes"]
    )
    assert "click_delivery_failed" not in delivered.details["failure_codes"]
    assert fingerprint not in fixture.controller._action_confirmations
    assert fixture.controller._flow_pause_until[fingerprint] > current_time[0]

    immediate = fixture.controller.reconnect()

    assert immediate.details["clicked_windows"] == 0
    assert mouse.clicks == [(1, (0.5, 0.5))]


@pytest.mark.parametrize(
    "failure_code",
    [
        "mouse_down_delivery_uncertain",
        "mouse_up_delivery_uncertain",
    ],
)
def test_uncertain_delivery_clears_confirmation_and_never_retries_immediately(
    failure_code,
):
    current_time = [100.0]
    windows = [make_window(1), make_window(2)]
    mouse = FakeMouseBackend(
        click_results=(
            MouseClickResult(
                False,
                True,
                True,
                failure_code,
            ),
        )
    )
    fixture = make_controller(
        [2, 1],
        windows=windows,
        clock=lambda: current_time[0],
        mouse=mouse,
    )
    fingerprint = windows[0].launch_fingerprint

    fixture.controller.reconnect()
    current_time[0] = 105.0
    uncertain = fixture.controller.reconnect()

    assert uncertain.details["clicked_windows"] == 0
    assert failure_code in uncertain.details["failure_codes"]
    assert "click_delivery_uncertain" in uncertain.details["failure_codes"]
    assert "click_delivery_failed" not in uncertain.details["failure_codes"]
    assert fingerprint not in fixture.controller._action_confirmations

    immediate = fixture.controller.reconnect()

    assert immediate.details["clicked_windows"] == 0
    assert mouse.clicks == [(1, (0.5, 0.5))]


def test_delivered_click_keeps_reclick_guard_when_capture_settings_change():
    current_time = [100.0]
    windows = [make_window(1), make_window(2)]

    class SettingsChangingMouse(FakeMouseBackend):
        controller = None

        def click_relative(
            self,
            handle,
            point,
            expected_process_id,
            instance_token,
        ):
            result = super().click_relative(
                handle,
                point,
                expected_process_id,
                instance_token,
            )
            self.controller.set_capture_settings(
                SmartReconnectCaptureSettings(
                    visible=True,
                    obscured=True,
                    minimized=False,
                )
            )
            return result

    mouse = SettingsChangingMouse(
        click_results=(
            MouseClickResult(True, False, False, None),
        )
    )
    fixture = make_controller(
        [2, 1],
        windows=windows,
        clock=lambda: current_time[0],
        mouse=mouse,
    )
    mouse.controller = fixture.controller
    fingerprint = windows[0].launch_fingerprint

    fixture.controller.reconnect()
    current_time[0] = 105.0
    delivered = fixture.controller.reconnect()

    assert delivered.details["clicked_windows"] == 1
    assert "input_window_restore_failed" in (
        delivered.details["failure_codes"]
    )
    assert fingerprint in fixture.controller._action_retry_after
    assert fixture.controller._flow_pause_until[fingerprint] > current_time[0]
    assert fingerprint in fixture.controller.reconnecting_fingerprints()
    assert fingerprint not in (
        fixture.controller._active_automation_fingerprints
    )

    immediate = fixture.controller.reconnect()

    assert immediate.details["clicked_windows"] == 0
    assert mouse.clicks == [(1, (0.5, 0.5))]


def test_unknown_screen_never_clicks_and_uses_one_minute_retry():
    fixture = make_controller([1, 255])

    result = fixture.controller.reconnect()

    assert result.success is False
    assert result.code == "reconnect.waiting"
    assert fixture.mouse.clicks == []
    assert result.details["unknown_windows"] == 1
    assert result.details["next_check_seconds"] == 60
    assert "screen_unknown" in result.details["failure_codes"]


def test_unknown_sibling_does_not_block_confirmed_disconnected_window():
    fixture = make_controller([2, 255])

    first = fixture.controller.reconnect()
    second = fixture.controller.reconnect()

    assert first.code == "reconnect.waiting"
    assert second.code == "reconnect.progressed_with_isolation"
    assert fixture.mouse.clicks == [(1, (0.5, 0.5))]
    assert second.details["actionable_windows"] == 1
    assert "screen_unknown" in second.details["failure_codes"]
    assert fixture.controller.reconnecting_fingerprints() == frozenset(
        {make_window(1).launch_fingerprint}
    )


def test_failed_sibling_capture_does_not_block_confirmed_disconnected_window():
    fixture = make_controller([2, 1])
    fixture.capture.states[2] = None

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert fixture.mouse.clicks == [(1, (0.5, 0.5))]
    assert result.details["actionable_windows"] == 1
    assert "capture_failed" in result.details["failure_codes"]
    assert fixture.controller.reconnecting_fingerprints() == frozenset(
        {make_window(1).launch_fingerprint}
    )


def test_real_mixed_fourteen_window_shape_only_recovers_confirmed_disconnects():
    windows = [make_window(handle) for handle in range(1, 15)]
    fixture = make_controller(
        [
            2,
            2,
            3,
            3,
            1,
            255,
            255,
            255,
            255,
            255,
            255,
            255,
            255,
            255,
        ],
        windows=windows,
        expected_windows=14,
    )

    first = fixture.controller.reconnect()
    second = fixture.controller.reconnect()

    assert first.details["actionable_windows"] == 0
    assert second.code == "reconnect.progressed_with_isolation"
    assert second.details["state_counts"] == {
        "connected": 1,
        "disconnected": 2,
        "login_start": 2,
        "unknown": 9,
    }
    assert second.details["actionable_windows"] == 2
    assert second.details["clicked_windows"] == 2
    assert fixture.mouse.clicks == [
        (1, (0.5, 0.5)),
        (2, (0.5, 0.5)),
    ]


def test_minimized_active_reconnect_uses_refresh_capture_provider():
    windows = [make_window(1, minimized=True), make_window(2)]
    active_refresh = FakeCaptureProvider({1: 2})
    fixture = make_controller(
        [2, 1],
        windows=windows,
        active_refresh_capture_provider=active_refresh,
    )

    passive = fixture.controller.check_connection()

    assert passive.details["actionable_windows"] == 0
    assert active_refresh.calls == []

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 1
    assert active_refresh.calls == [1, 1, 1]
    assert fixture.mouse.clicks == [(1, (0.5, 0.5))]


def test_disabled_minimized_check_never_captures_clicks_or_reports_online():
    window = make_window(1, minimized=True)
    active_refresh = FakeCaptureProvider({1: 1})
    fixture = make_controller(
        [1],
        windows=[window],
        expected_windows=1,
        active_refresh_capture_provider=active_refresh,
    )
    fixture.controller.set_capture_settings(
        SmartReconnectCaptureSettings(
            visible=True,
            obscured=True,
            minimized=False,
        )
    )

    result = fixture.controller.reconnect()

    assert active_refresh.calls == []
    assert fixture.capture.calls == []
    assert result.details["connected_windows"] == 0
    assert result.details["unknown_windows"] == 1
    assert result.details["state_counts"] == {"check_disabled": 1}
    assert result.details["all_connected"] is False
    assert fixture.mouse.clicks == []


def test_minimized_stale_connected_frame_is_refreshed_before_decision():
    windows = [make_window(1, minimized=True), make_window(2)]
    active_refresh = FakeCaptureProvider({1: 2})
    fixture = make_controller(
        [1, 1],
        windows=windows,
        active_refresh_capture_provider=active_refresh,
    )

    passive = fixture.controller.check_connection()

    assert passive.details["unknown_windows"] == 1
    assert passive.details["all_connected"] is False
    assert active_refresh.calls == []

    first = fixture.controller.reconnect()
    second = fixture.controller.reconnect()

    assert first.details["clicked_windows"] == 0
    assert second.details["clicked_windows"] == 1
    assert active_refresh.calls == [1, 1, 1]
    assert fixture.mouse.clicks == [(1, (0.5, 0.5))]


def test_minimized_failed_refresh_never_trusts_stale_connected_frame():
    windows = [make_window(1, minimized=True), make_window(2)]
    active_refresh = FakeCaptureProvider({1: None})
    fixture = make_controller(
        [1, 1],
        windows=windows,
        active_refresh_capture_provider=active_refresh,
    )

    result = fixture.controller.reconnect()

    assert result.details["unknown_windows"] == 1
    assert result.details["all_connected"] is False
    assert result.details["clicked_windows"] == 0
    assert active_refresh.calls == [1]
    assert fixture.mouse.clicks == []


def test_observed_state_replaces_nonfresh_minimized_frame_with_unknown():
    window = make_window(1, minimized=True)
    fixture = make_controller([1], windows=[window], expected_windows=1)

    observed = fixture.controller.observe_screen_states(
        [window.launch_fingerprint]
    )

    assert observed == {
        window.launch_fingerprint: ReconnectScreenState.UNKNOWN
    }
    assert fixture.controller.role_screen_states() == {
        window.launch_fingerprint: ReconnectScreenState.UNKNOWN
    }


def test_all_connected_is_reported_without_sending_input():
    fixture = make_controller([1, 1])

    result = fixture.controller.reconnect()

    assert result.success is True
    assert result.code == "reconnect.connected"
    assert fixture.controller.state is ReconnectState.CONNECTED
    assert fixture.mouse.clicks == []
    assert result.details["all_connected"] is True
    assert result.details["next_check_seconds"] == 5


def test_safe_report_never_contains_identifiers_pixels_or_coordinates():
    fixture = make_controller([2, 3])

    payload = fixture.controller.reconnect().details
    serialized = repr(payload)

    assert "handle" not in serialized
    assert "process_id" not in serialized
    assert "launch_fingerprint" not in serialized
    assert not any(isinstance(value, bytes) for value in payload.values())
    assert "(0.5, 0.5)" not in serialized
    assert payload["raw_arguments_emitted"] is False
    assert payload["fingerprints_emitted"] is False
    assert payload["captured_pixels_persisted"] is False
    assert payload["click_coordinates_emitted"] is False
