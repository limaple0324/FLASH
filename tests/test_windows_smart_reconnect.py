import ctypes
import hashlib
import inspect
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
from adapters.windows_smart_reconnect_observation_broker import (
    SmartReconnectObservationSnapshot,
    SmartReconnectShortcutObservation,
    SmartReconnectWindowObservation,
    WindowsSmartReconnectObservationBroker,
)
from adapters.windows_window import WindowInfo
from config.config_manager import ConfigManager
from core.reconnect_policy import ReconnectScreenState
from core.smart_reconnect_authorization import (
    ReconnectAuthorizationTarget,
    ReconnectLaunchMode,
    ReconnectSourceIdentity,
    ShortcutFileIdentity,
    ShortcutSeal,
)
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
from services.smart_reconnect_authorization_coordinator import (
    SmartReconnectAuthorizationCoordinator,
)
from services.smart_reconnect_evidence_store import (
    RuntimeSourceIdentity,
    SmartReconnectEvidenceRecorder,
)
from services.smart_reconnect_target_identity_service import (
    SmartReconnectTargetIdentity,
)
from core.target_window_contract import ObservationActionLease
from services.identity_data_transaction_coordinator import (
    IdentityDataTransactionCoordinator,
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


class StaticControllerObservationBroker:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.current = True
        self.latest_calls = 0
        self.refresh_calls = 0
        self.seal_witness_calls = []
        self.events = None
        self.lease = ObservationActionLease(
            1,
            snapshot.generation,
            time.monotonic() + 120,
        )

    def latest_snapshot(self):
        self.latest_calls += 1
        return self.snapshot

    def current_snapshot(self):
        self.latest_calls += 1
        return self.snapshot if self.current else None

    def stable_snapshot(self):
        self.latest_calls += 1
        return self.snapshot

    def action_snapshot(self):
        return (self.snapshot, self.lease) if self.current else None

    def published_snapshot_without_wait(self):
        return self.snapshot if self.current else None

    def run_if_generation_current(self, generation, callback):
        if not self.is_generation_current(generation):
            return False, None
        return True, callback()

    def run_if_action_current(self, lease, callback):
        if (
            not self.current
            or lease is not self.lease
            or lease.deadline_monotonic <= time.monotonic()
        ):
            return False, None
        return True, callback()

    def seal_is_witnessed(self, expected):
        return (
            self.current
            and self.lease.deadline_monotonic > time.monotonic()
            and expected in self.seal_witness_calls
        )

    def seal_is_witnessed_without_wait(self, expected):
        return expected in self.seal_witness_calls

    def refresh(self, _paths=()):
        self.refresh_calls += 1
        raise AssertionError("controller must not refresh process observation")

    def is_generation_current(self, generation):
        return self.current and generation == self.snapshot.generation

    def seal_witness(self, expected):
        self.seal_witness_calls.append(expected)
        if self.events is not None:
            self.events.append(("seal_witness", expected))
        return object()

    def revalidate_reopen_seal(self, expected):
        return self.seal_witness(expected)


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


class FakeClosedBattleRestarter(FakeBattleRestarter):
    """Model a verified close whose first shortcut open did not complete."""

    def restart(self, window, target):
        self.calls.append((window, target))
        return BattleRestartResult(
            False,
            "battle_shortcut_open_failed",
            window_closed=True,
        )


class FakeShortcutSealResolver:
    def __init__(self):
        self.changed_fingerprints = set()
        self.calls = []

    def revalidate(self, expected):
        self.calls.append(expected)
        return expected.launch_fingerprint not in self.changed_fingerprints


@dataclass
class Fixture:
    controller: WindowsSmartReconnectController
    capture: FakeCaptureProvider
    mouse: FakeMouseBackend
    authorization: SmartReconnectAuthorizationCoordinator | None = None
    preparation: "FakeAuthorizationPreparation | None" = None
    shortcut_seals: FakeShortcutSealResolver | None = None


class FakeAuthorizationPreparation:
    def __init__(self, coordinator, source, targets, target_provider=None):
        self.authorization_coordinator = coordinator
        self._source = source
        self._targets = tuple(targets)
        self._target_provider = target_provider
        self._generation = source.identity_generation

    def prepare(self, *, launch_mode, retained_targets=()):
        assert launch_mode is ReconnectLaunchMode.IDENTITY_BOUND
        preparation_token = self.authorization_coordinator.begin_reprepare()
        source = replace(
            self._source,
            identity_generation=self._generation,
        )
        self._generation += 1
        targets = (
            tuple(self._target_provider(self._targets))
            if self._target_provider is not None
            else self._targets
        )
        current_fingerprints = frozenset(
            target.fingerprint for target in targets
        )
        targets = (
            *targets,
            *(
                target
                for target in retained_targets
                if target.fingerprint not in current_fingerprints
            ),
        )
        return self.authorization_coordinator.publish_if_current(
            preparation_token,
            source,
            launch_mode,
            targets,
        )


class DynamicActualPreparation:
    def __init__(
        self,
        coordinator,
        target_windows_provider,
        identities,
        *,
        identity_generation_provider=lambda: 1,
    ):
        self.authorization_coordinator = coordinator
        self._target_windows_provider = target_windows_provider
        self._identities = identities
        self._identity_generation_provider = identity_generation_provider
        self._seals = {}

    def _seal(self, fingerprint):
        seal = self._seals.get(fingerprint)
        if seal is None:
            index = len(self._seals) + 1
            seal = ShortcutSeal(
                ShortcutFileIdentity(
                    str(
                        Path.cwd()
                        / ".pytest-shortcuts"
                        / f"actual-{fingerprint}.lnk"
                    ),
                    99,
                    index,
                ),
                hashlib.sha256(fingerprint.encode()).hexdigest(),
                fingerprint,
            )
            self._seals[fingerprint] = seal
        return seal

    def prepare(self, *, launch_mode, retained_targets=()):
        assert launch_mode is ReconnectLaunchMode.IDENTITY_BOUND
        preparation_token = self.authorization_coordinator.begin_reprepare()
        resolved = self._target_windows_provider()
        if (
            not isinstance(resolved, ResolvedTargetWindows)
            or not resolved.actual_window_snapshot
            or resolved.failure_codes
        ):
            raise ValueError("actual target source is unavailable")
        targets = []
        missing_identity = set()
        for window in resolved.windows:
            fingerprint = window.launch_fingerprint
            identity = self._identities.get(fingerprint)
            instance = WindowInstanceToken.from_window(window)
            if identity is None or instance is None:
                if fingerprint is not None:
                    missing_identity.add(fingerprint)
                continue
            targets.append(
                ReconnectAuthorizationTarget(
                    fingerprint=fingerprint,
                    instance=instance,
                    character_id=identity.character_id,
                    role_aliases=identity.role_aliases,
                    importance=identity.importance,
                    original_slot_index=identity.original_slot_index,
                    original_line_number=identity.original_line_number,
                    shortcut_seal=self._seal(fingerprint),
                )
            )
        current = {target.fingerprint for target in targets}
        targets.extend(
            target
            for target in retained_targets
            if target.fingerprint not in current
        )
        isolated = frozenset(
            set(resolved.blocked_fingerprints) | missing_identity
        )
        source = ReconnectSourceIdentity(
            identity_generation=self._identity_generation_provider(),
            config_revision=1,
            group_id="actual-window-source",
            group_name="實際存在視窗",
            character_ids=tuple(
                target.character_id
                for target in targets
                if target.character_id is not None
            ),
        )
        return self.authorization_coordinator.publish_if_current(
            preparation_token,
            source,
            launch_mode,
            tuple(targets),
            isolated,
            resolved.isolated_window_count + len(missing_identity),
            resolved.anonymous_isolated_window_count,
        )


def _authorization_targets_for_current_windows(
    targets,
    window_backend,
    target_windows_provider,
):
    raw = (
        target_windows_provider()
        if target_windows_provider is not None
        else window_backend.list_windows()
    )
    if isinstance(raw, ResolvedTargetWindows):
        if raw.failure_codes or raw.blocked_fingerprints:
            raise ValueError("authorization window source is unsafe")
        windows = tuple(raw.windows)
    else:
        windows = tuple(raw)
    target_fingerprints = frozenset(
        target.fingerprint
        for target in targets
    )
    scoped_windows = tuple(
        window
        for window in windows
        if window.launch_fingerprint in target_fingerprints
    )
    resolved = []
    for target in targets:
        matches = tuple(
            window
            for window in scoped_windows
            if window.launch_fingerprint == target.fingerprint
        )
        if len(matches) != 1:
            raise ValueError("authorization window source is incomplete")
        instance = WindowInstanceToken.from_window(matches[0])
        if instance is None:
            raise ValueError("authorization window instance is incomplete")
        resolved.append(replace(target, instance=instance))
    if len(scoped_windows) != len(resolved):
        raise ValueError("authorization window source has extra identities")
    return tuple(resolved)


def make_controller(
    screen_states,
    *,
    windows=None,
    expected_windows=2,
    points=None,
    mouse=None,
    primary_capture_provider=None,
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
    target_identity_provider=None,
    verified_slot_recorder=None,
    verified_line_recorder=None,
    ungrouped_shortcut_provider=None,
    evidence_recorder=None,
    evidence_required=False,
    evidence_initialization_failed=False,
    authorization_original_line_number=1,
    authorization_missing_last_target=False,
    authorization_target_identities=None,
    shortcut_seal_resolver=None,
    identity_generation_runner=None,
    identity_alias_catalog_provider=None,
    observation_broker=None,
):
    if clock is None:
        default_time = [-5.0]

        def clock():
            default_time[0] += 5.0
            return default_time[0]

    if windows is None:
        windows = [
            make_window(index)
            for index in range(1, expected_windows + 1)
        ]
    controller_window_backend = window_backend or FakeWindowBackend(windows)
    capture = primary_capture_provider or FakeCaptureProvider(
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
    shortcut_seal_resolver = (
        shortcut_seal_resolver or FakeShortcutSealResolver()
    )
    authorization_coordinator = SmartReconnectAuthorizationCoordinator()
    authorization_targets = []
    for index, window in enumerate(windows, start=1):
        fingerprint = window.launch_fingerprint
        instance = WindowInstanceToken.from_window(window)
        authorization_identity = (
            authorization_target_identities.get(fingerprint)
            if authorization_target_identities is not None
            else None
        )
        if (
            authorization_identity is None
            and target_identity_provider is not None
        ):
            try:
                candidate_identity = target_identity_provider(fingerprint)
            except (OSError, RuntimeError, TypeError, ValueError):
                candidate_identity = None
            if (
                isinstance(candidate_identity, SmartReconnectTargetIdentity)
                and candidate_identity.fingerprint == fingerprint
                and candidate_identity.original_slot_index is not None
                and candidate_identity.original_line_number is not None
            ):
                authorization_identity = candidate_identity
            else:
                authorization_targets = []
                break
        plan_target = (
            group_launch_plan.target_for_fingerprint(fingerprint)
            if group_launch_plan is not None
            else None
        )
        if instance is None:
            authorization_targets = []
            break
        if (
            authorization_identity is not None
            and authorization_identity.fingerprint != fingerprint
        ):
            authorization_targets = []
            break
        character_id = (
            authorization_identity.character_id
            if authorization_identity is not None
            else (
                plan_target.entry_id
                if plan_target is not None and plan_target.entry_id
                else f"test-character-{index}"
            )
        )
        role_aliases = (
            authorization_identity.role_aliases
            if authorization_identity is not None
            else (
                plan_target.role_id
                if (
                    len(windows) == 1
                    and plan_target is not None
                    and plan_target.role_id
                )
                else f"{index:03x}-test-identity"
            ,)
        )
        importance = (
            authorization_identity.importance
            if authorization_identity is not None
            else (
                plan_target.importance
                if plan_target is not None
                and plan_target.importance is not None
                else CharacterImportance.PRIMARY
            )
        )
        original_slot_index = (
            authorization_identity.original_slot_index
            if authorization_identity is not None
            else (index - 1) % 3
        )
        original_line_number = (
            authorization_identity.original_line_number
            if authorization_identity is not None
            else authorization_original_line_number
        )
        shortcut_path = str(
            plan_target.shortcut_path
            if plan_target is not None
            else Path.cwd() / ".pytest-shortcuts" / f"{index}.lnk"
        )
        try:
            authorization_targets.append(
                ReconnectAuthorizationTarget(
                    fingerprint=fingerprint,
                    instance=instance,
                    character_id=character_id,
                    role_aliases=role_aliases,
                    importance=importance,
                    original_slot_index=original_slot_index,
                    original_line_number=original_line_number,
                    shortcut_seal=ShortcutSeal(
                        file_identity=ShortcutFileIdentity(
                            normalized_path=shortcut_path,
                            volume_serial_number=1,
                            file_index=index,
                        ),
                        content_sha256=f"{index:064x}",
                        launch_fingerprint=fingerprint,
                    ),
                )
            )
        except (TypeError, ValueError):
            authorization_targets = []
            break
    if require_expected_window_count and len(windows) != expected_windows:
        authorization_targets = []
    expected_character_ids = tuple(
        target.character_id
        for target in authorization_targets
        if target.character_id is not None
    )
    if authorization_missing_last_target and authorization_targets:
        authorization_targets = authorization_targets[:-1]
    preparation_service = None
    if authorization_targets:
        try:
            authorization_source = ReconnectSourceIdentity(
                    identity_generation=1,
                    config_revision=1,
                    group_id="test-group-id",
                    group_name=(
                        group_launch_plan.group_name
                        if group_launch_plan is not None
                        else "test-group"
                    ),
                    character_ids=expected_character_ids,
                )
            preparation_service = FakeAuthorizationPreparation(
                authorization_coordinator,
                authorization_source,
                tuple(authorization_targets),
                target_provider=lambda targets: (
                    _authorization_targets_for_current_windows(
                        targets,
                        controller_window_backend,
                        target_windows_provider,
                    )
                ),
            )
        except (TypeError, ValueError):
            preparation_service = None
    controller = WindowsSmartReconnectController(
            expected_windows=expected_windows,
            title_keywords=("Adobe Flash Player",),
            window_backend=controller_window_backend,
            capture_provider=capture,
            visible_capture_provider=visible_capture_provider,
            obscured_capture_provider=obscured_capture_provider,
            active_refresh_capture_provider=active_refresh_capture_provider,
            primary_capture_is_trusted=primary_capture_is_trusted,
            recognizer=recognizer,
            mouse_backend=mouse,
            monotonic_clock=clock,
            state_path=state_path,
            execution_enabled=False,
            require_expected_window_count=require_expected_window_count,
            battle_restarter=battle_restarter,
            failure_status_service=failure_status_service,
            failure_record_callback=failure_record_callback,
            target_windows_provider=target_windows_provider,
            registered_role_provider=registered_role_provider,
            target_identity_provider=target_identity_provider,
            verified_slot_recorder=verified_slot_recorder,
            verified_line_recorder=verified_line_recorder,
            operation_gate=operation_gate,
            authorization_coordinator=authorization_coordinator,
            preparation_service=preparation_service,
            shortcut_seal_resolver=shortcut_seal_resolver,
            identity_generation_runner=identity_generation_runner,
            identity_alias_catalog_provider=identity_alias_catalog_provider,
            observation_broker=observation_broker,
            evidence_recorder=evidence_recorder,
            evidence_required=evidence_required,
            evidence_initialization_failed=(
                evidence_initialization_failed
            ),
        )
    if ungrouped_shortcut_provider is not None:
        controller.set_ungrouped_shortcut_provider(
            ungrouped_shortcut_provider
        )
    if preparation_service is not None:
        prepared = controller.prepare_execution_snapshot()
        if prepared.success:
            controller._initial_login_authorizations.clear()
            if group_launch_plan is not None:
                controller.set_group_launch_plan(group_launch_plan)
            controller.set_execution_enabled(True)
    return Fixture(
        controller=controller,
        capture=capture,
        mouse=mouse,
        authorization=authorization_coordinator,
        preparation=preparation_service,
        shortcut_seals=shortcut_seal_resolver,
    )


def make_actual_controller(
    windows,
    *,
    identities=None,
    screen_states=None,
    recognizer=None,
    clock=None,
    mouse=None,
    active_refresh_capture_provider=None,
    capture_settings=None,
    identity_generation_provider=lambda: 1,
    identity_generation_runner=None,
    identity_alias_catalog_provider=None,
    verified_slot_recorder=None,
    verified_line_recorder=None,
    battle_restarter=None,
    isolated_window_count=0,
    anonymous_isolated_window_count=0,
):
    windows = tuple(windows)
    if clock is None:
        current = [-5.0]

        def clock():
            current[0] += 5.0
            return current[0]

    if identities is None:
        identities = {
            window.launch_fingerprint: SmartReconnectTargetIdentity(
                fingerprint=window.launch_fingerprint,
                character_id=f"actual-character-{index}",
                role_aliases=(f"實際角色{index}",),
                importance=(
                    CharacterImportance.PRIMARY
                    if index == 1
                    else CharacterImportance.SECONDARY
                ),
                original_slot_index=(index - 1) % 3,
                original_line_number=1,
            )
            for index, window in enumerate(windows, start=1)
        }
    state = {
        "windows": list(windows),
        "failure_codes": (),
        "blocked_fingerprints": frozenset(),
        "isolated_window_count": isolated_window_count,
        "anonymous_isolated_window_count": (
            anonymous_isolated_window_count
        ),
    }

    def target_windows_provider():
        return ResolvedTargetWindows(
            tuple(state["windows"]),
            tuple(state["failure_codes"]),
            frozenset(state["blocked_fingerprints"]),
            int(state["isolated_window_count"]),
            int(state["anonymous_isolated_window_count"]),
            True,
        )

    coordinator = SmartReconnectAuthorizationCoordinator()
    preparation = DynamicActualPreparation(
        coordinator,
        target_windows_provider,
        identities,
        identity_generation_provider=identity_generation_provider,
    )
    shortcut_seals = FakeShortcutSealResolver()
    capture = FakeCaptureProvider(
        screen_states
        or {window.handle: 1 for window in windows}
    )
    if recognizer is None:
        recognizer = FakeRecognizer(
            {
                1: ReconnectScreenState.CONNECTED,
                2: ReconnectScreenState.DISCONNECTED,
                3: ReconnectScreenState.LOGIN_START,
                5: ReconnectScreenState.CHARACTER_SELECTION,
                255: ReconnectScreenState.UNKNOWN,
            },
            {2: (0.5, 0.5), 3: (0.5, 0.8), 5: (0.35, 0.85)},
        )
    mouse = mouse or FakeMouseBackend()
    if identity_alias_catalog_provider is None:
        identity_alias_catalog_provider = lambda: tuple(
            (alias, identity.character_id)
            for identity in identities.values()
            for alias in identity.role_aliases
        )
    backend = FakeWindowBackend(windows)
    controller = WindowsSmartReconnectController(
        expected_windows=max(1, len(windows)),
        title_keywords=("Adobe Flash Player",),
        window_backend=backend,
        capture_provider=capture,
        active_refresh_capture_provider=active_refresh_capture_provider,
        primary_capture_is_trusted=True,
        capture_settings=capture_settings,
        recognizer=recognizer,
        mouse_backend=mouse,
        monotonic_clock=clock,
        execution_enabled=False,
        require_expected_window_count=False,
        target_windows_provider=target_windows_provider,
        verified_slot_recorder=verified_slot_recorder,
        verified_line_recorder=verified_line_recorder,
        battle_restarter=battle_restarter,
        authorization_coordinator=coordinator,
        preparation_service=preparation,
        shortcut_seal_resolver=shortcut_seals,
        identity_generation_runner=identity_generation_runner,
        identity_alias_catalog_provider=identity_alias_catalog_provider,
    )
    assert controller.prepare_execution_snapshot().success is True
    assert controller.set_execution_enabled(True) is True
    return (
        Fixture(
            controller=controller,
            capture=capture,
            mouse=mouse,
            authorization=coordinator,
            preparation=preparation,
            shortcut_seals=shortcut_seals,
        ),
        state,
        identities,
    )


def activate_current_window_snapshot(
    fixture: Fixture,
):
    fixture.controller.set_execution_enabled(False)
    prepared = fixture.controller.prepare_execution_snapshot()
    if prepared.success:
        fixture.controller.set_execution_enabled(True)
    return prepared


def _controller_observation_snapshot(windows, *, generation=1):
    return SmartReconnectObservationSnapshot(
        generation=generation,
        windows=tuple(
            SmartReconnectWindowObservation(
                window=window,
                instance=WindowInstanceToken.from_window(window),
                sample=CaptureSample(
                    width=2,
                    height=2,
                    pixels=bytes([1, 0, 0, 255] * 4),
                    api_succeeded=True,
                ),
                recognition=ScreenRecognition(
                    state=ReconnectScreenState.CONNECTED,
                    score=1.0,
                    click_point=None,
                    reference_name="connected",
                ),
                fresh_capture=True,
                capture_route="visible",
                role_id=None,
            )
            for window in windows
        ),
    )


def test_broker_passive_observation_reads_latest_without_refresh_or_direct_io():
    window = make_window(1)
    broker = StaticControllerObservationBroker(
        _controller_observation_snapshot((window,), generation=7)
    )

    class ForbiddenWindowBackend:
        def list_windows(self):
            raise AssertionError("passive observation enumerated windows")

    class ForbiddenCapture:
        def capture(self, _handle):
            raise AssertionError("passive observation captured a window")

    controller = WindowsSmartReconnectController(
        expected_windows=1,
        title_keywords=("Adobe Flash Player",),
        window_backend=ForbiddenWindowBackend(),
        capture_provider=ForbiddenCapture(),
        recognizer=FakeRecognizer({}, {}),
        mouse_backend=FakeMouseBackend(),
        require_expected_window_count=False,
        observation_broker=broker,
    )

    observed = controller.observe_screen_states(
        (window.launch_fingerprint,)
    )

    assert observed == {
        window.launch_fingerprint: ReconnectScreenState.CONNECTED,
    }
    assert broker.latest_calls == 1
    assert broker.refresh_calls == 0


def test_broker_passive_observation_keeps_stable_connected_snapshot_during_refresh():
    window = make_window(1)
    broker = StaticControllerObservationBroker(
        _controller_observation_snapshot((window,), generation=8)
    )
    broker.current = False
    controller = WindowsSmartReconnectController(
        expected_windows=1,
        title_keywords=("Adobe Flash Player",),
        window_backend=FakeWindowBackend((window,)),
        capture_provider=FakeCaptureProvider({window.handle: 1}),
        recognizer=FakeRecognizer({1: ReconnectScreenState.CONNECTED}, {}),
        mouse_backend=FakeMouseBackend(),
        require_expected_window_count=False,
        observation_broker=broker,
    )

    observed = controller.observe_screen_states((window.launch_fingerprint,))

    assert observed == {
        window.launch_fingerprint: ReconnectScreenState.CONNECTED,
    }


def test_broker_generation_change_interleaves_without_deadlock_or_stale_input(
    tmp_path,
    monkeypatch,
):
    window = make_window(1)
    fingerprint = window.launch_fingerprint
    seal = ShortcutSeal(
        ShortcutFileIdentity(
            str(Path.cwd() / ".pytest-shortcuts" / "1.lnk"),
            1,
            1,
        ),
        f"{1:064x}",
        fingerprint,
    )
    broker = WindowsSmartReconnectObservationBroker(reference_dir=tmp_path)
    serial = broker._next_request()
    published = broker._publish(
        serial,
        replace(
            _controller_observation_snapshot((window,), generation=0),
            shortcuts=(
                SmartReconnectShortcutObservation(
                    seal.file_identity.normalized_path,
                    fingerprint,
                    seal,
                ),
            ),
        ),
    )
    assert published is not None
    fixture = make_controller(
        [1],
        windows=(window,),
        expected_windows=1,
        observation_broker=broker,
    )
    context = fixture.controller._action_context_for(
        fingerprint,
        WindowInstanceToken.from_window(window),
    )
    entered = threading.Event()
    release = threading.Event()
    delivered = []
    outcomes = []
    original_current = broker.current_snapshot

    def blocked_current_snapshot():
        snapshot = original_current()
        if threading.current_thread().name == "authorized-delivery":
            entered.set()
            assert release.wait(2)
        return snapshot

    monkeypatch.setattr(broker, "current_snapshot", blocked_current_snapshot)
    fixture.controller._broker_scan_generation = published.generation
    worker = threading.Thread(
        name="authorized-delivery",
        target=lambda: outcomes.append(
            fixture.controller._run_authorized_backend_call(
                lambda: delivered.append("input"),
                action_context=context,
                expected_capture_settings_revision=None,
                capture_route="visible",
                expected_source_state_generation=None,
            )
        ),
    )
    worker.start()
    assert entered.wait(2)

    broker.set_capture_modes(
        visible=False,
        obscured=True,
        minimized=True,
    )
    release.set()
    worker.join(2)

    assert worker.is_alive() is False
    assert outcomes == [(False, None)]
    assert delivered == []
    assert broker.current_snapshot() is None
    assert broker.close() is True


def test_scan_waiting_for_broker_holds_no_scan_config_or_identity_lock(
    tmp_path,
    monkeypatch,
):
    window = make_window(1)
    broker = WindowsSmartReconnectObservationBroker(reference_dir=tmp_path)
    serial = broker._next_request()
    assert broker._publish(
        serial,
        _controller_observation_snapshot((window,), generation=0),
    ) is not None
    fixture = make_controller(
        [1],
        windows=(window,),
        expected_windows=1,
        observation_broker=broker,
    )
    config = ConfigManager(tmp_path / "config" / "settings.json")
    identity = IdentityDataTransactionCoordinator()
    entered = threading.Event()
    results = []
    original_action = broker.action_snapshot

    def announced_action_snapshot():
        entered.set()
        return original_action()

    monkeypatch.setattr(
        broker,
        "action_snapshot",
        announced_action_snapshot,
    )
    with broker._state_lock:
        worker = threading.Thread(
            target=lambda: results.append(
                fixture.controller.check_connection()
            )
        )
        worker.start()
        assert entered.wait(2)
        assert fixture.controller._scan_lock.acquire(timeout=0.5) is True
        fixture.controller._scan_lock.release()
        with config.resource_guard():
            assert config.snapshot_state_locked().revision >= 0
        assert identity.read_consistent(lambda: "identity-free") == (
            "identity-free"
        )

    worker.join(2)

    assert worker.is_alive() is False
    assert len(results) == 1
    assert broker.close() is True


def test_execution_stop_invalidates_real_broker_action_but_keeps_stable_view(
    tmp_path,
):
    window = make_window(1)
    broker = WindowsSmartReconnectObservationBroker(reference_dir=tmp_path)
    serial = broker._next_request()
    published = broker._publish(
        serial,
        _controller_observation_snapshot((window,), generation=0),
    )
    assert published is not None
    fixture = make_controller(
        [1],
        windows=(window,),
        expected_windows=1,
        observation_broker=broker,
    )
    action = broker.action_snapshot()
    assert action is not None

    assert fixture.controller.set_execution_enabled(False) is True

    assert broker.action_snapshot() is None
    assert broker.run_if_action_current(action[1], lambda: "late") == (
        False,
        None,
    )
    assert broker.stable_snapshot() is published
    assert broker.close() is True


def test_broker_preparation_wait_does_not_hold_controller_scan_lock():
    windows = (make_window(1), make_window(2))
    broker = StaticControllerObservationBroker(
        _controller_observation_snapshot(windows, generation=11)
    )
    fixture = make_controller(
        [1, 1],
        windows=windows,
        expected_windows=2,
        observation_broker=broker,
    )
    fixture.controller.set_execution_enabled(False)
    original_prepare = fixture.preparation.prepare
    entered = threading.Event()
    release = threading.Event()
    results = []

    def blocked_prepare(**kwargs):
        entered.set()
        assert release.wait(2)
        return original_prepare(**kwargs)

    fixture.preparation.prepare = blocked_prepare
    worker = threading.Thread(
        target=lambda: results.append(
            fixture.controller.prepare_execution_snapshot()
        )
    )
    worker.start()
    assert entered.wait(2)

    assert fixture.controller._scan_lock.acquire(timeout=0.5) is True
    fixture.controller._scan_lock.release()
    release.set()
    worker.join(2)

    assert worker.is_alive() is False
    assert len(results) == 1
    assert results[0].success is True
    assert broker.refresh_calls == 0


def test_true_reopen_reads_broker_seal_witness_before_restarter(tmp_path):
    window = make_window(1)
    plan = make_group_plan(tmp_path, (window,), "witness")
    target = plan.targets[0]
    seal = ShortcutSeal(
        ShortcutFileIdentity(target.shortcut_path, 1, 1),
        f"{1:064x}",
        window.launch_fingerprint,
    )
    observation = SmartReconnectObservationSnapshot(
        generation=19,
        windows=(
            SmartReconnectWindowObservation(
                window=window,
                instance=WindowInstanceToken.from_window(window),
                sample=CaptureSample(2, 2, bytes([2, 0, 0, 255] * 4), True),
                recognition=ScreenRecognition(
                    state=ReconnectScreenState.DISCONNECTED,
                    score=1.0,
                    click_point=(0.5, 0.5),
                    reference_name="disconnected",
                    battle_context=True,
                ),
                fresh_capture=True,
                capture_route="print_window",
                role_id=None,
            ),
        ),
        shortcuts=(
            SmartReconnectShortcutObservation(
                str(target.shortcut_path),
                window.launch_fingerprint,
                seal,
            ),
        ),
    )
    broker = StaticControllerObservationBroker(observation)
    events = []
    broker.events = events

    class WitnessRestarter(FakeClosedBattleRestarter):
        def restart(self, current_window, current_target):
            events.append(("restart", current_target.fingerprint))
            return super().restart(current_window, current_target)

    restarter = WitnessRestarter()
    fixture = make_controller(
        [2],
        windows=[window],
        expected_windows=1,
        battle_restarter=restarter,
        group_launch_plan=plan,
        observation_broker=broker,
    )

    fixture.controller.reconnect()
    fixture.controller.reconnect()

    assert restarter.calls
    restart_index = next(
        index for index, event in enumerate(events) if event[0] == "restart"
    )
    assert any(
        event[0] == "seal_witness" for event in events[:restart_index]
    )
    assert broker.seal_witness_calls[-1] == seal


def test_action_lease_is_rechecked_at_click_leaf_and_normal_scan_reads_no_seal(
    tmp_path,
):
    window = make_window(1)
    plan = make_group_plan(tmp_path, (window,), "leaf-expiry")
    target = plan.targets[0]
    seal = ShortcutSeal(
        ShortcutFileIdentity(target.shortcut_path, 1, 1),
        f"{1:064x}",
        window.launch_fingerprint,
    )
    observation = SmartReconnectObservationSnapshot(
        generation=20,
        windows=(
            SmartReconnectWindowObservation(
                window=window,
                instance=WindowInstanceToken.from_window(window),
                sample=CaptureSample(2, 2, bytes([2, 0, 0, 255] * 4), True),
                recognition=ScreenRecognition(
                    state=ReconnectScreenState.DISCONNECTED,
                    score=1.0,
                    click_point=(0.5, 0.5),
                    reference_name="disconnected",
                    battle_context=False,
                ),
                fresh_capture=True,
                capture_route="visible",
                role_id=None,
            ),
        ),
        shortcuts=(
            SmartReconnectShortcutObservation(
                str(target.shortcut_path),
                window.launch_fingerprint,
                seal,
            ),
        ),
    )
    broker = StaticControllerObservationBroker(observation)
    leaf_checks = []

    def expire_at_leaf(lease, _callback):
        leaf_checks.append(lease)
        return False, None

    broker.run_if_action_current = expire_at_leaf
    fixture = make_controller(
        [2],
        windows=[window],
        expected_windows=1,
        group_launch_plan=plan,
        observation_broker=broker,
    )

    fixture.controller.reconnect()
    fixture.controller.reconnect()

    assert leaf_checks and all(lease is broker.lease for lease in leaf_checks)
    assert fixture.mouse.clicks == []
    assert broker.seal_witness_calls == []


def test_action_lease_expiring_while_capture_lock_waits_blocks_final_click(
    tmp_path,
):
    window = make_window(1)
    plan = make_group_plan(tmp_path, (window,), "leaf-lock-expiry")
    target = plan.targets[0]
    seal = ShortcutSeal(
        ShortcutFileIdentity(target.shortcut_path, 1, 1),
        f"{1:064x}",
        window.launch_fingerprint,
    )
    observation = SmartReconnectObservationSnapshot(
        generation=21,
        windows=(
            SmartReconnectWindowObservation(
                window=window,
                instance=WindowInstanceToken.from_window(window),
                sample=CaptureSample(2, 2, bytes([2, 0, 0, 255] * 4), True),
                recognition=ScreenRecognition(
                    state=ReconnectScreenState.DISCONNECTED,
                    score=1.0,
                    click_point=(0.5, 0.5),
                    reference_name="disconnected",
                    battle_context=False,
                ),
                fresh_capture=True,
                capture_route="visible",
                role_id=None,
            ),
        ),
        shortcuts=(
            SmartReconnectShortcutObservation(
                str(target.shortcut_path),
                window.launch_fingerprint,
                seal,
            ),
        ),
    )
    broker = StaticControllerObservationBroker(observation)
    fixture = make_controller(
        [2],
        windows=[window],
        expected_windows=1,
        group_launch_plan=plan,
        observation_broker=broker,
    )
    fixture.controller.reconnect()
    broker.lease = ObservationActionLease(
        2,
        observation.generation,
        time.monotonic() + 0.2,
    )
    first_check = threading.Event()
    continue_after_first_check = threading.Event()
    leaf_checks = []

    def wait_after_first_check(lease, callback):
        if (
            not broker.current
            or lease is not broker.lease
            or lease.deadline_monotonic <= time.monotonic()
        ):
            return False, None
        leaf_checks.append(lease)
        first_check.set()
        if not continue_after_first_check.wait(1):
            return False, None
        return True, callback()

    broker.run_if_action_current = wait_after_first_check
    outcomes = []
    worker = threading.Thread(
        target=lambda: outcomes.append(fixture.controller.reconnect()),
        daemon=True,
    )
    worker.start()
    assert first_check.wait(1) is True
    fixture.controller._capture_settings_lock.acquire()
    try:
        continue_after_first_check.set()
        while time.monotonic() <= broker.lease.deadline_monotonic:
            time.sleep(0.005)
    finally:
        fixture.controller._capture_settings_lock.release()
    worker.join(1)

    assert worker.is_alive() is False
    assert outcomes
    assert leaf_checks == [broker.lease]
    assert fixture.mouse.clicks == []


def test_controller_never_fails_preparation_without_its_token():
    source = inspect.getsource(WindowsSmartReconnectController)

    assert "fail_preparation()" not in source


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
    target_identity_provider=None,
    verified_slot_recorder=None,
    verified_line_recorder=None,
):
    if clock is None:
        clock = lambda: 0.0
    window = make_window(1)
    authorization_identity = None
    authorization_missing = False
    if target_identity_provider is not None:
        try:
            candidate_identity = target_identity_provider(
                window.launch_fingerprint
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            candidate_identity = None
        if (
            isinstance(candidate_identity, SmartReconnectTargetIdentity)
            and candidate_identity.fingerprint == window.launch_fingerprint
            and candidate_identity.original_slot_index is not None
            and candidate_identity.original_line_number is not None
        ):
            authorization_identity = candidate_identity
        else:
            authorization_missing = True
    elif registered_role_provider is not None:
        try:
            registered_roles = tuple(registered_role_provider())
        except (OSError, RuntimeError, TypeError, ValueError):
            registered_roles = ()
        primaries = tuple(
            role
            for role in registered_roles
            if (
                isinstance(role, RegisteredReconnectRole)
                and role.importance is CharacterImportance.PRIMARY
            )
        )
        candidates = ()
        if len(primaries) == 1 and isinstance(
            recognizer,
            _CharacterSequenceRecognizer,
        ):
            try:
                candidates = tuple(recognizer.provider(1))
            except (OSError, RuntimeError, TypeError, ValueError):
                candidates = ()
        role_matches = tuple(
            candidate
            for candidate in candidates
            if (
                isinstance(candidate, CharacterSelectionCandidate)
                and isinstance(candidate.identity, str)
                and candidate.identity.strip().casefold()
                == primaries[0].role_id.casefold()
            )
        ) if len(primaries) == 1 else ()
        if len(role_matches) == 1:
            authorization_identity = _stable_target_identity(
                window,
                character_id=f"registered:{primaries[0].role_id}",
                role_aliases=(primaries[0].role_id,),
                importance=primaries[0].importance,
                slot_index=role_matches[0].slot_index,
                line_number=1,
            )
        else:
            authorization_missing = True
    else:
        authorization_identity = _stable_target_identity(
            window,
            character_id="test-character-1",
            role_aliases=("001-test-identity",),
            importance=CharacterImportance.PRIMARY,
            slot_index=0,
            line_number=1,
        )
    fixture = make_controller(
        [5],
        windows=[window],
        expected_windows=1,
        clock=clock,
        recognizer=recognizer,
        registered_role_provider=registered_role_provider,
        target_identity_provider=target_identity_provider,
        verified_slot_recorder=verified_slot_recorder,
        verified_line_recorder=verified_line_recorder,
        authorization_target_identities=(
            {window.launch_fingerprint: authorization_identity}
            if authorization_identity is not None
            else None
        ),
        authorization_missing_last_target=authorization_missing,
    )
    return fixture, window


def make_bound_pending_reopen_fixture(tmp_path):
    windows = [make_window(1), make_window(2)]
    restarter = FakeClosedBattleRestarter()
    fixture = make_controller(
        [2, 1],
        windows=windows,
        expected_windows=2,
        battle_markers={2},
        battle_restarter=restarter,
        group_launch_plan=make_group_plan(tmp_path, windows, "120"),
        failure_status_service=ReconnectFailureStatusService(),
    )
    fixture.controller.reconnect()
    fixture.controller.reconnect()
    missing = windows[0].launch_fingerprint
    assert missing in fixture.controller._pending_reopen_fingerprints
    assert missing in fixture.controller._pending_reopen_authorizations
    fixture.controller._window_backend.windows = [windows[1]]
    return fixture, windows, restarter


class _CharacterSequenceRecognizer:
    def __init__(self, provider):
        self.provider = provider
        self.calls = 0

    def recognize_capture(self, _sample):
        self.calls += 1
        return _character_recognition(self.provider(self.calls))


def _stable_target_identity(
    window,
    *,
    character_id="character-1",
    role_aliases=("AlphaHero",),
    importance=CharacterImportance.SECONDARY,
    slot_index=2,
    line_number=1,
):
    fingerprint = (
        window
        if isinstance(window, str)
        else window.launch_fingerprint
    )
    return SmartReconnectTargetIdentity(
        fingerprint,
        character_id,
        role_aliases,
        importance,
        original_slot_index=slot_index,
        original_line_number=line_number,
    )


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
    selected_windows = [windows[0], windows[2]]
    fixture = make_controller(
        [2, 4],
        windows=selected_windows,
        expected_windows=2,
        window_backend=FakeWindowBackend(windows),
    )

    first = fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert first.code == "reconnect.waiting"
    assert result.success is True
    assert result.details["discovered_windows"] == 2
    assert fixture.capture.calls == [1, 3, 1, 3, 1]
    assert fixture.mouse.clicks == [
        (1, (0.5, 0.5)),
    ]


def test_selected_group_change_revokes_stale_action_before_full_reprepare():
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
    assert result.details["discovered_windows"] == 2
    assert result.details["all_connected"] is False
    assert result.details["failure_codes"] == []
    assert fixture.capture.calls == [1, 1, 2]
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
            (windows[0],),
            ("window_identity_duplicate",),
        ),
    )
    fixture.controller.set_allowed_fingerprints(selected)

    result = fixture.controller.reconnect()

    assert result.success is False
    assert result.code == "reconnect.waiting"
    assert result.details["all_connected"] is False
    assert result.details["connected_windows"] == 0
    assert result.details["failure_codes"] == [
        "authorization_batch_unavailable"
    ]
    assert fixture.authorization.current_authorization() is None
    assert fixture.mouse.clicks == []


