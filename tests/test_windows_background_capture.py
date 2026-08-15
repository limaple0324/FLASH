import ctypes
from pathlib import Path

import pytest

from adapters.windows_background_capture import (
    CaptureSample,
    Win32PrintWindowProvider,
    Win32RecoveringPrintWindowProvider,
    Win32TemporarilyRevealedCaptureProvider,
    Win32VisibleRegionCaptureProvider,
    WindowsGraphicsCaptureProvider,
    WindowsBackgroundCaptureBackend,
    _query_window_instance_credential,
)


class FakeProvider:
    def __init__(self, sample):
        self.sample = sample
        self.handles = []

    def capture(self, window_handle):
        self.handles.append(window_handle)
        return self.sample


class CallbackProvider:
    def __init__(self, callback):
        self.callback = callback
        self.handles = []

    def capture(self, window_handle):
        self.handles.append(window_handle)
        return self.callback(window_handle)


def sample(pixels, *, width=2, height=2, api_succeeded=True):
    return CaptureSample(
        width=width,
        height=height,
        pixels=bytes(pixels),
        api_succeeded=api_succeeded,
    )


def test_capture_probe_is_unknown_when_provider_cannot_capture():
    provider = FakeProvider(None)
    backend = WindowsBackgroundCaptureBackend(provider=provider)

    assert backend.probe_background_capture(123) is None
    assert provider.handles == [123]
    assert backend.last_sample is None


def test_capture_probe_rejects_failed_printwindow_call():
    provider = FakeProvider(sample([0, 20, 80, 255] * 4, api_succeeded=False))
    backend = WindowsBackgroundCaptureBackend(provider=provider)

    assert backend.probe_background_capture(123) is False


def test_capture_probe_rejects_blank_frame():
    provider = FakeProvider(sample([0, 0, 0, 255] * 4))
    backend = WindowsBackgroundCaptureBackend(provider=provider)

    assert backend.probe_background_capture(123) is False


def test_capture_probe_accepts_non_blank_frame():
    provider = FakeProvider(
        sample(
            [
                0, 0, 0, 255,
                30, 80, 120, 255,
                200, 100, 20, 255,
                255, 255, 255, 255,
            ]
        )
    )
    backend = WindowsBackgroundCaptureBackend(provider=provider)

    assert backend.probe_background_capture(321) is True
    assert backend.last_sample is provider.sample


def test_input_capabilities_remain_unknown_without_user_approved_probe():
    backend = WindowsBackgroundCaptureBackend(provider=FakeProvider(None))

    assert backend.probe_background_input(1) is None
    assert backend.probe_minimized_input(1) is None


def test_visible_capture_guards_character_and_right_gameplay_regions():
    points = Win32VisibleRegionCaptureProvider.REQUIRED_VISIBLE_POINTS

    assert len(points) >= 20
    assert any(x >= 0.95 and 0.3 <= y <= 0.75 for x, y in points)
    for required_x in (0.355, 0.50, 0.651):
        assert any(
            abs(x - required_x) < 0.001 and 0.70 <= y <= 0.87
            for x, y in points
        )


class FakeWindowStateApi:
    def __init__(
        self,
        *,
        minimized,
        restore_succeeds=True,
        minimize_succeeds=True,
        foreground_after_restore=None,
        process_after_restore=None,
        position_restore_succeeds=True,
    ):
        self.target = 123
        self.minimized = minimized
        self.restore_succeeds = restore_succeeds
        self.minimize_succeeds = minimize_succeeds
        self.foreground = 700
        self.foreground_after_restore = foreground_after_restore
        self.process_after_restore = process_after_restore
        self.position_restore_succeeds = position_restore_succeeds
        self.z_order = [700, 300, self.target, 400]
        self.process_ids = {
            700: 70,
            300: 30,
            self.target: 12,
            400: 40,
            888: 88,
        }
        self.window_classes = {
            handle: f"WindowClass{handle}"
            for handle in self.process_ids
        }
        self.rects = {
            handle: (10, 20, 926, 649)
            for handle in self.process_ids
        }
        self.normal_rect = (10, 20, 926, 649)
        self.visible = {handle: True for handle in self.process_ids}
        self.topmost = set()
        self.show_commands = []
        self.position_calls = []
        self.foreground_calls = []
        self.capture_finished = False
        self.arm_reuse_after_capture_foreground = False
        self.reuse_after_next_pid_query = False
        self.IsWindow = FakeCallable(
            lambda handle: handle_value(handle) in self.process_ids
        )
        self.IsWindowVisible = FakeCallable(
            lambda handle: self.visible.get(handle_value(handle), False)
        )
        self.IsIconic = FakeCallable(
            lambda handle: (
                self.minimized
                if handle_value(handle) == self.target
                else False
            )
        )
        self.GetWindowRect = FakeCallable(self._get_window_rect)
        self.GetWindow = FakeCallable(self._get_window)
        self.GetWindowLongW = FakeCallable(
            lambda handle, _index: (
                Win32TemporarilyRevealedCaptureProvider.WS_EX_TOPMOST
                if handle_value(handle) in self.topmost
                else 0
            )
        )
        self.GetForegroundWindow = FakeCallable(self._get_foreground_window)
        self.GetWindowThreadProcessId = FakeCallable(
            self._get_window_thread_process_id
        )
        self.GetClassNameW = FakeCallable(self._get_class_name)
        self.GetWindowPlacement = FakeCallable(self._get_window_placement)
        self.SetWindowPos = FakeCallable(self._set_window_pos)
        self.SetForegroundWindow = FakeCallable(self._set_foreground_window)
        self.ShowWindow = FakeCallable(self._show_window)

    def _show_window(self, handle, command):
        value = handle_value(handle)
        self.show_commands.append(command)
        if command == Win32RecoveringPrintWindowProvider.SW_SHOWNOACTIVATE:
            if self.restore_succeeds:
                self.minimized = False
                self.z_order.remove(value)
                self.z_order.insert(0, value)
                if self.foreground_after_restore is not None:
                    self.foreground = self.foreground_after_restore
                if self.process_after_restore is not None:
                    self.process_ids[value] = self.process_after_restore
        elif command == Win32RecoveringPrintWindowProvider.SW_SHOWMINNOACTIVE:
            if self.minimize_succeeds:
                self.minimized = True
                self.z_order.remove(value)
                self.z_order.append(value)
        return True

    def _get_window_rect(self, handle, pointer):
        rect = self.rects.get(handle_value(handle))
        if rect is None:
            return False
        target = pointer._obj
        target.left, target.top, target.right, target.bottom = rect
        return True

    def _get_window(self, handle, command):
        value = handle_value(handle)
        if value not in self.z_order:
            return 0
        index = self.z_order.index(value)
        if (
            command
            == Win32TemporarilyRevealedCaptureProvider.GW_HWNDPREV
        ):
            return self.z_order[index - 1] if index > 0 else 0
        if command == Win32TemporarilyRevealedCaptureProvider.GW_HWNDNEXT:
            return (
                self.z_order[index + 1]
                if index + 1 < len(self.z_order)
                else 0
            )
        return 0

    def _get_window_thread_process_id(self, handle, process_pointer):
        value = handle_value(handle)
        process_id = self.process_ids.get(value, 0)
        process_pointer._obj.value = process_id
        if (
            value == self.target
            and self.reuse_after_next_pid_query
        ):
            self.process_ids[value] = 99
            self.reuse_after_next_pid_query = False
        return 1 if process_id else 0

    def _get_class_name(self, handle, buffer, maximum):
        name = self.window_classes.get(handle_value(handle))
        if not name:
            return 0
        buffer.value = name[: max(0, maximum - 1)]
        return len(buffer.value)

    def _get_foreground_window(self):
        if (
            self.capture_finished
            and self.arm_reuse_after_capture_foreground
        ):
            self.reuse_after_next_pid_query = True
            self.arm_reuse_after_capture_foreground = False
        return self.foreground

    def _get_window_placement(self, handle, pointer):
        if handle_value(handle) not in self.process_ids:
            return False
        placement = pointer._obj
        placement.flags = 0
        placement.showCmd = 2 if self.minimized else 1
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

    def _set_window_pos(
        self,
        handle,
        insert_after,
        _x,
        _y,
        _width,
        _height,
        _flags,
    ):
        value = handle_value(handle)
        anchor = handle_value(insert_after)
        self.position_calls.append((value, anchor))
        if not self.position_restore_succeeds:
            return False
        if value not in self.z_order:
            return False
        self.z_order.remove(value)
        if anchor in (
            Win32TemporarilyRevealedCaptureProvider.HWND_TOP,
            Win32TemporarilyRevealedCaptureProvider.HWND_TOPMOST,
        ):
            self.z_order.insert(0, value)
        elif anchor in self.z_order:
            self.z_order.insert(self.z_order.index(anchor) + 1, value)
        else:
            return False
        return True

    def _set_foreground_window(self, handle):
        value = handle_value(handle)
        self.foreground_calls.append(value)
        if value not in self.process_ids:
            return False
        self.foreground = value
        return True


