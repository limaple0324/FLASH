"""Direct contracts for FU-LOCAL-20260828-003 without creating a Tk root."""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import flash_sync_v02 as appmod


SOURCE = Path(__file__).with_name("flash_sync_v02.py")
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
APP = next(node for node in TREE.body
           if isinstance(node, ast.ClassDef) and node.name == "FlashSyncApp")
METHODS = {node.name: node for node in APP.body if isinstance(node, ast.FunctionDef)}


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Harness:
    window_size_values = appmod.FlashSyncApp.window_size_values
    poll_timed_click = appmod.FlashSyncApp.poll_timed_click
    fire_timed_click = appmod.FlashSyncApp.fire_timed_click
    send_timed_click_once = appmod.FlashSyncApp.send_timed_click_once
    timed_click_target_failure = appmod.FlashSyncApp.timed_click_target_failure
    clear_timed_click_overlay = appmod.FlashSyncApp.clear_timed_click_overlay
    show_timed_click_point = appmod.FlashSyncApp.show_timed_click_point
    game_time_ms_to_text = appmod.FlashSyncApp.game_time_ms_to_text
    update_estimated_game_time_label = appmod.FlashSyncApp.update_estimated_game_time_label
    poll_game_time_tick = appmod.FlashSyncApp.poll_game_time_tick
    on_close = appmod.FlashSyncApp.on_close


class FixedSizeAndToolbarTests(unittest.TestCase):
    def test_restore_client_size_is_always_900_by_572(self):
        self.assertEqual(Harness().window_size_values(), (900, 572))

    def test_toolbar_uses_short_time_and_clockbar_keeps_full_time(self):
        app = Harness()
        app.game_time_text = Value("")
        app.clock_bar = MagicMock()
        app.estimated_game_time_text = lambda: "12:08:59.329"
        app.estimated_game_time_ms = lambda: ((12 * 60 + 8) * 60 + 59) * 1000 + 329
        app.update_estimated_game_time_label()
        self.assertEqual(app.game_time_text.get(), "08:59:329")
        app.clock_bar.update.assert_called_once_with("12:08:59.329")
        app.estimated_game_time_text = lambda: None
        app.estimated_game_time_ms = lambda: None
        app.update_estimated_game_time_label()
        self.assertEqual(app.game_time_text.get(), "尚未校正")