def test_single_blocked_source_window_rejects_the_entire_batch():
    now = [0.0]
    healthy = make_window(1, fingerprint="a" * 64)
    blocked = make_window(2, fingerprint="b" * 64)
    provider_state = {
        "value": ResolvedTargetWindows(
            (healthy,),
            ("window_identity_duplicate",),
            frozenset({blocked.launch_fingerprint}),
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

    assert prepared.success is False
    assert prepared.details["failure_codes"] == [
        "authorization_batch_unavailable"
    ]
    assert first.details["clicked_windows"] == 0
    assert recovered.details["clicked_windows"] == 0
    assert first.details["failure_codes"] == [
        "authorization_batch_unavailable"
    ]
    assert recovered.details["failure_codes"] == [
        "authorization_batch_unavailable"
    ]
    assert fixture.capture.calls == []
    assert fixture.mouse.clicks == []
    assert fixture.authorization.current_authorization() is None


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
    assert result.details["connected_windows"] == 0
    assert result.details["failure_codes"] == [
        "authorization_batch_unavailable"
    ]
    assert result.details["next_check_seconds"] == 60
    assert fixture.mouse.clicks == []
    assert fixture.authorization.current_authorization() is None


@pytest.mark.parametrize(
    ("failure_codes", "block_missing"),
    (
        (("window_offline",), False),
        ((), True),
    ),
)
def test_scoped_source_failure_revokes_the_complete_authorization_batch(
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
        windows[0].launch_fingerprint: ReconnectScreenState.UNKNOWN,
        affected: ReconnectScreenState.UNKNOWN,
    }
    assert fixture.controller._trusted_connected_evidence == {}
    assert fixture.mouse.clicks == []
    assert fixture.authorization.current_authorization() is None


def test_scoped_source_subset_without_failure_revokes_complete_batch():
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
    assert result.details["source_missing_windows"] == 2
    assert fixture.controller.role_screen_states() == {
        windows[0].launch_fingerprint: ReconnectScreenState.UNKNOWN,
        missing: ReconnectScreenState.UNKNOWN,
    }
    assert fixture.controller._trusted_connected_evidence == {}
    assert fixture.mouse.clicks == []
    assert fixture.authorization.current_authorization() is None


def test_scoped_source_subset_without_failure_advances_source_generation():
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
    provider_state["value"] = ResolvedTargetWindows((windows[0],))
    result = fixture.controller.reconnect()

    assert result.details["all_connected"] is False
    assert result.details["source_missing_windows"] == 2
    assert fixture.controller._source_state_generation > generation_before
    assert fixture.controller.role_screen_states() == {
        windows[0].launch_fingerprint: ReconnectScreenState.UNKNOWN,
        missing: ReconnectScreenState.UNKNOWN,
    }
    assert fixture.authorization.current_authorization() is None


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
    provider_state["value"] = ResolvedTargetWindows((windows[0],))

    result = fixture.controller.reconnect()

    assert result.details["all_connected"] is False
    assert result.details["source_missing_windows"] == 2
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


def test_global_source_subset_revokes_complete_connected_batch():
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
    provider_state["value"] = ResolvedTargetWindows((windows[0],))
    result = fixture.controller.reconnect()

    assert connected.code == "reconnect.connected"
    assert result.details["source_missing_windows"] == 2
    assert fixture.controller.role_screen_states() == {
        windows[0].launch_fingerprint: ReconnectScreenState.UNKNOWN,
        windows[1].launch_fingerprint: ReconnectScreenState.UNKNOWN,
    }
    assert fixture.controller._trusted_connected_evidence == {}
    assert fixture.controller._source_state_generation > generation_before
    assert fixture.capture.calls == [window.handle for window in windows]
    assert fixture.authorization.current_authorization() is None


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
            (windows[0],),
            ("unidentified_candidate_window",),
        ),
    )
    fixture.controller.set_allowed_fingerprints(selected)

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.success is False
    assert result.code == "reconnect.waiting"
    assert result.details["clicked_windows"] == 0
    assert result.details["failure_codes"] == [
        "authorization_batch_unavailable"
    ]
    assert fixture.mouse.clicks == []
    assert fixture.authorization.current_authorization() is None
    assert windows[0].launch_fingerprint not in (
        fixture.controller._action_confirmations
    )


