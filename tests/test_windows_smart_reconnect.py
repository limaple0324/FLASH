import ctypes
import hashlib
import json
import threading
import time
from dataclasses import dataclass, replace
from ctypes import wintypes
from pathlib import Path

import pytest
from PIL import Image

from adapters.game_screen_recognizer import (
    CHARACTER_ENTER_CLICK_POINT,
    CharacterSelectionCandidate,
    POST_DISCONNECT_WAITING_REFERENCE_FILE,
    ReferenceScreenRecognizer,
    ScreenRecognition,
)
from adapters.windows_auto_battle import AutoBattleEvidence
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
from services.target_window_contract_service import (
    ResolvedTargetWindows,
    TargetFailureEvidence,
)


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
        line_selection = state is ReconnectScreenState.LINE_SELECTION
        return ScreenRecognition(
            state=state,
            score=0.0 if state is not ReconnectScreenState.UNKNOWN else None,
            click_point=self.points.get(marker),
            reference_name=state.value,
            battle_context=marker in self.battle_markers,
            line_number=1 if line_selection else None,
            recent_line_present=False if line_selection else None,
        )


class RecognitionByMarker:
    def __init__(self, recognitions):
        self.recognitions = dict(recognitions)
        self.calls = []

    def recognize_capture(self, sample):
        marker = sample.pixels[0] if sample is not None else 255
        self.calls.append(marker)
        return self.recognitions.get(
            marker,
            ScreenRecognition(
                ReconnectScreenState.UNKNOWN,
                None,
                None,
                None,
            ),
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
        self.scrolls = []
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

    def scroll_relative(
        self,
        handle,
        point,
        delta,
        expected_process_id,
        instance_token,
    ):
        self.scrolls.append((handle, point, delta))
        self.expected_process_ids.append(expected_process_id)
        self.instance_tokens.append(instance_token)
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
        self.ClientToScreen = FakeWin32Function(self._client_to_screen)
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

    def _client_to_screen(self, handle, pointer):
        rect = self.rects.get(win32_handle_value(handle))
        if rect is None:
            return False
        point = pointer._obj
        point.x += rect[0]
        point.y += rect[1]
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
        self.process_handle_to_id = {
            self.process_handle: user32.expected_process_id,
        }
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
        if process_id not in self.user32.process_ids.values():
            return 0
        process_handle = (
            self.process_handle
            if process_id == self.user32.expected_process_id
            else 10000 + int(process_id)
        )
        self.process_handle_to_id[process_handle] = int(process_id)
        return process_handle

    def _get_process_times(
        self,
        process_handle,
        created_pointer,
        exited_pointer,
        kernel_pointer,
        user_pointer,
    ):
        process_id = self.process_handle_to_id.get(process_handle)
        if process_id is None:
            return False
        token = (
            self.user32.process_lifecycle_token
            if process_id == self.user32.expected_process_id
            else (int(process_id) * 1000000) + 321
        )
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
        process_id = self.process_handle_to_id.get(process_handle)
        if (
            process_id is not None
            and process_id in self.user32.process_ids.values()
            and (
                process_id != self.user32.expected_process_id
                or self.process_alive
            )
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


@pytest.mark.parametrize("preserved_edge", ["previous", "next"])
def test_win32_mouse_minimized_restore_accepts_one_exact_original_edge(
    monkeypatch,
    preserved_edge,
):
    api = FakeWin32MouseApi(minimized=True)
    original_position = api.SetWindowPos.callback

    def restore_then_mirror(*args):
        result = original_position(*args)
        if not result:
            return result
        if preserved_edge == "previous":
            api.z_order.remove(400)
            api.z_order.insert(1, 400)
        else:
            api.z_order.remove(300)
            api.z_order.insert(0, 300)
        return result

    api.SetWindowPos = FakeWin32Function(restore_then_mirror)
    backend = win32_mouse_backend(api, monkeypatch)

    result = backend.click_relative(
        api.target,
        (0.5, 0.5),
        api.expected_process_id,
        api.instance_token(),
    )

    assert result == MouseClickResult(True, True, False, None)
    assert api.minimized[api.target] is True
    assert api.process_ids[300] == 30
    assert api.process_ids[400] == 40
    target_index = api.z_order.index(api.target)
    assert (
        target_index > 0
        and api.z_order[target_index - 1] == 300
    ) is (preserved_edge == "previous")
    assert (
        target_index + 1 < len(api.z_order)
        and api.z_order[target_index + 1] == 400
    ) is (preserved_edge == "next")


def test_win32_mouse_minimized_restore_rejects_both_lost_edges(
    monkeypatch,
):
    api = FakeWin32MouseApi(minimized=True)
    original_position = api.SetWindowPos.callback

    def restore_then_lose_both(*args):
        result = original_position(*args)
        if result:
            api.z_order = [300, 400, 700, api.target]
        return result

    api.SetWindowPos = FakeWin32Function(restore_then_lose_both)
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
    assert api.process_ids[300] == 30
    assert api.process_ids[400] == 40


def test_win32_mouse_revalidates_transient_neighbor_after_adjacency_read(
    monkeypatch,
):
    api = FakeWin32MouseApi(minimized=True)

    def arm_neighbor_replacement(current, message):
        if message == Win32MouseMessageBackend.WM_LBUTTONUP:
            current.after_next_neighbor = (
                lambda target: target.process_ids.__setitem__(700, 999)
            )

    api.after_message = arm_neighbor_replacement
    backend = win32_mouse_backend(api, monkeypatch)

    result = backend.click_relative(
        api.target,
        (0.5, 0.5),
        api.expected_process_id,
        api.instance_token(),
    )

    assert result == MouseClickResult(
        False,
        False,
        True,
        "input_window_state_changed_during_click",
    )
    assert api.process_ids[700] == 999
    assert api.show_calls == [
        (api.target, backend.SW_SHOWNOACTIVATE),
    ]


def test_win32_mouse_minimized_restore_rejects_original_neighbor_replacement(
    monkeypatch,
):
    api = FakeWin32MouseApi(minimized=True)
    original_position = api.SetWindowPos.callback

    def restore_then_replace_neighbor(*args):
        result = original_position(*args)
        if result:
            api.process_ids[300] = 999
        return result

    api.SetWindowPos = FakeWin32Function(restore_then_replace_neighbor)
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
    assert api.process_ids[300] == 999


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
        self.close_kwargs = []
        self.reopen_kwargs = []
        self._closed_window = None

    def close_verified(self, window, candidate_windows, **kwargs):
        self.calls.append((window, tuple(candidate_windows)))
        self.close_kwargs.append(kwargs)
        if self.succeeds:
            self._closed_window = window
        return BattleRestartResult(
            self.succeeds,
            None if self.succeeds else self.failure_code,
            window_closed=self.succeeds,
        )

    def reopen_missing(self, target, candidate_windows, **kwargs):
        self.reopen_calls.append((target, tuple(candidate_windows)))
        self.reopen_kwargs.append(kwargs)
        return BattleRestartResult(
            self.succeeds,
            None if self.succeeds else self.failure_code,
            shortcut_open_requested=self.succeeds,
        )

    def post_close_contract(self, value):
        """Model the provider's real post-close owner-offline transition."""

        owner = self._closed_window
        if not isinstance(value, ResolvedTargetWindows) or owner is None:
            return value
        owner_token = WindowInstanceToken.from_window(owner)
        pairs = tuple(
            zip(value.sync_entry_ids, value.windows, value.sync_windows)
        )
        matches = tuple(
            (entry_id, window)
            for entry_id, window, sync_window in pairs
            if (
                WindowInstanceToken.from_window(window) == owner_token
                and WindowInstanceToken.from_window(sync_window) == owner_token
            )
        )
        if len(matches) != 1:
            return value
        entry_id, owner_window = matches[0]
        remaining = tuple(
            (paired_entry_id, window, sync_window)
            for paired_entry_id, window, sync_window in pairs
            if paired_entry_id != entry_id
        )
        evidence = tuple(
            item
            for item in value.target_failure_evidence
            if item.entry_id != entry_id
        ) + (
            TargetFailureEvidence(
                entry_id,
                owner_window.launch_fingerprint,
                ("window_offline",),
            ),
        )
        return ResolvedTargetWindows(
            windows=tuple(window for _entry_id, window, _sync in remaining),
            sync_windows=tuple(
                sync_window for _entry_id, _window, sync_window in remaining
            ),
            sync_entry_ids=tuple(
                paired_entry_id
                for paired_entry_id, _window, _sync in remaining
            ),
            sync_scope_entry_ids=value.sync_scope_entry_ids,
            sync_controller_entry_id=value.sync_controller_entry_id,
            target_failure_evidence=evidence,
            global_failure_codes=value.global_failure_codes,
            detection_only_windows=value.detection_only_windows,
        )


class SequenceTcpCounts:
    def __init__(self, observations):
        self.observations = list(observations)
        self.calls = []

    def __call__(self, process_ids):
        self.calls.append(process_ids)
        return self.observations.pop(0)


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
    recognizer=None,
    registered_role_provider=None,
    tcp_connection_count_provider=None,
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
    if recognizer is None:
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
                4: (0.5, 0.327),
                5: (0.35, 0.85),
                6: (0.86, 0.12),
                7: (0.81, 0.18),
                9: (0.5, 0.57),
            },
            battle_markers=battle_markers,
        )
    mouse = mouse or FakeMouseBackend()
    if (
        isinstance(battle_restarter, FakeBattleRestarter)
        and target_windows_provider is not None
    ):
        original_target_windows_provider = target_windows_provider

        def target_windows_provider():
            return battle_restarter.post_close_contract(
                original_target_windows_provider()
            )

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
            registered_role_provider=registered_role_provider,
            operation_gate=operation_gate,
            tcp_connection_count_provider=tcp_connection_count_provider,
        )
    if group_launch_plan is not None:
        controller.set_group_launch_plan(group_launch_plan)
    return Fixture(
        controller=controller,
        capture=capture,
        mouse=mouse,
    )


def activate_current_window_snapshot(
    fixture: Fixture,
):
    fixture.controller.set_execution_enabled(False)
    prepared = fixture.controller.prepare_execution_snapshot()
    if prepared.success:
        fixture.controller.set_execution_enabled(True)
    return prepared


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