class ShutdownLifecycleTests(unittest.TestCase):
    def test_closing_pending_tick_clears_id_without_poll_update_or_reschedule(self):
        app = Harness()
        app.closing_app = True
        app.game_time_tick_after_id = "pending-tick"
        app.poll_game_clock_acquisition = MagicMock()
        app.update_estimated_game_time_label = MagicMock()
        app.schedule_game_time_tick = MagicMock()

        app.poll_game_time_tick()

        self.assertIsNone(app.game_time_tick_after_id)
        app.poll_game_clock_acquisition.assert_not_called()
        app.update_estimated_game_time_label.assert_not_called()
        app.schedule_game_time_tick.assert_not_called()

    def test_clock_label_update_skips_closing_destroyed_and_missing_bar(self):
        app = Harness()
        app.closing_app = True
        app.game_time_text = Value("")
        app.estimated_game_time_ms = MagicMock(return_value=1_000)
        app.clock_bar = MagicMock()
        app.update_estimated_game_time_label()
        app.estimated_game_time_ms.assert_not_called()
        app.clock_bar.update.assert_not_called()

        app.closing_app = False
        app.clock_bar._destroyed = True
        app.update_estimated_game_time_label()
        app.clock_bar.update.assert_not_called()
        app.clock_bar = None
        app.update_estimated_game_time_label()

    def test_clockbar_update_stops_after_destroy_or_missing_tk_window(self):
        bar = appmod.ClockBar.__new__(appmod.ClockBar)
        bar._destroyed = False
        bar.value = "尚未校正"
        bar.window = MagicMock()
        bar.update_floating_status = MagicMock()
        bar.destroy()
        bar.update("01:02:03.004")
        bar.update_floating_status.assert_not_called()

        detached = appmod.ClockBar.__new__(appmod.ClockBar)
        detached._destroyed = False
        detached.value = "尚未校正"
        detached.window = SimpleNamespace(winfo_exists=lambda: False)
        detached.update_floating_status = MagicMock()
        detached.update("01:02:03.004")
        self.assertTrue(detached._destroyed)
        detached.update_floating_status.assert_not_called()

    def test_on_close_cancels_game_tick_before_destroying_clock_bar(self):
        events = []
        app = Harness()
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
        app.game_time_tick_after_id = "pending-tick"
        app.after_cancel = lambda timer_id: events.append(("cancel", timer_id))
        app.clock_bar = SimpleNamespace(destroy=lambda: events.append(("destroy", "clock")))
        app.close_notification_bar = MagicMock()
        app.clear_restore_fishing_overlay = MagicMock()
        app.auto_game_time = Value(True)
        app.game_time_auto_after_id = None
        app.timed_click_after_id = None
        app.auto_resize_after_id = None
        app.launch_wait_after_id = None
        app.disconnect_detect_enabled = Value(False)
        app.disconnect_detect_after_id = None
        app.relogin_auto_enabled = Value(False)
        app.relogin_after_ids = {}
        app.relogin_resume_groups = set()
        app.stop_autoclick = MagicMock()
        app.hotkey_after_id = None
        app.groups = []
        app.uninstall_mouse_wheel_hook = MagicMock()
        app.remove_tray_icon = MagicMock()
        app.floating_status_window = None
        app.events = SimpleNamespace(put=MagicMock())
        app.destroy = MagicMock()

        app.on_close()

        self.assertEqual(events, [("cancel", "pending-tick"), ("destroy", "clock")])
        self.assertIsNone(app.game_time_tick_after_id)
        self.assertIsNone(app.clock_bar)

    def test_visible_ui_has_only_approved_time_size_and_timed_controls(self):
        build = ast.unparse(METHODS["_build_ui"])
        self.assertNotIn("game_time_source_text", build)
        for forbidden in ("UTC+8", "PID", "HWND", "持續校正", "提前ms", "連點：", "取主窗尺寸"):
            self.assertNotIn(forbidden, build)
        for removed_var in ("timed_click_lead_ms_text", "timed_click_repeat_count_text",
                            "timed_click_repeat_interval_ms_text", "window_size_width_text",
                            "window_size_height_text"):
            self.assertNotIn(removed_var, build)
        for required in ("目標時間：", "設定按鈕位置", "顯示定位", "啟用定時", "取消",
                         "固定客戶區：900×572", "套用目前組", "套用全部 Flash", "新視窗自動套用"):
            self.assertIn(required, build)