def test_unscoped_incomplete_window_set_still_fails_before_capture():
    windows = [make_window(1)]
    fixture = make_controller([2], windows=windows, expected_windows=2)

    result = fixture.controller.reconnect()

    assert result.success is False
    assert result.details["failure_codes"] == [
        "authorization_batch_unavailable"
    ]
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


def _test_evidence_recorder(tmp_path, *, auto_battle_required=False):
    return SmartReconnectEvidenceRecorder(
        tmp_path / "evidence",
        source_identity=RuntimeSourceIdentity(
            "1" * 40,
            False,
            "test",
        ),
        auto_battle_required=auto_battle_required,
        session_id="1" * 24,
    )


def _read_evidence_records(recorder):
    return tuple(
        json.loads(line)
        for line in recorder.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def test_required_evidence_initialization_failure_blocks_execution():
    window = make_window(1)
    fixture = make_controller(
        [2],
        windows=[window],
        expected_windows=1,
        evidence_required=True,
        evidence_initialization_failed=True,
    )

    prepared = fixture.controller.prepare_execution_snapshot()
    result = fixture.controller.reconnect()

    assert prepared.success is False
    assert prepared.code == "reconnect.snapshot_evidence_unavailable"
    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []


def test_unknown_screen_is_recorded_without_input(tmp_path):
    recorder = _test_evidence_recorder(tmp_path)
    window = make_window(1)
    fixture = make_controller(
        [255],
        windows=[window],
        expected_windows=1,
        evidence_recorder=recorder,
        evidence_required=True,
    )
    assert activate_current_window_snapshot(fixture).success is True

    result = fixture.controller.reconnect()
    records = _read_evidence_records(recorder)

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []
    assert any(
        record.get("record_type") == "observation"
        and record.get("state") == "unknown"
        and isinstance(record.get("scan_duration_ms"), int)
        for record in records
    )
    assert not any(record.get("record_type") == "action" for record in records)


def test_confirmed_disconnect_records_durable_intent_and_result(tmp_path):
    recorder = _test_evidence_recorder(tmp_path)
    window = make_window(1)
    fixture = make_controller(
        [2],
        windows=[window],
        expected_windows=1,
        evidence_recorder=recorder,
        evidence_required=True,
    )
    assert activate_current_window_snapshot(fixture).success is True

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()
    records = _read_evidence_records(recorder)

    assert result.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [(window.handle, (0.5, 0.5))]
    intents = tuple(
        record
        for record in records
        if record.get("record_type") == "action_intent"
        and record.get("action") == "confirm_disconnect"
    )
    actions = tuple(
        record
        for record in records
        if record.get("record_type") == "action"
        and record.get("action") == "confirm_disconnect"
    )
    assert len(intents) == 1
    assert len(actions) == 1
    assert actions[0]["intent_sequence"] == intents[0]["sequence"]
    assert actions[0]["clicked"] is True
    assert actions[0]["identity_verified"] is True


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


def test_global_reconnect_uses_the_authorized_main_role_for_a_level_tie():
    window = make_window(1)
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

    fixture = make_controller(
        [5],
        windows=[window],
        expected_windows=14,
        recognizer=TiedLevelRecognizer(),
        mouse=mouse,
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
        authorization_target_identities={
            window.launch_fingerprint: _stable_target_identity(
                window,
                character_id="main-character",
                role_aliases=("角色甲",),
                importance=CharacterImportance.PRIMARY,
                slot_index=1,
                line_number=1,
            )
        },
    )
    controller = fixture.controller
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
    assert restarter.calls == [(windows[0], plan.targets[0])]
    assert result.details["next_check_seconds"] == 2


def test_battle_disconnect_without_unique_target_retries_short_and_zero_input():
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [2, 1],
        battle_markers={2},
        battle_restarter=restarter,
        authorization_missing_last_target=True,
    )

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.success is False
    assert "authorization_batch_unavailable" in result.details["failure_codes"]
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

    fixture.controller._primary_entry_authorized.add(
        windows[0].launch_fingerprint
    )
    complete_with_fresh_connected_frames(fixture)
    assert statuses.messages() == ()