class FakeCallable:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


def install_minimized_neighbor_reorder(user32, preserved_relation):
    original_set_window_pos = user32.SetWindowPos.callback

    def restore_then_reorder(
        handle,
        insert_after,
        x,
        y,
        width,
        height,
        flags,
    ):
        restored = original_set_window_pos(
            handle,
            insert_after,
            x,
            y,
            width,
            height,
            flags,
        )
        if not restored or handle_value(insert_after) != 300:
            return restored
        target_index = user32.z_order.index(user32.target)
        user32.z_order.remove(700)
        target_index = user32.z_order.index(user32.target)
        if preserved_relation == "previous":
            user32.z_order.insert(target_index + 1, 700)
        elif preserved_relation == "next":
            user32.z_order.insert(target_index, 700)
        elif preserved_relation == "none":
            user32.z_order.insert(target_index, 700)
            target_index = user32.z_order.index(user32.target)
            user32.z_order.insert(target_index + 1, 888)
        else:
            raise AssertionError("unsupported relation")
        return restored

    user32.SetWindowPos = FakeCallable(restore_then_reorder)


class FakeVisibleRegionUser32:
    def __init__(self):
        self.target = 123
        self.process_ids = {self.target: 12}
        self.window_classes = {self.target: "FlashVisibleWindow"}
        self.occluded = False
        self.point_checks = 0
        self.IsWindow = FakeCallable(lambda handle: handle_value(handle) == 123)
        self.IsWindowVisible = FakeCallable(
            lambda handle: handle_value(handle) == 123
        )
        self.IsIconic = FakeCallable(lambda _handle: False)
        self.GetWindowRect = FakeCallable(self._get_window_rect)
        self.GetWindowPlacement = FakeCallable(lambda _handle, _pointer: True)
        self.GetWindowDC = FakeCallable(lambda _handle: 1)
        self.PrintWindow = FakeCallable(lambda _handle, _dc, _flags: True)
        self.ReleaseDC = FakeCallable(lambda _handle, _dc: 1)
        self.GetDC = FakeCallable(lambda _handle: 1)
        self.WindowFromPoint = FakeCallable(self._window_from_point)
        self.GetAncestor = FakeCallable(
            lambda handle, _mode: handle_value(handle)
        )
        self.GetWindowThreadProcessId = FakeCallable(
            self._get_window_thread_process_id
        )
        self.GetClassNameW = FakeCallable(self._get_class_name)

    @staticmethod
    def _get_window_rect(_handle, pointer):
        rect = pointer._obj
        rect.left, rect.top, rect.right, rect.bottom = (10, 20, 110, 60)
        return True

    def _window_from_point(self, _point):
        self.point_checks += 1
        return 999 if self.occluded else self.target

    def _get_window_thread_process_id(self, handle, process_pointer):
        process_id = self.process_ids.get(handle_value(handle), 0)
        process_pointer._obj.value = process_id
        return 1 if process_id else 0

    def _get_class_name(self, handle, buffer, maximum):
        name = self.window_classes.get(handle_value(handle))
        if not name:
            return 0
        buffer.value = name[: max(0, maximum - 1)]
        return len(buffer.value)


class FakeVisibleRegionGdi32:
    def __init__(
        self,
        user32,
        *,
        occlude_after_copy,
        replace_process_after_copy=None,
    ):
        self.user32 = user32
        self.occlude_after_copy = occlude_after_copy
        self.replace_process_after_copy = replace_process_after_copy
        self.bitblt_calls = 0
        self.CreateCompatibleDC = FakeCallable(lambda _dc: 2)
        self.CreateCompatibleBitmap = FakeCallable(
            lambda _dc, _width, _height: 3
        )
        self.SelectObject = FakeCallable(lambda _dc, _object: 4)
        self.BitBlt = FakeCallable(self._bitblt)
        self.GetDIBits = FakeCallable(
            lambda _dc, _bitmap, _start, height, _buffer, _info, _mode: height
        )
        self.DeleteObject = FakeCallable(lambda _object: True)
        self.DeleteDC = FakeCallable(lambda _dc: True)

    def _bitblt(self, *_args):
        self.bitblt_calls += 1
        if self.occlude_after_copy:
            self.user32.occluded = True
        if self.replace_process_after_copy is not None:
            self.user32.process_ids[self.user32.target] = (
                self.replace_process_after_copy
            )
        return True


def test_visible_capture_rechecks_all_regions_after_parallel_occlusion(
    monkeypatch,
):
    user32 = FakeVisibleRegionUser32()
    gdi32 = FakeVisibleRegionGdi32(
        user32,
        occlude_after_copy=True,
    )
    provider = Win32VisibleRegionCaptureProvider(
        process_lifecycle_provider=fake_process_lifecycle_token,
    )
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, gdi32))

    assert provider.capture(user32.target) is None

    assert gdi32.bitblt_calls == 1
    assert user32.point_checks == len(provider.REQUIRED_VISIBLE_POINTS) + 1


def test_visible_capture_accepts_only_after_both_visibility_checks(
    monkeypatch,
):
    user32 = FakeVisibleRegionUser32()
    gdi32 = FakeVisibleRegionGdi32(
        user32,
        occlude_after_copy=False,
    )
    provider = Win32VisibleRegionCaptureProvider(
        process_lifecycle_provider=fake_process_lifecycle_token,
    )
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, gdi32))

    captured = provider.capture(user32.target)

    assert captured is not None
    assert captured.api_succeeded is True
    assert user32.point_checks == 2 * len(provider.REQUIRED_VISIBLE_POINTS)


def test_visible_capture_rejects_reused_target_handle_during_copy(
    monkeypatch,
):
    user32 = FakeVisibleRegionUser32()
    gdi32 = FakeVisibleRegionGdi32(
        user32,
        occlude_after_copy=False,
        replace_process_after_copy=99,
    )
    provider = Win32VisibleRegionCaptureProvider(
        process_lifecycle_provider=fake_process_lifecycle_token,
    )
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, gdi32))

    assert provider.capture(user32.target) is None

    assert user32.process_ids[user32.target] == 99
    assert gdi32.bitblt_calls == 1


def test_visible_capture_fails_closed_without_target_lifecycle(
    monkeypatch,
):
    user32 = FakeVisibleRegionUser32()
    gdi32 = FakeVisibleRegionGdi32(
        user32,
        occlude_after_copy=False,
    )
    provider = Win32VisibleRegionCaptureProvider(
        process_lifecycle_provider=missing_process_lifecycle_token,
    )
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, gdi32))

    assert provider.capture(user32.target) is None

    assert gdi32.bitblt_calls == 0
    assert user32.point_checks == 0


def test_recovering_provider_refreshes_and_restores_minimized_window(monkeypatch):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeWindowStateApi(minimized=True)
    fresh = FakeProvider(expected)
    provider = Win32RecoveringPrintWindowProvider(
        paint_settle_seconds=0,
        fresh_capture_provider=fresh,
        process_lifecycle_provider=fake_process_lifecycle_token,
    )
    original_order = list(user32.z_order)
    original_rect = user32.rects[user32.target]
    original_normal_rect = user32.normal_rect
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, object()))
    monkeypatch.setattr(
        Win32PrintWindowProvider,
        "capture",
        lambda _self, _handle: (_ for _ in ()).throw(
            AssertionError("passive PrintWindow fallback is forbidden")
        ),
    )

    assert provider.capture(123) is expected
    assert fresh.handles == [123]
    assert provider.last_failure_stage is None
    assert user32.minimized is True
    assert user32.foreground == 700
    assert user32.foreground_calls == []
    assert user32.z_order == original_order
    assert user32.rects[user32.target] == original_rect
    assert user32.normal_rect == original_normal_rect
    assert user32.position_calls == [(user32.target, 300)]
    assert user32.show_commands == [
        provider.SW_SHOWNOACTIVATE,
        provider.SW_SHOWMINNOACTIVE,
    ]


def test_minimized_capture_accepts_exact_previous_with_current_reordered_next(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeWindowStateApi(minimized=True)
    install_minimized_neighbor_reorder(user32, "previous")
    provider = Win32RecoveringPrintWindowProvider(
        paint_settle_seconds=0,
        fresh_capture_provider=FakeProvider(expected),
        process_lifecycle_provider=fake_process_lifecycle_token,
    )
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, object()))

    captured = provider.capture(user32.target)

    assert captured is expected
    assert user32.minimized is True
    assert user32.foreground == 700
    assert user32.process_ids[300] == 30
    assert user32.process_ids[400] == 40
    target_index = user32.z_order.index(user32.target)
    assert user32.z_order[target_index - 1] == 300
    assert user32.z_order[target_index + 1] != 400