class TimedClickTests(unittest.TestCase):
    def make_app(self):
        app = Harness()
        app.timed_click_enabled = Value(True)
        app.timed_click_fired = False
        app.timed_click_after_id = "old"
        app.timed_click_target_text = Value("08:59:329")
        app.timed_click_status_text = Value("")
        app.parse_target_time_ms = lambda _text: 539_329
        app.schedule_timed_click_poll = MagicMock()
        app.fire_timed_click = MagicMock()
        app.write_log = MagicMock()
        return app

    def test_first_click_cannot_trigger_before_target_and_triggers_at_target(self):
        app = self.make_app()
        app.estimated_game_time_ms = lambda: 539_328
        app.poll_timed_click()
        app.fire_timed_click.assert_not_called()
        app.schedule_timed_click_poll.assert_called_once()
        app.schedule_timed_click_poll.reset_mock()
        app.estimated_game_time_ms = lambda: 539_329
        app.poll_timed_click()
        app.fire_timed_click.assert_called_once_with(539_329, 539_329)
        app.schedule_timed_click_poll.assert_not_called()

    def test_invalid_clock_disarms_current_schedule_without_click_or_retry(self):
        app = self.make_app()
        app.estimated_game_time_ms = lambda: None
        app.poll_timed_click()
        self.assertFalse(app.timed_click_enabled.get())
        app.fire_timed_click.assert_not_called()
        app.schedule_timed_click_poll.assert_not_called()
        self.assertIn("時鐘失效", app.timed_click_status_text.get())

    def test_fire_uses_exact_three_safe_delays_and_disarms(self):
        app = Harness()
        app.timed_click_hwnd = 11
        app.timed_click_point = (20, 30)
        app.timed_click_fired = False
        app.timed_click_enabled = Value(True)
        app.timed_click_status_text = Value("")
        app.write_log = MagicMock()
        app.send_timed_click_once = MagicMock()
        scheduled = []
        app.after = lambda delay, callback: scheduled.append((delay, callback))
        app.fire_timed_click(539_329, 539_329)
        self.assertEqual([delay for delay, _callback in scheduled], [0, 50, 100])
        self.assertTrue(app.timed_click_fired)
        self.assertFalse(app.timed_click_enabled.get())
        for _delay, callback in scheduled:
            callback()
        self.assertEqual(app.send_timed_click_once.call_count, 3)

    def test_each_actual_click_revalidates_window_visibility_minimize_and_coordinate(self):
        app = Harness()
        app.timed_click_hwnd = 11
        app.timed_click_point = (20, 30)
        app.write_log = MagicMock()
        app.after = MagicMock()
        native = MagicMock()
        native.IsWindow.return_value = True
        native.IsWindowVisible.return_value = True
        native.IsIconic.return_value = False
        with patch.object(appmod, "user32", native), \
                patch.object(appmod, "get_client_size", return_value=(900, 572)), \
                patch.object(appmod, "child_at_client_point", return_value=(11, 20, 30)):
            self.assertTrue(app.send_timed_click_once(11, (20, 30)))
            self.assertGreaterEqual(native.IsWindow.call_count, 1)
            native.PostMessageW.reset_mock()
            native.IsWindowVisible.return_value = False
            self.assertFalse(app.send_timed_click_once(11, (20, 30)))
            native.PostMessageW.assert_not_called()
            native.IsWindowVisible.return_value = True
            native.IsIconic.return_value = True
            self.assertFalse(app.send_timed_click_once(11, (20, 30)))
            native.PostMessageW.assert_not_called()
            native.IsIconic.return_value = False
            self.assertFalse(app.send_timed_click_once(11, (900, 30)))
            native.PostMessageW.assert_not_called()

    def test_display_location_contains_no_input_or_guessing_calls(self):
        text = ast.unparse(METHODS["show_timed_click_point"])
        for forbidden in ("PostMessage", "mouse_event", "send_timed_click_once",
                          "get_cursor_pos", "get_window_under_cursor"):
            self.assertNotIn(forbidden, text)
        self.assertIn("ClientToScreen", text)
        self.assertIn("timed_click_target_failure", text)
        self.assertIn("3000", text)

    def test_display_location_strictly_fails_when_client_to_screen_returns_false(self):
        app = Harness()
        app.timed_click_overlay_windows = []
        app.timed_click_hwnd = 11
        app.timed_click_point = (20, 30)
        app.timed_click_status_text = Value("")
        app.timed_click_target_failure = lambda: None
        app.write_log = MagicMock()
        app.after = MagicMock()
        native = MagicMock()
        native.ClientToScreen.return_value = False
        top_level = MagicMock()
        with patch.object(appmod, "user32", native), \
                patch.object(appmod.tk, "Toplevel", top_level), \
                patch.object(appmod, "enter_flash_window_dpi_context", return_value=object()), \
                patch.object(appmod, "leave_flash_window_dpi_context") as leave, \
                patch.object(appmod.messagebox, "showwarning") as warning:
            self.assertFalse(app.show_timed_click_point())
        native.ClientToScreen.assert_called_once()
        leave.assert_called_once()
        top_level.assert_not_called()
        self.assertEqual(app.timed_click_overlay_windows, [])
        self.assertIn("無法換算", app.timed_click_status_text.get())
        warning.assert_called_once_with("無法顯示定位", "無法換算目前保存座標。")
        native.PostMessageW.assert_not_called()
        native.mouse_event.assert_not_called()


if __name__ == "__main__":
    unittest.main()