def make_tcp_group_plan(tmp_path, windows, group_name="current"):
    plan = make_group_plan(tmp_path, windows, group_name)
    return replace(
        plan,
        targets=tuple(
            replace(
                target,
                entry_id=f"entry-{index}",
                role_id=f"role-{index}",
            )
            for index, target in enumerate(plan.targets)
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


def _character_recognition(
    candidates,
    *,
    reference_name="character_selection",
):
    candidates = tuple(candidates)
    selected = tuple(item for item in candidates if item.selected)
    representative = selected[0] if len(selected) == 1 else candidates[0]
    return ScreenRecognition(
        state=ReconnectScreenState.CHARACTER_SELECTION,
        score=0.0,
        click_point=representative.click_point,
        reference_name=reference_name,
        character_level=representative.level,
        character_importance=representative.importance,
        character_slot_index=representative.slot_index,
        character_slot_selected=representative.selected,
        character_identity=representative.identity,
        character_candidates=candidates,
    )


def _single_window_character_fixture(
    recognizer,
    *,
    clock=None,
    registered_role_provider=None,
):
    if clock is None:
        clock = lambda: 0.0
    window = make_window(1)
    capture = FakeCaptureProvider({window.handle: 5})
    mouse = FakeMouseBackend()
    controller = WindowsSmartReconnectController(
        expected_windows=1,
        title_keywords=("Adobe Flash Player",),
        window_backend=FakeWindowBackend([window]),
        capture_provider=capture,
        primary_capture_is_trusted=True,
        recognizer=recognizer,
        mouse_backend=mouse,
        execution_enabled=True,
        monotonic_clock=clock,
        registered_role_provider=registered_role_provider,
    )
    return Fixture(controller, capture, mouse), window


class _CharacterSequenceRecognizer:
    def __init__(self, provider):
        self.provider = provider
        self.calls = 0

    def recognize_capture(self, _sample):
        self.calls += 1
        return _character_recognition(self.provider(self.calls))


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


def test_tcp_disconnect_confirmation_is_read_only_and_resets_on_recovery():
    window = make_window(1, process_id=101)
    now = [0.0]
    tcp = SequenceTcpCounts(
        [
            {101: 2, 999: 0},
            {101: 0, 999: 3},
            {101: 0, 999: 3},
            {101: 0, 999: 3},
            {101: 1, 999: 0},
            {101: 0, 999: 4},
        ]
    )
    fixture = make_controller(
        [1],
        windows=[window],
        expected_windows=1,
        clock=lambda: now[0],
        tcp_connection_count_provider=tcp,
    )

    normal = fixture.controller.check_connection()
    now[0] = 1.0
    first_zero = fixture.controller.check_connection()
    now[0] = 4.0
    second_zero = fixture.controller.check_connection()
    now[0] = 8.0
    confirmed = fixture.controller.check_connection()
    now[0] = 9.0
    recovered = fixture.controller.check_connection()
    now[0] = 10.0
    zero_after_recovery = fixture.controller.check_connection()

    assert normal.code == "reconnect.connected"
    assert first_zero.details["failure_codes"] == ["tcp_disconnect_suspected"]
    assert second_zero.details["failure_codes"] == ["tcp_disconnect_suspected"]
    assert confirmed.details["failure_codes"] == ["tcp_disconnect_confirmed"]
    assert recovered.code == "reconnect.connected"
    assert zero_after_recovery.details["failure_codes"] == [
        "tcp_disconnect_suspected"
    ]
    assert confirmed.details["tcp_observation"] == {
        "generation": 4,
        "observed_at_monotonic": 8.0,
        "query_succeeded": True,
        "observed_window_count": 1,
        "zero_window_count": 1,
        "confirmed_window_count": 1,
    }
    assert tcp.calls == [frozenset({101})] * 6
    assert fixture.mouse.clicks == []
    assert fixture.mouse.expected_process_ids == []


@pytest.mark.parametrize(
    "change",
    [
        {"handle": 2},
        {"process_id": 202},
        {"thread_id": 303},
        {"process_lifecycle_token": 404},
    ],
)
def test_tcp_disconnect_history_is_cleared_by_instance_change(change):
    window = make_window(1, process_id=101)
    tcp = SequenceTcpCounts(
        [{101: 1}, {101: 0}, {101: 0, 202: 0}]
    )
    fixture = make_controller(
        [1],
        windows=[window],
        expected_windows=1,
        clock=lambda: 20.0,
        tcp_connection_count_provider=tcp,
    )

    fixture.controller.check_connection()
    suspected = fixture.controller.check_connection()
    replacement = replace(window, **change)
    fixture.controller._window_backend.windows = [replacement]
    fixture.capture.states[replacement.handle] = 1
    changed = fixture.controller.check_connection()

    assert "tcp_disconnect_suspected" in suspected.details["failure_codes"]
    assert "tcp_disconnect_suspected" not in changed.details["failure_codes"]
    assert "tcp_disconnect_confirmed" not in changed.details["failure_codes"]
    assert fixture.mouse.clicks == []


def test_tcp_query_failure_is_unknown_and_breaks_confirmation_sequence():
    window = make_window(1, process_id=101)
    now = [0.0]
    tcp = SequenceTcpCounts([{101: 1}, None, {101: 0}, {101: 0}])
    fixture = make_controller(
        [1],
        windows=[window],
        expected_windows=1,
        clock=lambda: now[0],
        tcp_connection_count_provider=tcp,
    )

    fixture.controller.check_connection()
    now[0] = 1.0
    unavailable = fixture.controller.check_connection()
    now[0] = 20.0
    first_zero = fixture.controller.check_connection()
    now[0] = 30.0
    second_zero = fixture.controller.check_connection()

    assert unavailable.details["failure_codes"] == [
        "tcp_observation_unavailable"
    ]
    assert first_zero.details["failure_codes"] == ["tcp_disconnect_suspected"]
    assert second_zero.details["failure_codes"] == ["tcp_disconnect_suspected"]
    assert fixture.mouse.clicks == []


def test_multiple_tcp_zero_processes_confirm_independently_without_actions():
    windows = [make_window(1, process_id=101), make_window(2, process_id=202)]
    now = [0.0]
    tcp = SequenceTcpCounts(
        [
            {101: 1, 202: 1},
            {101: 0, 202: 0},
            {101: 0, 202: 0},
            {101: 0, 202: 0},
        ]
    )
    fixture = make_controller(
        [1, 1],
        windows=windows,
        expected_windows=2,
        clock=lambda: now[0],
        tcp_connection_count_provider=tcp,
    )

    fixture.controller.check_connection()
    results = []
    for observed_at in (1.0, 8.0, 20.0):
        now[0] = observed_at
        results.append(fixture.controller.check_connection())

    assert [result.details["failure_codes"] for result in results] == [
        ["tcp_disconnect_suspected"],
        ["tcp_disconnect_suspected"],
        ["tcp_disconnect_confirmed"],
    ]
    assert results[-1].details["tcp_observation"]["confirmed_window_count"] == 2
    assert fixture.mouse.clicks == []
    assert fixture.mouse.expected_process_ids == []


def test_multiple_tcp_zero_queues_only_first_plan_owner_for_mutation(
    tmp_path,
):
    windows = [make_window(index, process_id=100 + index) for index in (1, 2)]
    restarter = FakeBattleRestarter()
    tcp = SequenceTcpCounts(
        [
            {101: 1, 102: 1},
            {101: 0, 102: 0},
            {101: 0, 102: 0},
            {101: 0, 102: 0},
            {101: 0, 102: 0},
            {101: 0, 102: 0},
        ]
    )
    fixture = make_controller(
        [2, 1],
        windows=windows,
        expected_windows=2,
        battle_markers=(2,),
        battle_restarter=restarter,
        group_launch_plan=make_tcp_group_plan(tmp_path, windows),
        target_windows_provider=lambda: tcp_resolved_targets(windows),
        tcp_connection_count_provider=tcp,
    )
    auto_calls = []
    fixture.controller._run_auto_battle_for_connected = (
        lambda *args, **kwargs: auto_calls.append(args) or True
    )
    assert activate_current_window_snapshot(fixture).success is True

    fixture.controller.check_connection()
    fixture.controller.reconnect()
    fixture.controller.reconnect()
    # TCP confirmation grants the owner only. Two fresh matching frames still
    # have to complete before the close/reopen boundary.
    fixture.controller.reconnect()
    restarted = fixture.controller.reconnect()

    assert restarted.details["restarted_windows"] == 1
    assert [call[0] for call in restarter.calls] == [windows[0]]
    assert len(restarter.reopen_calls) == 1
    assert restarter.reopen_calls[0][0].fingerprint == (
        windows[0].launch_fingerprint
    )
    assert restarter.reopen_calls[0][1] == (windows[1],)
    assert fixture.mouse.clicks == []
    assert auto_calls == []
    peer_state = next(
        state
        for (entry_id, _token), state in fixture.controller._tcp_s.items()
        if entry_id == "entry-1"
    )
    assert peer_state.zero_count >= 3


def tcp_resolved_targets(
    windows,
    *,
    sync_windows=None,
    entry_ids=None,
    scope_entry_ids=None,
    target_failures=(),
    global_failure_codes=(),
    detection_only_windows=(),
):
    entry_ids = tuple(entry_ids or (
        f"entry-{index}" for index in range(len(windows))
    ))
    return ResolvedTargetWindows(
        windows=tuple(windows),
        sync_windows=tuple(sync_windows or windows),
        sync_entry_ids=entry_ids,
        sync_scope_entry_ids=tuple(scope_entry_ids or entry_ids),
        sync_controller_entry_id=(entry_ids[0] if entry_ids else None),
        target_failure_evidence=tuple(target_failures),
        global_failure_codes=tuple(global_failure_codes),
        detection_only_windows=tuple(detection_only_windows),
    )


def tcp_target_failure(
    entry_id,
    fingerprint,
    *failure_codes,
    candidate_windows=(),
):
    return TargetFailureEvidence(
        entry_id,
        fingerprint,
        tuple(failure_codes),
        tuple(candidate_windows),
    )


def test_confirmed_tcp_restarts_only_the_single_contract_target(tmp_path):
    windows = [make_window(index, process_id=100 + index) for index in (1, 2, 3)]
    resolved = tcp_resolved_targets(windows)
    restarter = FakeBattleRestarter()
    now = [0.0]
    tcp = SequenceTcpCounts(
        [{101: 1, 102: 1, 103: 1}]
        + [{101: 0, 102: 1, 103: 1}] * 5
    )
    fixture = make_controller(
        [1, 1, 1],
        windows=windows,
        expected_windows=3,
        clock=lambda: now[0],
        tcp_connection_count_provider=tcp,
        target_windows_provider=lambda: resolved,
        battle_restarter=restarter,
        group_launch_plan=make_tcp_group_plan(tmp_path, windows),
    )

    assert activate_current_window_snapshot(fixture).success is True
    for observed_at in (0.0, 1.0, 4.0, 8.0):
        now[0] = observed_at
        fixture.controller.check_connection()
    now[0] = 9.0
    result = fixture.controller.reconnect()

    assert [call[0] for call in restarter.calls] == [windows[0]]
    assert len(restarter.reopen_calls) == 1
    assert restarter.reopen_calls[0][0].fingerprint == (
        windows[0].launch_fingerprint
    )
    assert restarter.reopen_calls[0][1] == tuple(windows[1:])
    assert fixture.mouse.clicks == []
    assert fixture.mouse.expected_process_ids == []
    assert tcp.calls == [frozenset({101, 102, 103})] * 6
    assert fixture.controller._tcp_gen == 6
    assert result.code == "reconnect.progressed"
    assert result.details["all_connected"] is False
    assert result.details["restarted_windows"] == 1
    assert "tcp_disconnect_confirmed" not in result.details["failure_codes"]
    assert fixture.controller._pending_reopen_fingerprints == {
        windows[0].launch_fingerprint
    }
    assert fixture.controller._login_only_recovery_fingerprints == {
        windows[0].launch_fingerprint
    }


def test_tcp_sixty_second_timeout_isolates_owner_and_allows_peer_queue(
    tmp_path,
):
    windows = [
        make_window(1, process_id=101),
        make_window(2, process_id=102),
    ]
    restarter = FakeBattleRestarter()
    now = [0.0]
    tcp = SequenceTcpCounts(
        [{101: 1, 102: 1}] + [{101: 0, 102: 0}] * 20
    )
    fixture = make_controller(
        [2, 1],
        windows=windows,
        expected_windows=2,
        battle_markers=(2,),
        clock=lambda: now[0],
        tcp_connection_count_provider=tcp,
        target_windows_provider=lambda: tcp_resolved_targets(windows),
        battle_restarter=restarter,
        group_launch_plan=make_tcp_group_plan(tmp_path, windows),
    )
    assert activate_current_window_snapshot(fixture).success is True
    for observed_at in (0.0, 1.0, 4.0, 8.0):
        now[0] = observed_at
        fixture.controller.check_connection()

    now[0] = 9.0
    fixture.controller.reconnect()
    assert [call[0] for call in restarter.calls] == [windows[0]]

    now[0] = 70.0
    timeout = fixture.controller.reconnect()

    assert windows[0].launch_fingerprint in fixture.controller._tcp_timeout_isolated
    assert windows[0].launch_fingerprint not in (
        fixture.controller._login_only_recovery_fingerprints
    )
    assert "tcp_reconnect_timeout" in timeout.details["failure_codes"]

    peer_confirmation = fixture.controller.reconnect()
    assert peer_confirmation.details["restarted_windows"] == 0

    fixture.controller.reconnect()

    assert [call[0] for call in restarter.calls] == [windows[0], windows[1]]


def test_tcp_read_only_checks_do_not_consume_owner_recovery_budget(tmp_path):
    window = make_window(1, process_id=101)
    now = [0.0]
    counts = {window.process_id: 1}

    def tcp_connection_counts(process_ids):
        return {process_id: counts.get(process_id, 0) for process_id in process_ids}

    fixture = make_controller(
        [2],
        windows=[window],
        expected_windows=1,
        battle_markers=(2,),
        clock=lambda: now[0],
        tcp_connection_count_provider=tcp_connection_counts,
        target_windows_provider=lambda: tcp_resolved_targets((window,)),
        battle_restarter=FakeBattleRestarter(),
        group_launch_plan=make_tcp_group_plan(tmp_path, (window,)),
    )
    assert activate_current_window_snapshot(fixture).success is True

    fixture.controller.check_connection()
    counts[window.process_id] = 0
    for observed_at in (1.0, 4.0, 8.0):
        now[0] = observed_at
        fixture.controller.check_connection()

    assert fixture.controller._reconnect_timing_flows == {}

    now[0] = 9.0
    fixture.controller.reconnect()
    assert (
        window.launch_fingerprint,
        "tcp_disconnect_to_connected",
    ) in fixture.controller._reconnect_timing_flows

    now[0] = 70.0
    fixture.controller.check_connection()

    assert window.launch_fingerprint not in fixture.controller._tcp_timeout_isolated
    assert (
        window.launch_fingerprint,
        "tcp_disconnect_to_connected",
    ) in fixture.controller._reconnect_timing_flows


def test_slow_tcp_restart_consumes_the_original_sixty_second_budget(tmp_path):
    window = make_window(1, process_id=101)
    now = [0.0]
    counts = {window.process_id: 1}

    class SlowRestarter(FakeBattleRestarter):
        def close_verified(self, window_arg, candidate_windows, **kwargs):
            result = super().close_verified(
                window_arg,
                candidate_windows,
                **kwargs,
            )
            now[0] += 61.0
            return result

    def tcp_connection_counts(process_ids):
        return {process_id: counts.get(process_id, 0) for process_id in process_ids}

    restarter = SlowRestarter()
    fixture = make_controller(
        [2],
        windows=[window],
        expected_windows=1,
        battle_markers=(2,),
        clock=lambda: now[0],
        tcp_connection_count_provider=tcp_connection_counts,
        target_windows_provider=lambda: tcp_resolved_targets((window,)),
        battle_restarter=restarter,
        group_launch_plan=make_tcp_group_plan(tmp_path, (window,)),
    )
    assert activate_current_window_snapshot(fixture).success is True

    fixture.controller.check_connection()
    counts[window.process_id] = 0
    for observed_at in (1.0, 4.0, 8.0):
        now[0] = observed_at
        fixture.controller.check_connection()

    now[0] = 9.0
    result = fixture.controller.reconnect()

    assert [call[0] for call in restarter.calls] == [window]
    assert "tcp_reconnect_timeout" in result.details["failure_codes"]
    assert window.launch_fingerprint in fixture.controller._tcp_timeout_isolated
    assert window.launch_fingerprint not in (
        fixture.controller._login_only_recovery_fingerprints
    )


def test_confirmed_tcp_restarts_from_a_fresh_unknown_game_frame(tmp_path):
    windows = [make_window(index, process_id=100 + index) for index in (1, 2, 3)]
    restarter = FakeBattleRestarter()
    now = [0.0]
    fixture = make_controller(
        [255, 1, 1],
        windows=windows,
        expected_windows=3,
        clock=lambda: now[0],
        tcp_connection_count_provider=SequenceTcpCounts(
            [{101: 1, 102: 1, 103: 1}]
            + [{101: 0, 102: 1, 103: 1}] * 5
        ),
        target_windows_provider=lambda: tcp_resolved_targets(windows),
        battle_restarter=restarter,
        group_launch_plan=make_tcp_group_plan(tmp_path, windows),
    )
    assert activate_current_window_snapshot(fixture).success is True

    for observed_at in (0.0, 1.0, 4.0, 8.0):
        now[0] = observed_at
        fixture.controller.check_connection()
    now[0] = 9.0
    result = fixture.controller.reconnect()

    assert result.code == "reconnect.progressed_with_isolation"
    assert result.details["restarted_windows"] == 1
    assert "screen_unknown" in result.details["failure_codes"]
    assert [call[0] for call in restarter.calls] == [windows[0]]
    assert fixture.mouse.clicks == []


def test_tcp_formal_chain_maps_source_and_monitored_fingerprints_by_token(
    tmp_path,
):
    windows = [make_window(index, process_id=100 + index) for index in (1, 2)]
    monitored = [
        replace(window, launch_fingerprint=f"{index + 8:x}" * 64)
        for index, window in enumerate(windows)
    ]
    resolved = tcp_resolved_targets(windows, sync_windows=monitored)
    restarter = FakeBattleRestarter()
    now = [0.0]
    tcp = SequenceTcpCounts(
        [{101: 1, 102: 1}] + [{101: 0, 102: 1}] * 5
    )
    fixture = make_controller(
        [1, 1],
        windows=windows,
        expected_windows=2,
        clock=lambda: now[0],
        tcp_connection_count_provider=tcp,
        target_windows_provider=lambda: resolved,
        battle_restarter=restarter,
        group_launch_plan=make_tcp_group_plan(tmp_path, windows),
    )
    assert activate_current_window_snapshot(fixture).success is True

    for observed_at in (0.0, 1.0, 4.0, 8.0):
        now[0] = observed_at
        fixture.controller.check_connection()
    now[0] = 9.0
    result = fixture.controller.reconnect()

    assert result.details["restarted_windows"] == 1
    assert [call[0] for call in restarter.calls] == [windows[0]]
    assert fixture.controller._pending_reopen_fingerprints == {
        windows[0].launch_fingerprint
    }


def test_disable_preserves_plan_but_discards_tcp_authorization_history(
    tmp_path,
):
    window = make_window(1, process_id=101)
    plan = make_tcp_group_plan(tmp_path, [window])
    now = [0.0]
    tcp = SequenceTcpCounts(
        [{101: 1}, {101: 0}, {101: 0}, {101: 1}]
        + [{101: 0}] * 3
    )
    fixture = make_controller(
        [1],
        windows=[window],
        expected_windows=1,
        clock=lambda: now[0],
        tcp_connection_count_provider=tcp,
        target_windows_provider=lambda: tcp_resolved_targets([window]),
        group_launch_plan=plan,
    )
    assert activate_current_window_snapshot(fixture).success is True
    fixture.controller.check_connection()
    now[0] = 1.0
    fixture.controller.check_connection()
    now[0] = 4.0
    fixture.controller.check_connection()
    assert next(iter(fixture.controller._tcp_s.values())).zero_count == 2

    fixture.controller.set_execution_enabled(False)
    assert fixture.controller._group_launch_plan == plan
    assert fixture.controller._tcp_s == {}
    assert not fixture.controller._execution_enabled.is_set()
    assert fixture.controller.prepare_execution_snapshot().success is True
    fixture.controller.set_execution_enabled(True)

    observed = []
    for observed_at in (8.0, 9.0, 13.0, 17.0):
        now[0] = observed_at
        observed.append(fixture.controller.check_connection())
    assert [item.details["failure_codes"] for item in observed] == [
        [],
        ["tcp_disconnect_suspected"],
        ["tcp_disconnect_suspected"],
        ["tcp_disconnect_confirmed"],
    ]


def test_same_window_token_cannot_reuse_tcp_history_across_entry_changes(
    tmp_path,
):
    window = make_window(1, process_id=101)
    resolved = [tcp_resolved_targets([window])]
    now = [0.0]
    fixture = make_controller(
        [1],
        windows=[window],
        expected_windows=1,
        clock=lambda: now[0],
        tcp_connection_count_provider=SequenceTcpCounts(
            [{101: 1}, {101: 0}, {101: 0}, {101: 0}, {101: 0}]
        ),
        target_windows_provider=lambda: resolved[0],
        group_launch_plan=make_tcp_group_plan(tmp_path, [window]),
    )
    fixture.controller.check_connection()
    for observed_at in (1.0, 4.0):
        now[0] = observed_at
        fixture.controller.check_connection()
    state = next(iter(fixture.controller._tcp_s.values()))
    assert state.entry_id == "entry-0"
    assert state.zero_count == 2

    resolved[0] = tcp_resolved_targets(
        [window], entry_ids=("replacement-entry",)
    )
    now[0] = 8.0
    changed = fixture.controller.check_connection()
    assert changed.details["failure_codes"] == []
    assert fixture.controller._tcp_s == {}

    resolved[0] = tcp_resolved_targets([window])
    now[0] = 20.0
    restored = fixture.controller.check_connection()
    state = next(iter(fixture.controller._tcp_s.values()))
    assert restored.details["failure_codes"] == []
    assert state.entry_id == "entry-0"
    assert state.zero_count == 0


def test_failed_tcp_restart_never_creates_login_session(tmp_path):
    windows = [make_window(index, process_id=100 + index) for index in (1, 2, 3)]
    now = [0.0]
    fixture = make_controller(
        [1, 1, 1], windows=windows, expected_windows=3,
        clock=lambda: now[0],
        tcp_connection_count_provider=SequenceTcpCounts(
            [{101: 1, 102: 1, 103: 1}]
            + [{101: 0, 102: 1, 103: 1}] * 5),
        target_windows_provider=lambda: tcp_resolved_targets(windows),
        battle_restarter=FakeBattleRestarter(succeeds=False),
        group_launch_plan=make_tcp_group_plan(tmp_path, windows),
    )
    assert activate_current_window_snapshot(fixture).success is True
    for observed_at in (0.0, 1.0, 4.0, 8.0):
        now[0] = observed_at
        fixture.controller.check_connection()
    now[0] = 9.0
    fixture.controller.reconnect()

    assert fixture.controller._pending_reopen_fingerprints == set()
    assert fixture.controller._login_only_recovery_fingerprints == set()


def test_tcp_restart_attempt_is_once_until_tcp_recovers_and_reconfirms(
    tmp_path,
):
    window = make_window(1, process_id=101)
    now = [0.0]
    restarter = FakeBattleRestarter(succeeds=False)
    tcp = SequenceTcpCounts(
        [{101: 1}]
        + [{101: 0}] * 7
        + [{101: 1}]
        + [{101: 0}] * 5
    )
    fixture = make_controller(
        [255],
        windows=[window],
        expected_windows=1,
        clock=lambda: now[0],
        tcp_connection_count_provider=tcp,
        target_windows_provider=lambda: tcp_resolved_targets([window]),
        battle_restarter=restarter,
        group_launch_plan=make_tcp_group_plan(tmp_path, [window]),
    )
    assert activate_current_window_snapshot(fixture).success is True

    for observed_at, execute in (
        (0.0, False),
        (1.0, False),
        (4.0, False),
        (8.0, False),
        (9.0, True),
        (10.0, True),
        (11.0, True),
    ):
        now[0] = observed_at
        (fixture.controller.reconnect if execute else
         fixture.controller.check_connection)()
    assert len(restarter.calls) == 1

    now[0] = 12.0
    fixture.controller.check_connection()
    for observed_at, execute in (
        (20.0, False),
        (24.0, False),
        (28.0, False),
        (29.0, True),
    ):
        now[0] = observed_at
        (fixture.controller.reconnect if execute else
         fixture.controller.check_connection)()
    assert len(restarter.calls) == 2


@pytest.mark.parametrize(
    "change", ["pid_reuse", "lifecycle", "not_unique", "entry_id"]
)
def test_confirmed_tcp_rechecks_identity_and_unique_entry_before_restart(
    tmp_path, change,
):
    windows = [make_window(index, process_id=100 + index) for index in (1, 2, 3)]
    resolved = tcp_resolved_targets(windows)
    if change == "pid_reuse":
        replacement = replace(
            windows[0], handle=99, thread_id=199, process_lifecycle_token=999
        )
        action_view = tcp_resolved_targets((replacement, *windows[1:]))
    elif change == "lifecycle":
        replacement = replace(windows[0], process_lifecycle_token=999)
        action_view = tcp_resolved_targets((replacement, *windows[1:]))
    elif change == "not_unique":
        action_view = tcp_resolved_targets((windows[0], windows[0], windows[2]))
    else:
        action_view = tcp_resolved_targets(
            windows, entry_ids=("replacement-entry", "entry-1", "entry-2")
        )
    provider_calls = [0]

    def provider():
        provider_calls[0] += 1
        return action_view if provider_calls[0] == 5 else resolved

    restarter = FakeBattleRestarter()
    now = [0.0]
    fixture = make_controller(
        [1, 1, 1],
        windows=windows,
        expected_windows=3,
        clock=lambda: now[0],
        tcp_connection_count_provider=SequenceTcpCounts(
            [{101: 1, 102: 1, 103: 1}] + [{101: 0, 102: 1, 103: 1}] * 3
        ),
        target_windows_provider=provider,
        battle_restarter=restarter,
        group_launch_plan=make_tcp_group_plan(tmp_path, windows),
    )
    for observed_at in (0.0, 1.0, 4.0, 8.0):
        now[0] = observed_at
        fixture.controller.check_connection()

    assert restarter.calls == []
    assert restarter.reopen_calls == []
    assert fixture.mouse.clicks == []


@pytest.mark.parametrize(
    "final, expected_restart",
    [
        ({101: 1, 102: 1, 103: 1}, False),
        (None, False),
        ({101: -1, 102: 1, 103: 1}, False),
        ({101: 0, 102: 0, 103: 1}, True),
    ],
)
def test_confirmed_tcp_requires_fresh_exact_zero_before_restart(
    tmp_path,
    final,
    expected_restart,
):
    windows = [make_window(index, process_id=100 + index) for index in (1, 2, 3)]
    restarter = FakeBattleRestarter()
    tcp = SequenceTcpCounts(
        [{101: 1, 102: 1, 103: 1}]
        + [{101: 0, 102: 1, 103: 1}] * 4
        + [final]
    )
    now = [0.0]
    fixture = make_controller(
        [1, 1, 1],
        windows=windows,
        expected_windows=3,
        clock=lambda: now[0],
        tcp_connection_count_provider=tcp,
        target_windows_provider=lambda: tcp_resolved_targets(windows),
        battle_restarter=restarter,
        group_launch_plan=make_tcp_group_plan(tmp_path, windows),
    )

    assert activate_current_window_snapshot(fixture).success is True
    for observed_at in (0.0, 1.0, 4.0, 8.0):
        now[0] = observed_at
        fixture.controller.check_connection()
    now[0] = 9.0
    fixture.controller.reconnect()

    assert len(restarter.calls) == int(expected_restart)
    if expected_restart:
        assert restarter.calls[0][0] == windows[0]
    assert tcp.calls[-1] == frozenset({101, 102, 103})
    assert fixture.controller._tcp_gen == 6


def test_failed_final_tcp_query_does_not_consume_later_reconfirmed_event(
    tmp_path,
):
    window = make_window(1, process_id=101)
    restarter = FakeBattleRestarter()
    now = [0.0]
    tcp = SequenceTcpCounts(
        [{101: 1}] + [{101: 0}] * 4 + [None] + [{101: 0}] * 5
    )
    fixture = make_controller(
        [1],
        windows=[window],
        expected_windows=1,
        clock=lambda: now[0],
        tcp_connection_count_provider=tcp,
        target_windows_provider=lambda: tcp_resolved_targets([window]),
        battle_restarter=restarter,
        group_launch_plan=make_tcp_group_plan(tmp_path, [window]),
    )
    assert activate_current_window_snapshot(fixture).success is True
    for observed_at in (0.0, 1.0, 4.0, 8.0):
        now[0] = observed_at
        fixture.controller.check_connection()
    now[0] = 9.0
    fixture.controller.reconnect()
    assert restarter.calls == []
    assert fixture.controller._battle_restart_attempts == {}

    for observed_at in (10.0, 14.0, 18.0):
        now[0] = observed_at
        fixture.controller.check_connection()
    now[0] = 19.0
    fixture.controller.reconnect()
    assert [call[0] for call in restarter.calls] == [window]


@pytest.mark.parametrize("change", ["target_entry", "peer_token"])
def test_confirmed_tcp_rechecks_complete_final_restart_contract(
    tmp_path,
    change,
):
    windows = [make_window(index, process_id=100 + index) for index in (1, 2, 3)]
    resolved = tcp_resolved_targets(windows)
    rebound = (
        tcp_resolved_targets(
            windows, entry_ids=("replacement-entry", "entry-1", "entry-2")
        )
        if change == "target_entry"
        else tcp_resolved_targets(
            [windows[0], replace(
                windows[1], process_lifecycle_token=999999
            ), windows[2]]
        )
    )
    provider_calls = [0]
    final_query_seen = [False]

    def provider():
        provider_calls[0] += 1
        return rebound if final_query_seen[0] else resolved

    restarter = FakeBattleRestarter()
    tcp_calls = [0]

    def tcp(process_ids):
        tcp_calls[0] += 1
        if tcp_calls[0] == 6:
            final_query_seen[0] = True
        return (
            {101: 1, 102: 1, 103: 1}
            if tcp_calls[0] == 1
            else {101: 0, 102: 1, 103: 1}
        )
    now = [0.0]
    fixture = make_controller(
        [1, 1, 1],
        windows=windows,
        expected_windows=3,
        clock=lambda: now[0],
        tcp_connection_count_provider=tcp,
        target_windows_provider=provider,
        battle_restarter=restarter,
        group_launch_plan=make_tcp_group_plan(tmp_path, windows),
    )

    assert activate_current_window_snapshot(fixture).success is True
    for observed_at in (0.0, 1.0, 4.0, 8.0):
        now[0] = observed_at
        fixture.controller.check_connection()
    now[0] = 9.0
    fixture.controller.reconnect()

    assert provider_calls[0] >= 7
    assert tcp_calls == [6]
    assert restarter.calls == []
    assert restarter.reopen_calls == []


def tcp_login_fixture(
    tmp_path,
    *,
    candidates,
    extra_states=(1, 1),
    target_importance=CharacterImportance.PRIMARY,
    tcp_observations=None,
    detection_only=(),
):
    old = make_window(1, process_id=101)
    new = replace(old, handle=11, process_id=111, thread_id=211,
                  process_lifecycle_token=311)
    peers = [make_window(i, process_id=100 + i) for i in (2, 3)]
    detection_only = tuple(detection_only)
    configured_initial = [old, *peers]
    configured_windows = [new, *peers]
    initial_windows = [*configured_initial, *detection_only]
    windows = [*configured_windows, *detection_only]
    plan = GroupLaunchPlan(
        "current",
        (GroupLaunchTarget(1, "AlphaHero", tmp_path / "a.lnk",
                           old.launch_fingerprint, entry_id="entry-0",
                           role_id="AlphaHero", registered_level=120,
                           importance=target_importance),
         *tuple(GroupLaunchTarget(i, f"peer-{i}", tmp_path / f"p{i}.lnk",
                                   peers[i - 2].launch_fingerprint,
                                   entry_id=f"entry-{i - 1}",
                                   role_id=f"peer-{i}")
                 for i in (2, 3))),
    )
    frames = [tuple(candidates)]

    class LoginRecognizer:
        def recognize_capture(self, sample):
            marker = sample.pixels[0]
            if marker == 3:
                return ScreenRecognition(ReconnectScreenState.LOGIN_START, 0.0,
                                         (0.5, 0.8), "login")
            if marker == 5:
                return _character_recognition(frames[0])
            return ScreenRecognition(ReconnectScreenState.CONNECTED, 0.0, None,
                                     "connected")

    resolved = [
        tcp_resolved_targets(
            configured_initial,
            detection_only_windows=detection_only,
        )
    ]
    now = [0.0]
    initial_online = {
        window.process_id: 1 for window in initial_windows
    }
    target_zero = {**initial_online, old.process_id: 0}
    rebound_online = {
        window.process_id: 1 for window in windows
    }
    tcp = SequenceTcpCounts(
        tcp_observations
        if tcp_observations is not None
        else (
            [initial_online]
            + [target_zero] * 5
            + [rebound_online] * 40
        )
    )
    fixture = make_controller(
        [1] * len(initial_windows),
        windows=initial_windows,
        expected_windows=len(initial_windows),
        recognizer=LoginRecognizer(), group_launch_plan=plan,
        target_windows_provider=lambda: resolved[0],
        clock=lambda: now[0],
        tcp_connection_count_provider=tcp,
        battle_restarter=FakeBattleRestarter(),
    )
    assert activate_current_window_snapshot(fixture).success is True
    for observed_at in (0.0, 1.0, 4.0, 8.0):
        now[0] = observed_at
        fixture.controller.check_connection()
    now[0] = 9.0
    restarted = fixture.controller.reconnect()
    assert restarted.details["restarted_windows"] == 1
    resolved[0] = tcp_resolved_targets(
        configured_windows,
        detection_only_windows=detection_only,
    )
    fixture.controller._window_backend.windows = list(windows)
    fixture.capture.states.update(
        {new.handle: 3, **{
            peer.handle: marker
            for peer, marker in zip(peers, extra_states)
        }, **{
            peer.handle: 1 for peer in detection_only
        }}
    )
    tick = [9.0]

    def progress_clock():
        # A real monotonic source returns one continuous value through every
        # internal safety check in a scan.  Keep this fixture's synthetic time
        # similarly fine-grained so one public reconnect cycle cannot spend
        # the entire sixty-second TCP recovery budget by itself.
        tick[0] += 2.0
        return tick[0]

    fixture.controller._monotonic_clock = progress_clock
    return fixture, old, new, peers, frames


def test_configured_login_recovery_keeps_detection_only_peers_immutable(
    tmp_path,
):
    target = CharacterSelectionCandidate(
        120,
        CharacterImportance.PRIMARY,
        1,
        True,
        CHARACTER_ENTER_CLICK_POINT,
        digit_count=3,
        identity="AlphaHero",
    )
    detection_only = tuple(
        make_window(index, process_id=100 + index)
        for index in (4, 5, 6)
    )
    fixture, old, new, peers, _frames = tcp_login_fixture(
        tmp_path,
        candidates=(target,),
        detection_only=detection_only,
    )
    restarter = fixture.controller._battle_restarter

    fixture.controller.reconnect()
    login = fixture.controller.reconnect()
    fixture.capture.states[new.handle] = 5
    fixture.controller.reconnect()
    selected = fixture.controller.reconnect()
    complete_with_fresh_connected_frames(fixture, handle=new.handle)

    assert login.details["clicked_windows"] == 1
    assert selected.details["clicked_windows"] == 1
    assert [call[0] for call in restarter.calls] == [old]
    assert restarter.calls[0][1] == (old, *peers, *detection_only)
    assert restarter.reopen_calls[0][1] == (*peers, *detection_only)
    assert fixture.mouse.clicks == [
        (new.handle, (0.505, 0.856)),
        (new.handle, CHARACTER_ENTER_CLICK_POINT),
    ]
    assert all(
        handle not in {window.handle for window in detection_only}
        for handle, _point in fixture.mouse.clicks
    )
    fingerprint = old.launch_fingerprint
    assert fingerprint not in fixture.controller._pending_reopen_fingerprints
    assert fingerprint not in fixture.controller._pending_reconnect_fingerprints
    assert fingerprint not in (
        fixture.controller._login_only_recovery_fingerprints
    )


def tcp_missing_target(
    peers,
    *,
    blocked=(),
    failure_code="window_offline",
):
    peer_entry_ids = tuple(
        f"entry-{index}" for index in range(1, len(peers) + 1)
    )
    return ResolvedTargetWindows(
        windows=tuple(peers),
        sync_windows=tuple(peers),
        sync_entry_ids=peer_entry_ids,
        sync_scope_entry_ids=("entry-0", *peer_entry_ids),
        sync_controller_entry_id="entry-0",
        target_failure_evidence=tuple(
            tcp_target_failure("entry-0", fingerprint, failure_code)
            for fingerprint in blocked
        ),
    )


def test_tcp_expected_reopen_absence_keeps_strict_job_until_new_window(tmp_path):
    target = CharacterSelectionCandidate(120, None, 1, True,
                                         CHARACTER_ENTER_CLICK_POINT,
                                         digit_count=3, identity="AlphaHero")
    fixture, old, new, peers, _frames = tcp_login_fixture(
        tmp_path, candidates=(target,), extra_states=(3, 3)
    )
    fp = old.launch_fingerprint
    state = [tcp_missing_target(peers, blocked=(old.launch_fingerprint,))]
    fixture.controller._target_windows_provider = lambda: state[0]
    fixture.controller._reopen_retry_after[fp] = 999.0

    absent = fixture.controller.reconnect()
    assert fixture.controller._login_only_recovery_fingerprints == {fp}
    assert set(fixture.controller._pending_reopen_fingerprints) == {fp}
    assert absent.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []
    second_absent = fixture.controller.reconnect()
    assert second_absent.details["clicked_windows"] == 0
    assert fixture.controller._pending_reopen_fingerprints == {fp}

    state[0] = tcp_resolved_targets([new, *peers])
    fixture.capture.states[new.handle] = 3
    fixture.controller.reconnect()
    restored = fixture.controller.reconnect()
    assert restored.details["actionable_windows"] == 1
    assert fixture.mouse.clicks == [(new.handle, (0.505, 0.856))]


@pytest.mark.parametrize("unsafe", ["duplicate", "blocked"])
def test_tcp_target_local_unsafe_isolates_only_its_own_recovery(tmp_path, unsafe):
    target = CharacterSelectionCandidate(120, None, 1, True,
                                         CHARACTER_ENTER_CLICK_POINT,
                                         digit_count=3, identity="AlphaHero")
    fixture, old, new, peers, _frames = tcp_login_fixture(
        tmp_path, candidates=(target,)
    )
    fp = old.launch_fingerprint
    if unsafe == "duplicate":
        bad = tcp_resolved_targets(
            [new, peers[1]],
            entry_ids=("entry-0", "entry-2"),
            scope_entry_ids=("entry-0", "entry-1", "entry-2"),
            target_failures=(
                tcp_target_failure(
                    "entry-1",
                    peers[0].launch_fingerprint,
                    "window_identity_duplicate",
                    candidate_windows=(peers[0],),
                ),
            ),
        )
    else:
        bad = tcp_resolved_targets(
            peers,
            entry_ids=("entry-1", "entry-2"),
            scope_entry_ids=("entry-0", "entry-1", "entry-2"),
            target_failures=(
                tcp_target_failure(
                    "entry-0",
                    fp,
                    "shortcut_identity_unresolved",
                ),
            ),
        )
    fixture.controller._target_windows_provider = lambda: bad

    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []
    assert fixture.controller._execution_enabled.is_set()
    if unsafe == "duplicate":
        assert fp in fixture.controller._pending_reconnect_fingerprints
        assert fp in fixture.controller._login_only_recovery_fingerprints
    else:
        assert fp not in fixture.controller._pending_reconnect_fingerprints
        assert fp not in fixture.controller._login_only_recovery_fingerprints


def test_tcp_unsafe_login_owner_immediately_yields_to_confirmed_peer(tmp_path):
    windows = [
        make_window(1, process_id=101),
        make_window(2, process_id=102),
    ]
    now = [0.0]
    counts = {101: 1, 102: 1}
    provider_state = {"value": tcp_resolved_targets(windows)}
    restarter = FakeBattleRestarter()

    def tcp_connection_counts(process_ids):
        return {process_id: counts.get(process_id, 0) for process_id in process_ids}

    fixture = make_controller(
        [2, 2],
        windows=windows,
        expected_windows=2,
        battle_markers=(2,),
        clock=lambda: now[0],
        tcp_connection_count_provider=tcp_connection_counts,
        target_windows_provider=lambda: provider_state["value"],
        battle_restarter=restarter,
        group_launch_plan=make_tcp_group_plan(tmp_path, windows),
    )
    assert activate_current_window_snapshot(fixture).success is True

    fixture.controller.check_connection()
    counts[101] = 0
    for observed_at in (1.0, 4.0, 8.0):
        now[0] = observed_at
        fixture.controller.check_connection()
    now[0] = 9.0
    fixture.controller.reconnect()

    assert [call[0] for call in restarter.calls] == [windows[0]]
    assert windows[0].launch_fingerprint in (
        fixture.controller._login_only_recovery_fingerprints
    )

    fixture.controller._window_backend.windows = [windows[1]]
    provider_state["value"] = tcp_resolved_targets(
        (windows[1],),
        entry_ids=("entry-1",),
        scope_entry_ids=("entry-0", "entry-1"),
        target_failures=(
            tcp_target_failure(
                "entry-0",
                windows[0].launch_fingerprint,
                "shortcut_identity_unresolved",
            ),
        ),
    )
    counts[102] = 0
    for observed_at in (12.0, 15.0, 19.0):
        now[0] = observed_at
        fixture.controller.check_connection()

    assert windows[0].launch_fingerprint not in (
        fixture.controller._login_only_recovery_fingerprints
    )
    now[0] = 20.0
    first_peer = fixture.controller.reconnect()

    assert first_peer.details["restarted_windows"] == 1
    assert [call[0] for call in restarter.calls] == windows
    assert fixture.mouse.clicks == []


def test_explicit_observation_uses_only_its_own_target_failure_evidence(
    tmp_path,
):
    windows = [make_window(1), make_window(2)]
    provider_state = {"value": tcp_resolved_targets(windows)}
    fixture = make_controller(
        [1, 1],
        windows=windows,
        expected_windows=2,
        target_windows_provider=lambda: provider_state["value"],
        group_launch_plan=make_tcp_group_plan(tmp_path, windows),
    )

    healthy = fixture.controller.observe_screen_states(
        (windows[0].launch_fingerprint,),
    )
    provider_state["value"] = tcp_resolved_targets(
        (windows[0],),
        entry_ids=("entry-0",),
        scope_entry_ids=("entry-0", "entry-1"),
        target_failures=(
            tcp_target_failure(
                "entry-1",
                windows[1].launch_fingerprint,
                "window_identity_duplicate",
            ),
        ),
    )
    sibling_failed = fixture.controller.observe_screen_states(
        (windows[0].launch_fingerprint,),
    )
    provider_state["value"] = tcp_resolved_targets(
        (windows[0],),
        entry_ids=("entry-0",),
        scope_entry_ids=("entry-0", "entry-1"),
        global_failure_codes=("target_window_provider_failed",),
    )
    globally_failed = fixture.controller.observe_screen_states(
        (windows[0].launch_fingerprint,),
    )

    assert healthy == {
        windows[0].launch_fingerprint: ReconnectScreenState.CONNECTED,
    }
    assert sibling_failed == healthy
    assert globally_failed == {
        windows[0].launch_fingerprint: ReconnectScreenState.UNKNOWN,
    }
    assert fixture.capture.calls == [windows[0].handle, windows[0].handle]


def test_recovered_peer_can_be_revoked_again_in_a_new_source_generation():
    windows = [make_window(1), make_window(2), make_window(3)]
    provider_state = {
        "value": tcp_resolved_targets(
            (windows[2],),
            entry_ids=("entry-2",),
            scope_entry_ids=("entry-0", "entry-1", "entry-2"),
            target_failures=(
                tcp_target_failure(
                    "entry-0",
                    windows[0].launch_fingerprint,
                    "window_identity_duplicate",
                ),
                tcp_target_failure(
                    "entry-1",
                    windows[1].launch_fingerprint,
                    "window_identity_duplicate",
                ),
            ),
        )
    }
    fixture = make_controller(
        [1, 1, 1],
        windows=windows,
        expected_windows=3,
        require_expected_window_count=False,
        target_windows_provider=lambda: provider_state["value"],
    )
    fixture.controller.set_allowed_fingerprints(
        frozenset(window.launch_fingerprint for window in windows)
    )

    fixture.controller.reconnect()
    generation_after_both_bad = fixture.controller._source_state_generation
    assert windows[1].launch_fingerprint in (
        fixture.controller._source_revoked_fingerprints
    )

    provider_state["value"] = tcp_resolved_targets(
        (windows[1], windows[2]),
        entry_ids=("entry-1", "entry-2"),
        scope_entry_ids=("entry-0", "entry-1", "entry-2"),
        target_failures=(
            tcp_target_failure(
                "entry-0",
                windows[0].launch_fingerprint,
                "window_identity_duplicate",
            ),
        ),
    )
    fixture.controller.reconnect()

    assert windows[0].launch_fingerprint in (
        fixture.controller._source_revoked_fingerprints
    )
    assert windows[1].launch_fingerprint not in (
        fixture.controller._source_revoked_fingerprints
    )

    provider_state["value"] = tcp_resolved_targets(
        (windows[2],),
        entry_ids=("entry-2",),
        scope_entry_ids=("entry-0", "entry-1", "entry-2"),
        target_failures=(
            tcp_target_failure(
                "entry-0",
                windows[0].launch_fingerprint,
                "window_identity_duplicate",
            ),
            tcp_target_failure(
                "entry-1",
                windows[1].launch_fingerprint,
                "window_identity_duplicate",
            ),
        ),
    )
    fixture.controller.reconnect()

    assert fixture.controller._source_state_generation > (
        generation_after_both_bad
    )
    assert windows[1].launch_fingerprint in (
        fixture.controller._source_revoked_fingerprints
    )


def test_pending_reopen_ignores_mapped_duplicate_sibling_only(tmp_path):
    target = CharacterSelectionCandidate(
        120,
        None,
        1,
        True,
        CHARACTER_ENTER_CLICK_POINT,
        digit_count=3,
        identity="AlphaHero",
    )
    fixture, old, _new, peers, _frames = tcp_login_fixture(
        tmp_path,
        candidates=(target,),
    )
    restarter = fixture.controller._battle_restarter
    fixture.controller._window_backend.windows = list(peers)
    provider_state = {
        "value": tcp_resolved_targets(
            (peers[1],),
            entry_ids=("entry-2",),
            scope_entry_ids=("entry-0", "entry-1", "entry-2"),
            target_failures=(
                tcp_target_failure(
                    "entry-0",
                    old.launch_fingerprint,
                    "window_offline",
                ),
                tcp_target_failure(
                    "entry-1",
                    peers[0].launch_fingerprint,
                    "window_identity_duplicate",
                ),
            ),
        )
    }
    fixture.controller._target_windows_provider = lambda: provider_state["value"]
    reopen_count_before_retry = len(restarter.reopen_calls)

    fixture.controller.reconnect()

    assert len(restarter.reopen_calls) == reopen_count_before_retry + 1
    assert restarter.reopen_calls[-1][0].fingerprint == old.launch_fingerprint
    assert restarter.reopen_calls[-1][1] == (peers[1],)
    assert fixture.mouse.clicks == []


def test_tcp_login_actions_only_target_new_instance_and_original_entry(tmp_path):
    target = CharacterSelectionCandidate(120, CharacterImportance.PRIMARY, 1,
                                         False, (0.5, 0.706), digit_count=3,
                                         identity="AlphaHero")
    fixture, old, new, peers, frames = tcp_login_fixture(
        tmp_path, candidates=(target,), extra_states=(3, 3)
    )
    fixture.controller._pending_reconnect_fingerprints.update(
        peer.launch_fingerprint for peer in peers
    )
    fixture.controller.reconnect()
    login = fixture.controller.reconnect()
    assert login.details["actionable_windows"] == 1
    assert fixture.mouse.clicks == [(new.handle, (0.505, 0.856))]
    assert all(call[0] != peers[0].handle and call[0] != peers[1].handle
               for call in fixture.mouse.clicks)

    fixture.capture.states[new.handle] = 5
    fixture.controller.reconnect()
    fixture.controller.reconnect()
    frames[0] = (replace(target, selected=True,
                         click_point=CHARACTER_ENTER_CLICK_POINT),)
    fixture.controller._flow_pause_until.clear()
    fixture.controller.reconnect()
    fixture.controller.reconnect()
    assert fixture.mouse.clicks[-2:] == [(new.handle, target.click_point),
                                         (new.handle, CHARACTER_ENTER_CLICK_POINT)]
    assert old.handle not in [call[0] for call in fixture.mouse.clicks]
    fixture.capture.states[new.handle] = 6
    fixture.controller.reconnect()
    fixture.controller.reconnect()
    assert len(fixture.mouse.clicks) == 3


@pytest.mark.parametrize(
    "unsafe", ["old", "entry", "failure", "blocked", "none", "multi"]
)
def test_tcp_login_contract_change_blocks_every_click(tmp_path, unsafe):
    target = CharacterSelectionCandidate(120, CharacterImportance.PRIMARY, 1,
                                         True, CHARACTER_ENTER_CLICK_POINT,
                                         digit_count=3, identity="AlphaHero")
    fixture, old, new, peers, _selected = tcp_login_fixture(tmp_path, candidates=(target,))
    if unsafe == "old":
        windows = [old, *peers]
        resolved = tcp_resolved_targets(windows)
    elif unsafe == "entry":
        resolved = tcp_resolved_targets([new, *peers],
            entry_ids=("replacement", "entry-1", "entry-2"))
    elif unsafe == "failure":
        resolved = replace(tcp_resolved_targets([new, *peers]),
                           global_failure_codes=("unsafe",))
    elif unsafe == "blocked":
        resolved = replace(tcp_resolved_targets([new, *peers]),
                           global_failure_codes=("window_identity_duplicate",))
    elif unsafe == "none":
        resolved = None
    else:
        resolved = tcp_resolved_targets([new, *peers])
        peer = peers[0].launch_fingerprint
        fixture.controller._pending_reopen_fingerprints.add(peer)
        fixture.controller._login_only_recovery_fingerprints.add(peer)
    fixture.controller._target_windows_provider = lambda: resolved

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []


def test_tcp_login_ambiguous_role_and_unknown_screen_never_click(tmp_path):
    same = tuple(CharacterSelectionCandidate(120, None, slot, False,
                 (0.35 + slot * 0.15, 0.706), digit_count=3)
                 for slot in (0, 1))
    fixture, _old, _new, _peers, _selected = tcp_login_fixture(
        tmp_path, candidates=same
    )
    fixture.capture.states[11] = 5
    fixture.controller.reconnect()
    result = fixture.controller.reconnect()
    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []
    fixture.capture.states[11] = 255
    fixture.controller.reconnect()
    assert fixture.mouse.clicks == []


def test_tcp_login_rechecks_same_slot_before_enter(tmp_path):
    target = CharacterSelectionCandidate(120, None, 1, False,
                                         (0.5, 0.706), digit_count=3,
                                         identity="AlphaHero")
    fixture, _old, _new, _peers, frames = tcp_login_fixture(
        tmp_path, candidates=(target,)
    )
    fixture.capture.states[11] = 5
    fixture.controller.reconnect()
    fixture.controller.reconnect()
    frames[0] = (replace(target, slot_index=0, selected=True,
                         click_point=CHARACTER_ENTER_CLICK_POINT),)
    fixture.controller._flow_pause_until.clear()
    fixture.controller.reconnect()
    fixture.controller.reconnect()

    assert fixture.mouse.clicks == [(11, target.click_point)]


@pytest.mark.parametrize("bad_target", ["missing_role", "duplicate_entry"])
def test_tcp_login_requires_one_same_entry_role_target(tmp_path, bad_target):
    target = CharacterSelectionCandidate(120, None, 1, True,
                                         CHARACTER_ENTER_CLICK_POINT,
                                         digit_count=3, identity="AlphaHero")
    fixture, old, _new, _peers, _frames = tcp_login_fixture(
        tmp_path, candidates=(target,)
    )
    fixture.capture.states[11] = 5
    plan = fixture.controller._group_launch_plan
    first = plan.targets[0]
    fixture.controller._group_launch_plan = replace(
        plan,
        targets=((replace(first, role_id="") if bad_target == "missing_role"
                  else first), *plan.targets[1:],
                 *((replace(first, order=4),)
                   if bad_target == "duplicate_entry" else ())),
    )

    fixture.controller.reconnect()
    fixture.controller.reconnect()

    assert fixture.controller._login_only_recovery_fingerprints == set()
    assert not fixture.controller._execution_enabled.is_set()
    assert fixture.mouse.clicks == []


@pytest.mark.parametrize("change", ["entry", "token"])
def test_tcp_login_final_contract_rebind_cancels_click(tmp_path, change):
    target = CharacterSelectionCandidate(120, None, 1, True,
                                         CHARACTER_ENTER_CLICK_POINT,
                                         digit_count=3, identity="AlphaHero")
    fixture, _old, new, peers, _frames = tcp_login_fixture(
        tmp_path, candidates=(target,)
    )
    fixture.capture.states[new.handle] = 5
    safe = tcp_resolved_targets([new, *peers])
    rebound = (tcp_resolved_targets([new, *peers],
               entry_ids=("replacement", "entry-1", "entry-2"))
               if change == "entry" else
               tcp_resolved_targets([
                   replace(new, process_lifecycle_token=999), *peers
               ]))
    calls = [0]

    def provider():
        calls[0] += 1
        return safe if calls[0] <= 5 else rebound

    fixture.controller._target_windows_provider = provider
    fixture.controller.reconnect()
    fixture.controller.reconnect()

    assert calls[0] >= 6
    assert fixture.mouse.clicks == []


def test_tcp_login_final_role_change_cancels_click(tmp_path):
    target = CharacterSelectionCandidate(120, None, 1, True,
                                         CHARACTER_ENTER_CLICK_POINT,
                                         digit_count=3, identity="AlphaHero")
    fixture, _old, new, _peers, _frames = tcp_login_fixture(
        tmp_path, candidates=(target,)
    )
    fixture.capture.states[new.handle] = 5

    class ChangedRole:
        def __init__(self):
            self.calls = 0

        def recognize_capture(self, sample):
            if sample.pixels[0] != 5:
                return ScreenRecognition(ReconnectScreenState.CONNECTED, 0.0,
                                         None, "connected")
            self.calls += 1
            current = target if self.calls <= 2 else replace(
                target, identity="OtherHero"
            )
            return _character_recognition((current,))

    fixture.controller._recognizer = ChangedRole()
    fixture.controller.reconnect()
    fixture.controller.reconnect()
    assert fixture.controller._recognizer.calls >= 3
    assert fixture.mouse.clicks == []


@pytest.mark.parametrize("unknown_competitor", [False, True])
def test_tcp_login_level_fallback_requires_one_complete_candidate(
    tmp_path, unknown_competitor,
):
    target = CharacterSelectionCandidate(120, None, 1, True,
                                         CHARACTER_ENTER_CLICK_POINT,
                                         digit_count=3)
    candidates = (target,) + ((replace(target, level=None, slot_index=2),)
                              if unknown_competitor else ())
    fixture, _old, new, _peers, _frames = tcp_login_fixture(
        tmp_path, candidates=candidates
    )
    fixture.capture.states[new.handle] = 5
    fixture.controller.reconnect()
    result = fixture.controller.reconnect()
    assert result.details["clicked_windows"] == (0 if unknown_competitor else 1)
    assert fixture.mouse.clicks == ([] if unknown_competitor else
                                    [(new.handle, CHARACTER_ENTER_CLICK_POINT)])


def test_tcp_connected_terminal_never_runs_post_login_or_auto_battle(tmp_path):
    target = CharacterSelectionCandidate(120, None, 1, True,
                                         CHARACTER_ENTER_CLICK_POINT,
                                         digit_count=3, identity="AlphaHero")
    fixture, old, new, _peers, _frames = tcp_login_fixture(
        tmp_path, candidates=(target,)
    )
    fingerprint = old.launch_fingerprint
    auto_calls = []
    fixture.controller.set_auto_battle_enabled(True)
    fixture.controller._run_auto_battle_for_connected = (
        lambda *args, **kwargs: auto_calls.append(args) or True
    )
    fixture.controller.reconnect()
    fixture.controller.reconnect()
    baseline_clicks = list(fixture.mouse.clicks)
    fixture.controller._primary_entry_authorized.add(fingerprint)
    complete_with_fresh_connected_frames(fixture, handle=new.handle)

    assert fingerprint not in fixture.controller._login_only_recovery_fingerprints
    assert all(call[0].handle != new.handle for call in auto_calls)
    assert auto_calls == []
    assert fingerprint not in fixture.controller._pending_reopen_fingerprints
    auto_calls.clear()
    fixture.controller.reconnect()
    assert {call[0].handle for call in auto_calls} == {new.handle, 2, 3}
    fixture.capture.states[new.handle] = 6
    fixture.controller.reconnect()
    fixture.controller.reconnect()
    assert fixture.mouse.clicks == baseline_clicks


def test_tcp_confirmed_peer_waits_until_first_owner_reaches_terminal(tmp_path):
    target = CharacterSelectionCandidate(
        120,
        CharacterImportance.PRIMARY,
        1,
        True,
        CHARACTER_ENTER_CLICK_POINT,
        digit_count=3,
        identity="AlphaHero",
    )
    observations = (
        [{101: 1, 102: 1, 103: 1}]
        + [{101: 0, 102: 0, 103: 1}] * 6
        + [{111: 1, 102: 0, 103: 1}] * 80
    )
    fixture, old, new, peers, _frames = tcp_login_fixture(
        tmp_path,
        candidates=(target,),
        tcp_observations=observations,
    )
    restarter = fixture.controller._battle_restarter

    assert [call[0] for call in restarter.calls] == [old]
    assert all(call[0] != peers[0] for call in restarter.calls)

    fixture.controller.reconnect()
    fixture.controller.reconnect()
    fixture.capture.states[new.handle] = 5
    fixture.controller.reconnect()
    fixture.controller.reconnect()
    complete_with_fresh_connected_frames(fixture, handle=new.handle)

    assert old.launch_fingerprint not in (
        fixture.controller._login_only_recovery_fingerprints
    )
    assert [call[0] for call in restarter.calls] == [old]

    first_peer_scan = fixture.controller.reconnect()
    assert first_peer_scan.details["restarted_windows"] == 0
    second_peer_scan = fixture.controller.reconnect()

    assert second_peer_scan.details["restarted_windows"] == 1
    assert [call[0] for call in restarter.calls] == [old, peers[0]]
    assert all(call[0] != peers[1] for call in restarter.calls)


@pytest.mark.parametrize(
    "target_importance",
    [CharacterImportance.SECONDARY, None],
)
def test_tcp_non_primary_role_reaches_connected_terminal(
    tmp_path,
    target_importance,
):
    target = CharacterSelectionCandidate(
        120,
        target_importance,
        1,
        True,
        CHARACTER_ENTER_CLICK_POINT,
        digit_count=3,
        identity="AlphaHero",
    )
    fixture, old, new, peers, _frames = tcp_login_fixture(
        tmp_path,
        candidates=(target,),
        target_importance=target_importance,
    )
    fingerprint = old.launch_fingerprint
    fixture.controller.reconnect()
    fixture.controller.reconnect()
    fixture.capture.states[new.handle] = 5
    fixture.controller.reconnect()
    fixture.controller.reconnect()
    complete_with_fresh_connected_frames(fixture, handle=new.handle)

    assert fingerprint not in fixture.controller._pending_reopen_fingerprints
    assert fingerprint not in fixture.controller._pending_reconnect_fingerprints
    assert fingerprint not in fixture.controller._active_automation_fingerprints
    assert fingerprint not in fixture.controller._reconnect_entry_authorized
    assert fingerprint not in fixture.controller._login_only_recovery_fingerprints
    calls = []
    fixture.controller._run_auto_battle_for_connected = (
        lambda *args, **kwargs: calls.append(args[0].handle) or True
    )
    fixture.controller.reconnect()
    assert set(calls) == {new.handle, *(peer.handle for peer in peers)}


def test_non_tcp_auto_battle_remains_unchanged_after_tcp_completion(tmp_path):
    target = CharacterSelectionCandidate(120, None, 1, True,
                                         CHARACTER_ENTER_CLICK_POINT,
                                         digit_count=3, identity="AlphaHero")
    fixture, _old, new, peers, _frames = tcp_login_fixture(
        tmp_path, candidates=(target,)
    )
    calls = []
    fixture.controller.set_auto_battle_enabled(True)
    fixture.controller._login_only_recovery_fingerprints.clear()
    fixture.controller._pending_reconnect_fingerprints.clear()
    fixture.controller._pending_reopen_fingerprints.clear()
    fixture.capture.states[new.handle] = 1
    fixture.controller._run_auto_battle_for_connected = (
        lambda *args, **kwargs: calls.append(args[0].handle) or True
    )
    fixture.controller.reconnect()
    fixture.controller._clear_reconnect_session(_old.launch_fingerprint)
    calls.clear()
    fixture.controller.reconnect()
    assert set(calls) == {new.handle, *(peer.handle for peer in peers)}


def test_tcp_login_session_clears_on_stop_group_switch_and_revocation(tmp_path):
    target = CharacterSelectionCandidate(120, None, 1, True,
                                         CHARACTER_ENTER_CLICK_POINT,
                                         digit_count=3, identity="AlphaHero")
    fixture, old, _new, _peers, _frames = tcp_login_fixture(
        tmp_path, candidates=(target,)
    )
    fixture.controller.set_execution_enabled(False)
    assert fixture.controller._login_only_recovery_fingerprints == set()

    fixture, old, _new, peers, _frames = tcp_login_fixture(
        tmp_path, candidates=(target,)
    )
    fixture.controller.set_group_launch_plan(make_group_plan(tmp_path, peers, "next"))
    assert fixture.controller._login_only_recovery_fingerprints == set()

    fixture, old, _new, _peers, _frames = tcp_login_fixture(
        tmp_path, candidates=(target,)
    )
    with fixture.controller._screen_state_lock:
        fixture.controller._mark_fingerprints_unknown_locked(
            (old.launch_fingerprint,), revoke_runtime_authority=True
        )
    assert fixture.controller._login_only_recovery_fingerprints == set()


def test_clearing_group_plan_revokes_snapshot_and_all_mutations(tmp_path):
    window = make_window(1)
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [1, 2],
        windows=[window],
        expected_windows=1,
        points={2: (0.5, 0.5)},
        battle_restarter=restarter,
        group_launch_plan=make_tcp_group_plan(tmp_path, [window]),
        target_windows_provider=lambda: tcp_resolved_targets([window]),
    )
    assert activate_current_window_snapshot(fixture).success is True
    fixture.controller.set_group_launch_plan(None)

    fixture.controller.reconnect()
    fixture.controller.reconnect()

    assert fixture.controller._group_launch_plan is None
    assert fixture.controller._activation_snapshot_instances is None
    assert fixture.controller._tcp_s
    assert all(
        state.entry_id is None
        for state in fixture.controller._tcp_s.values()
    )
    assert not fixture.controller._execution_enabled.is_set()
    assert fixture.mouse.clicks == []
    assert restarter.calls == []
    assert restarter.reopen_calls == []


def test_switching_group_plan_drops_old_initial_login_authority(tmp_path):
    window = make_window(1)
    fixture = make_controller(
        [3],
        windows=[window],
        expected_windows=1,
        group_launch_plan=make_tcp_group_plan(tmp_path, [window], "old"),
        target_windows_provider=lambda: tcp_resolved_targets([window]),
    )
    assert activate_current_window_snapshot(fixture).success is True
    assert fixture.controller._initial_login_authorizations

    fixture.controller.set_group_launch_plan(
        make_tcp_group_plan(tmp_path, [window], "new")
    )
    fixture.controller.reconnect()
    fixture.controller.reconnect()

    assert fixture.controller._initial_login_authorizations == {}
    assert fixture.mouse.clicks == []


@pytest.mark.parametrize(
    "observations",
    [
        [None],
        [{101: 1, 102: 1, 103: 1}],
    ],
)
def test_tcp_unknown_or_normal_never_restarts(observations, tmp_path):
    windows = [make_window(index, process_id=100 + index) for index in (1, 2, 3)]
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [1, 1, 1],
        windows=windows,
        expected_windows=3,
        tcp_connection_count_provider=SequenceTcpCounts(observations),
        target_windows_provider=lambda: tcp_resolved_targets(windows),
        battle_restarter=restarter,
        group_launch_plan=make_tcp_group_plan(tmp_path, windows),
    )

    fixture.controller.check_connection()

    assert restarter.calls == []
    assert restarter.reopen_calls == []
    assert fixture.mouse.clicks == []


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