def test_unknown_battle_identity_uses_group_unknown_status():
    statuses = ReconnectFailureStatusService()
    fixture = make_controller(
        [2, 1],
        battle_markers={2},
        battle_restarter=FakeBattleRestarter(),
        failure_status_service=statuses,
        authorization_missing_last_target=True,
    )

    fixture.controller.reconnect()
    fixture.controller.reconnect()

    assert statuses.messages() == ()


def test_bound_missing_reopen_opens_once_and_keeps_sibling_authorized(tmp_path):
    fixture, windows, restarter = make_bound_pending_reopen_fixture(tmp_path)

    reopened = fixture.controller.reconnect()
    after_target_was_isolated = fixture.controller.reconnect()

    assert reopened.details["restarted_windows"] == 1
    assert len(restarter.reopen_calls) == 1
    assert restarter.reopen_calls[0][1] == (windows[1],)
    current = fixture.authorization.current_authorization()
    assert current is not None
    assert tuple(target.fingerprint for target in current.targets) == (
        windows[1].launch_fingerprint,
    )
    assert after_target_was_isolated.details["restarted_windows"] == 0
    assert after_target_was_isolated.details["clicked_windows"] == 0
    assert len(restarter.reopen_calls) == 1


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
    provider_state = {"value": ResolvedTargetWindows(tuple(windows))}
    shortcut = tmp_path / "only-target.lnk"
    shortcut.write_bytes(b"shortcut")
    restarter = FakeBattleRestarter(succeeds=False)
    fixture = make_controller(
        [2, 1],
        windows=windows,
        clock=lambda: now[0],
        battle_markers={2},
        battle_restarter=restarter,
        failure_status_service=ReconnectFailureStatusService(),
        target_windows_provider=lambda: provider_state["value"],
        visible_capture_provider=visible_capture,
        obscured_capture_provider=obscured_capture,
        window_backend=backend,
        ungrouped_shortcut_provider=lambda fingerprint: (
            shortcut
            if fingerprint == windows[0].launch_fingerprint
            else None
        ),
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
            tuple(windows),
            ("window_identity_blocked",),
            frozenset({windows[0].launch_fingerprint}),
        )
        now[0] = 6.0
        fixture.controller.reconnect()
        assert fixture.controller._source_state_generation > generation_before
        provider_state["value"] = ResolvedTargetWindows(tuple(windows))
        recovery_times = (7.0, 8.0, 12.0)

    for timestamp in recovery_times:
        now[0] = timestamp
        fixture.controller.reconnect()

    assert len(restarter.calls) == 1
    assert restarter.calls[0][0].handle == windows[0].handle
    assert fixture.mouse.clicks == []


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
    source_failed = [False]

    def target_windows():
        if source_failed[0]:
            raise RuntimeError("target source failed")
        return (window,)

    fixture = make_controller(
        [1],
        windows=[window],
        expected_windows=1,
        battle_restarter=restarter,
        group_launch_plan=make_group_plan(tmp_path, [window], "120"),
        target_windows_provider=target_windows,
    )
    source_failed[0] = True

    fixture.controller._report_reconnect_failure(window.launch_fingerprint)

    assert restarter.calls == []
    assert restarter.reopen_calls == []


def test_target_provider_failure_blocks_pending_role_reopen(tmp_path):
    window = make_window(1)
    restarter = FakeBattleRestarter()
    source_failed = [False]

    def target_windows():
        if source_failed[0]:
            raise RuntimeError("target source failed")
        return (window,)

    fixture = make_controller(
        [1],
        windows=[window],
        expected_windows=1,
        battle_restarter=restarter,
        group_launch_plan=make_group_plan(tmp_path, [window], "120"),
        target_windows_provider=target_windows,
    )
    fixture.controller._pending_reopen_fingerprints.add(
        window.launch_fingerprint
    )
    fixture.controller._reopen_retry_after[window.launch_fingerprint] = 0.0
    source_failed[0] = True

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
    assert followup.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == [
        (1, (0.5, 0.5)),
        (1, (0.505, 0.856)),
    ]


def test_same_group_restart_does_not_restore_old_input_authority(tmp_path):
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
        "login_start": 1,
    }
    assert before_deadline.details["clicked_windows"] == 0
    assert second.mouse.clicks == []

    now[0] = 1015.0
    after_deadline = second.controller.reconnect()

    assert after_deadline.details["clicked_windows"] == 0
    assert second.mouse.clicks == []


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
    assert isinstance(migrated["scope_token"], str)
    assert len(migrated["scope_token"]) == 64
    assert migrated["scope_token"] != "a" * 64
    assert migrated["pending_fingerprints"] == []
    assert migrated["active_fingerprints"] == []
    assert migrated["flow_pause_until"] == {}
    assert fingerprint not in state_path.read_text(encoding="utf-8")


def test_timeout_flow_pause_does_not_restore_input_authority_after_restart(
    tmp_path,
):
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
    assert after_deadline.details["clicked_windows"] == 0
    assert second.mouse.clicks == []


def test_popup_automation_context_does_not_restore_authority_after_restart(
    tmp_path,
):
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

    assert result.code == "reconnect.waiting"
    assert result.details["clicked_windows"] == 0
    assert second.mouse.clicks == []


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
    fixture = make_controller(
        [2, 1],
        windows=windows,
        expected_windows=2,
        recognizer=recognizer,
        mouse=mouse,
        clock=lambda: now[0],
        registered_role_provider=lambda: (
            RegisteredReconnectRole(
                "160帥",
                CharacterImportance.PRIMARY,
            ),
        ),
        authorization_target_identities={
            windows[0].launch_fingerprint: _stable_target_identity(
                windows[0],
                character_id="primary-character",
                role_aliases=("160帥",),
                importance=CharacterImportance.PRIMARY,
                slot_index=2,
                line_number=1,
            ),
            windows[1].launch_fingerprint: _stable_target_identity(
                windows[1],
                character_id="secondary-character",
                role_aliases=("secondary-role",),
                importance=CharacterImportance.SECONDARY,
                slot_index=0,
                line_number=1,
            ),
        },
    )
    controller = fixture.controller
    capture = fixture.capture

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
        fixture,
        now=now,
    )
    finish = controller.reconnect()

    assert finish.code == "reconnect.connected"
    assert finish.details["connected_windows"] == 2
    assert finish.details["all_connected"] is True
    assert controller.reconnecting_fingerprints() == frozenset()


def test_character_selection_confirms_exact_role_before_entering_game(tmp_path):
    windows = [make_window(1), make_window(2)]
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
                character_candidates=(
                    CharacterSelectionCandidate(
                        160,
                        CharacterImportance.PRIMARY,
                        2,
                        self.selected,
                        (
                            CHARACTER_ENTER_CLICK_POINT
                            if self.selected
                            else (0.651, 0.706)
                        ),
                        digit_count=3,
                        identity="160主",
                    ),
                ),
            )

    recognizer = CharacterSequenceRecognizer()
    fixture = make_controller(
        [5, 1],
        windows=windows,
        expected_windows=2,
        recognizer=recognizer,
        mouse=mouse,
        clock=lambda: now[0],
        authorization_target_identities={
            windows[0].launch_fingerprint: _stable_target_identity(
                windows[0],
                character_id="primary-character",
                role_aliases=("160主",),
                importance=CharacterImportance.PRIMARY,
                slot_index=2,
                line_number=1,
            ),
            windows[1].launch_fingerprint: _stable_target_identity(
                windows[1],
                character_id="secondary-character",
                role_aliases=("副角色二",),
                importance=CharacterImportance.SECONDARY,
                slot_index=0,
                line_number=1,
            ),
        },
    )
    controller = fixture.controller
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
        "reconnect.snapshot_identity_unsafe"
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
    assert duplicate_result.success is False
    assert duplicate_result.details["failure_codes"] == [
        "authorization_batch_unavailable"
    ]

    incomplete = make_controller(
        [1],
        windows=[make_window(1, process_id=0)],
        expected_windows=1,
    )
    incomplete.controller.set_execution_enabled(False)
    assert incomplete.controller.prepare_execution_snapshot().code == (
        "reconnect.snapshot_identity_unsafe"
    )


def test_activation_snapshot_rejects_one_incomplete_member_of_shared_batch():
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

    assert prepared.success is False
    assert prepared.details["failure_codes"] == [
        "authorization_batch_unavailable"
    ]
    assert result.details["clicked_windows"] == 0
    assert fixture.capture.calls == []
    assert fixture.mouse.clicks == []


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
        (4, None),
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

    if expected_point is None:
        assert result.details["clicked_windows"] == 0
        assert fixture.mouse.clicks == []
    else:
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
    assert fixture.controller._source_state_generation == generation_before
    assert accepted.details["discovered_windows"] == 1


def test_snapshot_duplicate_source_identity_revokes_the_complete_batch():
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

    assert result.details["discovered_windows"] == 0
    assert result.details["clicked_windows"] == 0
    assert "snapshot_identity_collision" in result.details["failure_codes"]
    assert fixture.mouse.clicks == []
    assert fixture.authorization.current_authorization() is None


def test_stop_revokes_snapshot_and_initial_login_authorization():
    fixture = make_controller([3], expected_windows=1)
    activate_current_window_snapshot(fixture)

    fixture.controller.set_execution_enabled(False)

    assert fixture.controller._activation_snapshot_instances is None
    assert fixture.controller._initial_login_authorizations == {}
    assert fixture.controller._allowed_fingerprints is None
    assert fixture.authorization.current_authorization() is None
    fixture.controller.set_execution_enabled(True)
    fixture.controller.reconnect()
    fixture.controller.reconnect()
    assert fixture.mouse.clicks == []


def test_identity_bound_snapshot_uses_its_sealed_restart_target(
    tmp_path,
):
    window = make_window(1, fingerprint="a" * 64)
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [2],
        windows=[window],
        expected_windows=1,
        battle_markers=(2,),
        battle_restarter=restarter,
        group_launch_plan=make_group_plan(tmp_path, [window]),
    )
    activate_current_window_snapshot(fixture)

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert "battle_restart_identity_unresolved" not in result.details[
        "failure_codes"
    ]
    assert result.details["restarted_windows"] == 1
    assert len(restarter.calls) == 1
    assert restarter.reopen_calls == []


def test_repeated_stop_reprepares_the_same_sealed_restart_target(
    tmp_path,
):
    window = make_window(1, fingerprint="a" * 64)
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [2],
        windows=[window],
        expected_windows=1,
        battle_markers=(2,),
        battle_restarter=restarter,
        group_launch_plan=make_group_plan(tmp_path, [window]),
    )

    fixture.controller.set_execution_enabled(False)
    fixture.controller.set_execution_enabled(False)
    prepared = fixture.controller.prepare_execution_snapshot()
    assert prepared.success is True
    assert fixture.controller.set_execution_enabled(True) is True

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert "battle_restart_identity_unresolved" not in result.details[
        "failure_codes"
    ]
    assert result.details["restarted_windows"] == 1
    assert len(restarter.calls) == 1
    assert restarter.reopen_calls == []


def test_failed_reprepare_does_not_change_later_sealed_restart_target(
    tmp_path,
):
    window = make_window(1, fingerprint="a" * 64)
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [2],
        windows=[window],
        expected_windows=1,
        battle_markers=(2,),
        battle_restarter=restarter,
        group_launch_plan=make_group_plan(tmp_path, [window]),
    )
    assert fixture.preparation is not None
    target_provider = fixture.preparation._target_provider

    fixture.controller.set_execution_enabled(False)
    fixture.preparation._target_provider = lambda _targets: ()
    failed = fixture.controller.prepare_execution_snapshot()
    fixture.preparation._target_provider = target_provider
    prepared = fixture.controller.prepare_execution_snapshot()
    assert failed.success is False
    assert prepared.success is True
    assert fixture.controller.set_execution_enabled(True) is True

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert "battle_restart_identity_unresolved" not in result.details[
        "failure_codes"
    ]
    assert result.details["restarted_windows"] == 1
    assert len(restarter.calls) == 1
    assert restarter.reopen_calls == []


@pytest.mark.parametrize("reset_plan", (False, True))
def test_group_plan_reset_cannot_change_current_sealed_restart_batch(
    tmp_path,
    reset_plan,
):
    window = make_window(1, fingerprint="a" * 64)
    plan = make_group_plan(tmp_path, [window])
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [2],
        windows=[window],
        expected_windows=1,
        battle_markers=(2,),
        battle_restarter=restarter,
        group_launch_plan=plan,
    )
    prepared = activate_current_window_snapshot(fixture)
    assert prepared.success is True

    if reset_plan:
        fixture.controller.set_group_launch_plan(None)
    fixture.controller.set_group_launch_plan(plan)
    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert "battle_restart_identity_unresolved" not in result.details[
        "failure_codes"
    ]
    assert result.details["restarted_windows"] == 1
    assert len(restarter.calls) == 1
    assert restarter.reopen_calls == []