def test_minimized_capture_accepts_exact_next_with_current_reordered_previous(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeWindowStateApi(minimized=True)
    install_minimized_neighbor_reorder(user32, "next")
    provider = Win32RecoveringPrintWindowProvider(
        paint_settle_seconds=0,
        fresh_capture_provider=FakeProvider(expected),
        process_lifecycle_provider=fake_process_lifecycle_token,
    )
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, object()))

    captured = provider.capture(user32.target)

    assert captured is expected
    assert user32.minimized is True
    assert user32.foreground == 700
    assert user32.process_ids[300] == 30
    assert user32.process_ids[400] == 40
    target_index = user32.z_order.index(user32.target)
    assert user32.z_order[target_index - 1] != 300
    assert user32.z_order[target_index + 1] == 400


def test_minimized_capture_rejects_when_neither_neighbor_edge_is_exact(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeWindowStateApi(minimized=True)
    install_minimized_neighbor_reorder(user32, "none")
    provider = Win32RecoveringPrintWindowProvider(
        paint_settle_seconds=0,
        fresh_capture_provider=FakeProvider(expected),
        process_lifecycle_provider=fake_process_lifecycle_token,
    )
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, object()))

    assert provider.capture(user32.target) is None
    assert user32.process_ids[300] == 30
    assert user32.process_ids[400] == 40


def test_minimized_capture_rejects_replaced_original_neighbor_instance(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    for replaced_handle in (300, 400):
        user32 = FakeWindowStateApi(minimized=True)

        def replace_neighbor(_handle, neighbor=replaced_handle):
            user32.process_ids[neighbor] = 999
            return expected

        provider = Win32RecoveringPrintWindowProvider(
            paint_settle_seconds=0,
            fresh_capture_provider=CallbackProvider(replace_neighbor),
            process_lifecycle_provider=fake_process_lifecycle_token,
        )
        monkeypatch.setattr(
            provider,
            "_libraries",
            lambda api=user32: (api, object()),
        )

        assert provider.capture(user32.target) is None, replaced_handle


def test_minimized_neighbor_trust_revalidates_after_relationship_reads():
    user32 = FakeWindowStateApi(minimized=True)
    state = Win32TemporarilyRevealedCaptureProvider
    previous_handle = 300
    next_handle = 400
    previous_instance = state._window_instance_credential(
        user32,
        ctypes.c_void_p(previous_handle),
        fake_process_lifecycle_token,
    )
    next_instance = state._window_instance_credential(
        user32,
        ctypes.c_void_p(next_handle),
        fake_process_lifecycle_token,
    )
    assert previous_instance is not None
    assert next_instance is not None
    original_get_window = user32.GetWindow.callback
    relationship_reads = 0

    def replace_neighbor_after_relationship_read(handle, command):
        nonlocal relationship_reads
        result = original_get_window(handle, command)
        if handle_value(handle) == user32.target and command in {
            state.GW_HWNDPREV,
            state.GW_HWNDNEXT,
        }:
            relationship_reads += 1
            if relationship_reads == 2:
                user32.process_ids[previous_handle] = 999
        return result

    user32.GetWindow = FakeCallable(
        replace_neighbor_after_relationship_read
    )

    assert not Win32RecoveringPrintWindowProvider._trusted_minimized_neighbor_restoration(
        user32,
        ctypes.c_void_p(user32.target),
        previous_handle=previous_handle,
        next_handle=next_handle,
        previous_instance=previous_instance,
        next_instance=next_instance,
        lifecycle_provider=fake_process_lifecycle_token,
    )
    assert relationship_reads == 2
    assert user32.process_ids[previous_handle] == 999


def test_minimized_capture_with_only_previous_requires_that_exact_edge(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    for break_after_repair, expected_result in (
        (False, expected),
        (True, None),
    ):
        user32 = FakeWindowStateApi(minimized=True)
        user32.z_order = [700, 300, user32.target]
        original_show = user32.ShowWindow.callback

        def show_then_reorder(handle, command):
            result = original_show(handle, command)
            if command == Win32RecoveringPrintWindowProvider.SW_SHOWMINNOACTIVE:
                user32.z_order.remove(700)
                target_index = user32.z_order.index(user32.target)
                user32.z_order.insert(target_index, 700)
            return result

        user32.ShowWindow = FakeCallable(show_then_reorder)
        original_position = user32.SetWindowPos.callback

        def restore_previous_then_optionally_break(*args):
            result = original_position(*args)
            if result and break_after_repair:
                user32.z_order.remove(700)
                target_index = user32.z_order.index(user32.target)
                user32.z_order.insert(target_index, 700)
            return result

        user32.SetWindowPos = FakeCallable(
            restore_previous_then_optionally_break
        )
        fresh = FakeProvider(expected)
        provider = Win32RecoveringPrintWindowProvider(
            paint_settle_seconds=0,
            fresh_capture_provider=fresh,
            process_lifecycle_provider=fake_process_lifecycle_token,
        )
        monkeypatch.setattr(
            provider,
            "_libraries",
            lambda api=user32: (api, object()),
        )

        assert provider.capture(user32.target) is expected_result
        assert fresh.handles == [user32.target]
        target_index = user32.z_order.index(user32.target)
        assert (
            user32.z_order[target_index - 1] == 300
        ) is (not break_after_repair)


def test_minimized_capture_with_only_next_requires_that_exact_edge(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    for break_after_repair, expected_result in (
        (False, expected),
        (True, None),
    ):
        user32 = FakeWindowStateApi(minimized=True)
        user32.z_order = [user32.target, 400, 700]
        original_position = user32.SetWindowPos.callback

        def restore_next_then_optionally_break(*args):
            result = original_position(*args)
            if result and break_after_repair:
                user32.z_order.remove(700)
                target_index = user32.z_order.index(user32.target)
                user32.z_order.insert(target_index + 1, 700)
            return result

        user32.SetWindowPos = FakeCallable(
            restore_next_then_optionally_break
        )
        fresh = FakeProvider(expected)
        provider = Win32RecoveringPrintWindowProvider(
            paint_settle_seconds=0,
            fresh_capture_provider=fresh,
            process_lifecycle_provider=fake_process_lifecycle_token,
        )
        monkeypatch.setattr(
            provider,
            "_libraries",
            lambda api=user32: (api, object()),
        )

        assert provider.capture(user32.target) is expected_result
        assert fresh.handles == [user32.target]
        target_index = user32.z_order.index(user32.target)
        exact_next = (
            target_index + 1 < len(user32.z_order)
            and user32.z_order[target_index + 1] == 400
        )
        assert exact_next is (not break_after_repair)


def test_recovering_provider_defaults_to_temporary_reveal_visible_pixels():
    provider = Win32RecoveringPrintWindowProvider(
        process_lifecycle_provider=fake_process_lifecycle_token,
    )

    assert isinstance(
        provider._fresh_capture_provider,
        Win32TemporarilyRevealedCaptureProvider,
    )
    assert isinstance(
        provider._fresh_capture_provider._visible_provider,
        Win32VisibleRegionCaptureProvider,
    )


def test_default_minimized_capture_composition_restores_every_window_state(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeDefaultMinimizedCaptureApi()
    visible = FakeVisibleCaptureProvider(expected)
    provider = Win32RecoveringPrintWindowProvider(
        paint_settle_seconds=0,
        process_lifecycle_provider=fake_process_lifecycle_token,
    )
    revealed = provider._fresh_capture_provider
    assert isinstance(revealed, Win32TemporarilyRevealedCaptureProvider)
    revealed._paint_settle_seconds = 0
    revealed._visible_provider = visible
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, object()))
    monkeypatch.setattr(revealed, "_libraries", lambda: (user32, object()))
    original_order = list(user32.z_order)
    original_rect = user32.rects[user32.target]
    original_placement = user32.normal_rect
    original_foreground = user32.foreground
    original_topmost = set(user32.topmost)

    captured = provider.capture(user32.target)

    assert captured is expected
    assert visible.handles == [user32.target]
    assert user32.show_commands == [
        provider.SW_SHOWNOACTIVATE,
        provider.SW_SHOWMINNOACTIVE,
    ]
    anchors = [call[1] for call in user32.position_calls]
    assert Win32TemporarilyRevealedCaptureProvider.HWND_TOPMOST in anchors
    assert Win32TemporarilyRevealedCaptureProvider.HWND_NOTOPMOST in anchors
    assert 300 in anchors
    assert user32.minimized is True
    assert user32.foreground == original_foreground
    assert user32.foreground_calls == []
    assert user32.z_order == original_order
    assert user32.rects[user32.target] == original_rect
    assert user32.normal_rect == original_placement
    assert user32.topmost == original_topmost


def test_default_minimized_capture_composition_rejects_any_restore_failure(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    for failure in (
        "inner_demote",
        "inner_restore",
        "outer_minimize",
        "outer_neighbor",
    ):
        user32 = FakeDefaultMinimizedCaptureApi(
            restoration_failure=failure,
        )
        visible = FakeVisibleCaptureProvider(expected)
        provider = Win32RecoveringPrintWindowProvider(
            paint_settle_seconds=0,
            process_lifecycle_provider=fake_process_lifecycle_token,
        )
        revealed = provider._fresh_capture_provider
        assert isinstance(
            revealed,
            Win32TemporarilyRevealedCaptureProvider,
        )
        revealed._paint_settle_seconds = 0
        revealed._visible_provider = visible
        monkeypatch.setattr(
            provider,
            "_libraries",
            lambda api=user32: (api, object()),
        )
        monkeypatch.setattr(
            revealed,
            "_libraries",
            lambda api=user32: (api, object()),
        )

        assert provider.capture(user32.target) is None, failure
        assert visible.handles == [user32.target], failure


def test_recovering_provider_rejects_stale_snapshot_of_normal_window(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeWindowStateApi(minimized=False)
    fresh = FakeProvider(expected)
    provider = Win32RecoveringPrintWindowProvider(
        paint_settle_seconds=0,
        fresh_capture_provider=fresh,
        process_lifecycle_provider=fake_process_lifecycle_token,
    )
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, object()))

    assert provider.capture(123) is None
    assert fresh.handles == []
    assert provider.last_failure_stage == "window_not_minimized"
    assert user32.show_commands == []


def test_recovering_provider_fails_closed_without_target_lifecycle(
    monkeypatch,
):
    user32 = FakeWindowStateApi(minimized=True)
    provider = Win32RecoveringPrintWindowProvider(
        paint_settle_seconds=0,
        process_lifecycle_provider=missing_process_lifecycle_token,
    )
    capture_calls = []
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, object()))
    monkeypatch.setattr(
        Win32PrintWindowProvider,
        "capture",
        lambda _self, handle: capture_calls.append(handle),
    )

    assert provider.capture(user32.target) is None

    assert capture_calls == []
    assert user32.show_commands == []
    assert user32.position_calls == []


