"""Direct lifecycle and Tk-boundary tests for FU-LOCAL-20260830-004."""
from __future__ import annotations

import ast
import copy
import ctypes
import queue
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import flash_sync_v02 as appmod
import v02_wheel_hook as wheel


BASE_COMMIT = "6310f4f3537c590d265530ee2a534d791802335e"


class FakeNative:
    def __init__(self, *, install_ok=True, message_error=False):
        self.install_ok = install_ok
        self.message_error = message_error
        self.messages = queue.Queue()
        self.callback = None
        self.install_threads = []
        self.unhook_threads = []
        self.install_count = 0
        self.unhook_count = 0
        self.call_next_count = 0
        self.queue_ready = False

    def make_callback(self, callback):
        self.callback = callback
        return callback

    def install(self, callback):
        self.install_threads.append(threading.get_ident())
        self.install_count += 1
        return object() if self.install_ok else None

    def call_next(self, _n_code, _w_param, _l_param):
        self.call_next_count += 1
        return 0

    def current_thread_id(self):
        return threading.get_ident()

    def ensure_message_queue(self):
        self.queue_ready = True

    def get_message(self):
        if self.message_error:
            raise OSError("synthetic message pump failure")
        item = self.messages.get(timeout=2)
        if item == "quit":
            return 0
        x, y, delta, timestamp = item
        info = wheel.MSLLHOOKSTRUCT()
        info.pt.x = x
        info.pt.y = y
        info.mouseData = (delta & 0xFFFF) << 16
        info.time = timestamp
        self.callback(wheel.HC_ACTION, wheel.WM_MOUSEWHEEL, ctypes.addressof(info))
        return 1

    def dispatch_message(self):
        return None

    def post_quit(self, _thread_id):
        if not self.queue_ready:
            return False
        self.messages.put("quit")
        return True

    def unhook(self, _hook):
        self.unhook_threads.append(threading.get_ident())
        self.unhook_count += 1
        return True

    def emit(self, x=10, y=20, delta=120, timestamp=30):
        self.messages.put((x, y, delta, timestamp))


class BlockingCallbackNative(FakeNative):
    def __init__(self):
        super().__init__()
        self.callback_entered = threading.Event()
        self.callback_release = threading.Event()

    def call_next(self, _n_code, _w_param, _l_param):
        self.callback_entered.set()
        self.callback_release.wait(2)
        return 0


class FailedUnhookNative(FakeNative):
    def unhook(self, _hook):
        self.unhook_threads.append(threading.get_ident())
        self.unhook_count += 1
        return False


class FlakyUnhookNative(FakeNative):
    def unhook(self, _hook):
        self.unhook_threads.append(threading.get_ident())
        self.unhook_count += 1
        return self.unhook_count >= 3


class DelayedThreadIdNative(FakeNative):
    def __init__(self):
        super().__init__()
        self.thread_id_entered = threading.Event()
        self.thread_id_release = threading.Event()
        self.delay_once = True

    def current_thread_id(self):
        if self.delay_once:
            self.delay_once = False
            self.thread_id_entered.set()
            self.thread_id_release.wait(2)
        return super().current_thread_id()


class DelayedInstallNative(FakeNative):
    def __init__(self):
        super().__init__()
        self.install_entered = threading.Event()
        self.install_release = threading.Event()
        self.delay_once = True

    def install(self, callback):
        self.install_threads.append(threading.get_ident())
        self.install_count += 1
        if self.delay_once:
            self.delay_once = False
            self.install_entered.set()
            self.install_release.wait(2)
        return object()


class CloseScheduler:
    def __init__(self):
        self.next_id = 1
        self.callbacks = {}
        self.cancelled = []

    def after(self, delay, callback):
        timer_id = f"close-{self.next_id}"
        self.next_id += 1
        self.callbacks[timer_id] = (delay, callback)
        return timer_id

    def after_cancel(self, timer_id):
        self.cancelled.append(timer_id)
        self.callbacks.pop(timer_id, None)

    def run(self, timer_id):
        _delay, callback = self.callbacks.pop(timer_id)
        callback()