def test_selected_group_missing_identity_blocks_confirmed_open_role_action():
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
    assert result.success is False
    assert result.code == "reconnect.waiting"
    assert result.details["expected_windows"] == 2
    assert result.details["discovered_windows"] == 1
    assert result.details["all_connected"] is False
    assert "group_identity_set_mismatch" in result.details["failure_codes"]
    assert fixture.capture.calls == [1, 1]
    assert fixture.mouse.clicks == []
    assert fixture.mouse.expected_process_ids == []
    assert fixture.mouse.instance_tokens == []


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
            windows=(windows[0],),
            global_failure_codes=("window_identity_duplicate",),
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


def test_single_blocked_source_window_isolated_while_healthy_window_recovers():
    now = [0.0]
    healthy = make_window(1, fingerprint="a" * 64)
    blocked = make_window(2, fingerprint="b" * 64)
    provider_state = {
        "value": ResolvedTargetWindows(
            windows=(healthy,),
            target_failure_evidence=(
                tcp_target_failure(
                    "entry-blocked",
                    blocked.launch_fingerprint,
                    "window_identity_duplicate",
                ),
            ),
        )
    }
    fixture = make_controller(
        [2, 2],
        windows=[healthy, blocked],
        expected_windows=2,
        clock=lambda: now[0],
        target_windows_provider=lambda: provider_state["value"],
    )

    prepared = activate_current_window_snapshot(fixture)
    first = fixture.controller.reconnect()
    now[0] = 5.0
    recovered = fixture.controller.reconnect()

    assert prepared.success is True
    assert prepared.details["window_count"] == 1
    assert prepared.details["isolated_window_count"] == 1
    assert first.details["clicked_windows"] == 0
    assert recovered.details["clicked_windows"] == 1
    assert fixture.capture.calls == [healthy.handle, healthy.handle, healthy.handle]
    assert fixture.mouse.clicks == [(healthy.handle, (0.5, 0.5))]
    assert blocked.handle not in fixture.capture.calls
    assert all(handle != blocked.handle for handle, _point in fixture.mouse.clicks)