def test_identity_bound_group_batch_with_sealed_target_restarts_that_target(
    tmp_path,
):
    window = make_window(1, fingerprint="a" * 64)
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [2],
        windows=[window],
        expected_windows=1,
        battle_markers=(2,),
        battle_restarter=restarter,
        group_launch_plan=make_group_plan(tmp_path, [window]),
    )

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["failure_codes"] == []
    assert result.details["restarted_windows"] == 1
    assert len(restarter.calls) == 1
    assert restarter.calls[0][0].handle == window.handle
    assert restarter.calls[0][1].fingerprint == window.launch_fingerprint
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
        authorization_original_line_number=8,
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


def test_real_controller_never_reveals_or_restores_windows_for_capture():
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


def test_real_controller_never_uses_obscured_provider_for_active_reconnect(
    monkeypatch,
):
    window = make_window(1)
    coordinator = SmartReconnectAuthorizationCoordinator()
    target = ReconnectAuthorizationTarget(
        fingerprint=window.launch_fingerprint,
        instance=WindowInstanceToken.from_window(window),
        character_id="capture-character",
        role_aliases=("capture-role",),
        importance=CharacterImportance.PRIMARY,
        original_slot_index=0,
        original_line_number=1,
        shortcut_seal=ShortcutSeal(
            file_identity=ShortcutFileIdentity(
                normalized_path=str(
                    Path.cwd() / ".pytest-shortcuts" / "capture.lnk"
                ),
                volume_serial_number=1,
                file_index=1,
            ),
            content_sha256="1" * 64,
            launch_fingerprint=window.launch_fingerprint,
        ),
    )
    preparation = FakeAuthorizationPreparation(
        coordinator,
        ReconnectSourceIdentity(
            identity_generation=1,
            config_revision=1,
            group_id="capture-group",
            group_name="capture-group",
            character_ids=("capture-character",),
        ),
        (target,),
    )
    controller = WindowsSmartReconnectController.for_real_windows(
        reference_dir=Path("assets") / "reconnect_reference",
        expected_windows=1,
        window_backend=ObscuredWindowBackend([window]),
        authorization_coordinator=coordinator,
        preparation_service=preparation,
        shortcut_seal_resolver=FakeShortcutSealResolver(),
        auto_battle_enabled=False,
    )
    visible = FakeCaptureProvider({window.handle: None})
    primary = FakeCaptureProvider({window.handle: None})
    assert controller._obscured_capture_provider is None
    assert controller._active_refresh_capture_provider is None
    monkeypatch.setattr(
        controller._visible_capture_provider,
        "capture",
        visible.capture,
    )
    monkeypatch.setattr(
        controller._capture_provider,
        "capture",
        primary.capture,
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
    assert visible.calls == [window.handle]
    assert primary.calls == []

    assert controller.prepare_execution_snapshot().success is True
    controller.set_execution_enabled(True)
    result = controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert visible.calls == [window.handle, window.handle]
    assert primary.calls == [window.handle]


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
        authorization_original_line_number=8,
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
    fixture = make_controller(
        [2, 1],
        windows=windows,
        expected_windows=2,
        primary_capture_provider=primary,
        visible_capture_provider=visible,
        recognizer=recognizer,
    )
    controller = fixture.controller

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
    fixture = make_controller(
        [2, 1],
        windows=windows,
        expected_windows=2,
        primary_capture_provider=primary,
        visible_capture_provider=visible,
        recognizer=recognizer,
        mouse=mouse,
    )
    controller = fixture.controller

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
    fixture = make_controller(
        [2, 1],
        windows=windows,
        expected_windows=2,
        window_backend=ObscuredWindowBackend(windows),
        primary_capture_provider=primary,
        visible_capture_provider=visible,
        active_refresh_capture_provider=active_refresh,
        recognizer=recognizer,
        mouse=mouse,
    )
    controller = fixture.controller

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


def test_obscured_failure_stage_is_preserved_in_capture_diagnostics():
    window = make_window(1)
    visible = FakeCaptureProvider({1: None})
    obscured = FakeCaptureProvider({1: None})
    obscured.last_failure_stage = "foreground_changed"
    fixture = make_controller(
        [1],
        windows=[window],
        expected_windows=1,
        visible_capture_provider=visible,
        obscured_capture_provider=obscured,
        primary_capture_is_trusted=False,
        window_backend=ObscuredWindowBackend([window]),
    )

    result = fixture.controller.reconnect()

    assert [
        item["rejection_gate"]
        for item in result.details["capture_diagnostics"]
    ] == ["foreground_changed"]
    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []


def test_passive_obscured_withholding_is_not_a_capture_failure():
    window = make_window(1)
    visible = FakeCaptureProvider({1: None})
    obscured = FakeCaptureProvider({1: None})
    obscured.last_failure_stage = "foreground_changed"
    fixture = make_controller(
        [1],
        windows=[window],
        expected_windows=1,
        visible_capture_provider=visible,
        obscured_capture_provider=obscured,
        primary_capture_is_trusted=False,
        window_backend=ObscuredWindowBackend([window]),
    )
    fixture.controller._begin_capture_diagnostics([window])

    observed = fixture.controller.observe_screen_states(
        [window.launch_fingerprint]
    )

    assert observed == {
        window.launch_fingerprint: ReconnectScreenState.UNKNOWN,
    }
    assert [
        item.rejection_gate
        for item in fixture.controller.anonymous_capture_diagnostics()
    ] == ["passive_capture_withheld"]
    assert obscured.calls == []
    assert fixture.mouse.clicks == []


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
    rebound_first_frame = fixture.controller.reconnect()
    now[0] = 40.0
    replacement_event = fixture.controller.reconnect()

    assert second_event.details["clicked_windows"] == 1
    assert rebound_first_frame.details["clicked_windows"] == 1
    assert replacement_event.details["clicked_windows"] == 0
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
    assert result.details["failure_codes"] == [
        "authorization_batch_unavailable"
    ]
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

    plan = GroupLaunchPlan(
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
    fixture = make_controller(
        [5, 1],
        windows=windows,
        expected_windows=2,
        primary_capture_provider=capture,
        recognizer=CandidateRecognizer(),
        mouse=mouse,
        group_launch_plan=plan,
        authorization_target_identities={
            windows[0].launch_fingerprint: _stable_target_identity(
                windows[0],
                character_id="character-120",
                role_aliases=("120古",),
                importance=CharacterImportance.PRIMARY,
                slot_index=1,
            ),
            windows[1].launch_fingerprint: _stable_target_identity(
                windows[1],
                character_id="character-160",
                role_aliases=("160靈",),
                importance=CharacterImportance.SECONDARY,
                slot_index=2,
            ),
        },
    )
    controller = fixture.controller
    controller._pending_reconnect_fingerprints.add(
        windows[0].launch_fingerprint
    )

    controller.reconnect()
    result = controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert mouse.clicks == []


def test_unprepared_user_reported_selection_never_restores_input_authority(
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

    assert right_result.details["clicked_windows"] == 0
    assert right_mouse.clicks == []


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
    fixture, windows, restarter = make_bound_pending_reopen_fixture(tmp_path)
    fingerprint = windows[0].launch_fingerprint
    changed = [False]

    original_backend_call = fixture.controller._run_authorized_backend_call

    def change_settings_before_reopen(callback, **kwargs):
        if not changed[0]:
            changed[0] = True
            fixture.controller.set_capture_settings(
                SmartReconnectCaptureSettings(
                    visible=True,
                    obscured=True,
                    minimized=False,
                )
            )
        return original_backend_call(callback, **kwargs)

    monkeypatch.setattr(
        fixture.controller,
        "_run_authorized_backend_call",
        change_settings_before_reopen,
    )

    result = fixture.controller.reconnect()

    assert changed == [True]
    assert result.details["restarted_windows"] == 0
    assert restarter.reopen_calls == []
    assert fingerprint not in fixture.controller._pending_reopen_fingerprints
    assert fixture.controller._pending_reopen_authorizations == {}


def test_temporarily_missing_role_revokes_the_complete_batch_confirmation():
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
    assert missing.details["source_missing_windows"] == 2
    assert fixture.authorization.current_authorization() is None
    assert fixture.controller._action_confirmations == {}
    fixture.controller._window_backend.windows = windows
    restored = fixture.controller.reconnect()

    assert restored.details["actionable_windows"] == 0
    assert fixture.mouse.clicks == []
    confirmed = fixture.controller.reconnect()
    assert confirmed.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [(1, (0.5, 0.5))]


def test_temporarily_missing_role_revokes_its_old_transition_wait_deadline():
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

    assert fingerprint not in fixture.controller._flow_pause_until
    assert fixture.authorization.current_authorization() is None

    fixture.controller._window_backend.windows = windows
    current_time[0] = 5.0
    prepared = activate_current_window_snapshot(fixture)
    assert prepared.success is True
    assert fixture.controller._action_wait_seconds(
        fingerprint,
        ReconnectScreenState.LOGIN_START,
        current_time[0],
    ) == 0


def test_unprepared_missing_role_blocks_open_disconnected_role_action(
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
    assert "authorization_batch_unavailable" in result.details["failure_codes"]
    assert fixture.capture.calls == []
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
    assert result.details["connected_windows"] == 0
    assert result.details["next_check_seconds"] == 60
    assert "authorization_batch_unavailable" in result.details["failure_codes"]
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
    assert "authorization_batch_unavailable" in result.details["failure_codes"]
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
    assert "authorization_batch_unavailable" in result.details["failure_codes"]
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
    assert "authorization_batch_unavailable" in (
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
    assert "authorization_batch_unavailable" in result.details["failure_codes"]
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
    assert fixture.controller.reconnecting_fingerprints() == frozenset()
    assert fixture.authorization.current_authorization() is None
    assert fixture.capture.calls == []
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


def test_passive_observer_cannot_revoke_an_active_reconnect_scan(
    monkeypatch,
):
    window = make_window(1)
    fixture = make_controller(
        [2],
        windows=[window],
        expected_windows=1,
    )
    assert activate_current_window_snapshot(fixture).success is True
    fixture.controller.reconnect()

    scan_capturing = threading.Event()
    release_scan = threading.Event()
    original_capture = fixture.controller._capture_and_recognize

    def capture_with_barrier(*args, **kwargs):
        if (
            threading.current_thread().name == "reconnect-worker"
            and not scan_capturing.is_set()
        ):
            scan_capturing.set()
            assert release_scan.wait(1)
        return original_capture(*args, **kwargs)

    monkeypatch.setattr(
        fixture.controller,
        "_capture_and_recognize",
        capture_with_barrier,
    )
    reconnect_results = []
    worker = threading.Thread(
        name="reconnect-worker",
        target=lambda: reconnect_results.append(
            fixture.controller.reconnect()
        ),
    )
    generation_before = fixture.controller._source_state_generation
    worker.start()
    assert scan_capturing.wait(1) is True

    conflicting_geometry = replace(
        window,
        rect=(10, 10, 910, 610),
    )
    started = time.monotonic()
    observed = fixture.controller.observe_screen_states(
        (window.launch_fingerprint,),
        candidate_windows=(conflicting_geometry,),
    )
    elapsed = time.monotonic() - started

    assert observed == {
        window.launch_fingerprint: ReconnectScreenState.UNKNOWN,
    }
    assert elapsed < 0.1
    assert fixture.controller._source_state_generation == generation_before

    release_scan.set()
    worker.join(1)

    assert worker.is_alive() is False
    assert reconnect_results[0].details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [(window.handle, (0.5, 0.5))]


def test_queued_execution_scan_cannot_replace_active_scan_owner(monkeypatch):
    fixture = make_controller([1])
    fixture.controller.reconnect()
    baseline = fixture.controller.last_result
    assert baseline is not None
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    release_second = threading.Event()
    worker_ids = {}

    def staged_scan(*, execute):
        assert execute is True
        if threading.current_thread().name == "first-execution-scan":
            first_entered.set()
            assert release_first.wait(1)
        else:
            second_entered.set()
            assert release_second.wait(1)
        return baseline

    monkeypatch.setattr(fixture.controller, "_scan_locked", staged_scan)

    def run_scan(worker_name):
        worker_ids[worker_name] = threading.get_ident()
        fixture.controller.reconnect()

    first = threading.Thread(
        name="first-execution-scan",
        target=run_scan,
        args=("first",),
    )
    second = threading.Thread(
        name="second-execution-scan",
        target=run_scan,
        args=("second",),
    )
    first.start()
    assert first_entered.wait(1) is True
    second.start()
    deadline = time.monotonic() + 1
    while "second" not in worker_ids and time.monotonic() < deadline:
        time.sleep(0.001)

    assert worker_ids["second"] != worker_ids["first"]
    assert second_entered.is_set() is False
    assert fixture.controller._execution_scan_thread_id == worker_ids["first"]

    release_first.set()
    first.join(1)
    assert first.is_alive() is False
    assert second_entered.wait(1) is True
    assert fixture.controller._execution_scan_thread_id == worker_ids["second"]

    release_second.set()
    second.join(1)

    assert second.is_alive() is False
    assert fixture.controller._execution_scan_active.is_set() is False
    assert fixture.controller._execution_scan_thread_id is None


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
    fixture = make_controller(
        [2, 1],
        windows=windows,
        expected_windows=2,
        window_backend=backend,
        primary_capture_provider=capture,
        recognizer=FakeRecognizer(
            {
                1: ReconnectScreenState.CONNECTED,
                2: ReconnectScreenState.DISCONNECTED,
            },
            points={2: (0.5, 0.5)},
        ),
        mouse=mouse,
        clock=clock,
    )
    controller = fixture.controller

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
    assert "authorization_batch_unavailable" in (
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
    fixture = make_controller(
        [2, 1],
        windows=windows,
        expected_windows=2,
        window_backend=backend,
        primary_capture_provider=capture,
        recognizer=FakeRecognizer(
            {
                1: ReconnectScreenState.CONNECTED,
                2: ReconnectScreenState.DISCONNECTED,
            },
            points={2: (0.5, 0.5)},
        ),
        mouse=mouse,
        clock=clock,
    )
    controller = fixture.controller

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
    fixture = make_controller(
        [2, 1],
        windows=windows,
        expected_windows=2,
        window_backend=backend,
        primary_capture_provider=capture,
        recognizer=FakeRecognizer(
            {
                1: ReconnectScreenState.CONNECTED,
                2: ReconnectScreenState.DISCONNECTED,
            },
            points={2: (0.5, 0.5)},
        ),
        mouse=mouse,
        clock=iter(float(value) for value in range(0, 200, 5)).__next__,
    )
    controller = fixture.controller

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
    fixture = make_controller(
        [2, 1],
        windows=windows,
        expected_windows=2,
        primary_capture_provider=capture,
        recognizer=FakeRecognizer(
            {
                1: ReconnectScreenState.CONNECTED,
                2: ReconnectScreenState.DISCONNECTED,
                3: ReconnectScreenState.LOGIN_START,
            },
            points={2: (0.5, 0.5), 3: (0.5, 0.8)},
        ),
        mouse=mouse,
        clock=clock,
    )
    controller = fixture.controller

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

    if replacement_kind == "same_fingerprint":
        prepared = activate_current_window_snapshot(fixture)
        assert prepared.success is True
    else:
        fixture.authorization.begin_reprepare()
        fixture.controller._authorization_batch = None
        fixture.controller._preparation = None

    now[0] = 10.0
    second = fixture.controller.reconnect()
    now[0] = 14.999
    third = fixture.controller.reconnect()
    now[0] = 15.0
    fourth = fixture.controller.reconnect()

    assert second.details["clicked_windows"] == 0
    assert third.details["clicked_windows"] == 0
    if replacement_kind == "same_fingerprint":
        assert fourth.details["clicked_windows"] == 1
        assert fixture.mouse.clicks == [(replacement.handle, (0.5, 0.5))]
    else:
        assert fourth.details["clicked_windows"] == 0
        assert "authorization_batch_unavailable" in fourth.details[
            "failure_codes"
        ]
        assert fixture.mouse.clicks == []


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
    prepared = activate_current_window_snapshot(fixture)
    assert prepared.success is True
    fixture.controller._pending_reconnect_fingerprints.add(fingerprint)
    fixture.controller._primary_entry_authorized.add(fingerprint)
    fixture.controller._terminal_ready_after[fingerprint] = 0.0
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
    assert fingerprint not in fixture.controller._pending_reopen_fingerprints
    assert fixture.authorization.current_authorization() is None
    assert restarter.reopen_calls == []
    assert restarter.calls == []


def test_unprepared_missing_role_never_opens_a_shortcut(tmp_path):
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

    assert "authorization_batch_unavailable" in result.details["failure_codes"]
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
    fixture, windows, restarter = make_bound_pending_reopen_fixture(tmp_path)
    missing, present = windows
    missing_fingerprint = missing.launch_fingerprint
    present_fingerprint = present.launch_fingerprint
    original_backend_call = fixture.controller._run_authorized_backend_call
    revoked = []

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

    result = fixture.controller.reconnect()

    assert revoked == [{present_fingerprint: ReconnectScreenState.UNKNOWN}]
    assert result.details["restarted_windows"] == 0
    assert restarter.reopen_calls == []
    assert missing_fingerprint in fixture.controller._pending_reopen_fingerprints


def test_failure_report_restart_rejects_revoked_source_generation(tmp_path):
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
    generation = fixture.controller._source_state_generation_snapshot()
    fixture.controller._revoke_source_failure_evidence(
        frozenset({fingerprint}),
        revoke_runtime_authority=True,
    )

    fixture.controller._report_reconnect_failure(
        fingerprint,
        expected_source_state_generation=generation,
    )

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
    plan = make_group_plan(tmp_path, [original])
    shortcut = plan.targets[0].shortcut_path
    shortcut.write_bytes(b"shortcut")
    fixture = make_controller(
        [2],
        windows=[original],
        expected_windows=1,
        window_backend=backend,
        battle_markers={2},
        battle_restarter=restarter,
        group_launch_plan=plan,
        ungrouped_shortcut_provider=lambda fingerprint: (
            shortcut
            if fingerprint == original.launch_fingerprint
            else None
        ),
        clock=lambda: now[0],
    )

    fixture.controller.reconnect()
    backend.windows = [replacement]
    fixture.capture.states[replacement.handle] = 2
    prepared = activate_current_window_snapshot(fixture)
    assert prepared.success is True
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
    recognizer = None
    if initial_marker == 4:
        recognizer = RecognitionByMarker(
            {
                4: ScreenRecognition(
                    ReconnectScreenState.LINE_SELECTION,
                    0.0,
                    (0.5, 0.327),
                    "line-selection",
                    line_number=1,
                    recent_line_present=True,
                ),
                5: ScreenRecognition(
                    ReconnectScreenState.CHARACTER_SELECTION,
                    0.0,
                    (0.35, 0.85),
                    "character-selection",
                ),
            }
        )
    fixture = make_controller(
        [initial_marker],
        windows=[window],
        expected_windows=1,
        clock=lambda: now[0],
        recognizer=recognizer,
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


def test_registered_character_ocr_variation_is_not_complete_identity():
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

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []


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
    assert entered.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == [(1, highest.click_point)]


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


@pytest.mark.parametrize("provider_mode", ("missing", "failure"))
def test_stable_target_provider_failure_never_falls_back_to_registered_role(
    provider_mode,
):
    selected = CharacterSelectionCandidate(
        160,
        CharacterImportance.PRIMARY,
        2,
        True,
        CHARACTER_ENTER_CLICK_POINT,
        digit_count=3,
        identity="AlphaHero",
    )

    def target_provider(_fingerprint):
        if provider_mode == "failure":
            raise RuntimeError("target provider failed")
        return None

    fixture, window = _single_window_character_fixture(
        _CharacterSequenceRecognizer(lambda _call: (selected,)),
        registered_role_provider=lambda: (
            RegisteredReconnectRole(
                "AlphaHero",
                CharacterImportance.PRIMARY,
            ),
        ),
        target_identity_provider=target_provider,
    )
    fixture.controller._pending_reconnect_fingerprints.add(
        window.launch_fingerprint
    )

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []


def test_stable_target_exact_alias_uses_immutable_batch_slot_in_two_stages():
    now = [0.0]
    other = CharacterSelectionCandidate(
        120,
        None,
        0,
        False,
        (0.355, 0.706),
        digit_count=3,
        identity="OtherRole",
    )
    target_candidate = CharacterSelectionCandidate(
        160,
        None,
        2,
        False,
        (0.651, 0.706),
        digit_count=3,
        identity="AlphaHero",
    )
    selected = replace(
        target_candidate,
        selected=True,
        click_point=CHARACTER_ENTER_CLICK_POINT,
        identity="Alpha…",
    )
    phase = ["select"]
    saved_slots = []
    recognizer = _CharacterSequenceRecognizer(
        lambda _call: (
            (other, target_candidate)
            if phase[0] == "select"
            else (other, selected)
        )
    )
    fixture, window = _single_window_character_fixture(
        recognizer,
        clock=lambda: now[0],
        target_identity_provider=lambda _fingerprint: (
            _stable_target_identity(_fingerprint)
        ),
        verified_slot_recorder=lambda fingerprint, character_id, slot: (
            saved_slots.append((fingerprint, character_id, slot)) or True
        ),
    )
    fixture.controller._pending_reconnect_fingerprints.add(
        window.launch_fingerprint
    )

    fixture.controller.reconnect()
    selected_slot = fixture.controller.reconnect()
    pending = fixture.controller._character_selection_targets[
        window.launch_fingerprint
    ]
    phase[0] = "enter"
    now[0] = 10.0
    fixture.controller.reconnect()
    entered = fixture.controller.reconnect()

    assert selected_slot.details["clicked_windows"] == 1
    assert pending.target.character_id == "character-1"
    assert entered.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == [(1, target_candidate.click_point)]
    assert saved_slots == []


@pytest.mark.parametrize("short_alias", ("甲", "甲乙"))
@pytest.mark.parametrize("saved_slot", (None, 2))
def test_stable_target_first_stage_rejects_short_complete_alias(
    short_alias,
    saved_slot,
):
    selected = saved_slot is not None
    candidate = CharacterSelectionCandidate(
        160,
        None,
        2,
        selected,
        (
            CHARACTER_ENTER_CLICK_POINT
            if selected
            else (0.651, 0.706)
        ),
        digit_count=3,
        identity=short_alias,
    )
    saved_slots = []
    fixture, window = _single_window_character_fixture(
        _CharacterSequenceRecognizer(lambda _call: (candidate,)),
        target_identity_provider=lambda _fingerprint: (
            _stable_target_identity(
                _fingerprint,
                role_aliases=(short_alias,),
                slot_index=saved_slot,
            )
        ),
        verified_slot_recorder=lambda fingerprint, character_id, slot: (
            saved_slots.append((fingerprint, character_id, slot)) or True
        ),
    )
    fingerprint = window.launch_fingerprint
    fixture.controller._pending_reconnect_fingerprints.add(fingerprint)

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []
    assert saved_slots == []
    assert fingerprint not in fixture.controller._character_selection_targets


@pytest.mark.parametrize("short_alias", ("甲", "甲乙"))
def test_stable_target_second_stage_rejects_short_complete_alias(
    short_alias,
):
    now = [0.0]
    candidate = CharacterSelectionCandidate(
        160,
        None,
        2,
        False,
        (0.651, 0.706),
        digit_count=3,
        identity="AlphaHero",
    )
    selected = replace(
        candidate,
        selected=True,
        click_point=CHARACTER_ENTER_CLICK_POINT,
        identity=short_alias,
    )
    phase = ["select"]
    saved_slots = []
    fixture, window = _single_window_character_fixture(
        _CharacterSequenceRecognizer(
            lambda _call: (
                (candidate,)
                if phase[0] == "select"
                else (selected,)
            )
        ),
        clock=lambda: now[0],
        target_identity_provider=lambda _fingerprint: (
            _stable_target_identity(
                _fingerprint,
                role_aliases=("AlphaHero", short_alias),
            )
        ),
        verified_slot_recorder=lambda fingerprint, character_id, slot: (
            saved_slots.append((fingerprint, character_id, slot)) or True
        ),
    )
    fixture.controller._pending_reconnect_fingerprints.add(
        window.launch_fingerprint
    )

    fixture.controller.reconnect()
    fixture.controller.reconnect()
    phase[0] = "enter"
    now[0] = 10.0
    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == [(1, candidate.click_point)]
    assert saved_slots == []


def test_stable_target_saved_slot_rejects_readable_identity_conflict():
    selected = CharacterSelectionCandidate(
        160,
        None,
        2,
        True,
        CHARACTER_ENTER_CLICK_POINT,
        digit_count=3,
        identity="BetaHero",
    )
    fixture, window = _single_window_character_fixture(
        _CharacterSequenceRecognizer(lambda _call: (selected,)),
        target_identity_provider=lambda _fingerprint: (
            _stable_target_identity(_fingerprint, slot_index=2)
        ),
    )
    fixture.controller._pending_reconnect_fingerprints.add(
        window.launch_fingerprint
    )

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []


def test_immutable_target_rejects_a_different_selected_unknown_slot():
    selected = CharacterSelectionCandidate(
        None,
        None,
        1,
        True,
        CHARACTER_ENTER_CLICK_POINT,
        digit_count=None,
        identity=None,
    )
    saved_slots = []
    fixture, window = _single_window_character_fixture(
        _CharacterSequenceRecognizer(lambda _call: (selected,)),
        target_identity_provider=lambda _fingerprint: (
            _stable_target_identity(_fingerprint)
        ),
        verified_slot_recorder=lambda fingerprint, character_id, slot: (
            saved_slots.append((fingerprint, character_id, slot)) or True
        ),
    )
    fixture.controller._pending_reconnect_fingerprints.add(
        window.launch_fingerprint
    )

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []
    assert saved_slots == []


def test_stable_target_multiple_unknown_aliases_are_zero_input():
    selected = CharacterSelectionCandidate(
        160,
        None,
        0,
        True,
        CHARACTER_ENTER_CLICK_POINT,
        digit_count=3,
        identity=None,
    )
    other = CharacterSelectionCandidate(
        120,
        None,
        1,
        False,
        (0.500, 0.706),
        digit_count=3,
        identity=None,
    )
    fixture, window = _single_window_character_fixture(
        _CharacterSequenceRecognizer(lambda _call: (selected, other)),
        target_identity_provider=lambda _fingerprint: (
            _stable_target_identity(_fingerprint)
        ),
    )
    fixture.controller._pending_reconnect_fingerprints.add(
        window.launch_fingerprint
    )

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []


def test_authorization_revocation_after_slot_click_blocks_enter():
    now = [0.0]
    target_candidate = CharacterSelectionCandidate(
        160,
        None,
        2,
        False,
        (0.651, 0.706),
        digit_count=3,
        identity="AlphaHero",
    )
    selected = replace(
        target_candidate,
        selected=True,
        click_point=CHARACTER_ENTER_CLICK_POINT,
        identity="Alpha…",
    )
    phase = ["select"]
    current_target = [None]

    def target_provider(fingerprint):
        if current_target[0] is None:
            current_target[0] = _stable_target_identity(fingerprint)
        return current_target[0]
    recognizer = _CharacterSequenceRecognizer(
        lambda _call: (
            (target_candidate,)
            if phase[0] == "select"
            else (selected,)
        )
    )
    fixture, window = _single_window_character_fixture(
        recognizer,
        clock=lambda: now[0],
        target_identity_provider=target_provider,
    )
    fixture.controller._pending_reconnect_fingerprints.add(
        window.launch_fingerprint
    )

    fixture.controller.reconnect()
    fixture.controller.reconnect()
    current_target[0] = replace(
        current_target[0],
        original_line_number=8,
    )
    fixture.authorization.begin_reprepare()
    fixture.controller._authorization_batch = None
    fixture.controller._preparation = None
    phase[0] = "enter"
    now[0] = 10.0
    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == [(1, target_candidate.click_point)]


def test_stable_target_second_stage_rejects_duplicate_full_alias():
    now = [0.0]
    target_candidate = CharacterSelectionCandidate(
        160,
        None,
        2,
        False,
        (0.651, 0.706),
        digit_count=3,
        identity="AlphaHero",
    )
    selected = replace(
        target_candidate,
        selected=True,
        click_point=CHARACTER_ENTER_CLICK_POINT,
        identity="Alpha…",
    )
    duplicate = replace(
        target_candidate,
        slot_index=1,
        click_point=(0.500, 0.706),
    )
    phase = ["select"]
    recognizer = _CharacterSequenceRecognizer(
        lambda _call: (
            (target_candidate,)
            if phase[0] == "select"
            else (selected, duplicate)
        )
    )
    fixture, window = _single_window_character_fixture(
        recognizer,
        clock=lambda: now[0],
        target_identity_provider=lambda _fingerprint: (
            _stable_target_identity(_fingerprint)
        ),
    )
    fixture.controller._pending_reconnect_fingerprints.add(
        window.launch_fingerprint
    )

    fixture.controller.reconnect()
    fixture.controller.reconnect()
    phase[0] = "enter"
    now[0] = 10.0
    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == [(1, target_candidate.click_point)]


def test_stable_target_change_at_delivery_check_cancels_input():
    selected = CharacterSelectionCandidate(
        160,
        None,
        2,
        True,
        CHARACTER_ENTER_CLICK_POINT,
        digit_count=3,
        identity="AlphaHero",
    )
    window = make_window(1)
    current_target = [_stable_target_identity(window)]
    mouse = FakeMouseBackend()

    def change_target_before_delivery(_handle, _timeout_ms):
        current_target[0] = replace(
            current_target[0],
            original_line_number=8,
        )
        return True

    mouse.probe_responsive = change_target_before_delivery
    fixture = make_controller(
        [5],
        windows=[window],
        expected_windows=1,
        mouse=mouse,
        recognizer=_CharacterSequenceRecognizer(
            lambda _call: (selected,)
        ),
        target_identity_provider=lambda _fingerprint: current_target[0],
    )
    fixture.controller._pending_reconnect_fingerprints.add(
        window.launch_fingerprint
    )

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []


def test_stable_target_saved_line_never_replaces_visible_different_line():
    raw = ScreenRecognition(
        ReconnectScreenState.LINE_SELECTION,
        0.0,
        (0.5, 0.327),
        "line-selection",
        line_number=1,
        recent_line_present=False,
        recent_login_role=None,
    )
    window = make_window(1)
    target = _stable_target_identity(window, line_number=8)
    saved_lines = []
    fixture = make_controller(
        [4],
        windows=[window],
        expected_windows=1,
        recognizer=RecognitionByMarker({4: raw}),
        target_identity_provider=lambda _fingerprint: target,
        verified_line_recorder=lambda fingerprint, character_id, line: (
            saved_lines.append((fingerprint, character_id, line)) or True
        ),
    )
    fixture.controller._pending_reconnect_fingerprints.add(
        window.launch_fingerprint
    )

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []
    assert saved_lines == []


def test_stable_target_saved_line_accepts_same_uniquely_visible_line():
    raw = ScreenRecognition(
        ReconnectScreenState.LINE_SELECTION,
        0.0,
        (0.5, 0.722),
        "line-selection",
        line_number=8,
        recent_line_present=True,
        recent_login_role=None,
    )
    window = make_window(1)
    target = _stable_target_identity(window, line_number=8)
    saved_lines = []
    fixture = make_controller(
        [4],
        windows=[window],
        expected_windows=1,
        recognizer=RecognitionByMarker({4: raw}),
        target_identity_provider=lambda _fingerprint: target,
        verified_line_recorder=lambda fingerprint, character_id, line: (
            saved_lines.append((fingerprint, character_id, line)) or True
        ),
    )
    fixture.controller._pending_reconnect_fingerprints.add(
        window.launch_fingerprint
    )

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [(1, (0.5, 0.722))]
    assert saved_lines == []


def test_stable_target_saved_line_without_visible_point_is_zero_input():
    raw = ScreenRecognition(
        ReconnectScreenState.LINE_SELECTION,
        0.0,
        None,
        "line-selection",
        line_number=8,
        recent_line_present=True,
        recent_login_role=None,
        line_scroll_delta=-120,
    )
    window = make_window(1)
    target = _stable_target_identity(window, line_number=8)
    fixture = make_controller(
        [4],
        windows=[window],
        expected_windows=1,
        recognizer=RecognitionByMarker({4: raw}),
        target_identity_provider=lambda _fingerprint: target,
    )
    fixture.controller._pending_reconnect_fingerprints.add(
        window.launch_fingerprint
    )

    fixture.controller.reconnect()
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []
    assert fixture.mouse.scrolls == []


def test_stable_target_evidence_uses_target_without_role_name_in_signature():
    selected = CharacterSelectionCandidate(
        160,
        None,
        2,
        True,
        CHARACTER_ENTER_CLICK_POINT,
        digit_count=3,
        identity="AlphaHero",
    )
    raw = _character_recognition((selected,))
    fixture, window = _single_window_character_fixture(
        _CharacterSequenceRecognizer(lambda _call: (selected,)),
        target_identity_provider=lambda _fingerprint: (
            _stable_target_identity(_fingerprint, line_number=8)
        ),
    )
    target = _stable_target_identity(window, line_number=8)
    instance = WindowInstanceToken.from_window(window)
    transformed = fixture.controller._recognition_for_session_action(
        window.launch_fingerprint,
        raw,
        target_identity=target,
        instance=instance,
        capture_route="primary",
        capture_settings_revision=0,
        source_state_generation=0,
    )
    transformed = fixture.controller._recognition_for_target_identity_scope(
        transformed,
        target,
    )
    line = fixture.controller._recognition_for_preferred_line(
        window.launch_fingerprint,
        ScreenRecognition(
            ReconnectScreenState.LINE_SELECTION,
            0.0,
            (0.5, 0.722),
            "line-selection",
            line_number=8,
            recent_line_present=True,
        ),
        target_identity=target,
    )
    line = fixture.controller._recognition_for_target_identity_scope(
        line,
        target,
    )

    assert fixture.controller._original_role_verified_for_evidence(
        window.launch_fingerprint,
        transformed,
    ) is True
    assert fixture.controller._original_line_verified_for_evidence(
        window.launch_fingerprint,
        line,
    ) is True
    assert "AlphaHero" not in repr(
        fixture.controller._action_signature(transformed)
    )
    assert "character-1" not in repr(
        fixture.controller._action_signature(transformed)
    )


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


def test_fifteen_duplicate_source_identities_are_rejected_as_one_batch():
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

    assert prepared.success is False
    assert prepared.code == "reconnect.snapshot_identity_unsafe"
    assert prepared.details["failure_codes"] == [
        "authorization_batch_unavailable"
    ]
    assert monitor_fingerprints is None

    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert "authorization_batch_unavailable" in result.details["failure_codes"]
    assert fixture.mouse.scrolls == []
    assert fixture.mouse.clicks == []

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

    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []
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
        authorization_target_identities={
            window.launch_fingerprint: _stable_target_identity(
                window,
                character_id="registered:120福",
                role_aliases=("120福",),
                importance=CharacterImportance.PRIMARY,
                slot_index=2,
                line_number=1,
            ),
        },
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

    if conflict == "multiple_primary":
        assert recovered.details["clicked_windows"] == 0
        assert fixture.mouse.clicks == []
        assert fixture.authorization.current_authorization() is None
    else:
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
        now[0] = 5.5
        rebound = fixture.controller.reconnect()
        assert rebound.details["clicked_windows"] == 0
        assert rebound.details["restarted_windows"] == 0
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
    shortcut = tmp_path / "only-a.lnk"
    shortcut.write_bytes(b"shortcut")
    authorization_target_identities = {
        windows[0].launch_fingerprint: _stable_target_identity(
            windows[0],
            character_id="character-a",
            role_aliases=("120福",),
            importance=CharacterImportance.PRIMARY,
            slot_index=2,
            line_number=8,
        ),
        windows[1].launch_fingerprint: _stable_target_identity(
            windows[1],
            character_id="character-b",
            role_aliases=("002-test-identity",),
            importance=CharacterImportance.SECONDARY,
            slot_index=0,
            line_number=1,
        ),
        windows[2].launch_fingerprint: _stable_target_identity(
            windows[2],
            character_id="character-c",
            role_aliases=("003-test-identity",),
            importance=CharacterImportance.SECONDARY,
            slot_index=1,
            line_number=1,
        ),
    }
    fixture = make_controller(
        [22 if battle else 21, 1, 1],
        windows=windows,
        expected_windows=3,
        clock=lambda: now[0],
        recognizer=RecognitionByMarker(_locked_reconnect_recognitions()),
        battle_restarter=restarter,
        registered_role_provider=_registered_roles,
        ungrouped_shortcut_provider=lambda fingerprint: (
            shortcut if fingerprint == "a" * 64 else None
        ),
        window_backend=backend,
        authorization_target_identities=authorization_target_identities,
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
    assert restarter.calls[0][1].fingerprint == "a" * 64
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
    ("source", "instance", "route", "revision", "shortcut"),
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
    else:
        fixture.controller.set_ungrouped_shortcut_provider(
            lambda _fingerprint: None
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


def _actual_grant(fixture, fingerprint):
    batch = fixture.authorization.current_authorization()
    assert batch is not None
    target = batch.target_for(fingerprint)
    assert target is not None
    assert target.authorization_id is not None
    return target


def test_actual_new_window_joins_without_resetting_sibling_grant_or_progress():
    first = make_window(401)
    added = make_window(402)
    identities = {
        first.launch_fingerprint: _stable_target_identity(
            first,
            character_id="actual-first",
            role_aliases=("ActualFirst",),
            slot_index=0,
        ),
        added.launch_fingerprint: _stable_target_identity(
            added,
            character_id="actual-added",
            role_aliases=("ActualAdded",),
            slot_index=1,
        ),
    }
    fixture, state, _identities = make_actual_controller(
        (first,),
        identities=identities,
        screen_states={first.handle: 2, added.handle: 1},
    )
    first_grant = _actual_grant(fixture, first.launch_fingerprint)
    fixture.controller._pending_reconnect_fingerprints.add(
        first.launch_fingerprint
    )

    state["windows"] = [first, added]
    fixture.controller.reconnect()

    assert _actual_grant(
        fixture,
        first.launch_fingerprint,
    ).authorization_id == first_grant.authorization_id
    assert _actual_grant(fixture, added.launch_fingerprint) is not None
    assert first.launch_fingerprint in (
        fixture.controller._pending_reconnect_fingerprints
    )


def test_actual_empty_authorization_monitors_and_adds_first_safe_window():
    added = make_window(411)
    identities = {
        added.launch_fingerprint: _stable_target_identity(
            added,
            character_id="actual-first-later",
            role_aliases=("ActualFirstLater",),
            slot_index=0,
        )
    }
    fixture, state, _identities = make_actual_controller(
        (),
        identities=identities,
        screen_states={added.handle: 1},
    )
    initial = fixture.authorization.current_authorization()
    assert initial is not None
    assert initial.targets == ()

    state["windows"] = [added]
    fixture.controller.reconnect()

    assert _actual_grant(fixture, added.launch_fingerprint) is not None


def test_final_action_gate_adds_new_safe_window_and_keeps_current_grant():
    current_window = make_window(412)
    added = make_window(413)
    identities = {
        current_window.launch_fingerprint: _stable_target_identity(
            current_window,
            character_id="final-current",
            role_aliases=("FinalCurrent",),
            slot_index=0,
        ),
        added.launch_fingerprint: _stable_target_identity(
            added,
            character_id="final-added",
            role_aliases=("FinalAdded",),
            slot_index=1,
        ),
    }
    fixture, state, _identities = make_actual_controller(
        (current_window,),
        identities=identities,
        screen_states={current_window.handle: 3, added.handle: 1},
    )
    current_grant = _actual_grant(
        fixture,
        current_window.launch_fingerprint,
    )
    instance = WindowInstanceToken.from_window(current_window)
    assert instance is not None
    state["windows"] = [current_window, added]
    candidates, failures, blocked = (
        fixture.controller._candidate_window_set()
    )

    resolved = fixture.controller._current_actual_action_window(
        candidates,
        failures,
        blocked,
        instance,
        current_window.launch_fingerprint,
    )

    assert resolved == current_window
    assert _actual_grant(
        fixture,
        current_window.launch_fingerprint,
    ).authorization_id == current_grant.authorization_id
    assert _actual_grant(fixture, added.launch_fingerprint) is not None


def test_actual_read_only_instance_change_isolates_only_changed_target():
    changed = make_window(421)
    sibling = make_window(422)
    now = [0.0]
    replacement = make_window(
        423,
        fingerprint=changed.launch_fingerprint,
    )
    fixture, state, _identities = make_actual_controller(
        (changed, sibling),
        screen_states={
            changed.handle: 1,
            sibling.handle: 1,
            replacement.handle: 1,
        },
        clock=lambda: now[0],
    )
    changed_grant = _actual_grant(fixture, changed.launch_fingerprint)
    sibling_grant = _actual_grant(fixture, sibling.launch_fingerprint)

    state["windows"] = [replacement, sibling]
    fixture.controller.check_connection()

    current = fixture.authorization.current_authorization()
    assert current is not None
    assert current.target_for(changed.launch_fingerprint) is None
    assert current.target_for(sibling.launch_fingerprint) == sibling_grant

    now[0] = 120.0
    fixture.controller.reconnect()

    replacement_grant = _actual_grant(
        fixture,
        changed.launch_fingerprint,
    )
    assert replacement_grant.instance.handle == replacement.handle
    assert replacement_grant.authorization_id != changed_grant.authorization_id
    assert replacement_grant.source_generation > changed_grant.source_generation
    assert _actual_grant(fixture, sibling.launch_fingerprint) == sibling_grant


def test_second_actual_rebind_rejects_action_without_revoking_sibling():
    original = make_window(424)
    sibling = make_window(425)
    first_replacement = make_window(
        426,
        fingerprint=original.launch_fingerprint,
    )
    second_replacement = make_window(
        427,
        fingerprint=original.launch_fingerprint,
    )
    fixture, state, _identities = make_actual_controller(
        (original, sibling),
        screen_states={
            original.handle: 3,
            sibling.handle: 1,
            first_replacement.handle: 3,
            second_replacement.handle: 3,
        },
    )
    sibling_grant = _actual_grant(fixture, sibling.launch_fingerprint)
    expected_instance = WindowInstanceToken.from_window(original)
    assert expected_instance is not None
    state["windows"] = [first_replacement, sibling]

    def second_changed_snapshot():
        state["windows"] = [second_replacement, sibling]
        return ResolvedTargetWindows(
            (second_replacement, sibling),
            (),
            frozenset(),
            0,
            0,
            True,
        )

    fixture.controller._target_windows_provider = second_changed_snapshot

    current = fixture.controller._current_actual_action_window(
        (first_replacement, sibling),
        (),
        frozenset(),
        expected_instance,
        original.launch_fingerprint,
    )

    assert current is None
    first_rebound_grant = _actual_grant(
        fixture,
        original.launch_fingerprint,
    )
    assert first_rebound_grant.instance == WindowInstanceToken.from_window(
        first_replacement
    )
    assert _actual_grant(
        fixture,
        sibling.launch_fingerprint,
    ).authorization_id == sibling_grant.authorization_id


def test_complete_100_characters_each_send_input_only_to_their_own_window():
    ancient = make_window(431)
    spirit = make_window(432)
    identities = {
        ancient.launch_fingerprint: _stable_target_identity(
            ancient,
            character_id="character-100-ancient",
            role_aliases=("100古",),
            slot_index=0,
        ),
        spirit.launch_fingerprint: _stable_target_identity(
            spirit,
            character_id="character-100-spirit",
            role_aliases=("100靈",),
            slot_index=1,
        ),
    }
    ancient_candidate = CharacterSelectionCandidate(
        100,
        CharacterImportance.SECONDARY,
        0,
        True,
        CHARACTER_ENTER_CLICK_POINT,
        digit_count=3,
        identity="100古",
    )
    spirit_candidate = CharacterSelectionCandidate(
        100,
        CharacterImportance.SECONDARY,
        1,
        True,
        CHARACTER_ENTER_CLICK_POINT,
        digit_count=3,
        identity="100靈",
    )
    saved_slots = []
    fixture, _state, _identities = make_actual_controller(
        (ancient, spirit),
        identities=identities,
        screen_states={ancient.handle: 10, spirit.handle: 11},
        recognizer=RecognitionByMarker(
            {
                10: _character_recognition((ancient_candidate,)),
                11: _character_recognition((spirit_candidate,)),
            }
        ),
        verified_slot_recorder=lambda fingerprint, character_id, slot: (
            saved_slots.append((fingerprint, character_id, slot)) or True
        ),
    )

    fixture.controller.reconnect()
    fixture.controller.reconnect()

    assert fixture.mouse.clicks == [
        (ancient.handle, CHARACTER_ENTER_CLICK_POINT),
        (spirit.handle, CHARACTER_ENTER_CLICK_POINT),
    ]
    assert saved_slots == []


@pytest.mark.parametrize("observed_identity", ("100", "100…"))
def test_fuzzy_100_is_zero_input_even_when_conflicting_role_is_not_open(
    observed_identity,
):
    ancient = make_window(441)
    unopened_spirit = make_window(442)
    identities = {
        ancient.launch_fingerprint: _stable_target_identity(
            ancient,
            character_id="character-100-ancient",
            role_aliases=("100古",),
            slot_index=0,
        ),
        unopened_spirit.launch_fingerprint: _stable_target_identity(
            unopened_spirit,
            character_id="character-100-spirit",
            role_aliases=("100靈",),
            slot_index=1,
        ),
    }
    candidate = CharacterSelectionCandidate(
        100,
        CharacterImportance.SECONDARY,
        0,
        True,
        CHARACTER_ENTER_CLICK_POINT,
        digit_count=3,
        identity=observed_identity,
    )
    fixture, _state, _identities = make_actual_controller(
        (ancient,),
        identities=identities,
        screen_states={ancient.handle: 12},
        recognizer=RecognitionByMarker(
            {12: _character_recognition((candidate,))}
        ),
    )

    fixture.controller.reconnect()
    fixture.controller.reconnect()

    assert fixture.mouse.clicks == []


def test_shared_complete_alias_is_zero_input_when_other_owner_is_not_open():
    opened = make_window(451)
    unopened = make_window(452)
    identities = {
        opened.launch_fingerprint: _stable_target_identity(
            opened,
            character_id="shared-owner-one",
            role_aliases=("SharedCompleteAlias",),
            slot_index=0,
        ),
        unopened.launch_fingerprint: _stable_target_identity(
            unopened,
            character_id="shared-owner-two",
            role_aliases=("SharedCompleteAlias",),
            slot_index=1,
        ),
    }
    candidate = CharacterSelectionCandidate(
        100,
        CharacterImportance.SECONDARY,
        0,
        True,
        CHARACTER_ENTER_CLICK_POINT,
        digit_count=3,
        identity="SharedCompleteAlias",
    )
    fixture, _state, _identities = make_actual_controller(
        (opened,),
        identities=identities,
        screen_states={opened.handle: 13},
        recognizer=RecognitionByMarker(
            {13: _character_recognition((candidate,))}
        ),
    )

    fixture.controller.reconnect()
    fixture.controller.reconnect()

    assert fixture.mouse.clicks == []


def test_level_100_without_visible_identity_blocks_only_that_window():
    ambiguous = make_window(453)
    sibling = make_window(454)
    identities = {
        ambiguous.launch_fingerprint: _stable_target_identity(
            ambiguous,
            character_id="level-only-character",
            role_aliases=("100古",),
            slot_index=0,
        ),
        sibling.launch_fingerprint: _stable_target_identity(
            sibling,
            character_id="level-safe-sibling",
            role_aliases=("SafeSibling",),
            slot_index=1,
        ),
    }
    level_only = CharacterSelectionCandidate(
        100,
        CharacterImportance.SECONDARY,
        0,
        True,
        CHARACTER_ENTER_CLICK_POINT,
        digit_count=3,
        identity=None,
    )
    fixture, _state, _identities = make_actual_controller(
        (ambiguous, sibling),
        identities=identities,
        screen_states={ambiguous.handle: 15, sibling.handle: 3},
        recognizer=RecognitionByMarker(
            {
                15: _character_recognition((level_only,)),
                3: ScreenRecognition(
                    ReconnectScreenState.LOGIN_START,
                    0.0,
                    (0.5, 0.8),
                    "login-start",
                ),
            }
        ),
    )

    fixture.controller.reconnect()
    fixture.controller.reconnect()

    assert [handle for handle, _point in fixture.mouse.clicks] == [
        sibling.handle
    ]


@pytest.mark.parametrize("catalog_mode", ("failure", "empty"))
def test_identity_alias_catalog_failure_or_empty_is_zero_input(catalog_mode):
    window = make_window(461)
    identity = _stable_target_identity(
        window,
        character_id="catalog-failure-character",
        role_aliases=("CatalogRole",),
        slot_index=0,
    )
    candidate = CharacterSelectionCandidate(
        100,
        CharacterImportance.SECONDARY,
        0,
        True,
        CHARACTER_ENTER_CLICK_POINT,
        digit_count=3,
        identity="CatalogRole",
    )

    def failed_catalog():
        if catalog_mode == "failure":
            raise RuntimeError("catalog unavailable")
        return ()

    fixture, _state, _identities = make_actual_controller(
        (window,),
        identities={window.launch_fingerprint: identity},
        screen_states={window.handle: 14},
        recognizer=RecognitionByMarker(
            {14: _character_recognition((candidate,))}
        ),
        identity_alias_catalog_provider=failed_catalog,
    )

    fixture.controller.reconnect()
    fixture.controller.reconnect()

    assert fixture.mouse.clicks == []


def test_changed_shortcut_seal_isolates_only_target_and_sibling_still_inputs():
    changed = make_window(471)
    sibling = make_window(472)
    fixture, _state, _identities = make_actual_controller(
        (changed, sibling),
        screen_states={changed.handle: 3, sibling.handle: 3},
    )
    sibling_grant = _actual_grant(fixture, sibling.launch_fingerprint)
    fixture.shortcut_seals.changed_fingerprints.add(
        changed.launch_fingerprint
    )
    old_seal = fixture.preparation._seals[changed.launch_fingerprint]
    fixture.preparation._seals[changed.launch_fingerprint] = ShortcutSeal(
        ShortcutFileIdentity(
            old_seal.file_identity.normalized_path,
            old_seal.file_identity.volume_serial_number,
            old_seal.file_identity.file_index + 1000,
        ),
        hashlib.sha256(b"changed-shortcut-content").hexdigest(),
        changed.launch_fingerprint,
    )

    fixture.controller.reconnect()
    fixture.controller.reconnect()

    current = fixture.authorization.current_authorization()
    assert current is not None
    assert current.target_for(changed.launch_fingerprint) is None
    assert current.target_for(sibling.launch_fingerprint).authorization_id == (
        sibling_grant.authorization_id
    )
    assert [handle for handle, _point in fixture.mouse.clicks] == [
        sibling.handle
    ]


def test_active_minimized_capture_is_not_called_after_shortcut_seal_changes():
    changed = make_window(481, minimized=True)
    sibling = make_window(482)
    active_capture = FakeCaptureProvider({changed.handle: 1})
    fixture, _state, _identities = make_actual_controller(
        (changed, sibling),
        screen_states={changed.handle: 1, sibling.handle: 1},
        active_refresh_capture_provider=active_capture,
    )
    sibling_grant = _actual_grant(fixture, sibling.launch_fingerprint)
    fixture.shortcut_seals.changed_fingerprints.add(
        changed.launch_fingerprint
    )

    fixture.controller.reconnect()

    assert active_capture.calls == []
    assert _actual_grant(
        fixture,
        sibling.launch_fingerprint,
    ).authorization_id == sibling_grant.authorization_id


def test_active_minimized_capture_is_not_called_after_identity_generation_changes():
    minimized = make_window(491, minimized=True)
    sibling = make_window(492)
    active_capture = FakeCaptureProvider({minimized.handle: 1})
    runner_state = {"armed": False, "calls": 0}

    def identity_generation_runner(callback):
        runner_state["calls"] += 1
        generation = (
            2
            if runner_state["armed"] and runner_state["calls"] > 1
            else 1
        )
        return callback(generation)

    fixture, _state, _identities = make_actual_controller(
        (minimized, sibling),
        screen_states={minimized.handle: 1, sibling.handle: 1},
        active_refresh_capture_provider=active_capture,
        identity_generation_runner=identity_generation_runner,
    )
    sibling_grant = _actual_grant(fixture, sibling.launch_fingerprint)
    runner_state.update(armed=True, calls=0)

    fixture.controller.reconnect()

    assert active_capture.calls == []
    assert _actual_grant(
        fixture,
        sibling.launch_fingerprint,
    ).authorization_id == sibling_grant.authorization_id


def test_anonymous_isolation_disappears_without_changing_sibling_grant():
    sibling = make_window(501)
    fixture, state, _identities = make_actual_controller(
        (sibling,),
        isolated_window_count=1,
        anonymous_isolated_window_count=1,
    )
    sibling_grant = _actual_grant(fixture, sibling.launch_fingerprint)
    assert fixture.controller._actual_isolated_window_count == 1
    assert fixture.controller._anonymous_isolated_window_count == 1

    state["isolated_window_count"] = 0
    state["anonymous_isolated_window_count"] = 0
    fixture.controller.check_connection()

    assert fixture.controller._actual_isolated_window_count == 0
    assert fixture.controller._anonymous_isolated_window_count == 0
    assert _actual_grant(
        fixture,
        sibling.launch_fingerprint,
    ).authorization_id == sibling_grant.authorization_id


def test_passive_actual_instance_change_keeps_sibling_confirmation_and_grant():
    changed = make_window(511)
    sibling = make_window(512)
    replacement = make_window(
        513,
        fingerprint=changed.launch_fingerprint,
    )
    fixture, state, _identities = make_actual_controller(
        (changed, sibling),
        screen_states={
            changed.handle: 1,
            sibling.handle: 3,
            replacement.handle: 1,
        },
    )
    sibling_grant = _actual_grant(fixture, sibling.launch_fingerprint)
    fixture.controller.reconnect()
    assert sibling.launch_fingerprint in fixture.controller._action_confirmations

    state["windows"] = [replacement, sibling]
    observed = fixture.controller.observe_screen_states(
        (changed.launch_fingerprint, sibling.launch_fingerprint)
    )

    assert observed == {
        changed.launch_fingerprint: ReconnectScreenState.UNKNOWN,
        sibling.launch_fingerprint: ReconnectScreenState.LOGIN_START,
    }
    assert sibling.launch_fingerprint in fixture.controller._action_confirmations
    assert _actual_grant(
        fixture,
        sibling.launch_fingerprint,
    ).authorization_id == sibling_grant.authorization_id

    fixture.controller.reconnect()
    assert [handle for handle, _point in fixture.mouse.clicks] == [
        sibling.handle
    ]


def test_actual_pending_reopen_survives_unrelated_new_window_and_sibling_inputs():
    missing = make_window(521)
    sibling = make_window(522)
    added = make_window(523)
    identities = {
        missing.launch_fingerprint: _stable_target_identity(
            missing,
            character_id="pending-ungrouped",
            role_aliases=("PendingUngrouped",),
            slot_index=0,
        ),
        sibling.launch_fingerprint: _stable_target_identity(
            sibling,
            character_id="pending-sibling",
            role_aliases=("PendingSibling",),
            slot_index=1,
        ),
        added.launch_fingerprint: _stable_target_identity(
            added,
            character_id="pending-added",
            role_aliases=("PendingAdded",),
            slot_index=2,
        ),
    }
    restarter = FakeClosedBattleRestarter(succeeds=False)
    fixture, state, _identities = make_actual_controller(
        (missing, sibling),
        identities=identities,
        screen_states={
            missing.handle: 2,
            sibling.handle: 1,
            added.handle: 1,
        },
        recognizer=FakeRecognizer(
            {
                1: ReconnectScreenState.CONNECTED,
                2: ReconnectScreenState.DISCONNECTED,
                3: ReconnectScreenState.LOGIN_START,
            },
            {2: (0.5, 0.5), 3: (0.5, 0.8)},
            battle_markers={2},
        ),
        battle_restarter=restarter,
    )
    missing_grant = _actual_grant(fixture, missing.launch_fingerprint)
    sibling_grant = _actual_grant(fixture, sibling.launch_fingerprint)

    fixture.controller.reconnect()
    fixture.controller.reconnect()
    assert missing.launch_fingerprint in (
        fixture.controller._pending_reopen_fingerprints
    )
    assert missing.launch_fingerprint in (
        fixture.controller._pending_reopen_authorizations
    )

    state["windows"] = [sibling, added]
    fixture.controller.reconnect()

    assert _actual_grant(
        fixture,
        missing.launch_fingerprint,
    ).authorization_id == missing_grant.authorization_id
    assert _actual_grant(
        fixture,
        sibling.launch_fingerprint,
    ).authorization_id == sibling_grant.authorization_id
    assert _actual_grant(fixture, added.launch_fingerprint) is not None
    assert missing.launch_fingerprint in (
        fixture.controller._pending_reopen_fingerprints
    )
    assert missing.launch_fingerprint in (
        fixture.controller._pending_reopen_authorizations
    )

    fixture.capture.states[sibling.handle] = 3
    fixture.controller._pending_reconnect_fingerprints.add(
        sibling.launch_fingerprint
    )
    fixture.controller.reconnect()
    fixture.controller.reconnect()

    assert [handle for handle, _point in fixture.mouse.clicks] == [
        sibling.handle
    ]
    assert missing.launch_fingerprint in (
        fixture.controller._pending_reopen_fingerprints
    )


def test_first_actual_character_slot_is_learned_only_after_exact_selection():
    window = make_window(531)
    identity = _stable_target_identity(
        window,
        character_id="first-slot-character",
        role_aliases=("FirstSlotRole",),
        slot_index=None,
        line_number=1,
    )
    unselected = CharacterSelectionCandidate(
        100,
        CharacterImportance.SECONDARY,
        2,
        False,
        (0.651, 0.706),
        digit_count=3,
        identity="FirstSlotRole",
    )
    selected = replace(
        unselected,
        selected=True,
        click_point=CHARACTER_ENTER_CLICK_POINT,
    )
    saved_slots = []
    fixture, _state, _identities = make_actual_controller(
        (window,),
        identities={window.launch_fingerprint: identity},
        screen_states={window.handle: 20},
        recognizer=RecognitionByMarker(
            {
                20: _character_recognition((unselected,)),
                21: _character_recognition((selected,)),
            }
        ),
        verified_slot_recorder=lambda fingerprint, character_id, slot: (
            saved_slots.append((fingerprint, character_id, slot)) or True
        ),
    )

    fixture.controller.reconnect()
    fixture.controller.reconnect()
    fixture.capture.states[window.handle] = 21
    fixture.controller.reconnect()
    fixture.controller.reconnect()

    assert fixture.mouse.clicks == [
        (window.handle, unselected.click_point),
        (window.handle, CHARACTER_ENTER_CLICK_POINT),
    ]
    assert saved_slots == [
        (window.launch_fingerprint, identity.character_id, 2)
    ]


def test_first_actual_line_is_learned_only_from_complete_visible_line():
    window = make_window(541)
    identity = _stable_target_identity(
        window,
        character_id="first-line-character",
        role_aliases=("FirstLineRole",),
        slot_index=0,
        line_number=None,
    )
    recognition = ScreenRecognition(
        ReconnectScreenState.LINE_SELECTION,
        0.0,
        (0.5, 0.5),
        "line-selection",
        line_number=3,
        recent_line_present=True,
        line_scroll_delta=0,
    )
    saved_lines = []
    fixture, _state, _identities = make_actual_controller(
        (window,),
        identities={window.launch_fingerprint: identity},
        screen_states={window.handle: 22},
        recognizer=RecognitionByMarker({22: recognition}),
        verified_line_recorder=lambda fingerprint, character_id, line: (
            saved_lines.append((fingerprint, character_id, line)) or True
        ),
    )

    fixture.controller.reconnect()
    fixture.controller.reconnect()

    assert fixture.mouse.clicks == [(window.handle, (0.5, 0.5))]
    assert saved_lines == [
        (window.launch_fingerprint, identity.character_id, 3)
    ]


def test_stop_waits_for_active_scan_and_performs_final_authorization_revoke(
    monkeypatch,
):
    window = make_window(551)
    fixture, _state, _identities = make_actual_controller((window,))
    fixture.controller.check_connection()
    baseline = fixture.controller.last_result
    assert baseline is not None
    scan_entered = threading.Event()
    release_scan = threading.Event()

    def publish_after_stop_started(*, execute):
        assert execute is True
        scan_entered.set()
        assert release_scan.wait(2)
        batch = fixture.preparation.prepare(
            launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
        )
        fixture.controller._bind_authorization_batch_locked(batch)
        return baseline

    monkeypatch.setattr(
        fixture.controller,
        "_scan_locked",
        publish_after_stop_started,
    )
    scan = threading.Thread(target=fixture.controller.reconnect)
    stopped = threading.Event()
    stopper = threading.Thread(
        target=lambda: (
            fixture.controller.set_execution_enabled(False),
            stopped.set(),
        )
    )

    scan.start()
    assert scan_entered.wait(1)
    stopper.start()
    assert stopped.wait(0.05) is False
    release_scan.set()
    scan.join(2)
    stopper.join(2)

    assert scan.is_alive() is False
    assert stopper.is_alive() is False
    assert stopped.is_set()
    assert fixture.authorization.current_authorization() is None