def test_recovering_provider_fails_closed_without_foreground_lifecycle(
    monkeypatch,
):
    user32 = FakeWindowStateApi(minimized=True)
    provider = Win32RecoveringPrintWindowProvider(
        paint_settle_seconds=0,
        process_lifecycle_provider=missing_foreground_lifecycle_token,
    )
    capture_calls = []
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, object()))
    monkeypatch.setattr(
        Win32PrintWindowProvider,
        "capture",
        lambda _self, handle: capture_calls.append(handle),
    )

    assert provider.capture(user32.target) is None

    assert capture_calls == []
    assert user32.show_commands == []
    assert user32.position_calls == []
    assert user32.foreground_calls == []


def test_recovering_provider_returns_no_fresh_frame_when_restore_fails(
    monkeypatch,
):
    user32 = FakeWindowStateApi(minimized=True, restore_succeeds=False)
    provider = Win32RecoveringPrintWindowProvider(
        paint_settle_seconds=0,
        process_lifecycle_provider=fake_process_lifecycle_token,
    )
    capture_calls = []
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, object()))
    monkeypatch.setattr(
        Win32PrintWindowProvider,
        "capture",
        lambda _self, handle: capture_calls.append(handle),
    )

    assert provider.capture(123) is None
    assert capture_calls == []
    assert user32.minimized is True
    assert user32.show_commands == [provider.SW_SHOWNOACTIVATE]


def test_recovering_provider_discards_sample_and_restores_unexpected_focus(
    monkeypatch,
):
    user32 = FakeWindowStateApi(
        minimized=True,
        foreground_after_restore=123,
    )
    provider = Win32RecoveringPrintWindowProvider(
        paint_settle_seconds=0,
        process_lifecycle_provider=fake_process_lifecycle_token,
    )
    capture_calls = []
    original_order = list(user32.z_order)
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, object()))
    monkeypatch.setattr(
        Win32PrintWindowProvider,
        "capture",
        lambda _self, handle: capture_calls.append(handle),
    )

    assert provider.capture(user32.target) is None

    assert capture_calls == []
    assert user32.minimized is True
    assert user32.z_order == original_order
    assert user32.foreground == 700
    assert user32.foreground_calls == [700]


def test_recovering_provider_never_focuses_reused_original_foreground(
    monkeypatch,
):
    user32 = FakeWindowStateApi(
        minimized=True,
        foreground_after_restore=123,
    )
    original_show = user32.ShowWindow.callback

    def restore_and_reuse_foreground(handle, command):
        result = original_show(handle, command)
        if command == Win32RecoveringPrintWindowProvider.SW_SHOWNOACTIVATE:
            user32.process_ids[700] = 999
        return result

    user32.ShowWindow = FakeCallable(restore_and_reuse_foreground)
    provider = Win32RecoveringPrintWindowProvider(
        paint_settle_seconds=0,
        process_lifecycle_provider=fake_process_lifecycle_token,
    )
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, object()))

    assert provider.capture(user32.target) is None

    assert user32.process_ids[700] == 999
    assert user32.foreground_calls == []


def test_recovering_provider_never_overrides_concurrent_user_focus(
    monkeypatch,
):
    user32 = FakeWindowStateApi(
        minimized=True,
        foreground_after_restore=888,
    )
    provider = Win32RecoveringPrintWindowProvider(
        paint_settle_seconds=0,
        process_lifecycle_provider=fake_process_lifecycle_token,
    )
    original_order = list(user32.z_order)
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, object()))

    assert provider.capture(user32.target) is None

    assert user32.minimized is True
    assert user32.z_order == original_order
    assert user32.foreground == 888
    assert user32.foreground_calls == []


def test_recovering_provider_never_mutates_reused_handle(monkeypatch):
    user32 = FakeWindowStateApi(
        minimized=True,
        process_after_restore=99,
    )
    provider = Win32RecoveringPrintWindowProvider(
        paint_settle_seconds=0,
        process_lifecycle_provider=fake_process_lifecycle_token,
    )
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, object()))

    assert provider.capture(user32.target) is None

    assert user32.process_ids[user32.target] == 99
    assert user32.show_commands == [provider.SW_SHOWNOACTIVATE]
    assert user32.position_calls == []
    assert user32.foreground_calls == []


def test_recovering_provider_discards_sample_when_reminimize_fails(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeWindowStateApi(
        minimized=True,
        minimize_succeeds=False,
    )
    fresh = FakeProvider(expected)
    provider = Win32RecoveringPrintWindowProvider(
        paint_settle_seconds=0,
        fresh_capture_provider=fresh,
        process_lifecycle_provider=fake_process_lifecycle_token,
    )
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, object()))

    assert provider.capture(user32.target) is None
    assert user32.minimized is False
    assert fresh.handles == [user32.target]
    assert provider.last_failure_stage == "restoration_barrier_failed"


def test_recovering_provider_discards_sample_when_z_order_restore_fails(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeWindowStateApi(
        minimized=True,
        position_restore_succeeds=False,
    )
    fresh = FakeProvider(expected)
    provider = Win32RecoveringPrintWindowProvider(
        paint_settle_seconds=0,
        fresh_capture_provider=fresh,
        process_lifecycle_provider=fake_process_lifecycle_token,
    )
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, object()))

    assert provider.capture(user32.target) is None
    assert user32.minimized is True
    assert user32.position_calls == [(user32.target, 300)]
    assert provider.last_failure_stage == "restoration_barrier_failed"


def test_recovering_provider_never_anchors_to_reused_neighbor(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeWindowStateApi(minimized=True)
    def capture_and_reuse_neighbor(_handle):
        user32.process_ids[300] = 999
        return expected

    fresh = CallbackProvider(capture_and_reuse_neighbor)
    provider = Win32RecoveringPrintWindowProvider(
        paint_settle_seconds=0,
        fresh_capture_provider=fresh,
        process_lifecycle_provider=fake_process_lifecycle_token,
    )
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, object()))

    assert provider.capture(user32.target) is None

    assert user32.process_ids[300] == 999
    assert user32.position_calls == []