def test_offline_target_source_failure_prevents_false_connected_result():
    windows = [make_window(1)]
    fixture = make_controller(
        [1],
        windows=windows,
        expected_windows=2,
        target_windows_provider=lambda: ResolvedTargetWindows(
            windows=tuple(windows),
            target_failure_evidence=(
                tcp_target_failure(
                    "entry-missing",
                    "f" * 64,
                    "window_offline",
                ),
            ),
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
    local_failure_code = (
        failure_codes[0] if failure_codes else "window_identity_duplicate"
    )
    provider_state["value"] = ResolvedTargetWindows(
        windows=(windows[0],),
        target_failure_evidence=(
            tcp_target_failure("entry-1", affected, local_failure_code),
        ),
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
    provider_state["value"] = ResolvedTargetWindows(
        windows=(windows[0],),
        target_failure_evidence=(
            tcp_target_failure("entry-1", missing, "window_offline"),
        ),
    )

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


def test_scoped_source_subset_without_failure_keeps_source_generation():
    windows = [make_window(1), make_window(2)]
    selected = frozenset(window.launch_fingerprint for window in windows)
    provider_state = {"value": ResolvedTargetWindows(tuple(windows))}
    fixture = make_controller(
        [1, 1],
        windows=windows,
        expected_windows=2,
        target_windows_provider=lambda: provider_state["value"],
    )
    fixture.controller.set_allowed_fingerprints(selected)
    assert fixture.controller.reconnect().code == "reconnect.connected"
    generation_before = fixture.controller._source_state_generation

    missing = windows[1].launch_fingerprint
    provider_state["value"] = ResolvedTargetWindows(
        windows=(windows[0],),
        target_failure_evidence=(
            tcp_target_failure("entry-1", missing, "window_offline"),
        ),
    )
    result = fixture.controller.reconnect()

    assert result.details["all_connected"] is False
    assert result.details["source_missing_windows"] == 1
    assert fixture.controller._source_state_generation > generation_before
    assert fixture.controller.role_screen_states() == {
        windows[0].launch_fingerprint: ReconnectScreenState.CONNECTED,
        missing: ReconnectScreenState.UNKNOWN,
    }


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

    def revoke_then_restore(fingerprints, **kwargs):
        original_revoke(fingerprints, **kwargs)
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
    provider_state["value"] = ResolvedTargetWindows(
        (windows[0],),
        target_failure_evidence=(
            tcp_target_failure("entry-1", missing, "window_offline"),
        ),
    )

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

    def capture_then_revoke(window_arg, fingerprint_arg, **kwargs):
        result = original_capture(window_arg, fingerprint_arg, **kwargs)
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


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("handle", 0),
        ("handle", True),
        ("process_id", 0),
        ("process_id", True),
        ("thread_id", 0),
        ("thread_id", True),
        ("window_class", "   "),
        ("process_lifecycle_token", 0),
        ("process_lifecycle_token", True),
        ("rect", (0, 0, 0, 600)),
        ("minimized", 1),
    ),
)
def test_capture_rejects_incomplete_instance_before_any_observation(
    field,
    value,
    monkeypatch,
):
    window = replace(make_window(1), **{field: value})
    fixture = make_controller([1], windows=[window], expected_windows=1)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("incomplete window must not reach this path")

    monkeypatch.setattr(
        fixture.controller,
        "_window_is_fully_visible_without_capture",
        forbidden,
    )
    monkeypatch.setattr(
        fixture.controller,
        "_remember_capture_route",
        forbidden,
    )
    monkeypatch.setattr(
        fixture.controller._recognizer,
        "recognize_capture",
        forbidden,
    )

    sample, recognition, fresh_capture, route = (
        fixture.controller._capture_and_recognize(
            window,
            window.launch_fingerprint,
            execute=True,
        )
    )

    assert sample is None
    assert recognition.state is ReconnectScreenState.UNKNOWN
    assert fresh_capture is False
    assert route is None
    assert fixture.capture.calls == []
    assert fixture.mouse.clicks == []


def test_observation_replaces_connected_evidence_for_incomplete_instance():
    window = make_window(1)
    fixture = make_controller([1], windows=[window], expected_windows=1)
    fingerprint = window.launch_fingerprint

    first = fixture.controller.observe_screen_states(
        (fingerprint,),
        candidate_windows=(window,),
    )
    incomplete_window = replace(window, thread_id=0)
    second = fixture.controller.observe_screen_states(
        (fingerprint,),
        candidate_windows=(incomplete_window,),
    )

    assert first == {fingerprint: ReconnectScreenState.CONNECTED}
    assert second == {fingerprint: ReconnectScreenState.UNKNOWN}
    assert fixture.capture.calls == [window.handle]
    assert fixture.controller.role_screen_states() == second
    assert fingerprint not in fixture.controller._trusted_connected_evidence


def test_global_empty_source_revokes_missing_connected_evidence():
    window = make_window(1)
    provider_state = {"value": ResolvedTargetWindows((window,))}
    fixture = make_controller(
        [1],
        windows=[window],
        expected_windows=14,
        require_expected_window_count=False,
        target_windows_provider=lambda: provider_state["value"],
    )
    fingerprint = window.launch_fingerprint

    connected = fixture.controller.reconnect()
    generation_before = fixture.controller._source_state_generation
    provider_state["value"] = ResolvedTargetWindows(())
    result = fixture.controller.reconnect()

    assert connected.code == "reconnect.connected"
    assert result.details["source_missing_windows"] == 1
    assert fixture.controller.role_screen_states() == {
        fingerprint: ReconnectScreenState.UNKNOWN,
    }
    assert fixture.controller._trusted_connected_evidence == {}
    assert fixture.controller._source_state_generation > generation_before
    assert fixture.capture.calls == [window.handle]


def test_global_source_subset_revokes_all_connected_evidence():
    windows = [make_window(1), make_window(2)]
    provider_state = {"value": ResolvedTargetWindows(tuple(windows))}
    fixture = make_controller(
        [1, 1],
        windows=windows,
        expected_windows=14,
        require_expected_window_count=False,
        target_windows_provider=lambda: provider_state["value"],
    )

    connected = fixture.controller.reconnect()
    generation_before = fixture.controller._source_state_generation
    provider_state["value"] = ResolvedTargetWindows(
        (windows[0],),
        global_failure_codes=("target_window_provider_failed",),
    )
    result = fixture.controller.reconnect()

    assert connected.code == "reconnect.connected"
    assert result.details["source_missing_windows"] == 2
    assert fixture.controller.role_screen_states() == {
        windows[0].launch_fingerprint: ReconnectScreenState.UNKNOWN,
        windows[1].launch_fingerprint: ReconnectScreenState.UNKNOWN,
    }
    assert fixture.controller._trusted_connected_evidence == {}
    assert fixture.controller._source_state_generation > generation_before
    assert fixture.capture.calls == [
        window.handle for window in windows
    ] + [windows[0].handle]


def test_global_source_revocation_rejects_late_passive_connected_state(
    monkeypatch,
):
    window = make_window(1)
    provider_state = {"value": ResolvedTargetWindows((window,))}
    fixture = make_controller(
        [1],
        windows=[window],
        expected_windows=14,
        require_expected_window_count=False,
        target_windows_provider=lambda: provider_state["value"],
    )
    fingerprint = window.launch_fingerprint
    assert fixture.controller.reconnect().code == "reconnect.connected"

    captured = threading.Event()
    release_capture = threading.Event()
    observed = []
    original_capture = fixture.controller._capture_and_recognize

    def capture_then_wait(
        window_arg,
        fingerprint_arg,
        *,
        execute=False,
        **kwargs,
    ):
        result = original_capture(
            window_arg,
            fingerprint_arg,
            execute=execute,
            **kwargs,
        )
        if not execute:
            captured.set()
            assert release_capture.wait(1) is True
        return result

    monkeypatch.setattr(
        fixture.controller,
        "_capture_and_recognize",
        capture_then_wait,
    )
    observer = threading.Thread(
        target=lambda: observed.append(
            fixture.controller.observe_screen_states(
                (fingerprint,),
                candidate_windows=(window,),
            )
        ),
    )
    observer.start()
    assert captured.wait(1) is True
    provider_state["value"] = ResolvedTargetWindows(())
    result = fixture.controller.reconnect()
    release_capture.set()
    observer.join(1)

    assert observer.is_alive() is False
    assert result.details["source_missing_windows"] == 1
    assert observed == [{fingerprint: ReconnectScreenState.UNKNOWN}]
    assert fixture.controller.role_screen_states() == {
        fingerprint: ReconnectScreenState.UNKNOWN,
    }
    assert fixture.controller._trusted_connected_evidence == {}


@pytest.mark.parametrize("shared_field", ("handle", "process_id"))
def test_passive_observation_rejects_cross_fingerprint_instance_conflicts(
    shared_field,
    monkeypatch,
):
    first = make_window(1)
    second = replace(
        make_window(2),
        **{shared_field: getattr(first, shared_field)},
    )
    fixture = make_controller([1, 1], windows=[first, second])

    def forbidden(*_args, **_kwargs):
        raise AssertionError("conflicting candidates must not reach this path")

    monkeypatch.setattr(
        fixture.controller,
        "_window_is_fully_visible_without_capture",
        forbidden,
    )
    monkeypatch.setattr(
        fixture.controller,
        "_remember_capture_route",
        forbidden,
    )
    monkeypatch.setattr(
        fixture.controller._recognizer,
        "recognize_capture",
        forbidden,
    )

    observed = fixture.controller.observe_screen_states(
        (first.launch_fingerprint, second.launch_fingerprint),
        candidate_windows=(first, second),
    )

    assert observed == {
        first.launch_fingerprint: ReconnectScreenState.UNKNOWN,
        second.launch_fingerprint: ReconnectScreenState.UNKNOWN,
    }
    assert fixture.capture.calls == []
    assert fixture.mouse.clicks == []


def test_passive_observation_rejects_duplicate_fingerprint_before_capture(
    monkeypatch,
):
    first = make_window(1)
    duplicate = replace(
        make_window(2),
        launch_fingerprint=first.launch_fingerprint,
    )
    fixture = make_controller([1, 1], windows=[first, duplicate])

    def forbidden(*_args, **_kwargs):
        raise AssertionError("ambiguous candidates must not reach this path")

    monkeypatch.setattr(
        fixture.controller,
        "_window_is_fully_visible_without_capture",
        forbidden,
    )
    monkeypatch.setattr(
        fixture.controller,
        "_remember_capture_route",
        forbidden,
    )
    monkeypatch.setattr(
        fixture.controller._recognizer,
        "recognize_capture",
        forbidden,
    )

    observed = fixture.controller.observe_screen_states(
        (first.launch_fingerprint,),
        candidate_windows=(first, duplicate),
    )

    assert observed == {
        first.launch_fingerprint: ReconnectScreenState.UNKNOWN,
    }
    assert fixture.capture.calls == []
    assert fixture.mouse.clicks == []


def test_passive_observation_revokes_connected_evidence_for_instance_conflict():
    first = make_window(1)
    conflicting = replace(make_window(2), handle=first.handle)
    backend = FakeWindowBackend([first])
    fixture = make_controller(
        [1],
        windows=[first],
        window_backend=backend,
    )

    connected = fixture.controller.observe_screen_states(
        (first.launch_fingerprint,),
        candidate_windows=(first,),
    )
    backend.windows = [first, conflicting]
    conflicted = fixture.controller.observe_screen_states(
        (first.launch_fingerprint,),
        candidate_windows=(first, conflicting),
    )

    assert connected == {
        first.launch_fingerprint: ReconnectScreenState.CONNECTED,
    }
    assert conflicted == {
        first.launch_fingerprint: ReconnectScreenState.UNKNOWN,
    }
    assert fixture.capture.calls == [first.handle]
    assert fixture.controller.role_screen_states() == conflicted
    assert first.launch_fingerprint not in (
        fixture.controller._trusted_connected_evidence
    )


def test_source_failure_blocks_final_click_for_a_selected_group():
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
            windows=(windows[0],),
            global_failure_codes=("unidentified_candidate_window",),
        ),
    )
    fixture.controller.set_allowed_fingerprints(selected)

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.success is False
    assert result.code == "reconnect.waiting"
    assert result.details["clicked_windows"] == 0
    assert "unidentified_candidate_window" in result.details["failure_codes"]
    assert fixture.mouse.clicks == []
    assert windows[0].launch_fingerprint not in (
        fixture.controller._action_confirmations
    )


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


def test_global_reconnect_never_enters_selected_role_without_primary_identity():
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

    assert result.details["clicked_windows"] == 0
    assert mouse.clicks == []


def test_global_reconnect_never_uses_unique_highest_as_primary_identity():
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

    assert result.details["clicked_windows"] == 0
    assert mouse.clicks == []


def test_candidate_importance_without_registered_identity_is_not_authority():
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

    assert result.details["clicked_windows"] == 0
    assert mouse.clicks == []


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
    assert result.details["next_check_seconds"] == 2


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
    assert second.details["next_check_seconds"] == 2


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
    assert [call[0] for call in restarter.calls] == [windows[0]]
    assert result.details["next_check_seconds"] == 2


def test_battle_disconnect_without_unique_target_retries_short_and_zero_input():
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
    assert result.details["next_check_seconds"] == 2
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
    assert tuple(item.message for item in statuses.snapshot()) == (
        "120古－重連失敗",
    )

    fixture.controller._primary_entry_authorized.add(
        windows[0].launch_fingerprint
    )
    complete_with_fresh_connected_frames(fixture)
    assert statuses.snapshot() == ()


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

    assert tuple(item.message for item in statuses.snapshot()) == (
        "目前組別中的未知角色－重連失敗",
    )


def test_missing_reopen_retries_immediately_without_touching_other_roles(
    tmp_path,
):
    now = [0.0]
    windows = [make_window(1), make_window(2)]
    plan = make_tcp_group_plan(tmp_path, windows, "120")
    provider_state = {"value": tcp_resolved_targets(windows)}
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [2, 1],
        windows=windows,
        clock=lambda: now[0],
        battle_markers={2},
        battle_restarter=restarter,
        group_launch_plan=plan,
        target_windows_provider=lambda: provider_state["value"],
        failure_status_service=ReconnectFailureStatusService(),
    )
    assert activate_current_window_snapshot(fixture).success is True

    fixture.controller.reconnect()
    now[0] = 5.0
    first = fixture.controller.reconnect()
    assert first.details["restarted_windows"] == 1
    reopen_count_after_initial_restart = len(restarter.reopen_calls)
    fixture.controller._window_backend.windows = [windows[1]]
    provider_state["value"] = tcp_missing_target(
        [windows[1]],
        blocked=(windows[0].launch_fingerprint,),
    )

    now[0] = 6.0
    before = fixture.controller.reconnect()
    now[0] = 7.0
    retry = fixture.controller.reconnect()
    now[0] = 8.0
    no_duplicate = fixture.controller.reconnect()
    now[0] = 9.0
    next_retry = fixture.controller.reconnect()

    assert before.details["restarted_windows"] == 0
    assert retry.details["restarted_windows"] == 1
    assert no_duplicate.details["restarted_windows"] == 0
    assert next_retry.details["restarted_windows"] == 1
    assert len(restarter.reopen_calls) == reopen_count_after_initial_restart + 2


def test_failed_battle_restart_is_attempted_once_until_new_disconnect_event(
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
        tcp_connection_count_provider=lambda process_ids: {
            process_id: 1 for process_id in process_ids
        },
    )

    fixture.controller.reconnect()
    now[0] = 5.0
    first = fixture.controller.reconnect()
    assert len(restarter.calls) == 1
    now[0] = 6.0
    before = fixture.controller.reconnect()
    assert len(restarter.calls) == 1
    now[0] = 8.0
    same_event = fixture.controller.reconnect()

    assert len(restarter.calls) == 1
    fixture.capture.states[windows[0].handle] = 1
    now[0] = 9.0
    fixture.controller.reconnect()
    fixture.capture.states[windows[0].handle] = 2
    now[0] = 10.0
    fixture.controller.reconnect()
    now[0] = 15.0
    new_event = fixture.controller.reconnect()

    assert first.details["next_check_seconds"] == 2
    assert before.details["restarted_windows"] == 0
    assert same_event.details["restarted_windows"] == 0
    assert new_event.details["next_check_seconds"] == 2
    assert len(restarter.calls) == 2
    assert all(call[0].handle == windows[0].handle for call in restarter.calls)


@pytest.mark.parametrize(
    "authority_change",
    ("capture_route", "capture_revision", "source_generation"),
)
def test_failed_battle_restart_public_flow_keeps_same_event_one_shot(
    tmp_path,
    authority_change,
):
    now = [0.0]
    windows = [make_window(1), make_window(2)]
    backend = FullyVisibleWindowBackend(windows)
    visible_capture = FakeCaptureProvider({1: 2, 2: 1})
    obscured_capture = FakeCaptureProvider({1: 2, 2: 1})
    plan = make_tcp_group_plan(tmp_path, windows)
    provider_state = {"value": tcp_resolved_targets(windows)}
    restarter = FakeBattleRestarter(succeeds=False)
    fixture = make_controller(
        [2, 1],
        windows=windows,
        clock=lambda: now[0],
        battle_markers={2},
        battle_restarter=restarter,
        group_launch_plan=plan,
        failure_status_service=ReconnectFailureStatusService(),
        target_windows_provider=lambda: provider_state["value"],
        visible_capture_provider=visible_capture,
        obscured_capture_provider=obscured_capture,
        window_backend=backend,
    )
    fixture.controller.set_auto_battle_enabled(False)
    assert activate_current_window_snapshot(fixture).success is True

    fixture.controller.reconnect()
    now[0] = 5.0
    fixture.controller.reconnect()
    assert len(restarter.calls) == 1

    if authority_change == "capture_route":
        visible_capture.states[windows[0].handle] = None
        backend.top_window_at = lambda _x, _y: 999999
        recovery_times = (6.0, 7.0, 12.0)
    elif authority_change == "capture_revision":
        fixture.controller.set_capture_settings(
            SmartReconnectCaptureSettings(
                visible=True,
                obscured=True,
                minimized=False,
            )
        )
        recovery_times = (6.0, 7.0, 12.0)
    else:
        generation_before = fixture.controller._source_state_generation
        provider_state["value"] = ResolvedTargetWindows(
            windows=tuple(windows),
            target_failure_evidence=(
                tcp_target_failure(
                    "entry-0",
                    windows[0].launch_fingerprint,
                    "window_identity_blocked",
                ),
            ),
        )
        now[0] = 6.0
        fixture.controller.reconnect()
        assert fixture.controller._source_state_generation > generation_before
        provider_state["value"] = tcp_resolved_targets(windows)
        recovery_times = (7.0, 8.0, 12.0)

    for timestamp in recovery_times:
        now[0] = timestamp
        fixture.controller.reconnect()

    assert len(restarter.calls) == 1
    assert restarter.calls[0][0].handle == windows[0].handle
    assert fixture.mouse.clicks == []


def test_each_known_role_failure_records_without_restarting_any_role(
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

    assert restarter.calls == []
    assert restarter.reopen_calls == []
    assert records[0] == ("120古", "重連失敗")
    assert records[1] == ("120古", "重連失敗")
    assert len(records) == 2


def test_known_role_failure_without_status_service_does_not_restart(tmp_path):
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

    assert restarter.calls == []
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


def test_failure_report_only_records_after_capture_settings_change(
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

    fixture.controller._report_reconnect_failure(window.launch_fingerprint)

    assert failures.has(f"role:{window.launch_fingerprint}") is True
    assert restarter.calls == []
    assert restarter.reopen_calls == []


def test_timeout_rebuilds_two_frame_evidence_before_retrying_same_action():
    now = [0.0]
    fixture = make_controller([2, 1], clock=lambda: now[0])

    observed = fixture.controller.reconnect()
    confirmed = fixture.controller.reconnect()
    now[0] = 5.0
    first = fixture.controller.reconnect()
    now[0] = 64.0
    before_deadline = fixture.controller.reconnect()
    now[0] = 65.0
    at_timeout = fixture.controller.reconnect()
    now[0] = 66.0
    rebuilt_first = fixture.controller.reconnect()
    now[0] = 71.0
    second = fixture.controller.reconnect()

    assert observed.details["clicked_windows"] == 0
    assert confirmed.details["clicked_windows"] == 0
    assert first.details["clicked_windows"] == 1
    assert before_deadline.details["clicked_windows"] == 0
    assert at_timeout.details["clicked_windows"] == 0
    assert rebuilt_first.details["clicked_windows"] == 0
    assert second.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [
        (1, (0.5, 0.5)),
        (1, (0.5, 0.5)),
    ]


def test_confirm_keeps_disconnect_wait_then_advances_on_new_states():
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

    now[0] = 1009.0
    before_deadline = second.controller.reconnect()
    now[0] = 1010.0
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
    first.controller._primary_entry_authorized.add(
        make_window(1).launch_fingerprint
    )
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


def test_login_start_after_disconnect_waits_for_existing_stable_transition():
    now = [0.0]
    fixture = make_controller([2, 1], clock=lambda: now[0])

    fixture.controller.reconnect()
    now[0] = 5.0
    disconnected = fixture.controller.reconnect()
    fixture.capture.states[1] = 3
    now[0] = 6.0
    fixture.controller.reconnect()
    now[0] = 7.0
    immediate = fixture.controller.reconnect()
    now[0] = 14.999
    before_stable = fixture.controller.reconnect()
    now[0] = 15.0
    stable = fixture.controller.reconnect()

    assert disconnected.details["clicked_windows"] == 1
    assert immediate.details["clicked_windows"] == 0
    assert before_stable.details["clicked_windows"] == 0
    assert stable.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [
        (1, (0.5, 0.5)),
        (1, (0.505, 0.856)),
    ]


def test_force_login_timeout_clears_start_evidence_then_waits_before_retry():
    now = [0.0]
    fixture = make_controller([2, 1], clock=lambda: now[0])

    fixture.controller.reconnect()
    now[0] = 5.0
    fixture.controller.reconnect()
    fixture.capture.states[1] = 3
    now[0] = 6.0
    fixture.controller.reconnect()
    now[0] = 15.0
    fixture.controller.reconnect()

    fixture.capture.states[1] = 9
    now[0] = 16.0
    fixture.controller.reconnect()
    now[0] = 17.0
    timeout_confirmed = fixture.controller.reconnect()

    fixture.capture.states[1] = 3
    now[0] = 18.0
    fixture.controller.reconnect()
    now[0] = 19.0
    immediate_retry = fixture.controller.reconnect()
    now[0] = 26.999
    before_stable = fixture.controller.reconnect()
    now[0] = 27.0
    stable_retry = fixture.controller.reconnect()

    assert timeout_confirmed.details["clicked_windows"] == 1
    assert immediate_retry.details["clicked_windows"] == 0
    assert before_stable.details["clicked_windows"] == 0
    assert stable_retry.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [
        (1, (0.5, 0.5)),
        (1, (0.505, 0.856)),
        (1, (0.5, 0.57)),
        (1, (0.505, 0.856)),
    ]


@pytest.mark.parametrize(
    "authority_change",
    ("source_generation", "instance", "capture_route", "capture_revision"),
)
def test_stable_force_login_rechecks_every_final_authority(authority_change):
    now = [0.0]
    windows = [make_window(1), make_window(2)]
    backend = FullyVisibleWindowBackend(windows)

    class MutatingCapture(FakeCaptureProvider):
        controller = None

        def __init__(self, states):
            super().__init__(states)
            self.login_calls = 0

        def capture(self, handle):
            sample = super().capture(handle)
            if handle == 1 and self.states.get(handle) == 3:
                self.login_calls += 1
                if self.login_calls == 3:
                    if authority_change == "source_generation":
                        with self.controller._source_authority_lock:
                            self.controller._source_state_generation += 1
                    elif authority_change == "instance":
                        backend.windows[0] = replace(
                            backend.windows[0],
                            thread_id=backend.windows[0].thread_id + 1,
                        )
                    elif authority_change == "capture_revision":
                        self.controller.set_capture_settings(
                            SmartReconnectCaptureSettings(
                                visible=True,
                                obscured=True,
                                minimized=False,
                            )
                        )
            return sample

    visible = MutatingCapture({1: 2, 2: 1})
    obscured = FakeCaptureProvider({1: 2, 2: 1})

    class RouteChangingRecognizer(FakeRecognizer):
        def __init__(self):
            super().__init__(
                {
                    1: ReconnectScreenState.CONNECTED,
                    2: ReconnectScreenState.DISCONNECTED,
                    3: ReconnectScreenState.LOGIN_START,
                },
                points={2: (0.5, 0.5), 3: (0.5, 0.8)},
            )
            self.login_calls = 0

        def recognize_capture(self, sample):
            result = super().recognize_capture(sample)
            if sample.pixels[0] == 3:
                self.login_calls += 1
                if (
                    authority_change == "capture_route"
                    and self.login_calls == 2
                ):
                    visible.states[1] = None
                    backend.top_window_at = lambda _x, _y: 999999
            return result

    fixture = make_controller(
        [2, 1],
        windows=windows,
        clock=lambda: now[0],
        window_backend=backend,
        visible_capture_provider=visible,
        obscured_capture_provider=obscured,
        primary_capture_is_trusted=False,
        recognizer=RouteChangingRecognizer(),
    )
    visible.controller = fixture.controller

    fixture.controller.reconnect()
    now[0] = 5.0
    fixture.controller.reconnect()
    visible.states[1] = 3
    obscured.states[1] = 3
    now[0] = 6.0
    fixture.controller.reconnect()
    now[0] = 15.0
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == [(1, (0.5, 0.5))]


def test_disconnected_flow_restores_game_with_login_and_character_selection():
    now = [0.0]
    windows = [make_window(1), make_window(2)]
    capture = FakeCaptureProvider({1: 2, 2: 1})
    mouse = FakeMouseBackend()

    class DisconnectFlowRecognizer:
        def __init__(self):
            self.character_selection_frames = 0

        def recognize_capture(self, sample):
            marker = sample.pixels[0]
            if marker == 2:
                return ScreenRecognition(
                    state=ReconnectScreenState.DISCONNECTED,
                    score=0.0,
                    click_point=(0.5, 0.5),
                    reference_name="disconnected",
                    battle_context=False,
                )
            if marker == 3:
                return ScreenRecognition(
                    state=ReconnectScreenState.LOGIN_START,
                    score=0.0,
                    click_point=(0.5, 0.8),
                    reference_name="login_start",
                    battle_context=False,
                )
            if marker in {1, 10, 11, 12}:
                return ScreenRecognition(
                    state=ReconnectScreenState.CONNECTED,
                    score=0.0,
                    click_point=None,
                    reference_name="connected",
                    battle_context=False,
                )
            self.character_selection_frames += 1
            return ScreenRecognition(
                state=ReconnectScreenState.CHARACTER_SELECTION,
                score=0.0,
                click_point=(0.651, 0.706),
                reference_name="character_selection",
                character_level=160,
                character_importance=CharacterImportance.PRIMARY,
                character_slot_index=2,
                character_slot_selected=True,
                character_candidates=(
                    CharacterSelectionCandidate(
                        160,
                        CharacterImportance.PRIMARY,
                        2,
                        True,
                        (0.651, 0.706),
                        digit_count=3,
                        identity="160帥",
                    ),
                ),
            )

    recognizer = DisconnectFlowRecognizer()
    controller = WindowsSmartReconnectController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=FakeWindowBackend(windows),
        capture_provider=capture,
        primary_capture_is_trusted=True,
        recognizer=recognizer,
        mouse_backend=mouse,
        execution_enabled=True,
        monotonic_clock=lambda: now[0],
        registered_role_provider=lambda: (
            RegisteredReconnectRole(
                "160帥",
                CharacterImportance.PRIMARY,
            ),
        ),
    )

    # First frame of disconnected.
    controller.reconnect()
    # Second frame confirms disconnection and clicks the login button.
    now[0] = 5.0
    first = controller.reconnect()
    assert first.details["clicked_windows"] == 1
    assert mouse.clicks == [(1, (0.5, 0.5))]

    capture.states[1] = 3
    # Login first frame.
    now[0] = 10.0
    controller.reconnect()
    # Login second frame confirms and clicks start game.
    now[0] = 15.0
    second = controller.reconnect()
    assert second.details["clicked_windows"] == 1
    assert mouse.clicks == [
        (1, (0.5, 0.5)),
        (1, (0.505, 0.856)),
    ]

    capture.states[1] = 5
    # Character selection first frame selects the planned role slot.
    now[0] = 20.0
    controller.reconnect()
    # Character selection second frame confirms and clicks enter.
    now[0] = 25.0
    third = controller.reconnect()
    assert third.details["clicked_windows"] == 1
    assert mouse.clicks == [
        (1, (0.5, 0.5)),
        (1, (0.505, 0.856)),
        (1, CHARACTER_ENTER_CLICK_POINT),
    ]

    # Simulate three fresh connected frames and confirm auto-reconnection end.
    complete_with_fresh_connected_frames(
        Fixture(controller=controller, capture=capture, mouse=mouse),
        now=now,
    )
    finish = controller.reconnect()

    assert finish.code == "reconnect.connected"
    assert finish.details["connected_windows"] == 2
    assert finish.details["all_connected"] is True
    assert controller.reconnecting_fingerprints() == frozenset()


def test_character_selection_confirms_exact_role_before_entering_game(tmp_path):
    windows = [make_window(1), make_window(2)]
    capture = FakeCaptureProvider({1: 5, 2: 1})
    mouse = FakeMouseBackend()
    now = [0.0]

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
        monotonic_clock=lambda: now[0],
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
    now[0] = 9.999
    before_entry_transition = controller.reconnect()
    now[0] = 10.0
    second = controller.reconnect()

    assert first.details["clicked_windows"] == 1
    assert before_entry_transition.details["clicked_windows"] == 0
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


def test_activation_snapshot_rejects_empty_and_all_incomplete_windows():
    empty = make_controller([1], windows=[make_window(1)], expected_windows=1)
    empty.controller.set_execution_enabled(False)
    empty.controller._window_backend.windows = []
    assert empty.controller.prepare_execution_snapshot().code == (
        "reconnect.snapshot_empty"
    )

    fingerprint = "a" * 64
    duplicate = make_controller(
        [1, 1],
        windows=[
            make_window(1, fingerprint=fingerprint),
            make_window(2, fingerprint=fingerprint),
        ],
    )
    duplicate.controller.set_execution_enabled(False)
    duplicate_result = duplicate.controller.prepare_execution_snapshot()
    assert duplicate_result.success is True
    assert duplicate_result.details["window_count"] == 2
    assert len(duplicate.controller._allowed_fingerprints) == 2

    incomplete = make_controller(
        [1],
        windows=[make_window(1, process_id=0)],
        expected_windows=1,
    )
    incomplete.controller.set_execution_enabled(False)
    assert incomplete.controller.prepare_execution_snapshot().code == (
        "reconnect.snapshot_identity_unsafe"
    )


def test_group_activation_snapshot_excludes_only_unique_empty_offline_entry(
    tmp_path,
):
    active = [
        make_window(1, fingerprint="a" * 64),
        make_window(2, fingerprint="b" * 64),
    ]
    offline = make_window(3, fingerprint="c" * 64)
    plan = make_tcp_group_plan(tmp_path, [*active, offline])
    resolved = tcp_resolved_targets(
        active,
        entry_ids=("entry-0", "entry-1"),
        scope_entry_ids=("entry-0", "entry-1", "entry-2"),
        target_failures=(
            tcp_target_failure(
                "entry-2",
                offline.launch_fingerprint,
                "window_offline",
            ),
        ),
    )
    fixture = make_controller(
        [1, 1],
        windows=active,
        expected_windows=3,
        group_launch_plan=plan,
        target_windows_provider=lambda: resolved,
    )

    prepared = activate_current_window_snapshot(fixture)

    assert prepared.success is True
    assert prepared.details["window_count"] == 2
    assert prepared.details["isolated_window_count"] == 1
    assert tuple(
        fixture.controller._activation_snapshot_source_fingerprints.values()
    ) == tuple(window.launch_fingerprint for window in active)
    assert all(
        monitored_window.launch_fingerprint == monitor_fingerprint
        for monitor_fingerprint, (monitored_window, _instance)
        in fixture.controller._activation_snapshot_candidate_instances(
            active
        )[0].items()
    )
    assert len(fixture.controller._activation_snapshot_instances) == 2


def test_group_activation_snapshot_tracks_detection_only_without_authority(
    tmp_path,
):
    configured = make_window(1, process_id=101, fingerprint="a" * 64)
    detection_only = tuple(
        make_window(
            index,
            process_id=100 + index,
            fingerprint=f"{index:064x}",
        )
        for index in (2, 3, 4)
    )
    windows = (configured, *detection_only)
    resolved = tcp_resolved_targets(
        (configured,),
        detection_only_windows=detection_only,
    )
    tcp = SequenceTcpCounts(
        [
            {
                window.process_id: 1
                for window in windows
            }
        ] * 3
    )
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [1, 3, 5, 2],
        windows=windows,
        expected_windows=4,
        group_launch_plan=make_tcp_group_plan(tmp_path, [configured]),
        target_windows_provider=lambda: resolved,
        tcp_connection_count_provider=tcp,
        battle_restarter=restarter,
    )

    prepared = activate_current_window_snapshot(fixture)
    initial_authorizations = set(
        fixture.controller._initial_login_authorizations
    )
    fixture.controller.check_connection()
    fixture.controller.reconnect()

    assert prepared.success is True
    assert prepared.details["window_count"] == 4
    assert fixture.controller._detection_only_fingerprints == frozenset(
        window.launch_fingerprint for window in detection_only
    )
    assert initial_authorizations == {
        configured.launch_fingerprint
    }
    assert tcp.calls == [
        frozenset(window.process_id for window in windows)
    ] * 2
    assert sum(
        state.entry_id is None
        for state in fixture.controller._tcp_s.values()
    ) == 3
    assert fixture.controller._pending_reopen_fingerprints == set()
    assert fixture.controller._login_only_recovery_fingerprints == set()
    assert restarter.calls == []
    assert restarter.reopen_calls == []
    assert fixture.mouse.clicks == []