class WheelHookServiceTests(unittest.TestCase):
    def test_msg_abi_matches_win32_pointer_width_layouts(self):
        self.assertEqual(
            wheel.MSG_ABI_BY_POINTER_SIZE,
            {
                4: (32, {"hwnd": 0, "message": 4, "wParam": 8, "lParam": 12,
                         "time": 16, "pt": 20, "lPrivate": 28}),
                8: (48, {"hwnd": 0, "message": 8, "wParam": 16, "lParam": 24,
                         "time": 32, "pt": 36, "lPrivate": 44}),
            },
        )
        pointer_size = ctypes.sizeof(ctypes.c_void_p)
        expected_size, expected_offsets = wheel.MSG_ABI_BY_POINTER_SIZE[pointer_size]
        self.assertEqual(ctypes.sizeof(wheel.MSG), expected_size)
        self.assertEqual(
            {name: getattr(wheel.MSG, name).offset for name in expected_offsets},
            expected_offsets,
        )

    def test_callback_has_zero_tk_ui_traversal_logging_or_after(self):
        source = Path(wheel.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        service = next(node for node in tree.body
                       if isinstance(node, ast.ClassDef) and node.name == "WheelHookService")
        method = next(node for node in service.body
                      if isinstance(node, ast.FunctionDef) and node.name == "_make_native_callback")
        callback = next(node for node in ast.walk(method)
                        if isinstance(node, ast.FunctionDef) and node.name == "callback")
        text = ast.unparse(callback)
        for forbidden in ("tk", "after", "write_log", "groups", "IsWindow", "point_in_client"):
            self.assertNotIn(forbidden, text)
        for required in ("info.pt.x", "info.pt.y", "delta", "info.time", "generation"):
            self.assertIn(required, text)

    def test_owner_thread_installs_and_unhooks_and_start_stop_are_idempotent(self):
        native = FakeNative()
        service = wheel.WheelHookService(native)
        self.assertTrue(service.start())
        self.assertTrue(service.start())
        self.assertEqual(native.install_count, 1)
        self.assertTrue(native.queue_ready)
        self.assertTrue(service.stop())
        self.assertTrue(service.stop())
        self.assertEqual(native.unhook_count, 1)
        self.assertEqual(native.install_threads, native.unhook_threads)
        self.assertNotEqual(native.install_threads[0], threading.get_ident())
        self.assertFalse(service.active)
        self.assertIsNone(service._callback)

    def test_callback_payload_and_generation_make_stale_events_discardable(self):
        native = FakeNative()
        service = wheel.WheelHookService(native)
        self.assertTrue(service.start())
        first_generation = service.generation
        native.emit(-4, 7, -120, 456)
        event = service.events.get(timeout=1)
        self.assertEqual(event, wheel.WheelHookEvent(-4, 7, -120, 456, first_generation))
        self.assertTrue(service.stop())
        self.assertTrue(service.start())
        self.assertNotEqual(first_generation, service.generation)
        self.assertNotEqual(event.generation, service.generation)
        self.assertTrue(service.stop())

    def test_install_failure_fails_closed_without_unhook_or_leaked_callback(self):
        native = FakeNative(install_ok=False)
        service = wheel.WheelHookService(native)
        self.assertFalse(service.start())
        deadline = time.monotonic() + 1
        while service._callback is not None and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertFalse(service.active)
        self.assertIsNone(service._callback)
        self.assertEqual(native.unhook_count, 0)
        self.assertIn("啟動失敗", service.errors.get(timeout=1).message)

    def test_closing_inflight_callback_uses_bounded_join_and_retains_callback_until_unhook(self):
        native = BlockingCallbackNative()
        service = wheel.WheelHookService(native)
        self.assertTrue(service.start())
        generation = service.generation
        native.emit()
        self.assertTrue(native.callback_entered.wait(1))
        started = time.monotonic()
        self.assertFalse(service.stop(timeout=0.01))
        self.assertLess(time.monotonic() - started, 0.25)
        self.assertTrue(service.closing)
        self.assertGreater(service.generation, generation)
        self.assertIsNotNone(service._callback)
        native.callback_release.set()
        self.assertTrue(service.stop(timeout=1))
        self.assertIsNone(service._callback)
        self.assertEqual(native.unhook_count, 1)

    def test_worker_exception_is_reported_only_through_error_queue(self):
        native = FakeNative(message_error=True)
        service = wheel.WheelHookService(native)
        service.start()
        error = service.errors.get(timeout=1)
        self.assertIn("synthetic message pump failure", error.message)
        self.assertTrue(service.stop(timeout=1))

    def test_failed_unhook_retains_callback_and_prevents_second_hook(self):
        native = FailedUnhookNative()
        service = wheel.WheelHookService(native)
        self.assertTrue(service.start())
        self.assertFalse(service.stop(timeout=1))
        self.assertIsNotNone(service._callback)
        self.assertIsNotNone(service._hook)
        self.assertFalse(service.start())
        self.assertEqual(native.install_count, 1)
        self.assertEqual(native.unhook_count, 3)
        self.assertEqual(native.install_threads * 3, native.unhook_threads)

    def test_owner_thread_bounded_unhook_retry_can_recover_and_clear_callback(self):
        native = FlakyUnhookNative()
        service = wheel.WheelHookService(native)
        self.assertTrue(service.start())
        self.assertTrue(service.stop(timeout=1))
        self.assertEqual(native.unhook_count, 3)
        self.assertEqual(native.install_threads * 3, native.unhook_threads)
        self.assertIsNone(service._hook)
        self.assertIsNone(service._callback)

    def test_start_timeout_before_thread_id_cancels_install_and_allows_restart(self):
        native = DelayedThreadIdNative()
        service = wheel.WheelHookService(native, install_timeout=0.02)
        self.assertFalse(service.start())
        self.assertTrue(native.thread_id_entered.is_set())
        self.assertEqual(native.install_count, 0)
        native.thread_id_release.set()
        self.assertTrue(service.stop(timeout=1))
        self.assertEqual(native.install_count, 0)
        self.assertIsNone(service._hook)
        self.assertIsNone(service._callback)
        self.assertTrue(service.start())
        self.assertEqual(native.install_count, 1)
        self.assertTrue(service.stop(timeout=1))

    def test_start_timeout_during_install_unhooks_on_owner_before_restart(self):
        native = DelayedInstallNative()
        service = wheel.WheelHookService(native, install_timeout=0.02)
        self.assertFalse(service.start())
        self.assertTrue(native.install_entered.is_set())
        native.install_release.set()
        self.assertTrue(service.stop(timeout=1))
        self.assertEqual(native.install_count, 1)
        self.assertEqual(native.unhook_count, 1)
        self.assertEqual(native.install_threads, native.unhook_threads)
        self.assertIsNone(service._hook)
        self.assertIsNone(service._callback)
        # FakeNative reuses one Queue across synthetic thread IDs; real Win32
        # destroys the old thread queue, so discard its already-posted WM_QUIT.
        while not native.messages.empty():
            native.messages.get_nowait()
        self.assertTrue(service.start())
        self.assertEqual(native.install_count, 2)
        self.assertTrue(service.stop(timeout=1))

    def test_one_hundred_lifecycle_cycles_have_no_hook_or_callback_leak(self):
        native = FakeNative()
        service = wheel.WheelHookService(native)
        stale = []
        for cycle in range(100):
            self.assertTrue(service.start(), cycle)
            active_generation = service.generation
            native.emit(cycle, cycle + 1, 120, cycle + 2)
            event = service.events.get(timeout=1)
            self.assertEqual(event.generation, active_generation)
            self.assertTrue(service.stop(timeout=1), cycle)
            stale.append(event)
            self.assertIsNone(service._callback)
            self.assertIsNone(service._hook)
            self.assertEqual(service._thread_id, 0)
            self.assertTrue(all(item.generation != service.generation for item in stale[-1:]))
        self.assertEqual(native.install_count, 100)
        self.assertEqual(native.unhook_count, 100)


class Value:
    def __init__(self, value):
        self.value = value
    def get(self):
        return self.value


class AppBoundaryHarness:
    any_group_running = appmod.FlashSyncApp.any_group_running
    start_sync = appmod.FlashSyncApp.start_sync
    stop_sync = appmod.FlashSyncApp.stop_sync
    poll_wheel_hook_queues = appmod.FlashSyncApp.poll_wheel_hook_queues
    poll_worker_errors = appmod.FlashSyncApp.poll_worker_errors
    poll_input = appmod.FlashSyncApp.poll_input
    run_sync_action = appmod.FlashSyncApp.run_sync_action
    on_close = appmod.FlashSyncApp.on_close
    install_mouse_wheel_hook = appmod.FlashSyncApp.install_mouse_wheel_hook
    uninstall_mouse_wheel_hook = appmod.FlashSyncApp.uninstall_mouse_wheel_hook


class AppWheelBoundaryTests(unittest.TestCase):
    def test_two_groups_reference_one_hook_first_start_last_stop(self):
        app = AppBoundaryHarness()
        app.groups = [
            appmod.SyncGroup("一", master_hwnd=1, followers=[2]),
            appmod.SyncGroup("二", master_hwnd=3, followers=[4]),
        ]
        app.active_group_index = Value(0)
        app.pending_sync_start_groups = set()
        app.current_group = lambda: app.groups[0]
        app.bind_existing_launch_windows_for_sync = MagicMock()
        app.prune_group_followers = MagicMock()
        app.refresh_followers = MagicMock()
        app.enabled_buttons = lambda _group: []
        app.keyboard_sync_inputs = lambda _group: []
        app.is_button_down = MagicMock(return_value=False)
        app.update_sync_state_text = MagicMock()
        app.write_log = MagicMock()
        app.install_mouse_wheel_hook = MagicMock(return_value=True)
        app.uninstall_mouse_wheel_hook = MagicMock(return_value=True)
        app.poll_after_id = None
        app.schedule_poll = MagicMock()
        app.after_cancel = MagicMock()
        with patch.object(appmod.user32, "IsWindow", return_value=True):
            app.start_sync(0)
            app.start_sync(1)
            app.install_mouse_wheel_hook.assert_called_once_with()
            app.stop_sync(0)
            app.uninstall_mouse_wheel_hook.assert_not_called()
            app.stop_sync(1)
            app.uninstall_mouse_wheel_hook.assert_called_once_with()

    def test_tk_poller_discards_stale_generation_and_keeps_filtering_on_tk_side(self):
        events = queue.SimpleQueue()
        errors = queue.SimpleQueue()
        events.put(wheel.WheelHookEvent(1, 2, 120, 10, 3))
        events.put(wheel.WheelHookEvent(4, 5, -120, 11, 4))
        errors.put(wheel.WheelHookError(4, "synthetic worker error"))
        app = AppBoundaryHarness()
        app.wheel_hook = SimpleNamespace(generation=4, events=events, errors=errors)
        app.queue_wheel_sync_at_point = MagicMock()
        app.write_log = MagicMock()
        app.poll_wheel_hook_queues()
        app.queue_wheel_sync_at_point.assert_called_once_with(4, 5, -120)
        app.write_log.assert_called_once_with("synthetic worker error")

    def test_flood_drain_is_bounded_and_click_keyboard_still_run_same_tick(self):
        events = queue.SimpleQueue()
        for index in range(300):
            events.put(wheel.WheelHookEvent(index, index, 120, index, 7))
        app = AppBoundaryHarness()
        app.groups = [SimpleNamespace(running=True)]
        app.poll_after_id = "scheduled"
        app.wheel_hook = SimpleNamespace(
            generation=7,
            events=events,
            errors=queue.SimpleQueue(),
        )
        app.worker_errors = queue.SimpleQueue()
        app.queue_wheel_sync_at_point = MagicMock()
        app.write_log = MagicMock()
        app.check_mouse_buttons = MagicMock()
        app.check_keyboard_keys = MagicMock()
        app.schedule_poll = MagicMock()
        app.poll_input()
        self.assertEqual(app.queue_wheel_sync_at_point.call_count, 256)
        app.check_mouse_buttons.assert_called_once_with()
        app.check_keyboard_keys.assert_called_once_with()
        app.schedule_poll.assert_called_once_with()
        remaining = 0
        while True:
            try:
                events.get_nowait()
                remaining += 1
            except queue.Empty:
                break
        self.assertEqual(remaining, 44)

    def test_install_failure_does_not_disable_click_keyboard_polling(self):
        source = ast.parse(Path(appmod.__file__).read_text(encoding="utf-8"))
        app_class = next(node for node in source.body
                         if isinstance(node, ast.ClassDef) and node.name == "FlashSyncApp")
        poll = next(node for node in app_class.body
                    if isinstance(node, ast.FunctionDef) and node.name == "poll_input")
        calls = {node.func.attr for node in ast.walk(poll)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertTrue({"poll_wheel_hook_queues", "check_mouse_buttons", "check_keyboard_keys"} <= calls)

    def test_sync_callback_exception_uses_ui_safe_worker_error_queue(self):
        app = AppBoundaryHarness()
        app.worker_errors = queue.SimpleQueue()
        app.run_sync_action(0, lambda: (_ for _ in ()).throw(ValueError("synthetic replay")))
        self.assertEqual(app.worker_errors.get_nowait(), "同步失敗：synthetic replay")

    def make_close_app(self):
        app = AppBoundaryHarness()
        app._close_attempt_in_progress = False
        app._close_attempt_token = 0
        app._close_retry_after_id = None
        app._close_wheel_attempts = 0
        app._close_wheel_preflight_complete = False
        app.closing_app = False
        scheduler = CloseScheduler()
        app.close_scheduler = scheduler
        app.after = scheduler.after
        app.after_cancel = scheduler.after_cancel
        app.write_log = MagicMock()
        app.game_clock_source = SimpleNamespace(shutdown=MagicMock(), is_busy=lambda: False)
        app.close_tray_menu = MagicMock()
        app.tray_restore_poll_after_id = None
        app.main_geometry_save_after_id = None
        app.save_launch_config = MagicMock()
        app.cancel_capture_custom_input = MagicMock()
        app.cancel_capture_follower_click = MagicMock()
        app.clear_role_id_overlay = MagicMock()
        app.clear_game_time_overlay = MagicMock()
        app.clear_timed_click_overlay = MagicMock()
        app.game_time_tick_after_id = None
        app.clock_bar = None
        app.close_notification_bar = MagicMock()
        app.clear_restore_fishing_overlay = MagicMock()
        app.auto_game_time = SimpleNamespace(set=MagicMock())
        app.game_time_auto_after_id = None
        app.timed_click_after_id = None
        app.auto_resize_after_id = None
        app.launch_wait_after_id = None
        app.disconnect_detect_enabled = SimpleNamespace(set=MagicMock())
        app.disconnect_detect_after_id = None
        app.relogin_auto_enabled = SimpleNamespace(set=MagicMock())
        app.relogin_after_ids = {}
        app.relogin_resume_groups = set()
        app.stop_autoclick = MagicMock()
        app.hotkey_after_id = None
        app.groups = []
        app.remove_tray_icon = MagicMock()
        app.floating_status_window = None
        app.events = SimpleNamespace(put=MagicMock())
        app.destroy = MagicMock()
        return app

    def test_close_has_bounded_fail_closed_hook_shutdown(self):
        app = self.make_close_app()
        app.uninstall_mouse_wheel_hook = MagicMock(return_value=False)

        with patch.object(appmod.messagebox, "showerror") as showerror:
            app.on_close()
            self.assertFalse(app.closing_app)
            self.assertTrue(app._close_attempt_in_progress)
            self.assertEqual(len(app.close_scheduler.callbacks), 1)
            first_id = next(iter(app.close_scheduler.callbacks))
            app.on_close()
            self.assertEqual(list(app.close_scheduler.callbacks), [first_id])
            app.close_scheduler.run(first_id)
            second_id = next(iter(app.close_scheduler.callbacks))
            app.close_scheduler.run(second_id)

        app.destroy.assert_not_called()
        self.assertFalse(app.closing_app)
        self.assertFalse(app._close_attempt_in_progress)
        self.assertEqual(app.close_scheduler.callbacks, {})
        app.game_clock_source.shutdown.assert_not_called()
        app.close_tray_menu.assert_not_called()
        self.assertTrue(any("本次關閉已取消" in call.args[0] for call in app.write_log.call_args_list))
        showerror.assert_called_once()

    def test_real_service_inflight_close_refuses_destroy_then_retry_succeeds(self):
        native = BlockingCallbackNative()
        service = wheel.WheelHookService(native)
        self.assertTrue(service.start())
        native.emit()
        self.assertTrue(native.callback_entered.wait(1))
        app = self.make_close_app()
        app.wheel_hook = service

        with patch.object(appmod.messagebox, "showerror") as showerror:
            app.on_close()
            first_id = next(iter(app.close_scheduler.callbacks))
            _delay, stale_callback = app.close_scheduler.callbacks[first_id]
            app.on_close()
            self.assertEqual(list(app.close_scheduler.callbacks), [first_id])
            app.close_scheduler.run(first_id)
            second_id = next(iter(app.close_scheduler.callbacks))
            app.close_scheduler.run(second_id)
            app.destroy.assert_not_called()
            self.assertFalse(app.closing_app)
            self.assertTrue(service._thread.is_alive())
            self.assertIsNotNone(service._callback)
            showerror.assert_called_once()

            native.callback_release.set()
            deadline = time.monotonic() + 1
            while service._callback is not None and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertIsNone(service._callback)
            stale_callback()
            app.destroy.assert_not_called()
            app.on_close()

        app.destroy.assert_called_once_with()
        self.assertFalse(service.active)

    def test_formal_busy_shutdown_blocks_sync_restart_and_never_reenters_preflight(self):
        native = FakeNative()
        service = wheel.WheelHookService(native)
        self.assertTrue(service.start())
        app = self.make_close_app()
        app.wheel_hook = service
        busy = [True]
        app.game_clock_source = SimpleNamespace(
            shutdown=MagicMock(),
            is_busy=lambda: busy[0],
        )
        group = appmod.SyncGroup("關閉測試", master_hwnd=1, followers=[2])
        app.groups = [group]

        app.on_close()

        self.assertTrue(app.closing_app)
        self.assertTrue(app._close_attempt_in_progress)
        self.assertTrue(app._close_wheel_preflight_complete)
        self.assertEqual(native.install_count, 1)
        app.start_sync(0)
        self.assertFalse(group.running)
        self.assertEqual(native.install_count, 1)
        self.assertFalse(app.install_mouse_wheel_hook())
        self.assertEqual(native.install_count, 1)

        retry_id = next(iter(app.close_scheduler.callbacks))
        busy[0] = False
        app.groups = []
        app.close_scheduler.run(retry_id)

        app.destroy.assert_called_once_with()
        self.assertEqual(native.install_count, 1)
        self.assertEqual(native.unhook_count, 1)


class ExactAstBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(appmod.__file__).resolve().parent.parent
        try:
            before_source = subprocess.check_output(
                ["git", "show", f"{BASE_COMMIT}:outputs/flash_sync_v02.py"],
                cwd=root, encoding="utf-8", stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            raise unittest.SkipTest(
                "historical outputs tree is intentionally absent from sanitized closure"
            )
        before_tree = ast.parse(before_source)
        after_tree = ast.parse(Path(appmod.__file__).read_text(encoding="utf-8"))
        before_app = next(node for node in before_tree.body
                          if isinstance(node, ast.ClassDef) and node.name == "FlashSyncApp")
        after_app = next(node for node in after_tree.body
                         if isinstance(node, ast.ClassDef) and node.name == "FlashSyncApp")
        cls.before = {node.name: node for node in before_app.body if isinstance(node, ast.FunctionDef)}
        cls.after = {node.name: node for node in after_app.body if isinstance(node, ast.FunctionDef)}

    def assert_ast_equal(self, left, right):
        self.assertEqual(ast.dump(left, include_attributes=False),
                         ast.dump(right, include_attributes=False))

    def test_unchanged_sync_and_forbidden_automation_gates_are_exact(self):
        exact = {"stop_sync", "check_mouse_buttons", "check_keyboard_keys"}
        exact.update(name for name in self.before
                     if "reconnect" in name or "relogin" in name or "restore_fishing" in name)
        self.assertTrue(exact)
        for name in exact:
            with self.subTest(name=name):
                self.assert_ast_equal(self.before[name], self.after[name])

    def test_start_sync_only_adds_first_group_hook_reference(self):
        normalized = copy.deepcopy(self.after["start_sync"])
        close_guard = next(node for node in normalized.body
                           if isinstance(node, ast.If)
                           and "_close_attempt_in_progress" in ast.unparse(node.test))
        normalized.body.remove(close_guard)
        normalized.body = [
            node for node in normalized.body
            if not (isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name)
                            and target.id == "first_running_group" for target in node.targets))
        ]
        hook_if = next(node for node in normalized.body
                       if isinstance(node, ast.If)
                       and isinstance(node.test, ast.Name)
                       and node.test.id == "first_running_group")
        index = normalized.body.index(hook_if)
        normalized.body[index:index + 1] = hook_if.body
        self.assert_ast_equal(self.before["start_sync"], normalized)

    def test_poll_only_adds_tk_queue_drains(self):
        normalized = copy.deepcopy(self.after["poll_input"])
        try_node = next(node for node in normalized.body if isinstance(node, ast.Try))
        expected_calls = {"poll_wheel_hook_queues", "poll_worker_errors"}
        try_node.body = [
            node for node in try_node.body
            if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Attribute)
                    and node.value.func.attr in expected_calls)
        ]
        self.assert_ast_equal(self.before["poll_input"], normalized)

    def test_worker_only_replaces_tk_after_with_error_queue(self):
        normalized = copy.deepcopy(self.after["_start_worker"])
        handler = next(node for node in ast.walk(normalized)
                       if isinstance(node, ast.ExceptHandler) and node.name == "exc")
        handler.body = ast.parse(
            'self.after(0, lambda e=exc: self.write_log(f"同步失敗：{e}"))'
        ).body
        self.assert_ast_equal(self.before["_start_worker"], normalized)

    def test_close_only_adds_bounded_hook_stop_before_existing_shutdown(self):
        normalized = copy.deepcopy(self.after["on_close"])
        closing_index = next(
            index for index, node in enumerate(normalized.body)
            if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Attribute)
            and node.targets[0].attr == "closing_app"
            and isinstance(node.value, ast.Constant) and node.value.value is True
        )
        normalized.body = normalized.body[closing_index:]
        before_busy = next(node for node in self.before["on_close"].body
                           if isinstance(node, ast.If)
                           and isinstance(node.test, ast.Call)
                           and isinstance(node.test.func, ast.Attribute)
                           and node.test.func.attr == "is_busy")
        busy_index = next(index for index, node in enumerate(normalized.body)
                          if isinstance(node, ast.If)
                          and isinstance(node.test, ast.Call)
                          and isinstance(node.test.func, ast.Attribute)
                          and node.test.func.attr == "is_busy")
        normalized.body[busy_index] = copy.deepcopy(before_busy)
        events_index = next(index for index, node in enumerate(normalized.body)
                            if isinstance(node, ast.Expr)
                            and isinstance(node.value, ast.Call)
                            and isinstance(node.value.func, ast.Attribute)
                            and node.value.func.attr == "put")
        destroy = next(node for node in normalized.body
                       if isinstance(node, ast.Expr)
                       and isinstance(node.value, ast.Call)
                       and isinstance(node.value.func, ast.Attribute)
                       and node.value.func.attr == "destroy")
        normalized.body = normalized.body[:events_index + 1] + [destroy]
        normalized.args = copy.deepcopy(self.before["on_close"].args)
        self.assert_ast_equal(self.before["on_close"], normalized)


if __name__ == "__main__":
    unittest.main()