def test_recovering_provider_discards_sample_after_position_race(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeWindowStateApi(minimized=True)
    provider = Win32RecoveringPrintWindowProvider(
        paint_settle_seconds=0,
        process_lifecycle_provider=fake_process_lifecycle_token,
    )
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, object()))

    def capture_and_move(_handle):
        user32.normal_rect = (30, 40, 946, 669)
        user32.rects[user32.target] = user32.normal_rect
        return expected

    provider._fresh_capture_provider = CallbackProvider(capture_and_move)

    assert provider.capture(user32.target) is None
    assert user32.minimized is True
    # A concurrent move is detected, but is not overwritten.
    assert user32.normal_rect == (30, 40, 946, 669)


def test_recovering_provider_rechecks_pid_immediately_before_reminimize(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeWindowStateApi(minimized=True)
    user32.arm_reuse_after_capture_foreground = True
    provider = Win32RecoveringPrintWindowProvider(
        paint_settle_seconds=0,
        process_lifecycle_provider=fake_process_lifecycle_token,
    )
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, object()))

    def finish_capture(_handle):
        user32.capture_finished = True
        return expected

    provider._fresh_capture_provider = CallbackProvider(finish_capture)

    assert provider.capture(user32.target) is None

    assert user32.process_ids[user32.target] == 99
    # The first finally PID read still saw the original process, then the
    # immediate pre-ShowWindow recheck caught the replacement.
    assert user32.show_commands == [provider.SW_SHOWNOACTIVATE]
    assert user32.position_calls == []


def test_minimized_and_obscured_capture_share_one_window_state_lock():
    assert (
        Win32RecoveringPrintWindowProvider._window_state_lock
        is Win32TemporarilyRevealedCaptureProvider._z_order_lock
    )


def fake_process_lifecycle_token(process_id):
    return int(process_id) * 1000


def missing_process_lifecycle_token(_process_id):
    return None


def missing_foreground_lifecycle_token(process_id):
    if int(process_id) == 70:
        return None
    return fake_process_lifecycle_token(process_id)


def handle_value(handle):
    if isinstance(handle, int):
        return handle
    value = int(getattr(handle, "value", 0) or 0)
    if value == int(ctypes.c_void_p(-1).value):
        return -1
    if value == int(ctypes.c_void_p(-2).value):
        return -2
    return value


class FakeRevealedWindowApi:
    def __init__(
        self,
        *,
        fail_raise=False,
        fail_demote=False,
        fail_restore=False,
        ignore_raise_effect=False,
        race_after_raise=None,
    ):
        self.target = 123
        self.foreground = 700
        self.z_order = [700, 300, self.target, 400]
        self.process_ids = {
            700: 70,
            300: 30,
            self.target: 12,
            400: 40,
            888: 88,
        }
        self.window_classes = {
            handle: f"WindowClass{handle}"
            for handle in self.process_ids
        }
        self.visible = {handle: True for handle in self.process_ids}
        self.minimized = {handle: False for handle in self.process_ids}
        self.rects = {
            handle: (10, 20, 926, 649)
            for handle in self.process_ids
        }
        self.topmost = set()
        self.fail_raise = fail_raise
        self.fail_demote = fail_demote
        self.fail_restore = fail_restore
        self.ignore_raise_effect = ignore_raise_effect
        self.race_after_raise = race_after_raise
        self.position_calls = []
        self.foreground_calls = []
        self.capture_finished = False
        self.arm_reuse_after_capture_foreground = False
        self.reuse_after_next_pid_query = False
        self.IsWindow = FakeCallable(
            lambda handle: handle_value(handle) in self.process_ids
        )
        self.IsWindowVisible = FakeCallable(
            lambda handle: self.visible.get(handle_value(handle), False)
        )
        self.IsIconic = FakeCallable(
            lambda handle: self.minimized.get(handle_value(handle), False)
        )
        self.GetWindowRect = FakeCallable(self._get_window_rect)
        self.GetWindow = FakeCallable(self._get_window)
        self.GetWindowLongW = FakeCallable(
            lambda handle, _index: (
                Win32TemporarilyRevealedCaptureProvider.WS_EX_TOPMOST
                if handle_value(handle) in self.topmost
                else 0
            )
        )
        self.GetForegroundWindow = FakeCallable(self._get_foreground_window)
        self.GetWindowThreadProcessId = FakeCallable(
            self._get_window_thread_process_id
        )
        self.GetClassNameW = FakeCallable(self._get_class_name)
        self.SetWindowPos = FakeCallable(self._set_window_pos)
        self.SetForegroundWindow = FakeCallable(self._set_foreground_window)

    def _get_window_rect(self, handle, pointer):
        rect = self.rects.get(handle_value(handle))
        if rect is None:
            return False
        target = pointer._obj
        target.left, target.top, target.right, target.bottom = rect
        return True

    def _get_window(self, handle, command):
        value = handle_value(handle)
        if value not in self.z_order:
            return 0
        index = self.z_order.index(value)
        if (
            command
            == Win32TemporarilyRevealedCaptureProvider.GW_HWNDPREV
        ):
            return self.z_order[index - 1] if index > 0 else 0
        if command == Win32TemporarilyRevealedCaptureProvider.GW_HWNDNEXT:
            return (
                self.z_order[index + 1]
                if index + 1 < len(self.z_order)
                else 0
            )
        return 0

    def _get_window_thread_process_id(self, handle, process_pointer):
        value = handle_value(handle)
        process_id = self.process_ids.get(value, 0)
        process_pointer._obj.value = process_id
        if (
            value == self.target
            and self.reuse_after_next_pid_query
        ):
            self.process_ids[value] = 99
            self.reuse_after_next_pid_query = False
        return 1 if process_id else 0

    def _get_class_name(self, handle, buffer, maximum):
        name = self.window_classes.get(handle_value(handle))
        if not name:
            return 0
        buffer.value = name[: max(0, maximum - 1)]
        return len(buffer.value)

    def _get_foreground_window(self):
        if (
            self.capture_finished
            and self.arm_reuse_after_capture_foreground
        ):
            self.reuse_after_next_pid_query = True
            self.arm_reuse_after_capture_foreground = False
        return self.foreground

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
        value = handle_value(handle)
        anchor = handle_value(insert_after)
        self.position_calls.append(
            (value, anchor, x, y, width, height, flags)
        )
        call_number = len(self.position_calls)
        if call_number == 1 and self.fail_raise:
            return False
        if call_number == 1 and self.ignore_raise_effect:
            return True
        if call_number == 2 and self.fail_demote:
            return False
        if call_number >= 3 and self.fail_restore:
            return False
        if value not in self.z_order:
            return False
        self.z_order.remove(value)
        if anchor == Win32TemporarilyRevealedCaptureProvider.HWND_TOPMOST:
            self.topmost.add(value)
            self.z_order.insert(0, value)
        elif (
            anchor
            in (
                Win32TemporarilyRevealedCaptureProvider.HWND_TOP,
                Win32TemporarilyRevealedCaptureProvider.HWND_NOTOPMOST,
            )
        ):
            self.topmost.discard(value)
            normal_index = next(
                (
                    index
                    for index, handle_value_ in enumerate(self.z_order)
                    if handle_value_ not in self.topmost
                ),
                len(self.z_order),
            )
            self.z_order.insert(normal_index, value)
        elif anchor in self.z_order:
            if anchor in self.topmost:
                self.topmost.add(value)
            else:
                self.topmost.discard(value)
            self.z_order.insert(self.z_order.index(anchor) + 1, value)
        else:
            return False
        if call_number == 1 and self.race_after_raise is not None:
            self.race_after_raise(self)
        return True

    def _set_foreground_window(self, handle):
        value = handle_value(handle)
        self.foreground_calls.append(value)
        if value not in self.process_ids:
            return False
        self.foreground = value
        return True