def test_configured_empty_roles_monitor_live_subset_without_recovery(
    tmp_path,
):
    configured = tuple(
        make_window(
            index + 1,
            process_id=1001 + index,
            fingerprint=f"{index + 1:064x}",
        )
        for index in range(103)
    )
    live = configured[:4]
    entry_ids = tuple(f"entry-{index}" for index in range(103))
    plan = make_tcp_group_plan(tmp_path, configured, "configured")
    plan = replace(
        plan,
        targets=tuple(
            replace(target, role_id="") for target in plan.targets
        ),
    )
    resolved = tcp_resolved_targets(
        live,
        entry_ids=entry_ids[:4],
        scope_entry_ids=entry_ids,
        target_failures=tuple(
            tcp_target_failure(
                entry_ids[index],
                configured[index].launch_fingerprint,
                "window_offline",
            )
            for index in range(4, 103)
        ),
    )
    tcp = SequenceTcpCounts(
        [
            {
                window.process_id: 1
                for window in live
            }
        ]
    )
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [1] * 4,
        windows=live,
        expected_windows=103,
        group_launch_plan=plan,
        target_windows_provider=lambda: resolved,
        tcp_connection_count_provider=tcp,
        battle_restarter=restarter,
    )

    prepared = activate_current_window_snapshot(fixture)
    observed = fixture.controller.check_connection()

    assert prepared.success is True
    assert prepared.details["window_count"] == 4
    assert prepared.details["isolated_window_count"] == 99
    assert fixture.controller._detection_only_fingerprints == frozenset(
        window.launch_fingerprint for window in live
    )
    assert fixture.controller._initial_login_authorizations == {}
    assert observed.details["tcp_observation"]["observed_window_count"] == 4
    assert tcp.calls == [
        frozenset(window.process_id for window in live)
    ]
    assert {
        state.entry_id for state in fixture.controller._tcp_s.values()
    } == set(entry_ids[:4])
    assert all(
        window.launch_fingerprint
        not in fixture.controller._allowed_fingerprints
        for window in configured[4:]
    )
    assert fixture.controller._pending_reopen_fingerprints == set()
    assert fixture.controller._login_only_recovery_fingerprints == set()
    assert restarter.calls == []
    assert restarter.reopen_calls == []
    assert fixture.mouse.clicks == []


def test_configured_empty_role_confirms_tcp_without_recovery_authority(
    tmp_path,
):
    window = make_window(1, process_id=101, fingerprint="a" * 64)
    plan = make_tcp_group_plan(tmp_path, (window,), "configured")
    plan = replace(
        plan,
        targets=(replace(plan.targets[0], role_id=""),),
    )
    resolved = tcp_resolved_targets((window,))
    now = [0.0]
    tcp = SequenceTcpCounts(
        [{101: 1}, {101: 0}, {101: 0}, {101: 0}, {101: 0}]
    )
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [255, 1],
        windows=(window,),
        expected_windows=1,
        battle_markers=(1,),
        clock=lambda: now[0],
        group_launch_plan=plan,
        target_windows_provider=lambda: resolved,
        tcp_connection_count_provider=tcp,
        battle_restarter=restarter,
    )
    auto_calls = []
    fixture.controller._run_auto_battle_for_connected = (
        lambda *args, **kwargs: auto_calls.append(args) or True
    )
    assert activate_current_window_snapshot(fixture).success is True

    fixture.controller.check_connection()
    confirmed = None
    for observed_at in (1.0, 4.0, 8.0):
        now[0] = observed_at
        confirmed = fixture.controller.check_connection()
    now[0] = 9.0
    attempted = fixture.controller.reconnect()

    assert confirmed is not None
    assert "tcp_disconnect_confirmed" in confirmed.details["failure_codes"]
    assert "recovery_identity_unavailable" in (
        confirmed.details["failure_codes"]
    )
    state = next(iter(fixture.controller._tcp_s.values()))
    assert state.entry_id == "entry-0"
    assert state.zero_count >= 3
    assert fixture.controller._detection_only_fingerprints == frozenset(
        (window.launch_fingerprint,)
    )
    assert "recovery_identity_unavailable" in (
        attempted.details["failure_codes"]
    )
    assert fixture.controller._pending_reopen_fingerprints == set()
    assert fixture.controller._login_only_recovery_fingerprints == set()
    assert restarter.calls == []
    assert restarter.reopen_calls == []
    assert fixture.mouse.clicks == []
    assert auto_calls == []


def test_detection_only_tcp_confirmation_never_gains_mutation_authority(
    tmp_path,
):
    configured = make_window(1, process_id=101, fingerprint="a" * 64)
    detection_only = make_window(2, process_id=102, fingerprint="b" * 64)
    windows = (configured, detection_only)
    resolved = tcp_resolved_targets(
        (configured,),
        detection_only_windows=(detection_only,),
    )
    now = [0.0]
    tcp = SequenceTcpCounts(
        [{101: 1, 102: 1}] + [{101: 1, 102: 0}] * 4
    )
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [255, 1],
        windows=windows,
        expected_windows=2,
        battle_markers=(1,),
        clock=lambda: now[0],
        group_launch_plan=make_tcp_group_plan(tmp_path, [configured]),
        target_windows_provider=lambda: resolved,
        tcp_connection_count_provider=tcp,
        battle_restarter=restarter,
    )
    auto_calls = []
    fixture.controller._run_auto_battle_for_connected = (
        lambda *args, **kwargs: auto_calls.append(args) or True
    )
    assert activate_current_window_snapshot(fixture).success is True

    fixture.controller.check_connection()
    confirmed = None
    for observed_at in (1.0, 4.0, 8.0):
        now[0] = observed_at
        confirmed = fixture.controller.check_connection()
    now[0] = 9.0
    attempted = fixture.controller.reconnect()

    assert confirmed is not None
    assert "tcp_disconnect_confirmed" in confirmed.details["failure_codes"]
    assert "recovery_identity_unavailable" in (
        confirmed.details["failure_codes"]
    )
    state = next(
        state
        for state in fixture.controller._tcp_s.values()
        if state.instance.process_id == detection_only.process_id
    )
    assert state.entry_id is None
    assert state.zero_count >= 3
    assert "recovery_identity_unavailable" in (
        attempted.details["failure_codes"]
    )
    assert fixture.controller._pending_reopen_fingerprints == set()
    assert fixture.controller._login_only_recovery_fingerprints == set()
    assert restarter.calls == []
    assert restarter.reopen_calls == []
    assert fixture.mouse.clicks == []
    assert all(
        args[1] != detection_only.launch_fingerprint
        for args in auto_calls
    )


def test_recovery_owner_ignores_configured_and_ungrouped_detection_only_peers(
    tmp_path,
):
    owner = make_window(1, process_id=101, fingerprint="a" * 64)
    configured_detection = make_window(
        2,
        process_id=102,
        fingerprint="b" * 64,
    )
    ungrouped_detection = make_window(
        3,
        process_id=103,
        fingerprint="c" * 64,
    )
    plan = make_tcp_group_plan(
        tmp_path,
        (owner, configured_detection),
        "configured",
    )
    plan = replace(
        plan,
        targets=(plan.targets[0], replace(plan.targets[1], role_id="")),
    )
    resolved = tcp_resolved_targets(
        (owner, configured_detection),
        detection_only_windows=(ungrouped_detection,),
    )
    restarter = FakeBattleRestarter()
    tcp = SequenceTcpCounts(
        [
            {101: 1, 102: 1, 103: 1},
            *({101: 0, 102: 1, 103: 1} for _index in range(5)),
        ]
    )
    fixture = make_controller(
        [2, 1, 1],
        windows=(owner, configured_detection, ungrouped_detection),
        expected_windows=3,
        battle_markers=(2,),
        group_launch_plan=plan,
        target_windows_provider=lambda: resolved,
        tcp_connection_count_provider=tcp,
        battle_restarter=restarter,
    )
    assert activate_current_window_snapshot(fixture).success is True

    fixture.controller.check_connection()
    fixture.controller.reconnect()
    fixture.controller.reconnect()
    fixture.controller.reconnect()
    restarted = fixture.controller.reconnect()

    assert restarted.details["restarted_windows"] == 1
    assert [call[0] for call in restarter.calls] == [owner]
    assert restarter.calls[0][1] == (
        owner,
        configured_detection,
        ungrouped_detection,
    )
    assert fixture.controller._detection_only_fingerprints == frozenset(
        (
            configured_detection.launch_fingerprint,
            ungrouped_detection.launch_fingerprint,
        )
    )
    assert fixture.controller._pending_reopen_fingerprints == {
        owner.launch_fingerprint
    }
    assert fixture.controller._login_only_recovery_fingerprints == {
        owner.launch_fingerprint
    }
    assert configured_detection.launch_fingerprint not in (
        fixture.controller._pending_reopen_fingerprints
    )
    assert ungrouped_detection.launch_fingerprint not in (
        fixture.controller._pending_reopen_fingerprints
    )
    assert fixture.mouse.clicks == []


def test_configured_owner_recovers_with_detection_only_peers_unchanged(
    tmp_path,
):
    owner = make_window(1, process_id=101, fingerprint="a" * 64)
    detection_only = tuple(
        make_window(
            index,
            process_id=100 + index,
            fingerprint=f"{index:064x}",
        )
        for index in (2, 3, 4)
    )
    windows = (owner, *detection_only)
    resolved = tcp_resolved_targets(
        (owner,),
        detection_only_windows=detection_only,
    )
    restarter = FakeBattleRestarter()
    tcp = SequenceTcpCounts(
        [
            {101: 1, 102: 1, 103: 1, 104: 1},
            *(
                {101: 0, 102: 1, 103: 1, 104: 1}
                for _index in range(5)
            ),
        ]
    )
    fixture = make_controller(
        [2, 1, 1, 1],
        windows=windows,
        expected_windows=4,
        battle_markers=(2,),
        group_launch_plan=make_tcp_group_plan(tmp_path, [owner]),
        target_windows_provider=lambda: resolved,
        tcp_connection_count_provider=tcp,
        battle_restarter=restarter,
    )
    assert activate_current_window_snapshot(fixture).success is True

    fixture.controller.check_connection()
    fixture.controller.reconnect()
    fixture.controller.reconnect()
    fixture.controller.reconnect()
    restarted = fixture.controller.reconnect()

    assert restarted.details["restarted_windows"] == 1
    assert [call[0] for call in restarter.calls] == [owner]
    assert restarter.calls[0][1] == windows
    assert len(restarter.reopen_calls) == 1
    assert restarter.reopen_calls[0][1] == detection_only
    assert fixture.controller._pending_reopen_fingerprints == {
        owner.launch_fingerprint
    }
    assert fixture.controller._login_only_recovery_fingerprints == {
        owner.launch_fingerprint
    }
    assert fixture.mouse.clicks == []


def test_post_close_contract_requires_detection_only_peers_unchanged(
    tmp_path,
):
    owner = make_window(1, process_id=101, fingerprint="a" * 64)
    detection_only = (
        make_window(2, process_id=102, fingerprint="b" * 64),
        make_window(3, process_id=103, fingerprint="c" * 64),
    )
    fixture = make_controller(
        [2, 1, 1],
        windows=(owner, *detection_only),
        expected_windows=3,
        group_launch_plan=make_tcp_group_plan(tmp_path, [owner]),
    )
    before = tcp_resolved_targets(
        (owner,),
        detection_only_windows=detection_only,
    )
    after = tcp_resolved_targets(
        (),
        entry_ids=(),
        scope_entry_ids=("entry-0",),
        target_failures=(
            tcp_target_failure(
                "entry-0",
                owner.launch_fingerprint,
                "window_offline",
            ),
        ),
        detection_only_windows=detection_only,
    )

    assert fixture.controller._post_close_backend_contract(
        before,
        after,
        "entry-0",
        owner,
    ) == detection_only

    changed = replace(
        detection_only[0],
        process_lifecycle_token=999999,
    )
    assert fixture.controller._post_close_backend_contract(
        before,
        replace(
            after,
            detection_only_windows=(changed, detection_only[1]),
        ),
        "entry-0",
        owner,
    ) is None


@pytest.mark.parametrize(
    "unsafe_evidence",
    (
        "other-code",
        "multiple-codes",
        "candidate-window",
        "duplicate-entry",
        "unknown-entry",
        "fingerprint-mismatch",
    ),
)
def test_group_activation_snapshot_rejects_non_pure_offline_evidence(
    tmp_path,
    unsafe_evidence,
):
    active = [
        make_window(1, fingerprint="a" * 64),
        make_window(2, fingerprint="b" * 64),
    ]
    offline = make_window(3, fingerprint="c" * 64)
    plan = make_tcp_group_plan(tmp_path, [*active, offline])
    entry_id = "entry-2"
    fingerprint = offline.launch_fingerprint
    failure_codes = ("window_offline",)
    candidates = ()
    if unsafe_evidence == "other-code":
        failure_codes = ("window_identity_duplicate",)
    elif unsafe_evidence == "multiple-codes":
        failure_codes = ("window_offline", "window_instance_incomplete")
    elif unsafe_evidence == "candidate-window":
        candidates = (offline,)
    elif unsafe_evidence == "unknown-entry":
        entry_id = "entry-unknown"
    elif unsafe_evidence == "fingerprint-mismatch":
        fingerprint = "d" * 64
    evidence = tcp_target_failure(
        entry_id,
        fingerprint,
        *failure_codes,
        candidate_windows=candidates,
    )
    target_failures = (
        (evidence, evidence)
        if unsafe_evidence == "duplicate-entry"
        else (evidence,)
    )
    resolved = tcp_resolved_targets(
        active,
        entry_ids=("entry-0", "entry-1"),
        scope_entry_ids=("entry-0", "entry-1", "entry-2"),
        target_failures=target_failures,
    )
    fixture = make_controller(
        [1, 1],
        windows=active,
        expected_windows=3,
        group_launch_plan=plan,
        target_windows_provider=lambda: resolved,
    )
    fixture.controller.set_execution_enabled(False)

    prepared = fixture.controller.prepare_execution_snapshot()

    assert prepared.success is False
    expected_code = (
        "reconnect.snapshot_source_failed"
        if unsafe_evidence in {
            "duplicate-entry",
            "unknown-entry",
            "fingerprint-mismatch",
        }
        else "reconnect.snapshot_identity_unsafe"
    )
    assert prepared.code == expected_code
    assert fixture.controller._activation_snapshot_instances is None


@pytest.mark.parametrize(
    "unsafe_contract",
    (
        "sync-entry-missing",
        "sync-entry-extra",
        "sync-entry-reordered",
        "sync-entry-duplicate",
        "empty-plan-entry-id",
        "duplicate-plan-entry",
        "scope-entry-missing",
        "scope-entry-extra",
        "scope-entry-reordered",
        "window-count-mismatch",
        "complete-instance-count-mismatch",
        "source-map-count-mismatch",
        "monitor-source-mismatch",
        "monitor-entry-ambiguous",
        "tcp-entry-mismatch",
    ),
)
def test_group_activation_snapshot_rejects_unproven_required_entry(
    tmp_path,
    monkeypatch,
    unsafe_contract,
):
    fingerprints = (
        ("a" * 64, "a" * 64)
        if unsafe_contract == "monitor-entry-ambiguous"
        else ("a" * 64, "b" * 64)
    )
    planned = [
        make_window(index, fingerprint=fingerprint)
        for index, fingerprint in enumerate(fingerprints, start=1)
    ]
    windows = list(planned)
    plan = make_tcp_group_plan(tmp_path, planned)
    entry_ids = ("entry-0", "entry-1")
    scope_entry_ids = ("entry-0", "entry-1")
    if unsafe_contract == "sync-entry-missing":
        entry_ids = ("entry-0",)
    elif unsafe_contract == "sync-entry-extra":
        entry_ids = ("entry-0", "entry-1", "entry-extra")
    elif unsafe_contract == "sync-entry-reordered":
        entry_ids = ("entry-1", "entry-0")
    elif unsafe_contract == "sync-entry-duplicate":
        entry_ids = ("entry-0", "entry-0")
    elif unsafe_contract == "empty-plan-entry-id":
        plan = replace(
            plan,
            targets=(
                replace(plan.targets[0], entry_id=""),
                plan.targets[1],
            ),
        )
    elif unsafe_contract == "duplicate-plan-entry":
        plan = replace(
            plan,
            targets=(
                plan.targets[0],
                replace(plan.targets[1], entry_id="entry-0"),
            ),
        )
    elif unsafe_contract == "scope-entry-missing":
        scope_entry_ids = ("entry-0",)
    elif unsafe_contract == "scope-entry-extra":
        scope_entry_ids = ("entry-0", "entry-1", "entry-extra")
    elif unsafe_contract == "scope-entry-reordered":
        scope_entry_ids = ("entry-1", "entry-0")
    elif unsafe_contract == "window-count-mismatch":
        windows = windows[:1]
    resolved = tcp_resolved_targets(
        windows,
        entry_ids=entry_ids,
        scope_entry_ids=scope_entry_ids,
    )
    fixture = make_controller(
        [1, 1],
        windows=windows,
        expected_windows=2,
        group_launch_plan=plan,
        target_windows_provider=lambda: resolved,
    )
    if unsafe_contract in {
        "complete-instance-count-mismatch",
        "source-map-count-mismatch",
        "monitor-source-mismatch",
    }:
        complete, sources, isolated = (
            fixture.controller._activation_snapshot_candidate_instances(
                windows
            )
        )
        monitor_fingerprint = next(iter(complete))
        if unsafe_contract == "complete-instance-count-mismatch":
            complete = {
                key: value
                for key, value in complete.items()
                if key != monitor_fingerprint
            }
        elif unsafe_contract == "source-map-count-mismatch":
            sources = {
                key: value
                for key, value in sources.items()
                if key != monitor_fingerprint
            }
        else:
            sources = {**sources, monitor_fingerprint: "d" * 64}
        monkeypatch.setattr(
            fixture.controller,
            "_activation_snapshot_candidate_instances",
            lambda _windows: (complete, sources, isolated),
        )
    if unsafe_contract in {"monitor-entry-ambiguous", "tcp-entry-mismatch"}:
        monkeypatch.setattr(
            fixture.controller,
            "_tcp_id",
            lambda *_args: (
                "entry-0"
                if unsafe_contract == "monitor-entry-ambiguous"
                else "entry-wrong"
            ),
        )
    fixture.controller.set_execution_enabled(False)

    prepared = fixture.controller.prepare_execution_snapshot()

    assert prepared.success is False
    if unsafe_contract == "window-count-mismatch":
        assert prepared.code == "reconnect.snapshot_source_failed"
        assert prepared.details["failure_codes"] == [
            "target_failure_unattributed"
        ]
    else:
        assert prepared.code == "reconnect.snapshot_identity_unsafe"
        assert prepared.details["failure_codes"] == [
            "window_identity_unsafe"
        ]
    assert fixture.controller._activation_snapshot_instances is None


def test_activation_snapshot_isolates_one_incomplete_shared_executable_window():
    now = [0.0]
    shared_fingerprint = "e" * 64
    windows = [
        make_window(handle, fingerprint=shared_fingerprint)
        for handle in range(1, 15)
    ] + [
        make_window(15, fingerprint=shared_fingerprint, process_id=0),
    ]
    fixture = make_controller(
        [2, *([1] * 14)],
        windows=windows,
        expected_windows=15,
        clock=lambda: now[0],
    )

    prepared = activate_current_window_snapshot(fixture)
    fixture.controller.reconnect()
    now[0] = 5.0
    result = fixture.controller.reconnect()

    assert prepared.success is True
    assert prepared.details["window_count"] == 14
    assert prepared.details["isolated_window_count"] == 1
    assert len(fixture.controller._allowed_fingerprints) == 14
    assert 15 not in fixture.capture.calls
    assert result.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [(1, (0.5, 0.5))]


def test_activation_snapshot_authorizes_known_login_only_after_two_frames():
    fixture = make_controller([3, 1])

    prepared = activate_current_window_snapshot(fixture)
    first = fixture.controller.reconnect()
    second = fixture.controller.reconnect()

    assert prepared.success is True
    assert prepared.details["window_count"] == 2
    assert first.details["clicked_windows"] == 0
    assert second.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [(1, (0.505, 0.856))]


@pytest.mark.parametrize(
    ("marker", "expected_point"),
    (
        (4, (0.5, 0.327)),
        (6, (0.86, 0.12)),
    ),
)
def test_activation_snapshot_authorizes_known_initial_flow_screens(
    marker,
    expected_point,
):
    fixture = make_controller([marker], expected_windows=1)
    activate_current_window_snapshot(fixture)

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [(1, expected_point)]


def test_activation_snapshot_unknown_and_single_login_frame_never_click():
    unknown = make_controller([255], expected_windows=1)
    activate_current_window_snapshot(unknown)
    unknown.controller.reconnect()
    unknown_result = unknown.controller.reconnect()

    one_frame = make_controller([3], expected_windows=1)
    activate_current_window_snapshot(one_frame)
    first = one_frame.controller.reconnect()
    one_frame.capture.states[1] = 255
    second = one_frame.controller.reconnect()

    ambiguous_role = make_controller([5], expected_windows=1)
    activate_current_window_snapshot(ambiguous_role)
    ambiguous_role.controller.reconnect()
    ambiguous_result = ambiguous_role.controller.reconnect()

    assert unknown_result.details["actionable_windows"] == 0
    assert unknown.mouse.clicks == []
    assert first.details["clicked_windows"] == 0
    assert second.details["clicked_windows"] == 0
    assert one_frame.mouse.clicks == []
    assert ambiguous_result.details["clicked_windows"] == 0
    assert ambiguous_role.mouse.clicks == []


def test_initial_login_authorization_is_revoked_by_capture_setting_change():
    fixture = make_controller([3], expected_windows=1)
    activate_current_window_snapshot(fixture)
    fixture.controller.reconnect()

    fixture.controller.set_capture_settings(
        SmartReconnectCaptureSettings(
            visible=True,
            obscured=False,
            minimized=True,
        )
    )
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.controller._initial_login_authorizations == {}
    assert fixture.mouse.clicks == []


def test_activation_snapshot_ignores_windows_opened_later():
    original = make_window(1, fingerprint="a" * 64)
    foreign = make_window(2, fingerprint="b" * 64)
    fixture = make_controller(
        [1],
        windows=[original],
        expected_windows=1,
    )
    activate_current_window_snapshot(fixture)
    fixture.controller._window_backend.windows.append(foreign)
    fixture.capture.states[foreign.handle] = 3

    first = fixture.controller.reconnect()
    second = fixture.controller.reconnect()

    assert first.details["discovered_windows"] == 1
    assert second.details["discovered_windows"] == 1
    assert fixture.mouse.clicks == []
    assert set(fixture.controller._activation_snapshot_instances) == {
        original.launch_fingerprint
    }


def test_snapshot_replacement_requires_existing_reconnect_session():
    fingerprint = "a" * 64
    original = make_window(1, fingerprint=fingerprint)
    replacement = make_window(9, fingerprint=fingerprint)
    fixture = make_controller(
        [1],
        windows=[original],
        expected_windows=1,
    )
    activate_current_window_snapshot(fixture)
    fixture.controller._window_backend.windows = [replacement]
    fixture.capture.states[replacement.handle] = 3

    denied = fixture.controller.reconnect()

    assert denied.details["discovered_windows"] == 0
    assert denied.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []
    assert fixture.controller._activation_snapshot_instances[
        fingerprint
    ].handle == original.handle

    fixture.controller._pending_reconnect_fingerprints.add(fingerprint)
    generation_before = fixture.controller._source_state_generation
    fixture.controller.reconnect()
    accepted = fixture.controller.reconnect()

    assert fixture.controller._activation_snapshot_instances[
        fingerprint
    ].handle == replacement.handle
    assert fixture.controller._source_state_generation > generation_before
    assert accepted.details["discovered_windows"] == 1


def test_snapshot_duplicate_identity_blocks_every_window_action():
    fingerprint = "a" * 64
    original = make_window(1, fingerprint=fingerprint)
    collision = make_window(2, fingerprint=fingerprint)
    fixture = make_controller(
        [3],
        windows=[original],
        expected_windows=1,
    )
    activate_current_window_snapshot(fixture)
    fixture.controller.reconnect()
    fixture.controller._window_backend.windows = [original, collision]
    fixture.capture.states[collision.handle] = 3

    result = fixture.controller.reconnect()

    assert result.details["discovered_windows"] == 1
    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []


def test_stop_revokes_snapshot_and_initial_login_authorization():
    fixture = make_controller([3], expected_windows=1)
    activate_current_window_snapshot(fixture)

    fixture.controller.set_execution_enabled(False)

    assert fixture.controller._activation_snapshot_instances is None
    assert fixture.controller._initial_login_authorizations == {}
    assert fixture.controller._allowed_fingerprints is None
    fixture.controller.set_execution_enabled(True)
    fixture.controller.reconnect()
    fixture.controller.reconnect()
    assert fixture.mouse.clicks == []


def test_snapshot_battle_with_unsafe_group_plan_stays_disabled(tmp_path):
    window = make_window(1, fingerprint="a" * 64)
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [2],
        windows=[window],
        expected_windows=1,
        battle_restarter=restarter,
        group_launch_plan=make_group_plan(tmp_path, [window]),
    )
    prepared = activate_current_window_snapshot(fixture)

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert prepared.success is False
    assert prepared.code == "reconnect.snapshot_identity_unsafe"
    assert result.details["restarted_windows"] == 0
    assert restarter.calls == []
    assert restarter.reopen_calls == []


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


def test_recent_line_target_change_requires_two_new_matching_frames():
    point = [(0.5, 0.665)]

    class MutableRecentLineRecognizer:
        def recognize_capture(self, _sample):
            return ScreenRecognition(
                ReconnectScreenState.LINE_SELECTION,
                0.0,
                point[0],
                "line-selection",
                line_number=8,
                recent_line_present=True,
            )

    fixture = make_controller(
        [4, 1],
        recognizer=MutableRecentLineRecognizer(),
    )
    fingerprint = make_window(1).launch_fingerprint
    fixture.controller._pending_reconnect_fingerprints.add(fingerprint)

    fixture.controller.reconnect()
    point[0] = (0.5, 0.722)
    changed = fixture.controller.reconnect()
    confirmed = fixture.controller.reconnect()

    assert changed.details["clicked_windows"] == 0
    assert confirmed.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [(1, (0.5, 0.722))]


def test_real_controller_wires_only_non_mutating_capture_routes(monkeypatch):
    tcp_queries = []

    def tcp_provider(process_ids):
        tcp_queries.append(process_ids)
        return {}

    monkeypatch.setattr(
        "adapters.windows_smart_reconnect._ipv4_established_counts_by_pid",
        tcp_provider,
    )
    controller = WindowsSmartReconnectController.for_real_windows(
        reference_dir=Path("assets") / "reconnect_reference",
        expected_windows=1,
    )

    assert isinstance(controller._capture_provider, Win32PrintWindowProvider)
    assert type(controller._capture_provider) is Win32PrintWindowProvider
    assert controller._obscured_capture_provider is None
    assert controller._active_refresh_capture_provider is None
    assert controller._primary_capture_is_trusted is True
    assert controller._primary_capture_is_fresh_without_visibility is False
    assert controller._tcp_counts is tcp_provider
    assert callable(controller._tcp_counts)
    assert tcp_queries == []


def test_real_obscured_window_stays_unknown_without_revealing_it(
    monkeypatch,
):
    window = make_window(1)
    controller = WindowsSmartReconnectController.for_real_windows(
        reference_dir=Path("assets") / "reconnect_reference",
        expected_windows=1,
        window_backend=ObscuredWindowBackend([window]),
    )
    visible = FakeCaptureProvider({window.handle: None})
    passive = FakeCaptureProvider({window.handle: 1})
    monkeypatch.setattr(
        controller._visible_capture_provider,
        "capture",
        visible.capture,
    )
    monkeypatch.setattr(
        controller._capture_provider,
        "capture",
        passive.capture,
    )
    controller._recognizer = FakeRecognizer(
        {1: ReconnectScreenState.CONNECTED}
    )

    observed = controller.observe_screen_states(
        [window.launch_fingerprint]
    )
    assert observed == {
        window.launch_fingerprint: ReconnectScreenState.UNKNOWN
    }
    assert passive.calls == []

    controller.set_execution_enabled(True)
    result = controller.reconnect()

    assert result.details["connected_windows"] == 0
    assert result.details["unknown_windows"] == 1
    assert result.details["clicked_windows"] == 0
    assert passive.calls == [window.handle]
    diagnostic = result.details["capture_diagnostics"][0]
    assert diagnostic["capture_path"] == "obscured"
    assert diagnostic["rejection_gate"] == "capture_not_fresh"


def test_real_minimized_window_stays_unknown_without_restoring_it(
    monkeypatch,
):
    window = make_window(1, minimized=True)
    tcp_queries = []

    def tcp_provider(process_ids):
        tcp_queries.append(process_ids)
        return {window.process_id: 0}

    monkeypatch.setattr(
        "adapters.windows_smart_reconnect._ipv4_established_counts_by_pid",
        tcp_provider,
    )
    controller = WindowsSmartReconnectController.for_real_windows(
        reference_dir=Path("assets") / "reconnect_reference",
        expected_windows=1,
        window_backend=FakeWindowBackend([window]),
    )
    passive = FakeCaptureProvider({window.handle: 1})
    monkeypatch.setattr(
        controller._capture_provider,
        "capture",
        passive.capture,
    )
    controller.set_execution_enabled(True)

    result = controller.reconnect()

    assert result.details["connected_windows"] == 0
    assert result.details["unknown_windows"] == 1
    assert result.details["clicked_windows"] == 0
    assert passive.calls == []
    assert tcp_queries == [frozenset({window.process_id})]
    diagnostic = result.details["capture_diagnostics"][0]
    assert diagnostic["capture_path"] == "minimized"
    assert diagnostic["rejection_gate"] == "capture_failed"


def test_failed_minimized_refresh_never_falls_back_to_passive_pixels():
    window = make_window(1, minimized=True)
    active_refresh = FakeCaptureProvider({window.handle: None})
    fixture = make_controller(
        [3],
        windows=[window],
        expected_windows=1,
        active_refresh_capture_provider=active_refresh,
    )
    fixture.controller._primary_capture_is_fresh_without_visibility = False
    activate_current_window_snapshot(fixture)

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert active_refresh.calls == [window.handle, window.handle]
    assert fixture.capture.calls == []
    assert result.details["unknown_windows"] == 1
    assert fixture.mouse.clicks == []


def test_saved_line_history_never_overrides_current_recent_line(
    tmp_path,
):
    state_path = tmp_path / "reconnect-state.json"

    class RecentLineEightRecognizer:
        def recognize_capture(self, _sample):
            return ScreenRecognition(
                ReconnectScreenState.LINE_SELECTION,
                0.0,
                (0.5, 0.722),
                "line-selection",
                line_number=8,
                recent_line_present=True,
            )

    fixture = make_controller(
        [4, 1],
        state_path=state_path,
        recognizer=RecentLineEightRecognizer(),
    )
    fingerprint = make_window(1).launch_fingerprint
    fixture.controller._pending_reconnect_fingerprints.add(fingerprint)
    fixture.controller._preferred_line_numbers[fingerprint] = 1

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [(1, (0.5, 0.722))]
    saved = ReconnectRuntimeStateStore(state_path).load()
    assert saved.preferred_line_numbers == {fingerprint: 8}


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
    assert all(
        item["capture_path"] == "visible"
        and item["rejection_gate"] is None
        for item in result.details["capture_diagnostics"]
    )


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
        window.launch_fingerprint: ReconnectScreenState.UNKNOWN
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


@pytest.mark.parametrize(
    "authority_change",
    (
        "source_generation",
        "capture_settings_revision",
        "capture_route",
        "temporary_obscured",
    ),
)
def test_same_force_login_timeout_is_confirmed_once_across_authority_changes(
    authority_change,
    monkeypatch,
):
    now = [0.0]
    window = make_window(1)
    fixture = make_controller(
        [9],
        windows=[window],
        expected_windows=1,
        clock=lambda: now[0],
    )
    fingerprint = window.launch_fingerprint
    fixture.controller._pending_reconnect_fingerprints.add(fingerprint)

    fixture.controller.reconnect()
    now[0] = 5.0
    first_delivery = fixture.controller.reconnect()

    assert first_delivery.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [(1, (0.5, 0.57))]

    if authority_change == "source_generation":
        with fixture.controller._source_authority_lock:
            fixture.controller._source_state_generation += 1
    elif authority_change == "capture_settings_revision":
        fixture.controller.set_capture_settings(
            SmartReconnectCaptureSettings(
                visible=True,
                obscured=True,
                minimized=False,
            )
        )
    else:
        original_capture = fixture.controller._capture_and_recognize
        capture_calls = [0]

        def changed_capture(*args, **kwargs):
            sample, recognition, fresh, _route = original_capture(*args, **kwargs)
            capture_calls[0] += 1
            if authority_change == "temporary_obscured" and capture_calls[0] == 1:
                return (
                    sample,
                    ScreenRecognition(
                        ReconnectScreenState.UNKNOWN,
                        None,
                        None,
                        None,
                    ),
                    False,
                    "obscured",
                )
            return sample, recognition, fresh, "obscured"

        monkeypatch.setattr(
            fixture.controller,
            "_capture_and_recognize",
            changed_capture,
        )

    # A capture-settings change revokes the session's ordinary action grants.
    # This explicit retained reconnect context verifies the timeout-event lock
    # itself cannot be bypassed by rebuilding otherwise valid new evidence.
    fixture.controller._pending_reconnect_fingerprints.add(fingerprint)
    now[0] = 15.0
    fixture.controller.reconnect()
    now[0] = 20.0
    fixture.controller.reconnect()
    if authority_change == "temporary_obscured":
        now[0] = 25.0
        fixture.controller.reconnect()

    assert fixture.mouse.clicks == [(1, (0.5, 0.57))]
    assert fingerprint in fixture.controller._force_login_timeout_attempts


def test_force_login_timeout_rearms_only_after_leave_or_new_window_session():
    now = [0.0]
    original = make_window(1, fingerprint="a" * 64)
    backend = FakeWindowBackend([original])
    fixture = make_controller(
        [9],
        windows=[original],
        expected_windows=1,
        clock=lambda: now[0],
        window_backend=backend,
    )
    fingerprint = original.launch_fingerprint
    fixture.controller._pending_reconnect_fingerprints.add(fingerprint)

    fixture.controller.reconnect()
    now[0] = 5.0
    fixture.controller.reconnect()

    fixture.capture.states[original.handle] = 8
    now[0] = 15.0
    left_timeout = fixture.controller.reconnect()
    assert left_timeout.details["clicked_windows"] == 0
    assert fingerprint not in fixture.controller._force_login_timeout_attempts

    fixture.capture.states[original.handle] = 9
    now[0] = 20.0
    fixture.controller.reconnect()
    now[0] = 25.0
    second_event = fixture.controller.reconnect()

    replacement = make_window(3, fingerprint=fingerprint)
    backend.windows = [replacement]
    fixture.capture.states[replacement.handle] = 9
    now[0] = 30.0
    fixture.controller.reconnect()
    now[0] = 35.0
    replacement_event = fixture.controller.reconnect()

    assert second_event.details["clicked_windows"] == 1
    assert replacement_event.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [
        (original.handle, (0.5, 0.57)),
        (original.handle, (0.5, 0.57)),
        (replacement.handle, (0.5, 0.57)),
    ]


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

    fixture.controller._primary_entry_authorized.add(
        make_window(1).launch_fingerprint
    )
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

    def confirm_then_change_settings(target, recognition, **kwargs):
        confirmed = original(target, recognition, **kwargs)
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
    assert result.details["state_counts"] == {"unknown": 1}
    assert fixture.controller.role_screen_states() == {
        window.launch_fingerprint: ReconnectScreenState.UNKNOWN
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

    def confirm_then_change_settings(target, recognition, **kwargs):
        confirmed = original(target, recognition, **kwargs)
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


def test_failed_missing_role_reopen_blocks_open_disconnected_role_action(
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
    assert result.success is False
    assert result.code == "reconnect.waiting"
    assert "group_identity_set_mismatch" in result.details["failure_codes"]
    assert "input_target_changed_before_delivery" in result.details["failure_codes"]
    assert fixture.capture.calls == [2, 2]
    assert fixture.mouse.clicks == []
    assert restarter.calls == []


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
    assert result.details["next_check_seconds"] == (
        fixture.controller._policy.connected_poll_seconds
    )
    assert result.details["failure_codes"] == []
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
            windows=(windows[1],),
            target_failure_evidence=(
                tcp_target_failure(
                    "entry-0",
                    blocked,
                    "window_identity_duplicate",
                ),
            ),
        ),
    )
    fixture.controller._pending_reopen_fingerprints.add(blocked)
    fixture.controller._reopen_retry_after[blocked] = 0.0

    result = fixture.controller.reconnect()
    fixture.controller._report_reconnect_failure(blocked)

    assert result.code == "reconnect.waiting"
    assert "window_identity_duplicate" in result.details["failure_codes"]
    assert restarter.calls == []
    assert restarter.reopen_calls == []
    assert fixture.mouse.clicks == []


def test_switching_group_revokes_old_sessions_and_blocks_incomplete_new_group(
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
    assert result.code == "reconnect.waiting"
    assert fixture.controller.reconnecting_fingerprints() == frozenset(
        {second_group[0].launch_fingerprint}
    )
    assert fixture.capture.calls == [3, 3]
    assert fixture.mouse.clicks == []


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
        monotonic_clock=iter(float(value) for value in range(0, 200, 5)).__next__,
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
            current_time[0] = 10.0
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
    assert fixture.controller._flow_pause_until[fingerprint] == 20.0
    assert fixture.controller._action_wait_seconds(
        fingerprint,
        ReconnectScreenState.LOGIN_START,
        10.0,
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


def test_minimized_recognition_never_delivers_background_input():
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

    assert result.details["clicked_windows"] == 0
    assert active_refresh.calls == [1, 1, 1]
    assert fixture.mouse.clicks == []


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
    assert second.details["clicked_windows"] == 0
    assert active_refresh.calls == [1, 1, 1]
    assert fixture.mouse.clicks == []


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


@pytest.mark.parametrize("replacement_kind", ("fingerprint", "instance"))
def test_passive_capture_rechecks_authoritative_instance_after_capture(
    replacement_kind,
    monkeypatch,
):
    original = make_window(1)
    replacement = replace(
        original,
        process_id=202,
        thread_id=1202,
        process_lifecycle_token=2202,
        launch_fingerprint=(
            make_window(2).launch_fingerprint
            if replacement_kind == "fingerprint"
            else original.launch_fingerprint
        ),
    )
    backend = FakeWindowBackend([original])
    fixture = make_controller(
        [1],
        windows=[original],
        expected_windows=1,
        window_backend=backend,
    )
    original_capture = fixture.controller._capture_and_recognize

    def capture_then_replace(
        window,
        fingerprint,
        *,
        execute=False,
        **kwargs,
    ):
        result = original_capture(
            window,
            fingerprint,
            execute=execute,
            **kwargs,
        )
        if not execute:
            backend.windows = [replacement]
        return result

    monkeypatch.setattr(
        fixture.controller,
        "_capture_and_recognize",
        capture_then_replace,
    )

    observed = fixture.controller.observe_screen_states(
        (original.launch_fingerprint,)
    )

    assert observed == {
        original.launch_fingerprint: ReconnectScreenState.UNKNOWN,
    }
    assert original.launch_fingerprint not in (
        fixture.controller._trusted_connected_evidence
    )
    assert original.launch_fingerprint not in (
        fixture.controller._last_trusted_capture_routes
    )


@pytest.mark.parametrize("replacement_kind", ("same_fingerprint", "new_fingerprint"))
def test_action_confirmation_and_disconnect_wait_restart_for_new_instance(
    replacement_kind,
):
    now = [0.0]
    original = make_window(1)
    replacement = make_window(
        2 if replacement_kind == "same_fingerprint" else 1,
        process_id=202,
        fingerprint=(
            original.launch_fingerprint
            if replacement_kind == "same_fingerprint"
            else make_window(2).launch_fingerprint
        ),
        thread_id=1202,
        process_lifecycle_token=2202,
    )
    backend = FakeWindowBackend([original])
    fixture = make_controller(
        [2],
        windows=[original],
        expected_windows=1,
        window_backend=backend,
        clock=lambda: now[0],
    )
    fixture.controller.reconnect()
    backend.windows = [replacement]
    fixture.capture.states[replacement.handle] = 2

    now[0] = 10.0
    second = fixture.controller.reconnect()
    now[0] = 14.999
    third = fixture.controller.reconnect()
    now[0] = 15.0
    fourth = fixture.controller.reconnect()

    assert second.details["clicked_windows"] == 0
    assert third.details["clicked_windows"] == 0
    assert fourth.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [(replacement.handle, (0.5, 0.5))]


def test_terminal_completion_restarts_three_frame_evidence_for_replaced_instance():
    now = [0.0]
    original = make_window(1)
    replacement = replace(
        original,
        process_id=202,
        thread_id=1202,
        process_lifecycle_token=2202,
    )
    backend = FakeWindowBackend([original])
    fixture = make_controller(
        [10],
        windows=[original],
        expected_windows=1,
        window_backend=backend,
        clock=lambda: now[0],
    )
    fingerprint = original.launch_fingerprint
    fixture.controller._pending_reconnect_fingerprints.add(fingerprint)
    fixture.controller._primary_entry_authorized.add(fingerprint)
    fixture.controller._terminal_ready_after[fingerprint] = 0.0

    fixture.controller.reconnect()
    backend.windows = [replacement]
    fixture.capture.states[replacement.handle] = 11
    now[0] = 5.0
    first_replacement = fixture.controller.reconnect()
    fixture.capture.states[replacement.handle] = 12
    now[0] = 6.0
    second_replacement = fixture.controller.reconnect()
    assert first_replacement.details["state_counts"] == {"reconnecting": 1}
    assert second_replacement.details["state_counts"] == {"reconnecting": 1}
    assert fingerprint in fixture.controller.reconnecting_fingerprints()
    fixture.capture.states[replacement.handle] = 10
    now[0] = 9.0
    completed = fixture.controller.reconnect()

    assert completed.details["state_counts"] == {"connected": 1}
    assert fingerprint not in fixture.controller.reconnecting_fingerprints()


def test_pending_reopen_and_failure_report_reject_unsafe_live_collection(
    tmp_path,
):
    missing = make_window(1)
    first = make_window(2)
    conflicting = replace(
        make_window(3),
        handle=first.handle,
    )
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [1, 1],
        windows=[first, conflicting],
        expected_windows=2,
        battle_restarter=restarter,
        group_launch_plan=make_group_plan(tmp_path, [missing, first]),
    )
    fingerprint = missing.launch_fingerprint
    fixture.controller._pending_reopen_fingerprints.add(fingerprint)

    result = fixture.controller.reconnect()
    fixture.controller._report_reconnect_failure(fingerprint)

    assert result.details["restarted_windows"] == 0
    # This direct private-state seed has no formal login-only session. A later
    # failure report cannot retain a synthetic reopen authorization.
    assert fingerprint not in fixture.controller._pending_reopen_fingerprints
    assert restarter.reopen_calls == []
    assert restarter.calls == []


def test_missing_role_without_formal_session_never_reopens(tmp_path):
    missing = make_window(1)
    present = make_window(2)
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [1],
        windows=[present],
        expected_windows=2,
        battle_restarter=restarter,
        group_launch_plan=make_group_plan(tmp_path, [missing, present]),
    )
    fixture.controller._pending_reopen_fingerprints.add(
        missing.launch_fingerprint
    )

    result = fixture.controller.reconnect()

    assert result.details["restarted_windows"] == 0
    assert restarter.reopen_calls == []


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("thread_id", 9999),
        ("window_class", "ReplacementFlash"),
        ("process_lifecycle_token", 9999),
        ("rect", (1, 1, 901, 601)),
        ("minimized", True),
    ),
)
def test_confirmed_action_token_is_rechecked_before_final_delivery(
    field,
    replacement,
    monkeypatch,
):
    window = make_window(1)
    fixture = make_controller([2], windows=[window], expected_windows=1)
    backend = fixture.controller._window_backend
    original_confirm = fixture.controller._action_is_confirmed

    def confirm_then_replace(fingerprint, recognition, **kwargs):
        confirmed = original_confirm(fingerprint, recognition, **kwargs)
        if confirmed:
            backend.windows = [replace(window, **{field: replacement})]
        return confirmed

    monkeypatch.setattr(
        fixture.controller,
        "_action_is_confirmed",
        confirm_then_replace,
    )

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []


@pytest.mark.parametrize("unsafe_frame", ("missing_point", "unknown"))
def test_action_confirmation_requires_two_contiguous_safe_frames(
    unsafe_frame,
):
    now = [0.0]
    window = make_window(1)
    fixture = make_controller(
        [2],
        windows=[window],
        expected_windows=1,
        clock=lambda: now[0],
    )
    fixture.controller.reconnect()

    now[0] = 1.0
    if unsafe_frame == "missing_point":
        fixture.controller._recognizer.points[2] = None
    else:
        fixture.capture.states[window.handle] = 255
    interrupted = fixture.controller.reconnect()

    fixture.controller._recognizer.points[2] = (0.5, 0.5)
    fixture.capture.states[window.handle] = 2
    now[0] = 6.0
    first_safe = fixture.controller.reconnect()
    now[0] = 7.0
    second_safe = fixture.controller.reconnect()

    assert interrupted.details["clicked_windows"] == 0
    assert first_safe.details["clicked_windows"] == 0
    if unsafe_frame == "unknown":
        now[0] = 11.0
        third_safe = fixture.controller.reconnect()
        assert second_safe.details["clicked_windows"] == 0
        assert third_safe.details["clicked_windows"] == 1
    else:
        assert second_safe.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [(window.handle, (0.5, 0.5))]


def test_missing_passive_target_revokes_late_disconnected_observation(
    monkeypatch,
):
    requested_window = make_window(1)
    unrelated_window = make_window(2)
    fixture = make_controller(
        [2, 1],
        windows=[unrelated_window],
        expected_windows=1,
    )
    fixture.capture.states[requested_window.handle] = 2
    captured = threading.Event()
    release = threading.Event()
    observed = []
    original_capture = fixture.controller._capture_and_recognize

    def capture_then_wait(
        window,
        fingerprint,
        *,
        execute=False,
        **kwargs,
    ):
        result = original_capture(
            window,
            fingerprint,
            execute=execute,
            **kwargs,
        )
        if not execute and fingerprint == requested_window.launch_fingerprint:
            captured.set()
            assert release.wait(1) is True
        return result

    monkeypatch.setattr(
        fixture.controller,
        "_capture_and_recognize",
        capture_then_wait,
    )
    older = threading.Thread(
        target=lambda: observed.append(
            fixture.controller.observe_screen_states(
                (requested_window.launch_fingerprint,),
                candidate_windows=(requested_window,),
            )
        )
    )
    older.start()
    assert captured.wait(1) is True

    missing = fixture.controller.observe_screen_states(
        (requested_window.launch_fingerprint,),
        candidate_windows=(),
    )
    release.set()
    older.join(1)

    assert older.is_alive() is False
    assert missing == {
        requested_window.launch_fingerprint: ReconnectScreenState.UNKNOWN,
    }
    assert observed == [missing]
    assert fixture.controller.role_screen_states() == missing