class FakeDefaultMinimizedCaptureApi(FakeWindowStateApi):
    """One fake Win32 state shared by both default provider layers."""

    def __init__(self, *, restoration_failure=None):
        super().__init__(
            minimized=True,
            minimize_succeeds=(restoration_failure != "outer_minimize"),
        )
        self.restoration_failure = restoration_failure
        self.position_calls = []
        self.SetWindowPos = FakeCallable(self._set_layered_window_pos)

    def _set_layered_window_pos(
        self,
        handle,
        insert_after,
        x,
        y,
        width,
        height,
        flags,
    ):
        value = handle_value(handle)
        anchor = handle_value(insert_after)
        self.position_calls.append(
            (value, anchor, x, y, width, height, flags)
        )
        if (
            self.restoration_failure == "inner_demote"
            and anchor
            == Win32TemporarilyRevealedCaptureProvider.HWND_NOTOPMOST
        ):
            return False
        if (
            self.restoration_failure == "inner_restore"
            and anchor == Win32TemporarilyRevealedCaptureProvider.HWND_TOP
            and len(self.position_calls) >= 3
        ):
            return False
        if (
            self.restoration_failure == "outer_neighbor"
            and anchor == 300
        ):
            return False
        if value not in self.z_order:
            return False
        self.z_order.remove(value)
        if anchor == Win32TemporarilyRevealedCaptureProvider.HWND_TOPMOST:
            self.topmost.add(value)
            self.z_order.insert(0, value)
        elif anchor in (
            Win32TemporarilyRevealedCaptureProvider.HWND_TOP,
            Win32TemporarilyRevealedCaptureProvider.HWND_NOTOPMOST,
        ):
            self.topmost.discard(value)
            first_normal = next(
                (
                    index
                    for index, candidate in enumerate(self.z_order)
                    if candidate not in self.topmost
                ),
                len(self.z_order),
            )
            self.z_order.insert(first_normal, value)
        elif anchor in self.z_order:
            if anchor in self.topmost:
                self.topmost.add(value)
            else:
                self.topmost.discard(value)
            self.z_order.insert(self.z_order.index(anchor) + 1, value)
        else:
            return False
        return True


class FakeVisibleCaptureProvider:
    def __init__(self, sample_value, *, before_return=None):
        self.sample_value = sample_value
        self.before_return = before_return
        self.handles = []

    def capture(self, window_handle):
        self.handles.append(window_handle)
        if self.before_return is not None:
            self.before_return()
        return self.sample_value


def reveal_provider(user32, visible, monkeypatch):
    provider = Win32TemporarilyRevealedCaptureProvider(
        visible_provider=visible,
        paint_settle_seconds=0,
        process_lifecycle_provider=fake_process_lifecycle_token,
    )
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, object()))
    return provider


def test_revealed_capture_restores_z_order_without_activation_or_movement(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeRevealedWindowApi()
    visible = FakeVisibleCaptureProvider(expected)
    provider = reveal_provider(user32, visible, monkeypatch)
    original_order = list(user32.z_order)
    original_rect = user32.rects[user32.target]

    assert provider.capture(user32.target) is expected

    assert visible.handles == [user32.target]
    assert user32.z_order == original_order
    assert user32.foreground == 700
    assert user32.foreground_calls == []
    assert user32.rects[user32.target] == original_rect
    assert user32.minimized[user32.target] is False
    assert len(user32.position_calls) == 3
    assert user32.position_calls[0][1] == provider.HWND_TOPMOST
    assert user32.position_calls[1][1] == provider.HWND_NOTOPMOST
    for _handle, _anchor, x, y, width, height, flags in user32.position_calls:
        assert (x, y, width, height) == (0, 0, 0, 0)
        assert flags & provider.SWP_NOMOVE
        assert flags & provider.SWP_NOSIZE
        assert flags & provider.SWP_NOACTIVATE


def test_revealed_capture_is_really_topmost_while_visible_sample_is_taken(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeRevealedWindowApi()
    observed = []

    def observe_state():
        observed.append(
            (
                user32.target in user32.topmost,
                user32.z_order[0],
                user32.foreground,
            )
        )

    visible = FakeVisibleCaptureProvider(
        expected,
        before_return=observe_state,
    )
    provider = reveal_provider(user32, visible, monkeypatch)

    assert provider.capture(user32.target) is expected
    assert observed == [(True, user32.target, 700)]


def test_revealed_capture_preserves_existing_topmost_band(monkeypatch):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeRevealedWindowApi()
    user32.z_order = [300, user32.target, 700, 400]
    user32.topmost = {300, user32.target}
    visible = FakeVisibleCaptureProvider(expected)
    provider = reveal_provider(user32, visible, monkeypatch)
    original_order = list(user32.z_order)

    assert provider.capture(user32.target) is expected

    assert user32.z_order == original_order
    assert user32.target in user32.topmost
    assert user32.position_calls[0][1] == provider.HWND_TOPMOST
    assert user32.position_calls[1][1] == provider.HWND_NOTOPMOST
    assert user32.position_calls[2][1] == 300


def test_revealed_capture_restores_z_order_when_capture_fails(monkeypatch):
    user32 = FakeRevealedWindowApi()
    visible = FakeVisibleCaptureProvider(None)
    provider = reveal_provider(user32, visible, monkeypatch)
    original_order = list(user32.z_order)

    assert provider.capture(user32.target) is None

    assert visible.handles == [user32.target]
    assert user32.z_order == original_order
    assert user32.foreground == 700


def test_revealed_capture_fails_closed_without_target_lifecycle(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeRevealedWindowApi()
    visible = FakeVisibleCaptureProvider(expected)
    provider = Win32TemporarilyRevealedCaptureProvider(
        visible_provider=visible,
        paint_settle_seconds=0,
        process_lifecycle_provider=missing_process_lifecycle_token,
    )
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, object()))

    assert provider.capture(user32.target) is None

    assert visible.handles == []
    assert user32.position_calls == []
    assert user32.foreground_calls == []


def test_revealed_capture_fails_closed_without_foreground_lifecycle(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeRevealedWindowApi()
    visible = FakeVisibleCaptureProvider(expected)
    provider = Win32TemporarilyRevealedCaptureProvider(
        visible_provider=visible,
        paint_settle_seconds=0,
        process_lifecycle_provider=missing_foreground_lifecycle_token,
    )
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, object()))

    assert provider.capture(user32.target) is None

    assert visible.handles == []
    assert user32.position_calls == []
    assert user32.foreground_calls == []


def test_revealed_capture_rejects_raise_failure_without_capturing(monkeypatch):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeRevealedWindowApi(fail_raise=True)
    visible = FakeVisibleCaptureProvider(expected)
    provider = reveal_provider(user32, visible, monkeypatch)
    original_order = list(user32.z_order)

    assert provider.capture(user32.target) is None

    assert visible.handles == []
    assert user32.z_order == original_order
    assert len(user32.position_calls) == 1