def test_passive_source_revocation_breaks_action_confirmation():
    now = [0.0]
    window = make_window(1)
    backend = FakeWindowBackend([window])
    fixture = make_controller(
        [2],
        windows=[window],
        expected_windows=1,
        window_backend=backend,
        clock=lambda: now[0],
    )
    fingerprint = window.launch_fingerprint

    fixture.controller.reconnect()
    assert fingerprint in fixture.controller._action_confirmations
    backend.windows = []
    observed = fixture.controller.observe_screen_states((fingerprint,))

    assert observed == {fingerprint: ReconnectScreenState.UNKNOWN}
    assert fingerprint not in fixture.controller._action_confirmations
    assert fingerprint not in fixture.controller._last_trusted_capture_routes

    backend.windows = [window]
    now[0] = 5.0
    first_after_unknown = fixture.controller.reconnect()
    now[0] = 10.0
    second_after_unknown = fixture.controller.reconnect()

    assert first_after_unknown.details["clicked_windows"] == 0
    assert second_after_unknown.details["clicked_windows"] == 1


def test_source_revocation_before_final_click_blocks_the_old_scan(
    monkeypatch,
):
    now = [0.0]
    window = make_window(1)
    backend = FakeWindowBackend([window])
    fixture = make_controller(
        [2],
        windows=[window],
        expected_windows=1,
        window_backend=backend,
        clock=lambda: now[0],
    )
    fingerprint = window.launch_fingerprint
    fixture.controller.reconnect()
    original_backend_call = fixture.controller._run_authorized_backend_call
    revoked = []

    def revoke_source_at_final_boundary(callback, **kwargs):
        if not revoked:
            backend.windows = []
            observed = fixture.controller.observe_screen_states((fingerprint,))
            backend.windows = [window]
            revoked.append(observed)
        return original_backend_call(callback, **kwargs)

    monkeypatch.setattr(
        fixture.controller,
        "_run_authorized_backend_call",
        revoke_source_at_final_boundary,
    )

    now[0] = 5.0
    blocked = fixture.controller.reconnect()

    assert revoked == [{fingerprint: ReconnectScreenState.UNKNOWN}]
    assert blocked.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []
    assert fingerprint not in fixture.controller._action_confirmations

    now[0] = 10.0
    first_after_revocation = fixture.controller.reconnect()
    now[0] = 15.0
    second_after_revocation = fixture.controller.reconnect()

    assert first_after_revocation.details["clicked_windows"] == 0
    assert second_after_revocation.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [(window.handle, (0.5, 0.5))]


def test_source_revocation_before_final_battle_restart_blocks_old_scan(
    tmp_path,
    monkeypatch,
):
    now = [0.0]
    window = make_window(1)
    backend = FakeWindowBackend([window])
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [2],
        windows=[window],
        expected_windows=1,
        window_backend=backend,
        battle_markers={2},
        battle_restarter=restarter,
        group_launch_plan=make_group_plan(tmp_path, [window]),
        clock=lambda: now[0],
    )
    fingerprint = window.launch_fingerprint
    fixture.controller.reconnect()
    original_backend_call = fixture.controller._run_authorized_backend_call
    revoked = []

    def revoke_source_at_final_boundary(callback, **kwargs):
        if not revoked:
            backend.windows = []
            observed = fixture.controller.observe_screen_states((fingerprint,))
            backend.windows = [window]
            revoked.append(observed)
        return original_backend_call(callback, **kwargs)

    monkeypatch.setattr(
        fixture.controller,
        "_run_authorized_backend_call",
        revoke_source_at_final_boundary,
    )

    now[0] = 5.0
    blocked = fixture.controller.reconnect()

    assert revoked == [{fingerprint: ReconnectScreenState.UNKNOWN}]
    assert blocked.details["restarted_windows"] == 0
    assert restarter.calls == []


def test_source_revocation_before_pending_reopen_blocks_old_scan(
    tmp_path,
    monkeypatch,
):
    now = [0.0]
    missing, present = make_window(1), make_window(2)
    provider_state = {
        "value": tcp_resolved_targets([missing, present]),
    }
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [2, 1],
        windows=[missing, present],
        expected_windows=2,
        clock=lambda: now[0],
        battle_markers={2},
        battle_restarter=restarter,
        group_launch_plan=make_tcp_group_plan(
            tmp_path,
            [missing, present],
        ),
        target_windows_provider=lambda: provider_state["value"],
    )
    assert activate_current_window_snapshot(fixture).success is True
    missing_fingerprint = missing.launch_fingerprint
    present_fingerprint = present.launch_fingerprint
    fixture.controller.reconnect()
    now[0] = 5.0
    assert fixture.controller.reconnect().details["restarted_windows"] == 1
    fixture.controller._window_backend.windows = [present]
    provider_state["value"] = tcp_missing_target(
        [present],
        blocked=(missing_fingerprint,),
    )
    now[0] = 6.0
    fixture.controller.reconnect()
    original_backend_call = fixture.controller._run_authorized_backend_call
    revoked = []
    reopen_count_before_revocation = len(restarter.reopen_calls)

    def revoke_source_at_final_boundary(callback, **kwargs):
        if not revoked:
            observed = fixture.controller.observe_screen_states(
                (present_fingerprint,),
                candidate_windows=(),
            )
            revoked.append(observed)
        return original_backend_call(callback, **kwargs)

    monkeypatch.setattr(
        fixture.controller,
        "_run_authorized_backend_call",
        revoke_source_at_final_boundary,
    )

    now[0] = 7.0
    result = fixture.controller.reconnect()

    assert revoked == [{present_fingerprint: ReconnectScreenState.UNKNOWN}]
    assert result.details["restarted_windows"] == 0
    assert len(restarter.reopen_calls) == reopen_count_before_revocation
    assert missing_fingerprint in fixture.controller._pending_reopen_fingerprints


def test_failure_report_after_revoked_source_generation_stays_read_only(tmp_path):
    window = make_window(1)
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [2],
        windows=[window],
        expected_windows=1,
        battle_restarter=restarter,
        group_launch_plan=make_group_plan(tmp_path, [window]),
    )
    fingerprint = window.launch_fingerprint
    fixture.controller._revoke_source_failure_evidence(
        frozenset({fingerprint}),
        revoke_runtime_authority=True,
    )

    fixture.controller._report_reconnect_failure(fingerprint)

    assert restarter.calls == []
    assert restarter.reopen_calls == []


def test_final_action_delivery_requires_the_complete_selected_group(
    monkeypatch,
):
    now = [0.0]
    target = make_window(1)
    companion = make_window(2)
    backend = FakeWindowBackend([target, companion])
    fixture = make_controller(
        [2, 2],
        windows=[target, companion],
        expected_windows=2,
        window_backend=backend,
        clock=lambda: now[0],
    )
    selected = {
        target.launch_fingerprint,
        companion.launch_fingerprint,
    }
    fixture.controller.set_allowed_fingerprints(selected)

    fixture.controller.reconnect()
    assert target.launch_fingerprint in fixture.controller._action_confirmations
    original_confirm = fixture.controller._action_is_confirmed
    removed_after_confirmation = []

    def confirm_then_remove_companion(fingerprint, recognition, **kwargs):
        confirmed = original_confirm(fingerprint, recognition, **kwargs)
        if (
            confirmed
            and fingerprint == target.launch_fingerprint
            and not removed_after_confirmation
        ):
            removed_after_confirmation.append(fingerprint)
            backend.windows = [target]
        return confirmed

    monkeypatch.setattr(
        fixture.controller,
        "_action_is_confirmed",
        confirm_then_remove_companion,
    )
    now[0] = 5.0
    result = fixture.controller.reconnect()

    assert removed_after_confirmation == [target.launch_fingerprint]
    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []
    assert target.launch_fingerprint not in fixture.controller._action_confirmations
    assert target.launch_fingerprint not in (
        fixture.controller._last_trusted_capture_routes
    )


def test_execution_disable_revokes_old_reconnect_authority_before_reenable():
    now = [0.0]
    window = make_window(1)
    fixture = make_controller(
        [2],
        windows=[window],
        expected_windows=1,
        clock=lambda: now[0],
    )
    fingerprint = window.launch_fingerprint
    fixture.controller.reconnect()
    fixture.controller._pending_reopen_fingerprints.add(fingerprint)
    fixture.controller._active_automation_fingerprints.add(fingerprint)
    fixture.controller._terminal_ready_after[fingerprint] = 0.0

    fixture.controller.set_execution_enabled(False)
    fixture.controller.set_execution_enabled(True)
    now[0] = 5.0
    after_reenable = fixture.controller.reconnect()

    assert after_reenable.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []
    assert fixture.controller._pending_reopen_fingerprints == set()
    assert fixture.controller._active_automation_fingerprints == set()
    assert fixture.controller._terminal_ready_after == {}
    assert fixture.controller._action_confirmations


def test_execution_disable_stops_a_confirmation_before_delivery(monkeypatch):
    fixture = make_controller([2], expected_windows=1, windows=[make_window(1)])
    original_confirm = fixture.controller._action_is_confirmed

    def confirm_then_disable(fingerprint, recognition, **kwargs):
        confirmed = original_confirm(fingerprint, recognition, **kwargs)
        if confirmed:
            fixture.controller.set_execution_enabled(False)
        return confirmed

    monkeypatch.setattr(
        fixture.controller,
        "_action_is_confirmed",
        confirm_then_disable,
    )

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []


def test_battle_restart_obeys_token_bound_disconnect_wait(tmp_path):
    now = [0.0]
    window = make_window(1)
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [2],
        windows=[window],
        expected_windows=1,
        battle_markers={2},
        battle_restarter=restarter,
        group_launch_plan=make_group_plan(tmp_path, [window]),
        clock=lambda: now[0],
    )

    first = fixture.controller.reconnect()
    now[0] = 4.999
    before_wait = fixture.controller.reconnect()
    now[0] = 5.0
    at_wait = fixture.controller.reconnect()

    assert first.details["restarted_windows"] == 0
    assert before_wait.details["restarted_windows"] == 0
    assert at_wait.details["restarted_windows"] == 1
    assert len(restarter.calls) == 1


def test_battle_restart_replacement_restarts_disconnect_wait(tmp_path):
    now = [0.0]
    original = make_window(1)
    replacement = make_window(
        2,
        process_id=202,
        fingerprint=original.launch_fingerprint,
        thread_id=1202,
        process_lifecycle_token=2202,
    )
    backend = FakeWindowBackend([original])
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [2],
        windows=[original],
        expected_windows=1,
        window_backend=backend,
        battle_markers={2},
        battle_restarter=restarter,
        group_launch_plan=make_group_plan(tmp_path, [original]),
        clock=lambda: now[0],
    )

    fixture.controller.reconnect()
    backend.windows = [replacement]
    fixture.capture.states[replacement.handle] = 2
    now[0] = 5.0
    first_replacement = fixture.controller.reconnect()
    now[0] = 9.999
    before_wait = fixture.controller.reconnect()
    now[0] = 10.0
    at_wait = fixture.controller.reconnect()

    assert first_replacement.details["restarted_windows"] == 0
    assert before_wait.details["restarted_windows"] == 0
    assert at_wait.details["restarted_windows"] == 1
    assert len(restarter.calls) == 1


@pytest.mark.parametrize(
    ("initial_marker", "following_marker", "pause_seconds"),
    (
        (4, 5, 3.0),
        (5, 6, 10.0),
        (6, 7, 5.0),
    ),
)
def test_transition_waits_are_controller_authorization_gates(
    initial_marker,
    following_marker,
    pause_seconds,
    monkeypatch,
):
    now = [0.0]
    window = make_window(1)
    fixture = make_controller(
        [initial_marker],
        windows=[window],
        expected_windows=1,
        clock=lambda: now[0],
    )
    fingerprint = window.launch_fingerprint
    fixture.controller._pending_reconnect_fingerprints.add(fingerprint)
    if 6 in {initial_marker, following_marker}:
        fixture.controller._active_automation_fingerprints.add(
            fingerprint
        )
        fixture.controller._active_automation_until[fingerprint] = 60.0
    if 5 in {initial_marker, following_marker}:
        # This test isolates the controller's ten-second transition gate.
        # Character-target identity validation has its own direct coverage.
        monkeypatch.setattr(
            fixture.controller,
            "_recognition_for_session_action",
            lambda _fingerprint, recognition, **_kwargs: recognition,
        )

    fixture.controller.reconnect()
    first_click = fixture.controller.reconnect()
    assert first_click.details["clicked_windows"] == 1
    fixture.capture.states[window.handle] = following_marker

    now[0] = pause_seconds - 0.001
    before_boundary = fixture.controller.reconnect()
    now[0] = pause_seconds
    at_boundary = fixture.controller.reconnect()

    assert before_boundary.details["clicked_windows"] == 0
    assert at_boundary.details["clicked_windows"] == 1
    assert len(fixture.mouse.clicks) == 2


def test_capture_and_screen_locks_complete_observe_setting_and_click(
    monkeypatch,
):
    now = [0.0]
    window = make_window(1)
    fixture = make_controller(
        [2],
        windows=[window],
        expected_windows=1,
        clock=lambda: now[0],
    )
    fingerprint = window.launch_fingerprint
    # Establish the first safe disconnected frame before the concurrent run.
    fixture.controller.reconnect()

    observer_initial_capture_finished = threading.Event()
    observer_holds_screen = threading.Event()
    observer_after_screen = threading.Event()
    allow_observer_finish = threading.Event()
    action_about_to_remember_route = threading.Event()
    setting_finished = threading.Event()
    action_capture_count = [0]
    observed: list[dict[str, ReconnectScreenState]] = []
    action_results = []
    original_capture = fixture.controller._capture_and_recognize
    original_remember = fixture.controller._remember_capture_route

    def capture_with_barrier(
        window,
        role_fingerprint,
        *,
        execute=False,
        **kwargs,
    ):
        is_action = threading.current_thread().name == "reconnect-click"
        is_observer = threading.current_thread().name == "passive-observer"
        if is_action and execute:
            action_capture_count[0] += 1
        result = original_capture(
            window,
            role_fingerprint,
            execute=execute,
            **kwargs,
        )
        if is_action and execute and action_capture_count[0] == 1:
            observer_initial_capture_finished.set()
        if is_observer:
            assert observer_initial_capture_finished.wait(1)
            with fixture.controller._screen_state_lock:
                observer_holds_screen.set()
                assert action_about_to_remember_route.wait(1)
                fixture.controller._capture_settings_snapshot()
            observer_after_screen.set()
            assert allow_observer_finish.wait(1)
        return result

    def remember_with_barrier(role_fingerprint, route):
        if (
            threading.current_thread().name == "reconnect-click"
            and action_capture_count[0] >= 2
        ):
            action_about_to_remember_route.set()
        return original_remember(role_fingerprint, route)

    monkeypatch.setattr(
        fixture.controller,
        "_capture_and_recognize",
        capture_with_barrier,
    )
    monkeypatch.setattr(
        fixture.controller,
        "_remember_capture_route",
        remember_with_barrier,
    )

    observer = threading.Thread(
        name="passive-observer",
        target=lambda: observed.append(
            fixture.controller.observe_screen_states((fingerprint,))
        ),
    )
    observer.start()
    now[0] = 5.0
    action = threading.Thread(
        name="reconnect-click",
        target=lambda: action_results.append(fixture.controller.reconnect()),
    )
    action.start()
    assert observer_holds_screen.wait(1)

    setting = threading.Thread(
        name="capture-setting",
        target=lambda: (
            fixture.controller.set_capture_settings(
                SmartReconnectCaptureSettings(
                    visible=False,
                    obscured=True,
                    minimized=True,
                )
            ),
            setting_finished.set(),
        ),
    )
    setting.start()

    action.join(1)
    assert not action.is_alive()
    assert observer_after_screen.wait(1)
    assert setting_finished.wait(1)
    allow_observer_finish.set()
    observer.join(1)
    setting.join(1)

    assert not observer.is_alive()
    assert not action.is_alive()
    assert not setting.is_alive()
    assert setting_finished.is_set()
    assert observed == [{fingerprint: ReconnectScreenState.UNKNOWN}]
    assert action_results[0].details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [(window.handle, (0.5, 0.5))]
    assert fixture.controller._trusted_connected_evidence == {}
    assert fixture.controller._action_confirmations == {}


def test_initial_login_authorization_enters_one_already_selected_character():
    selected = CharacterSelectionCandidate(
        120,
        CharacterImportance.PRIMARY,
        1,
        True,
        CHARACTER_ENTER_CLICK_POINT,
        digit_count=3,
        identity="AlphaHero",
    )
    fixture, _window = _single_window_character_fixture(
        _CharacterSequenceRecognizer(lambda _call: (selected,)),
        registered_role_provider=lambda: (
            RegisteredReconnectRole(
                "AlphaHero",
                CharacterImportance.PRIMARY,
            ),
        ),
    )

    prepared = activate_current_window_snapshot(fixture)
    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert prepared.success is True
    assert result.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [(1, CHARACTER_ENTER_CLICK_POINT)]


def test_formal_healthy_tcp_keeps_initial_login_authorization_actionable(
    tmp_path,
):
    selected = CharacterSelectionCandidate(
        120,
        CharacterImportance.PRIMARY,
        1,
        True,
        CHARACTER_ENTER_CLICK_POINT,
        digit_count=3,
        identity="AlphaHero",
    )
    window = make_window(1, process_id=101)
    plan = make_tcp_group_plan(tmp_path, [window])
    plan = replace(
        plan,
        targets=tuple(
            replace(target, role_id="AlphaHero") for target in plan.targets
        ),
    )
    fixture = make_controller(
        [5],
        windows=[window],
        expected_windows=1,
        recognizer=_CharacterSequenceRecognizer(
            lambda _call: (selected,)
        ),
        registered_role_provider=lambda: (
            RegisteredReconnectRole(
                "AlphaHero",
                CharacterImportance.PRIMARY,
            ),
        ),
        tcp_connection_count_provider=lambda process_ids: {
            process_id: 1 for process_id in process_ids
        },
        target_windows_provider=lambda: tcp_resolved_targets([window]),
        group_launch_plan=plan,
    )

    assert activate_current_window_snapshot(fixture).success is True
    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [(1, CHARACTER_ENTER_CLICK_POINT)]


def test_formal_tcp_suspected_or_unknown_never_uses_visual_disconnect(
    tmp_path,
):
    windows = [
        make_window(1, process_id=101),
        make_window(2, process_id=102),
    ]
    for tcp_counts, attempts, expected_failure in (
        (
            SequenceTcpCounts(
                [
                    {101: 1, 102: 1},
                    {101: 0, 102: 1},
                    {101: 0, 102: 1},
                ]
            ),
            3,
            "tcp_disconnect_suspected",
        ),
        (lambda _process_ids: {}, 2, "tcp_observation_unavailable"),
    ):
        restarter = FakeBattleRestarter()
        fixture = make_controller(
            [2, 2],
            windows=windows,
            expected_windows=2,
            battle_markers=(2,),
            battle_restarter=restarter,
            tcp_connection_count_provider=tcp_counts,
            target_windows_provider=lambda: tcp_resolved_targets(windows),
            group_launch_plan=make_tcp_group_plan(tmp_path, windows),
        )
        assert activate_current_window_snapshot(fixture).success is True

        for _attempt in range(attempts):
            result = fixture.controller.reconnect()

        assert expected_failure in result.details["failure_codes"]
        assert result.details["restarted_windows"] == 0
        assert restarter.calls == []
        assert fixture.mouse.clicks == []


def test_formal_tcp_owner_blocks_visual_disconnect_peer(tmp_path):
    windows = [
        make_window(1, process_id=101),
        make_window(2, process_id=102),
    ]
    restarter = FakeBattleRestarter()
    now = [0.0]
    fixture = make_controller(
        [1, 2],
        windows=windows,
        expected_windows=2,
        clock=lambda: now[0],
        battle_restarter=restarter,
        tcp_connection_count_provider=SequenceTcpCounts(
            [{101: 1, 102: 1}] + [{101: 0, 102: 1}] * 8
        ),
        target_windows_provider=lambda: tcp_resolved_targets(windows),
        group_launch_plan=make_tcp_group_plan(tmp_path, windows),
    )
    assert activate_current_window_snapshot(fixture).success is True

    for observed_at in (0.0, 1.0, 4.0, 8.0):
        now[0] = observed_at
        fixture.controller.check_connection()
    now[0] = 9.0
    result = fixture.controller.reconnect()

    assert result.details["restarted_windows"] == 1
    assert [call[0] for call in restarter.calls] == [windows[0]]
    assert fixture.mouse.clicks == []


def test_expired_initial_login_authorization_never_enters_character():
    now = [0.0]
    selected = CharacterSelectionCandidate(
        120,
        None,
        1,
        True,
        CHARACTER_ENTER_CLICK_POINT,
        digit_count=3,
    )
    fixture, _window = _single_window_character_fixture(
        _CharacterSequenceRecognizer(lambda _call: (selected,)),
        clock=lambda: now[0],
    )
    activate_current_window_snapshot(fixture)
    fixture.controller.reconnect()

    now[0] = 180.0
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.controller._initial_login_authorizations == {}
    assert fixture.mouse.clicks == []


def test_revoked_initial_login_authorization_never_enters_character():
    selected = CharacterSelectionCandidate(
        120,
        None,
        1,
        True,
        CHARACTER_ENTER_CLICK_POINT,
        digit_count=3,
    )
    fixture, _window = _single_window_character_fixture(
        _CharacterSequenceRecognizer(lambda _call: (selected,))
    )
    activate_current_window_snapshot(fixture)
    fixture.controller.reconnect()

    fixture.controller.set_capture_settings(
        SmartReconnectCaptureSettings(
            visible=True,
            obscured=False,
            minimized=True,
        )
    )
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.controller._initial_login_authorizations == {}
    assert fixture.mouse.clicks == []


def test_initial_login_authorization_rejects_changed_selected_slot():
    first = CharacterSelectionCandidate(
        120,
        None,
        0,
        True,
        CHARACTER_ENTER_CLICK_POINT,
        digit_count=3,
    )
    second = replace(first, slot_index=1)
    recognizer = _CharacterSequenceRecognizer(
        lambda call: (first,) if call == 1 else (second,)
    )
    fixture, _window = _single_window_character_fixture(recognizer)
    activate_current_window_snapshot(fixture)

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []


def test_initial_login_authorization_rejects_nonunique_selected_cards():
    first = CharacterSelectionCandidate(
        120,
        None,
        0,
        True,
        CHARACTER_ENTER_CLICK_POINT,
        digit_count=3,
    )
    second = replace(first, slot_index=1)
    recognizer = _CharacterSequenceRecognizer(
        lambda call: (first,) if call == 1 else (first, second)
    )
    fixture, _window = _single_window_character_fixture(recognizer)
    activate_current_window_snapshot(fixture)

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []


def test_click_time_selected_slot_change_cancels_initial_login_input():
    first = CharacterSelectionCandidate(
        120,
        None,
        0,
        True,
        CHARACTER_ENTER_CLICK_POINT,
        digit_count=3,
        identity="AlphaHero",
    )
    changed = replace(first, slot_index=1)
    recognizer = _CharacterSequenceRecognizer(
        lambda call: (first,) if call <= 2 else (changed,)
    )
    fixture, _window = _single_window_character_fixture(
        recognizer,
        registered_role_provider=lambda: (
            RegisteredReconnectRole(
                "AlphaHero",
                CharacterImportance.PRIMARY,
            ),
        ),
    )
    activate_current_window_snapshot(fixture)

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert recognizer.calls >= 3
    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []


def test_registered_character_ocr_variation_keeps_canonical_target():
    variants = ("AlphaHero", "Alpha…")
    recognizer = _CharacterSequenceRecognizer(
        lambda call: (
            CharacterSelectionCandidate(
                160,
                None,
                2,
                True,
                CHARACTER_ENTER_CLICK_POINT,
                digit_count=3,
                identity=variants[0] if call == 1 else variants[1],
            ),
        )
    )
    fixture, window = _single_window_character_fixture(
        recognizer,
        registered_role_provider=lambda: (
            RegisteredReconnectRole(
                "AlphaHero",
                CharacterImportance.PRIMARY,
            ),
        ),
    )
    fixture.controller._pending_reconnect_fingerprints.add(
        window.launch_fingerprint
    )

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [(1, CHARACTER_ENTER_CLICK_POINT)]


def test_canonical_character_target_change_cancels_click():
    recognizer = _CharacterSequenceRecognizer(
        lambda call: (
            CharacterSelectionCandidate(
                160,
                None,
                2,
                True,
                CHARACTER_ENTER_CLICK_POINT,
                digit_count=3,
                identity="AlphaHero" if call <= 2 else "BetaHero",
            ),
        )
    )
    fixture, window = _single_window_character_fixture(
        recognizer,
        registered_role_provider=lambda: (
            RegisteredReconnectRole(
                "AlphaHero",
                CharacterImportance.PRIMARY,
            ),
            RegisteredReconnectRole(
                "BetaHero",
                CharacterImportance.SECONDARY,
            ),
        ),
    )
    fixture.controller._pending_reconnect_fingerprints.add(
        window.launch_fingerprint
    )

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert recognizer.calls >= 3
    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []


def test_character_selection_uses_two_stages_and_rechecks_same_slot():
    now = [0.0]
    lower = CharacterSelectionCandidate(
        120,
        None,
        0,
        False,
        (0.355, 0.706),
        digit_count=3,
        identity="LowerRole",
    )
    highest = CharacterSelectionCandidate(
        160,
        None,
        2,
        False,
        (0.651, 0.706),
        digit_count=3,
        identity="AlphaHero",
    )
    selected = replace(
        highest,
        selected=True,
        click_point=CHARACTER_ENTER_CLICK_POINT,
        identity="Alpha…",
    )
    phase = ["select"]
    recognizer = _CharacterSequenceRecognizer(
        lambda _call: (
            (lower, highest)
            if phase[0] == "select"
            else (lower, selected)
        )
    )
    fixture, window = _single_window_character_fixture(
        recognizer,
        clock=lambda: now[0],
        registered_role_provider=lambda: (
            RegisteredReconnectRole(
                "AlphaHero",
                CharacterImportance.PRIMARY,
            ),
        ),
    )
    fixture.controller._pending_reconnect_fingerprints.add(
        window.launch_fingerprint
    )

    fixture.controller.reconnect()
    selected_slot = fixture.controller.reconnect()
    phase[0] = "enter"
    now[0] = 10.0
    fixture.controller.reconnect()
    entered = fixture.controller.reconnect()

    assert selected_slot.details["clicked_windows"] == 1
    assert entered.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [
        (1, highest.click_point),
        (1, CHARACTER_ENTER_CLICK_POINT),
    ]


def test_character_selection_rejects_different_slot_after_selection_click():
    now = [0.0]
    first = CharacterSelectionCandidate(
        120,
        None,
        0,
        False,
        (0.355, 0.706),
        digit_count=3,
        identity="OtherRole",
    )
    target = CharacterSelectionCandidate(
        160,
        None,
        2,
        False,
        (0.651, 0.706),
        digit_count=3,
        identity="AlphaHero",
    )
    wrong = replace(
        first,
        selected=True,
        click_point=CHARACTER_ENTER_CLICK_POINT,
    )
    phase = ["select"]
    recognizer = _CharacterSequenceRecognizer(
        lambda _call: (
            (first, target)
            if phase[0] == "select"
            else (wrong, target)
        )
    )
    fixture, window = _single_window_character_fixture(
        recognizer,
        clock=lambda: now[0],
        registered_role_provider=lambda: (
            RegisteredReconnectRole(
                "AlphaHero",
                CharacterImportance.PRIMARY,
            ),
        ),
    )
    fixture.controller._pending_reconnect_fingerprints.add(
        window.launch_fingerprint
    )

    fixture.controller.reconnect()
    fixture.controller.reconnect()
    phase[0] = "wrong"
    now[0] = 10.0
    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == [(1, target.click_point)]


def test_planned_level_is_blocked_by_unknown_three_digit_competitor(tmp_path):
    target = CharacterSelectionCandidate(
        120,
        None,
        0,
        False,
        (0.355, 0.706),
        digit_count=3,
    )
    unknown = CharacterSelectionCandidate(
        None,
        None,
        1,
        False,
        (0.500, 0.706),
        digit_count=3,
    )
    fixture, window = _single_window_character_fixture(
        _CharacterSequenceRecognizer(lambda _call: (target, unknown))
    )
    fixture.controller.set_group_launch_plan(
        GroupLaunchPlan(
            "current",
            targets=(
                GroupLaunchTarget(
                    1,
                    "TargetRole",
                    tmp_path / "target.lnk",
                    window.launch_fingerprint,
                    registered_level=120,
                ),
            ),
        )
    )
    fixture.controller._pending_reconnect_fingerprints.add(
        window.launch_fingerprint
    )

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []


# 第十輪後的直接測試只保留最新明確規格：最近線路、唯一已註冊主號、
# 普通斷線同窗恢復、戰鬥斷線單窗重開，以及共同六十秒期限。


def _recent_line_recognition(
    *,
    line_number=8,
    point=None,
    present=True,
    scroll_delta=0,
    recent_role="120福",
):
    return ScreenRecognition(
        ReconnectScreenState.LINE_SELECTION,
        0.0,
        point,
        "line-selection",
        line_number=line_number,
        recent_line_present=present,
        recent_login_role=recent_role,
        line_scroll_delta=scroll_delta,
    )


def _registered_primary_candidates(*, selected_slot=0):
    return (
        CharacterSelectionCandidate(
            120,
            None,
            0,
            selected_slot == 0,
            (
                CHARACTER_ENTER_CLICK_POINT
                if selected_slot == 0
                else (0.355, 0.706)
            ),
            digit_count=3,
            identity="120古",
        ),
        CharacterSelectionCandidate(
            91,
            None,
            1,
            selected_slot == 1,
            (
                CHARACTER_ENTER_CLICK_POINT
                if selected_slot == 1
                else (0.500, 0.706)
            ),
            digit_count=2,
            identity="91其他",
        ),
        CharacterSelectionCandidate(
            120,
            None,
            2,
            selected_slot == 2,
            (
                CHARACTER_ENTER_CLICK_POINT
                if selected_slot == 2
                else (0.651, 0.706)
            ),
            digit_count=3,
            identity="120福",
        ),
    )


def _registered_roles():
    return (
        RegisteredReconnectRole(
            "120古",
            CharacterImportance.SECONDARY,
        ),
        RegisteredReconnectRole(
            "120福",
            CharacterImportance.PRIMARY,
        ),
    )


def test_fifteen_shared_executable_windows_scroll_two_line_eights_independently():
    now = [0.0]
    shared_fingerprint = "f" * 64
    windows = [
        make_window(handle, fingerprint=shared_fingerprint)
        for handle in range(1, 16)
    ]

    class StatefulLineMouse(FakeMouseBackend):
        def __init__(self):
            super().__init__()
            self.scrolled_handles = set()

        def scroll_relative(
            self,
            handle,
            point,
            delta,
            expected_process_id,
            instance_token,
        ):
            result = super().scroll_relative(
                handle,
                point,
                delta,
                expected_process_id,
                instance_token,
            )
            self.scrolled_handles.add(handle)
            return result

    mouse = StatefulLineMouse()
    product_recognizer = ReferenceScreenRecognizer(
        Path("assets/reconnect_reference")
    )
    with Image.open(
        Path("assets/reconnect_reference") / "03_line_selection_dialog.png"
    ) as source:
        live_line_screen = source.convert("RGB")
    # Make the live frame non-identical to the historical reference while
    # retaining the confirmed line-selection structure and recent route 8.
    live_line_screen.paste((255, 0, 255), (510, 350, 710, 500))
    scrolled_line_screen = live_line_screen.copy()
    scrolled_line_screen.putpixel((0, 0), (1, 1, 1))

    class PublicLineRecognizer:
        def recognize_capture(self, sample):
            marker = sample.pixels[0]
            if marker == 1:
                return ScreenRecognition(
                    ReconnectScreenState.CONNECTED,
                    0.0,
                    None,
                    "connected",
                )
            handle = {41: 1, 42: 2}[marker]
            product_recognizer._visible_line_buttons = (
                lambda _candidate: (
                    ((8, (0.5, 0.722)),)
                    if handle in mouse.scrolled_handles
                    else ()
                )
            )
            return product_recognizer.recognize_image(
                scrolled_line_screen
                if handle in mouse.scrolled_handles
                else live_line_screen
            )

    fixture = make_controller(
        [41, 42, *([1] * 13)],
        windows=windows,
        expected_windows=15,
        clock=lambda: now[0],
        mouse=mouse,
        recognizer=PublicLineRecognizer(),
    )
    prepared = activate_current_window_snapshot(fixture)
    monitor_fingerprints = fixture.controller._allowed_fingerprints

    assert prepared.success is True
    assert prepared.details["window_count"] == 15
    assert prepared.details["isolated_window_count"] == 0
    assert monitor_fingerprints is not None
    assert len(monitor_fingerprints) == 15
    assert all(len(value) == 64 for value in monitor_fingerprints)
    assert shared_fingerprint not in monitor_fingerprints

    fixture.controller.reconnect()
    scrolled = fixture.controller.reconnect()

    assert fixture.mouse.scrolls == [
        (1, (0.5, 0.530), -120),
        (2, (0.5, 0.530), -120),
    ]
    assert fixture.mouse.clicks == []
    assert scrolled.details["clicked_windows"] == 2

    now[0] = 2.0
    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert fixture.mouse.clicks == [
        (1, (0.5, 0.722)),
        (2, (0.5, 0.722)),
    ]
    assert result.details["clicked_windows"] == 2
    assert now[0] < 60.0
    assert all(point != (0.5, 0.327) for _handle, point in fixture.mouse.clicks)
    assert all(handle in {1, 2} for handle, _point in fixture.mouse.clicks)

    fixture.controller.set_execution_enabled(False)

    assert fixture.controller._allowed_fingerprints is None
    assert fixture.controller._activation_snapshot_instances is None
    assert fixture.controller._activation_snapshot_source_fingerprints is None


def test_recent_line_absence_alone_falls_back_to_line_one():
    recognition = _recent_line_recognition(
        line_number=1,
        point=(0.5, 0.327),
        present=False,
        recent_role=None,
    )
    fixture = make_controller(
        [4],
        windows=[make_window(1)],
        expected_windows=1,
        recognizer=RecognitionByMarker({4: recognition}),
    )
    fingerprint = make_window(1).launch_fingerprint
    fixture.controller._pending_reconnect_fingerprints.add(fingerprint)

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [(1, (0.5, 0.327))]
    assert fixture.mouse.scrolls == []


def test_ambiguous_recent_line_never_scrolls_or_clicks():
    fixture = make_controller(
        [4],
        windows=[make_window(1)],
        expected_windows=1,
        recognizer=RecognitionByMarker(
            {4: _recent_line_recognition(line_number=None, present=True)}
        ),
    )
    fingerprint = make_window(1).launch_fingerprint
    fixture.controller._pending_reconnect_fingerprints.add(fingerprint)

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []
    assert fixture.mouse.scrolls == []


def test_registered_primary_beats_selected_equal_level_then_enters_fresh_slot():
    now = [0.0]
    selected_slot = [0]
    recognizer = _CharacterSequenceRecognizer(
        lambda _call: _registered_primary_candidates(
            selected_slot=selected_slot[0]
        )
    )
    fixture, window = _single_window_character_fixture(
        recognizer,
        clock=lambda: now[0],
        registered_role_provider=_registered_roles,
    )
    fingerprint = window.launch_fingerprint
    fixture.controller._pending_reconnect_fingerprints.add(fingerprint)
    fixture.controller._recent_login_role_ids[fingerprint] = "120福"

    fixture.controller.reconnect()
    selected = fixture.controller.reconnect()

    assert selected.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [(1, (0.651, 0.706))]

    selected_slot[0] = 2
    now[0] = 1.0
    fixture.controller.reconnect()
    entered = fixture.controller.reconnect()

    assert entered.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [
        (1, (0.651, 0.706)),
        (1, CHARACTER_ENTER_CLICK_POINT),
    ]


@pytest.mark.skipif(
    not Path(
        r"C:\Users\USER\AppData\Local\Temp\codex-clipboard-73af9ffe-3404-4dcc-9d09-f1a7a75bf3d3.png"
    ).is_file(),
    reason="使用者三槽主號原圖只保留在本機證據路徑",
)
def test_public_reconnect_uses_real_three_slot_primary_then_fresh_selected_frame():
    source_path = Path(
        r"C:\Users\USER\AppData\Local\Temp\codex-clipboard-73af9ffe-3404-4dcc-9d09-f1a7a75bf3d3.png"
    )
    with Image.open(source_path) as source:
        left_selected = source.convert("RGB")
    right_selected = left_selected.copy()
    slots = ((375, 575, 575, 677), (578, 575, 778, 677), (780, 575, 980, 677))

    def selected_side_boxes(slot):
        left, top, right, bottom = slot
        return (
            (left + 8, bottom - 10, right - 8, bottom),
            (left, top + 10, left + 8, bottom - 10),
            (right - 8, top + 10, right, bottom - 10),
        )

    for selected_box, neutral_box, target_box in zip(
        selected_side_boxes(slots[0]),
        selected_side_boxes(slots[1]),
        selected_side_boxes(slots[2]),
    ):
        right_selected.paste(
            left_selected.crop(neutral_box).resize(
                (
                    selected_box[2] - selected_box[0],
                    selected_box[3] - selected_box[1],
                )
            ),
            selected_box[:2],
        )
        right_selected.paste(
            left_selected.crop(selected_box).resize(
                (
                    target_box[2] - target_box[0],
                    target_box[3] - target_box[1],
                )
            ),
            target_box[:2],
        )

    product_recognizer = ReferenceScreenRecognizer(
        Path("assets/reconnect_reference")
    )
    initial = product_recognizer.recognize_image(left_selected)
    assert initial.state is ReconnectScreenState.CHARACTER_SELECTION
    assert len(initial.character_candidates) == 3
    assert initial.character_candidates[2].identity == "120福"

    class PublicImageRecognizer:
        def __init__(self):
            self.current = left_selected

        def recognize_capture(self, _sample):
            return product_recognizer.recognize_image(self.current)

    public = PublicImageRecognizer()
    mouse = FakeMouseBackend()
    original_click = mouse.click_relative

    def click_relative(handle, point, process_id, token):
        result = original_click(handle, point, process_id, token)
        if point == (0.651, 0.706):
            public.current = right_selected
        return result

    mouse.click_relative = click_relative
    window = make_window(1)
    fixture = make_controller(
        [5],
        windows=[window],
        expected_windows=1,
        mouse=mouse,
        recognizer=public,
        registered_role_provider=lambda: (
            RegisteredReconnectRole(
                "120福",
                CharacterImportance.PRIMARY,
            ),
        ),
    )
    fixture.controller._pending_reconnect_fingerprints.add(
        window.launch_fingerprint
    )

    fixture.controller.reconnect()
    fixture.controller.reconnect()
    fixture.controller.reconnect()
    fixture.controller.reconnect()

    assert mouse.clicks == [
        (window.handle, (0.651, 0.706)),
        (window.handle, CHARACTER_ENTER_CLICK_POINT),
    ]


@pytest.mark.parametrize("conflict", ("multiple_primary", "recent_role"))
def test_primary_identity_conflict_is_zero_input_and_retriable(conflict):
    providers = {
        "multiple_primary": lambda: (
            RegisteredReconnectRole(
                "120古",
                CharacterImportance.PRIMARY,
            ),
            RegisteredReconnectRole(
                "120福",
                CharacterImportance.PRIMARY,
            ),
        ),
        "recent_role": _registered_roles,
    }
    fixture, window = _single_window_character_fixture(
        _CharacterSequenceRecognizer(
            lambda _call: _registered_primary_candidates(selected_slot=0)
        ),
        registered_role_provider=providers[conflict],
    )
    fingerprint = window.launch_fingerprint
    fixture.controller._pending_reconnect_fingerprints.add(fingerprint)
    if conflict == "recent_role":
        fixture.controller._recent_login_role_ids[fingerprint] = "120靈"

    fixture.controller.reconnect()
    first = fixture.controller.reconnect()

    assert first.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []
    assert fingerprint not in fixture.controller._character_selection_targets

    fixture.controller._registered_role_provider = _registered_roles
    fixture.controller._recent_login_role_ids[fingerprint] = "120福"
    fixture.controller.reconnect()
    recovered = fixture.controller.reconnect()

    assert recovered.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [(1, (0.651, 0.706))]


def _locked_reconnect_recognitions():
    connected = ScreenRecognition(
        ReconnectScreenState.CONNECTED,
        0.0,
        None,
        "connected",
    )
    return {
        1: connected,
        10: replace(connected, reference_name="connected-10"),
        11: replace(connected, reference_name="connected-11"),
        12: replace(connected, reference_name="connected-12"),
        21: ScreenRecognition(
            ReconnectScreenState.DISCONNECTED,
            0.0,
            (0.5, 0.5),
            "normal-disconnect",
            battle_context=False,
        ),
        22: ScreenRecognition(
            ReconnectScreenState.DISCONNECTED,
            0.0,
            (0.5, 0.5),
            "battle-disconnect",
            battle_context=True,
        ),
        23: ScreenRecognition(
            ReconnectScreenState.LOGIN_START,
            0.0,
            (0.505, 0.856),
            "login-start",
        ),
        24: _recent_line_recognition(
            line_number=8,
            point=(0.5, 0.722),
        ),
        25: _character_recognition(
            _registered_primary_candidates(selected_slot=0)
        ),
        26: _character_recognition(
            _registered_primary_candidates(selected_slot=2)
        ),
        27: ScreenRecognition(
            ReconnectScreenState.CONNECTED,
            0.0,
            None,
            POST_DISCONNECT_WAITING_REFERENCE_FILE,
            battle_context=True,
        ),
    }


def _advance_locked_reconnect_to_connected(
    fixture,
    backend,
    now,
    *,
    original_handle,
    replacement_handle,
):
    fingerprint = backend.windows[0].launch_fingerprint
    if replacement_handle != original_handle:
        replacement = make_window(
            replacement_handle,
            fingerprint=fingerprint,
        )
        backend.windows[0] = replacement
        fixture.capture.states[replacement_handle] = 23
    active_handle = replacement_handle

    for marker, first_time, second_time in (
        (23, 6.0, 15.0),
        (24, 16.0, 17.0),
        (25, 18.0, 19.0),
        (26, 20.0, 29.0),
    ):
        fixture.capture.states[active_handle] = marker
        now[0] = first_time
        fixture.controller.reconnect()
        now[0] = second_time
        fixture.controller.reconnect()

    final = None
    for marker, timestamp in ((10, 30.0), (11, 32.0), (12, 34.0)):
        fixture.capture.states[active_handle] = marker
        now[0] = timestamp
        final = fixture.controller.reconnect()
    return final


def _three_window_reconnect_fixture(tmp_path, *, battle):
    now = [0.0]
    windows = [
        make_window(1, fingerprint="a" * 64),
        make_window(2, fingerprint="b" * 64),
        make_window(3, fingerprint="c" * 64),
    ]
    backend = FakeWindowBackend(windows)
    restarter = FakeBattleRestarter()
    plan = GroupLaunchPlan(
        "current",
        tuple(
            GroupLaunchTarget(
                index,
                "120福" if index == 1 else f"peer-{index}",
                tmp_path / f"target-{index}.lnk",
                window.launch_fingerprint,
                entry_id=f"entry-{index - 1}",
                role_id="120福" if index == 1 else f"peer-{index}",
                registered_level=120 if index == 1 else None,
                importance=(
                    CharacterImportance.PRIMARY if index == 1 else None
                ),
            )
            for index, window in enumerate(windows, start=1)
        ),
    )
    fixture = make_controller(
        [22 if battle else 21, 1, 1],
        windows=windows,
        expected_windows=3,
        clock=lambda: now[0],
        recognizer=RecognitionByMarker(_locked_reconnect_recognitions()),
        battle_restarter=restarter,
        group_launch_plan=plan,
        target_windows_provider=lambda: tcp_resolved_targets(
            backend.windows,
            entry_ids=("entry-0", "entry-1", "entry-2"),
        ),
        registered_role_provider=_registered_roles,
        window_backend=backend,
    )
    fixture.controller.set_auto_battle_enabled(False)
    assert activate_current_window_snapshot(fixture).success is True
    return fixture, backend, restarter, now


def test_normal_map_disconnect_uses_same_window_and_never_restarts(tmp_path):
    fixture, backend, restarter, now = _three_window_reconnect_fixture(
        tmp_path,
        battle=False,
    )

    fixture.controller.reconnect()
    now[0] = 5.0
    confirmed = fixture.controller.reconnect()

    assert confirmed.details["clicked_windows"] == 1
    assert restarter.calls == []

    final = _advance_locked_reconnect_to_connected(
        fixture,
        backend,
        now,
        original_handle=1,
        replacement_handle=1,
    )

    assert restarter.calls == []
    assert final.details["all_connected"] is True
    assert fixture.mouse.clicks == [
        (1, (0.5, 0.5)),
        (1, (0.505, 0.856)),
        (1, (0.5, 0.722)),
        (1, (0.651, 0.706)),
        (1, CHARACTER_ENTER_CLICK_POINT),
    ]
    assert all(handle == 1 for handle, _point in fixture.mouse.clicks)
    completed = [
        item
        for item in fixture.controller.anonymous_reconnect_timing_diagnostics()
        if item.status == "completed"
    ]
    assert {item.lifecycle for item in completed} == {
        "disconnect_to_primary_auto",
        "start_game_to_primary_connected",
    }
    assert all(item.total_seconds < 60.0 for item in completed)


def test_battle_disconnect_restarts_only_same_fingerprint_then_primary(tmp_path):
    fixture, backend, restarter, now = _three_window_reconnect_fixture(
        tmp_path,
        battle=True,
    )

    fixture.controller.reconnect()
    now[0] = 5.0
    restarted = fixture.controller.reconnect()

    assert restarted.details["restarted_windows"] == 1
    assert len(restarter.calls) == 1
    assert restarter.calls[0][0].handle == 1
    assert restarter.calls[0][1][0].launch_fingerprint == "a" * 64
    assert fixture.mouse.clicks == []

    final = _advance_locked_reconnect_to_connected(
        fixture,
        backend,
        now,
        original_handle=1,
        replacement_handle=11,
    )

    assert len(restarter.calls) == 1
    assert final.details["all_connected"] is True
    assert fixture.mouse.clicks == [
        (11, (0.505, 0.856)),
        (11, (0.5, 0.722)),
        (11, (0.651, 0.706)),
        (11, CHARACTER_ENTER_CLICK_POINT),
    ]
    assert all(handle == 11 for handle, _point in fixture.mouse.clicks)
    completed = [
        item
        for item in fixture.controller.anonymous_reconnect_timing_diagnostics()
        if item.status == "completed"
    ]
    assert completed
    assert all(item.total_seconds < 60.0 for item in completed)


class _LifecycleAutoBattleRecognizer:
    def __init__(self, action_kind):
        self.action_kind = action_kind
        self.calls = 0
        self.target_calls = 0

    def read(self, image):
        self.calls += 1
        marker = image.getpixel((0, 0))[2]
        if marker not in {10, 11, 12}:
            return AutoBattleEvidence(False, False, False)
        self.target_calls += 1
        if self.target_calls >= 3:
            return AutoBattleEvidence(False, True, True)
        if self.action_kind == "normal-red-x":
            return AutoBattleEvidence(
                True,
                False,
                True,
                red_x_box=(0, 0, 2, 2),
            )
        return AutoBattleEvidence(
            False,
            False,
            False,
            battle_button_box=(0, 0, 2, 2),
        )


@pytest.mark.parametrize(
    "battle_disconnect,action_kind,replacement_handle",
    (
        (False, "normal-red-x", 1),
        (True, "battle-button", 11),
    ),
)
def test_disconnect_to_primary_and_auto_battle_share_one_under_sixty_budget(
    tmp_path,
    battle_disconnect,
    action_kind,
    replacement_handle,
):
    fixture, backend, restarter, now = _three_window_reconnect_fixture(
        tmp_path,
        battle=battle_disconnect,
    )
    auto_recognizer = _LifecycleAutoBattleRecognizer(action_kind)
    fixture.controller._auto_battle_recognizer = auto_recognizer
    fixture.controller.set_auto_battle_enabled(True)
    if action_kind == "battle-button":
        for marker in (10, 11, 12):
            item = fixture.controller._recognizer.recognitions[marker]
            fixture.controller._recognizer.recognitions[marker] = replace(
                item,
                battle_context=True,
            )

    fixture.controller.reconnect()
    now[0] = 5.0
    fixture.controller.reconnect()
    final = _advance_locked_reconnect_to_connected(
        fixture,
        backend,
        now,
        original_handle=1,
        replacement_handle=replacement_handle,
    )

    assert final.details["all_connected"] is True
    assert len(restarter.calls) == int(battle_disconnect)
    assert auto_recognizer.target_calls == 0
    now[0] += 1.0
    resumed = fixture.controller.reconnect()
    assert resumed.details["all_connected"] is True
    assert auto_recognizer.target_calls == 3
    assert fixture.mouse.clicks[-1] == (replacement_handle, (0.5, 0.5))
    completed = [
        item
        for item in fixture.controller.anonymous_reconnect_timing_diagnostics()
        if item.status == "completed"
    ]
    assert {item.lifecycle for item in completed} == {
        "disconnect_to_primary_auto",
        "start_game_to_primary_connected",
    }
    assert all(item.total_seconds < 60.0 for item in completed)


def test_waiting_battle_without_disconnect_never_restarts(tmp_path):
    fixture, _backend, restarter, now = _three_window_reconnect_fixture(
        tmp_path,
        battle=False,
    )
    fixture.capture.states[1] = 27

    fixture.controller.reconnect()
    now[0] = 20.0
    result = fixture.controller.reconnect()

    assert result.details["restarted_windows"] == 0
    assert restarter.calls == []
    assert fixture.mouse.clicks == []


@pytest.mark.parametrize(
    "mutation",
    ("source", "instance", "route", "revision"),
)
def test_battle_disconnect_final_authority_change_is_zero_restart(
    tmp_path,
    mutation,
):
    fixture, backend, restarter, now = _three_window_reconnect_fixture(
        tmp_path,
        battle=True,
    )
    fixture.controller.reconnect()
    fingerprint = "a" * 64

    if mutation == "source":
        fixture.controller._source_state_generation += 1
    elif mutation == "instance":
        backend.windows[0] = replace(backend.windows[0], process_id=999)
    elif mutation == "route":
        capture_and_recognize = fixture.controller._capture_and_recognize

        def changed_route(*args, **kwargs):
            sample, recognition, fresh, _route = capture_and_recognize(
                *args,
                **kwargs,
            )
            return sample, recognition, fresh, "obscured"

        fixture.controller._capture_and_recognize = changed_route
    elif mutation == "revision":
        fixture.controller.set_capture_settings(
            SmartReconnectCaptureSettings(
                visible=True,
                obscured=False,
                minimized=True,
            )
        )

    now[0] = 5.0
    result = fixture.controller.reconnect()

    assert result.details["restarted_windows"] == 0
    assert restarter.calls == []
    assert fixture.mouse.clicks == []


def test_reconnect_timeout_records_anonymous_stage_and_keeps_monitoring(tmp_path):
    fixture, _backend, _restarter, now = _three_window_reconnect_fixture(
        tmp_path,
        battle=False,
    )
    fixture.controller.reconnect()
    now[0] = 5.0
    fixture.controller.reconnect()

    now[0] = 65.0
    fixture.capture.states[1] = 23
    timed_out = fixture.controller.reconnect()

    diagnostics = timed_out.details["timing_diagnostics"]
    timeout = next(item for item in diagnostics if item["status"] == "timeout")
    assert timeout["window_id"] == hashlib.sha256(
        ("a" * 64).encode("ascii")
    ).hexdigest()[:12]
    assert timeout["total_seconds"] >= 60.0
    assert "a" * 64 not in json.dumps(diagnostics, ensure_ascii=False)
    assert fixture.controller._execution_allowed() is True
    assert ("a" * 64, "disconnect_to_primary_auto") in (
        fixture.controller._reconnect_timing_flows
    )