def test_revealed_capture_rejects_false_success_when_topmost_did_not_change(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeRevealedWindowApi(ignore_raise_effect=True)
    visible = FakeVisibleCaptureProvider(expected)
    provider = reveal_provider(user32, visible, monkeypatch)
    original_order = list(user32.z_order)

    assert provider.capture(user32.target) is None

    assert visible.handles == []
    assert user32.z_order == original_order
    assert user32.target not in user32.topmost


def test_revealed_capture_rejects_handle_reuse_before_capture(monkeypatch):
    expected = sample([0, 20, 80, 255] * 4)

    def replace_target(api):
        api.process_ids[api.target] = 99

    user32 = FakeRevealedWindowApi(race_after_raise=replace_target)
    visible = FakeVisibleCaptureProvider(expected)
    provider = reveal_provider(user32, visible, monkeypatch)

    assert provider.capture(user32.target) is None

    assert visible.handles == []
    # The reused handle must not receive a restoration mutation.
    assert len(user32.position_calls) == 1
    assert user32.foreground_calls == []


def test_revealed_capture_restores_focus_if_target_was_unexpectedly_activated(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)

    def activate_target(api):
        api.foreground = api.target

    user32 = FakeRevealedWindowApi(race_after_raise=activate_target)
    visible = FakeVisibleCaptureProvider(expected)
    provider = reveal_provider(user32, visible, monkeypatch)
    original_order = list(user32.z_order)

    assert provider.capture(user32.target) is None

    assert visible.handles == []
    assert user32.z_order == original_order
    assert user32.foreground == 700
    assert user32.foreground_calls == [700]


def test_revealed_capture_never_focuses_reused_original_foreground(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)

    def activate_target_and_reuse_foreground(api):
        api.foreground = api.target
        api.process_ids[700] = 999

    user32 = FakeRevealedWindowApi(
        race_after_raise=activate_target_and_reuse_foreground,
    )
    visible = FakeVisibleCaptureProvider(expected)
    provider = reveal_provider(user32, visible, monkeypatch)

    assert provider.capture(user32.target) is None

    assert user32.process_ids[700] == 999
    assert user32.foreground_calls == []


def test_revealed_capture_never_anchors_to_reused_neighbor(monkeypatch):
    expected = sample([0, 20, 80, 255] * 4)

    def reuse_previous_neighbor(api):
        api.process_ids[300] = 999

    user32 = FakeRevealedWindowApi(
        race_after_raise=reuse_previous_neighbor,
    )
    visible = FakeVisibleCaptureProvider(expected)
    provider = reveal_provider(user32, visible, monkeypatch)

    assert provider.capture(user32.target) is None

    assert user32.process_ids[300] == 999
    assert [call[1] for call in user32.position_calls] == [
        provider.HWND_TOPMOST,
        provider.HWND_NOTOPMOST,
    ]
    assert user32.target not in user32.topmost


def test_revealed_capture_does_not_override_concurrent_user_focus(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)

    def user_changes_focus(api):
        api.foreground = 888

    user32 = FakeRevealedWindowApi(race_after_raise=user_changes_focus)
    visible = FakeVisibleCaptureProvider(expected)
    provider = reveal_provider(user32, visible, monkeypatch)
    original_order = list(user32.z_order)

    assert provider.capture(user32.target) is None

    assert visible.handles == []
    assert user32.z_order == original_order
    assert user32.foreground == 888
    assert user32.foreground_calls == []


def test_revealed_capture_rejects_minimize_race_without_restoring_window(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)

    def minimize_target(api):
        api.minimized[api.target] = True

    user32 = FakeRevealedWindowApi(race_after_raise=minimize_target)
    visible = FakeVisibleCaptureProvider(expected)
    provider = reveal_provider(user32, visible, monkeypatch)
    original_order = list(user32.z_order)

    assert provider.capture(user32.target) is None

    assert visible.handles == []
    assert user32.z_order == original_order
    # The provider never calls ShowWindow, so a user's concurrent minimize is
    # not reversed.
    assert user32.minimized[user32.target] is True


def test_revealed_capture_rejects_position_race_and_never_moves_window(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeRevealedWindowApi()

    def move_target():
        user32.rects[user32.target] = (30, 40, 946, 669)

    visible = FakeVisibleCaptureProvider(
        expected,
        before_return=move_target,
    )
    provider = reveal_provider(user32, visible, monkeypatch)
    original_order = list(user32.z_order)

    assert provider.capture(user32.target) is None

    assert visible.handles == [user32.target]
    assert user32.z_order == original_order
    assert user32.rects[user32.target] == (30, 40, 946, 669)
    assert all(
        call[2:6] == (0, 0, 0, 0)
        for call in user32.position_calls
    )


def test_revealed_capture_discards_sample_when_z_order_restore_fails(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeRevealedWindowApi(fail_restore=True)
    visible = FakeVisibleCaptureProvider(expected)
    provider = reveal_provider(user32, visible, monkeypatch)

    assert provider.capture(user32.target) is None

    assert visible.handles == [user32.target]
    assert len(user32.position_calls) == 3


def test_revealed_capture_restores_original_topmost_band_after_exact_failure(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeRevealedWindowApi()
    user32.topmost = {300, user32.target}
    original_set_window_pos = user32.SetWindowPos.callback

    def fail_only_first_exact_restore(*args):
        if len(user32.position_calls) == 2:
            user32.fail_restore = True
            try:
                return original_set_window_pos(*args)
            finally:
                user32.fail_restore = False
        return original_set_window_pos(*args)

    user32.SetWindowPos = FakeCallable(fail_only_first_exact_restore)
    visible = FakeVisibleCaptureProvider(expected)
    provider = reveal_provider(user32, visible, monkeypatch)

    assert provider.capture(user32.target) is None

    assert [call[1] for call in user32.position_calls] == [
        provider.HWND_TOPMOST,
        provider.HWND_NOTOPMOST,
        300,
        provider.HWND_TOPMOST,
    ]
    assert user32.target in user32.topmost


def test_revealed_capture_discards_sample_when_topmost_demotion_fails(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeRevealedWindowApi(fail_demote=True)
    visible = FakeVisibleCaptureProvider(expected)
    provider = reveal_provider(user32, visible, monkeypatch)

    assert provider.capture(user32.target) is None

    assert visible.handles == [user32.target]
    # The provider still makes its best restoration attempt, but does not
    # publish a sample after the required demotion reported failure.
    assert len(user32.position_calls) == 3
    assert user32.target not in user32.topmost


def test_revealed_capture_rechecks_pid_immediately_before_demotion(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeRevealedWindowApi()
    user32.arm_reuse_after_capture_foreground = True

    def finish_capture():
        user32.capture_finished = True

    visible = FakeVisibleCaptureProvider(
        expected,
        before_return=finish_capture,
    )
    provider = reveal_provider(user32, visible, monkeypatch)

    assert provider.capture(user32.target) is None

    assert user32.process_ids[user32.target] == 99
    # The first finally PID read returned the original process and then
    # replaced the handle. The immediate pre-SetWindowPos check prevents
    # demoting or repositioning the replacement.
    assert len(user32.position_calls) == 1


def test_window_instance_credential_rejects_invalid_lifecycle_values():
    for invalid_lifecycle in (None, 0, -1, False):
        user32 = FakeRevealedWindowApi()

        assert (
            _query_window_instance_credential(
                user32,
                ctypes.c_void_p(user32.target),
                lambda _process_id, value=invalid_lifecycle: value,
            )
            is None
        )


def test_window_instance_credential_requires_stable_lifecycle_value():
    user32 = FakeRevealedWindowApi()
    lifecycle_values = iter((12000, 13000))

    assert (
        _query_window_instance_credential(
            user32,
            ctypes.c_void_p(user32.target),
            lambda _process_id: next(lifecycle_values),
        )
        is None
    )


def test_window_instance_credential_rejects_reuse_during_every_role_query():
    for checked_handle in (123, 700, 300):
        user32 = FakeRevealedWindowApi()
        original_query = user32.GetWindowThreadProcessId.callback
        reused = False

        def reuse_after_first_identity_read(handle, process_pointer):
            nonlocal reused
            result = original_query(handle, process_pointer)
            if handle_value(handle) == checked_handle and not reused:
                user32.process_ids[checked_handle] = 999
                reused = True
            return result

        user32.GetWindowThreadProcessId = FakeCallable(
            reuse_after_first_identity_read
        )

        assert (
            _query_window_instance_credential(
                user32,
                ctypes.c_void_p(checked_handle),
                fake_process_lifecycle_token,
            )
            is None
        )
        assert user32.process_ids[checked_handle] == 999


def test_revealed_capture_never_restores_to_neighbor_reused_at_call_boundary(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeRevealedWindowApi()
    visible = FakeVisibleCaptureProvider(expected)
    provider = reveal_provider(user32, visible, monkeypatch)
    original_query = user32.GetWindowThreadProcessId.callback
    original_plan = (
        Win32TemporarilyRevealedCaptureProvider
        ._restore_insert_after_for_instances
    )
    armed = False

    def plan_then_arm(_cls, *args, **kwargs):
        nonlocal armed
        result = original_plan(*args, **kwargs)
        armed = result is not None
        return result

    def reuse_neighbor_during_final_target_check(handle, process_pointer):
        nonlocal armed
        result = original_query(handle, process_pointer)
        if armed and handle_value(handle) == user32.target:
            user32.process_ids[300] = 999
            armed = False
        return result

    monkeypatch.setattr(
        Win32TemporarilyRevealedCaptureProvider,
        "_restore_insert_after_for_instances",
        classmethod(plan_then_arm),
    )
    user32.GetWindowThreadProcessId = FakeCallable(
        reuse_neighbor_during_final_target_check
    )

    assert provider.capture(user32.target) is None

    assert user32.process_ids[300] == 999
    assert [call[1] for call in user32.position_calls] == [
        provider.HWND_TOPMOST,
        provider.HWND_NOTOPMOST,
    ]


def test_minimized_capture_never_restores_to_neighbor_reused_at_call_boundary(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeWindowStateApi(minimized=True)
    provider = Win32RecoveringPrintWindowProvider(
        paint_settle_seconds=0,
        fresh_capture_provider=FakeProvider(expected),
        process_lifecycle_provider=fake_process_lifecycle_token,
    )
    original_query = user32.GetWindowThreadProcessId.callback
    original_plan = (
        Win32TemporarilyRevealedCaptureProvider
        ._restore_insert_after_for_instances
    )
    armed = False

    def plan_then_arm(_cls, *args, **kwargs):
        nonlocal armed
        result = original_plan(*args, **kwargs)
        armed = result is not None
        return result

    def reuse_neighbor_during_final_target_check(handle, process_pointer):
        nonlocal armed
        result = original_query(handle, process_pointer)
        if armed and handle_value(handle) == user32.target:
            user32.process_ids[300] = 999
            armed = False
        return result

    monkeypatch.setattr(
        Win32TemporarilyRevealedCaptureProvider,
        "_restore_insert_after_for_instances",
        classmethod(plan_then_arm),
    )
    user32.GetWindowThreadProcessId = FakeCallable(
        reuse_neighbor_during_final_target_check
    )
    monkeypatch.setattr(provider, "_libraries", lambda: (user32, object()))

    assert provider.capture(user32.target) is None

    assert user32.process_ids[300] == 999
    assert user32.position_calls == []


def test_revealed_capture_never_raises_after_final_target_check_reuses_neighbor(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeRevealedWindowApi()
    visible = FakeVisibleCaptureProvider(expected)
    provider = reveal_provider(user32, visible, monkeypatch)
    original_query = user32.GetWindowThreadProcessId.callback
    initial_snapshot_complete = False
    neighbor_reused = False

    def reuse_neighbor_after_final_target_check(handle, process_pointer):
        nonlocal initial_snapshot_complete, neighbor_reused
        value = handle_value(handle)
        result = original_query(handle, process_pointer)
        if value == 400:
            initial_snapshot_complete = True
        elif (
            value == user32.target
            and initial_snapshot_complete
            and not neighbor_reused
        ):
            user32.process_ids[300] = 999
            neighbor_reused = True
        return result

    user32.GetWindowThreadProcessId = FakeCallable(
        reuse_neighbor_after_final_target_check
    )

    assert provider.capture(user32.target) is None

    assert neighbor_reused is True
    assert user32.position_calls == []
    assert visible.handles == []


def test_revealed_capture_never_demotes_after_final_target_check_reuses_neighbor(
    monkeypatch,
):
    expected = sample([0, 20, 80, 255] * 4)
    user32 = FakeRevealedWindowApi()

    def mark_capture_finished():
        user32.capture_finished = True

    visible = FakeVisibleCaptureProvider(
        expected,
        before_return=mark_capture_finished,
    )
    provider = reveal_provider(user32, visible, monkeypatch)
    original_same_instance = (
        Win32TemporarilyRevealedCaptureProvider._same_window_instance
    )
    completed_target_checks = 0

    def reuse_neighbor_after_final_target_check(
        _cls,
        checked_user32,
        hwnd,
        expected_instance,
        lifecycle_provider,
    ):
        nonlocal completed_target_checks
        result = original_same_instance(
            checked_user32,
            hwnd,
            expected_instance,
            lifecycle_provider,
        )
        if (
            user32.capture_finished
            and len(user32.position_calls) == 1
            and handle_value(hwnd) == user32.target
        ):
            completed_target_checks += 1
            if completed_target_checks == 3:
                user32.process_ids[300] = 999
        return result

    monkeypatch.setattr(
        Win32TemporarilyRevealedCaptureProvider,
        "_same_window_instance",
        classmethod(reuse_neighbor_after_final_target_check),
    )

    assert provider.capture(user32.target) is None

    assert completed_target_checks >= 3
    assert user32.process_ids[300] == 999
    assert [call[1] for call in user32.position_calls] == [
        provider.HWND_TOPMOST,
        provider.HWND_NOTOPMOST,
    ]
    assert user32.target not in user32.topmost


class FakeWgcLibrary:
    def __init__(self, access_state=1):
        self.access_state = access_state
        self.capture_calls = []
        self.FlashWgcHelperAbiVersion = FakeCallable(lambda: 1)
        self.FlashWgcPrepareBorderlessAccess = FakeCallable(
            lambda: self.access_state
        )
        self.FlashWgcCaptureWindow = FakeCallable(self._capture)

    @staticmethod
    def _value(value):
        return value.value if hasattr(value, "value") else int(value)

    def _capture(
        self,
        window_handle,
        after_timestamp,
        _timeout_ms,
        destination,
        capacity,
        output,
    ):
        handle = self._value(window_handle)
        after = self._value(after_timestamp)
        self.capture_calls.append((handle, after, destination is None))
        frame = output._obj
        frame.width = 2
        frame.height = 2
        frame.stride = 8
        frame.required_bytes = 16
        frame.timestamp = after + 1
        if destination is None:
            return WindowsGraphicsCaptureProvider.CAPTURE_BUFFER_TOO_SMALL
        if self._value(capacity) < frame.required_bytes:
            return -6
        pixels = bytes([handle & 0xFF, 20, 80, 255] * 4)
        for index, value in enumerate(pixels):
            destination[index] = value
        return WindowsGraphicsCaptureProvider.CAPTURE_OK


def wgc_provider(
    library,
    *,
    minimized=False,
    identity_provider=None,
):
    def stable_identity(handle):
        return (
            handle,
            201,
            301,
            "ShockwaveFlash",
            (0, 0, 800, 600),
            False,
            401,
        )
    provider = WindowsGraphicsCaptureProvider(
        library_loader=lambda _path: library,
        minimized_provider=lambda _handle: minimized,
        window_identity_provider=(
            identity_provider or stable_identity
        ),
    )
    return provider


def test_case_09_wgc_visible_and_occluded_windows_return_fresh_bgra_frames():
    library = FakeWgcLibrary()
    provider = wgc_provider(library)

    assert provider.prepare_borderless_access() is True
    visible = provider.capture(101)
    occluded = provider.capture(202)

    assert visible == sample([101, 20, 80, 255] * 4)
    assert occluded == sample([202, 20, 80, 255] * 4)
    assert library.capture_calls == [
        (101, 0, True),
        (101, 1, False),
        (202, 0, True),
        (202, 1, False),
    ]


def test_case_13_wgc_minimized_new_window_is_unknown_without_capture():
    library = FakeWgcLibrary()
    provider = wgc_provider(library, minimized=True)

    assert provider.prepare_borderless_access() is True
    assert provider.capture(303) is None
    assert provider.last_failure_stage == "window_minimized"
    assert library.capture_calls == []


@pytest.mark.parametrize(
    ("field", "changed_value"),
    (
        (0, 999),
        (1, 999),
        (2, 999),
        (3, "ChangedFlash"),
        (4, (1, 2, 801, 602)),
        (5, True),
        (6, 999),
    ),
)
def test_wgc_rejects_complete_instance_change_during_capture(
    field,
    changed_value,
):
    library = FakeWgcLibrary()
    stable = [
        101,
        201,
        301,
        "ShockwaveFlash",
        (0, 0, 800, 600),
        False,
        401,
    ]
    changed = list(stable)
    changed[field] = changed_value
    observations = [tuple(stable), tuple(changed)]

    def identity(_handle):
        return observations.pop(0) if observations else tuple(changed)

    provider = wgc_provider(library, identity_provider=identity)
    assert provider.prepare_borderless_access() is True

    assert provider.capture(101) is None
    assert provider.last_failure_stage == "fresh_frame_probe_failed"
    assert library.capture_calls == [(101, 0, True)]


def test_wgc_rejects_complete_instance_change_after_pixel_copy():
    library = FakeWgcLibrary()
    stable = (
        101,
        201,
        301,
        "ShockwaveFlash",
        (0, 0, 800, 600),
        False,
        401,
    )
    changed = (*stable[:-1], 999)
    observations = [stable, stable, changed]

    def identity(_handle):
        return observations.pop(0) if observations else changed

    provider = wgc_provider(library, identity_provider=identity)
    assert provider.prepare_borderless_access() is True

    assert provider.capture(101) is None
    assert provider.last_failure_stage == "fresh_frame_copy_failed"
    assert library.capture_calls == [
        (101, 0, True),
        (101, 1, False),
    ]


def test_case_15_native_wgc_helper_has_no_window_or_input_mutation_api():
    source = Path("native/windows_graphics_capture_helper.cpp").read_text(
        encoding="utf-8"
    )

    for forbidden in (
        "ShowWindow",
        "SetWindowPos",
        "BringWindowToTop",
        "SetForegroundWindow",
        "SetFocus",
        "SetActiveWindow",
        "GetForegroundWindow",
        "SetCursorPos",
        "GetCursorPos",
        "SendInput",
        "keybd_event",
        "mouse_event",
        "SendMessage",
        "PostMessage",
    ):
        assert forbidden not in source


def test_case_16_borderless_grant_requires_and_verifies_zero_border():
    library = FakeWgcLibrary(access_state=1)
    provider = wgc_provider(library)
    source = Path("native/windows_graphics_capture_helper.cpp").read_text(
        encoding="utf-8"
    )

    assert provider.prepare_borderless_access() is True
    assert provider.access_status == "allowed"
    compact_source = "".join(source.split())
    assert (
        "GraphicsCaptureAccess::RequestAccessAsync("
        "GraphicsCaptureAccessKind::Borderless).get()"
    ) in compact_source
    assert "session.IsBorderRequired(false);" in source
    assert "if (session.IsBorderRequired())" in source


def test_case_17_denied_borderless_access_fails_closed_without_capture():
    library = FakeWgcLibrary(access_state=2)
    provider = wgc_provider(library)

    assert provider.prepare_borderless_access() is False
    assert provider.access_status == "denied"
    assert provider.capture(404) is None
    assert provider.last_failure_stage == "borderless_access_unavailable"
    assert library.capture_calls == []
