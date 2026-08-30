"""Synthetic-only notification contracts. No live app, game, memory or config IO.

Model tests exercise data and viewport math. Wiring tests compile actual selected
class methods. Native-contract tests reuse the existing Win32/Tk test doubles;
real Windows HWND/layout evidence is a separate isolated-fixture acceptance gate.
"""
from __future__ import annotations

import ast
import copy
from pathlib import Path
import subprocess
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import v02_notification_bar as notification
from v02_game_clock_bar import ClockBar
from test_flash_sync_status_window import Panel, Value, Group, NAMESPACE, TREE, APP_NODE

BASELINE = "8e3faf06e432889d831e13059f5df69deb26449b"
ROOT = Path(__file__).resolve().parent
NEW_METHODS = {
    "create_notification_bar", "notification_bar_settings", "load_notification_bar_settings",
    "refresh_notification_context", "reset_notification_context", "notify_disconnect_window",
    "set_notification_phase", "close_notification_bar",
}
ADDITIONS = {
    "__init__": ["self.pending_notification_bar_settings = {}", "self.notification_bar = None",
                 "self.notification_model = NotificationModel()", "self.create_notification_bar()"],
    "load_launch_config": ["self.load_notification_bar_settings(app_state.get('notification_window', {}))"],
    "refresh_group_ui": ["self.refresh_notification_context()"],
    "reset_disconnect_detected_names": ["self.reset_notification_context()"],
    "remember_disconnect_detected_window": ["self.notify_disconnect_window(hwnd, name)"],
    "scan_disconnect_once": ["self.set_notification_phase('single_scanning')",
                             "self.set_notification_phase('single_done')",
                             "self.set_notification_phase('single_done')"],
    "poll_disconnect_detect": ["self.set_notification_phase('continuous')"],
    "on_close": ["self.close_notification_bar()"],
    "floating_status_contains_point": [
        "notification_bar = getattr(self, 'notification_bar', None)",
        "if notification_bar is not None and notification_bar.floating_status_contains_point(screen_x, screen_y):\n    return True",
    ],
}


def strip_notification_wiring(method):
    """Remove only exact approved statements, never entire protected methods."""
    allowed = [ast.dump(ast.parse(text).body[0]) for text in ADDITIONS.get(method.name, [])]
    class Strip(ast.NodeTransformer):
        def visit(self, node):
            if isinstance(node, ast.stmt) and ast.dump(node) in allowed:
                return None
            return super().visit(node)

        def visit_Dict(self, node):
            if method.name == "save_launch_config":
                pairs = [(key, value) for key, value in zip(node.keys, node.values)
                         if not (isinstance(key, ast.Constant) and key.value == "notification_window"
                                 and ast.dump(value) == ast.dump(ast.parse(
                                     "self.notification_bar_settings()", mode="eval").body))]
                node.keys, node.values = [p[0] for p in pairs], [p[1] for p in pairs]
            return self.generic_visit(node)
    return Strip().visit(copy.deepcopy(method))


def baseline_file(name):
    return subprocess.check_output(["git", "show", f"{BASELINE}:outputs/{name}"],
                                   cwd=ROOT, encoding="utf-8")


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.group = SimpleNamespace(name="合成測試組")
        self.model = notification.NotificationModel()
        self.model.sync_context(self.group, False)

    def test_no_event_text_is_empty_for_every_phase(self):
        for phase in notification.NotificationModel.PHASES:
            with self.subTest(phase=phase):
                model = notification.NotificationModel()
                model.sync_context(self.group, phase != "disabled")
                model.set_phase(phase)
                self.assertEqual(model.text(), "")

    def test_only_remember_reveals_existing_format_then_reset_hides(self):
        self.model.sync_context(self.group, True)
        self.model.set_phase("continuous")
        self.model.remember(101, "合成事件")
        self.assertEqual(
            self.model.text(),
            "合成測試組｜持續偵測｜斷線偵測紀錄 1 個：合成測試組｜合成事件",
        )
        self.model.reset()
        self.assertEqual(self.model.text(), "")

    def test_group_change_without_event_is_empty(self):
        self.model.remember(101, "舊事件")
        self.model.sync_context(SimpleNamespace(name="其他合成組"), False)
        self.assertFalse(self.model.events)
        self.assertEqual(self.model.text(), "")

    def test_enable_change_without_event_is_empty(self):
        self.model.sync_context(self.group, True)
        self.model.remember(101, "舊事件")
        self.model.sync_context(self.group, False)
        self.assertFalse(self.model.events)
        self.assertEqual(self.model.text(), "")

    def test_restart_without_event_is_empty(self):
        restarted = notification.NotificationModel()
        restarted.sync_context(SimpleNamespace(name="重啟合成組"), True)
        self.assertEqual(restarted.text(), "")

    def test_hwnd_identity_deduplicates_repeat_but_not_same_names(self):
        for hwnd in (101, 101, 102):
            self.model.remember(hwnd, "合成同名視窗")
        self.assertEqual(len(self.model.events), 2)
        self.assertEqual(self.model.text().count("合成同名視窗"), 2)
        self.assertIn("紀錄 2 個", self.model.text())

    def test_complete_list_and_very_long_names_are_never_summarized(self):
        for hwnd in range(10):
            self.model.remember(hwnd + 1, f"合成視窗{hwnd}頭" + "長" * 10000 + f"尾{hwnd}")
        text = self.model.text()
        for _hwnd, (group, name) in self.model.events.items():
            self.assertIn(f"{group}｜{name}", text)
        self.assertNotIn("+7", text)

    def test_false_enabled_poll_preserves_single_scan_result(self):
        self.model.set_phase("single_done")
        self.model.remember(101, "合成單次事件")
        snapshot = self.model.snapshot()
        for _ in range(20):
            self.model.sync_context(self.group, False)
        self.assertEqual(self.model.snapshot(), snapshot)

    def test_switch_away_back_and_same_index_object_replacement_clear(self):
        for replacement in (SimpleNamespace(name="其他合成組"),
                            SimpleNamespace(name="合成測試組"), self.group):
            self.model.remember(101, "舊合成事件")
            epoch = self.model.epoch
            self.model.sync_context(replacement, False)
            self.assertGreater(self.model.epoch, epoch)
            self.assertFalse(self.model.events)
            self.assertIs(self.model.context, replacement)

    def test_reset_and_disable_clear_without_modifying_group(self):
        self.model.sync_context(self.group, True)
        self.model.remember(101, "合成事件")
        self.model.reset()
        self.assertFalse(self.model.events)
        self.assertEqual(self.model.phase, "waiting")
        self.model.remember(101, "合成事件")
        self.model.sync_context(self.group, False)
        self.assertFalse(self.model.events)
        self.assertEqual(self.model.phase, "disabled")
        self.assertEqual(vars(self.group), {"name": "合成測試組"})

    def test_continuous_phase_and_duplicate_hits_keep_stable_snapshot(self):
        self.model.sync_context(self.group, True)
        self.model.set_phase("continuous")
        self.model.remember(101, "合成事件")
        snapshot = self.model.snapshot()
        for _ in range(100):
            self.model.set_phase("continuous")
            self.model.remember(101, "合成事件")
        self.assertEqual(self.model.snapshot(), snapshot)
        self.assertIn("斷線偵測紀錄", self.model.text())

    def test_linebreaks_are_spaces_not_hidden_tail(self):
        self.model.remember(101, "頭\r\n中\t尾")
        self.assertIn("頭  中 尾", self.model.text())
        self.assertEqual(self.model.events[101][1], "頭\r\n中\t尾")

    def test_invalid_phase_is_rejected(self):
        with self.assertRaises(ValueError):
            self.model.set_phase("all_healthy")


class MarqueeTests(unittest.TestCase):
    def test_short_text_does_not_move(self):
        ticker = notification.Marquee()
        ticker.reset("短", len, 50)
        for _ in range(200):
            ticker.advance()
        self.assertEqual(ticker.offset, 0)
        self.assertEqual(list(ticker.visible()), [(0, 0, "短")])

    def test_long_text_reaches_tail_then_restarts_after_hold(self):
        ticker = notification.Marquee()
        ticker.reset("123456789", len, 3)
        for _ in range(ticker.HOLD_FRAMES + 3):
            ticker.advance()
        self.assertEqual(ticker.offset, 6)
        self.assertEqual(list(ticker.visible())[0][2][-3:], "789")
        for _ in range(ticker.HOLD_FRAMES + 1):
            ticker.advance()
        self.assertEqual(ticker.offset, 0)

    def test_huge_text_chunk_reconstruction_and_last_character_access(self):
        ticker = notification.Marquee()
        text = "合成測試：" + "超長名😀" * 30000 + "最後名稱終點"
        measured = []
        def measure(chunk):
            measured.append(len(chunk))
            return len(chunk) * 20
        ticker.reset(text, measure, 400)
        self.assertEqual("".join(ticker.chunks), text)
        self.assertLessEqual(max(measured), 128)
        ticker.offset = ticker.total - ticker.viewport
        visible = list(ticker.visible())
        self.assertTrue(visible[-1][2].endswith("最後名稱終點"))
        self.assertLessEqual(len(visible), 2)
        self.assertTrue(all(abs(x) <= 2560 for _index, x, _chunk in visible))

    def test_all_chunks_are_reachable_without_gaps(self):
        ticker = notification.Marquee()
        text = "".join(f"合成事件{i:04d}；" for i in range(100))
        ticker.reset(text, len, 70)
        seen = set()
        for offset in range(ticker.total - ticker.viewport + 1):
            ticker.offset = offset
            seen.update(index for index, _x, _text in ticker.visible())
        self.assertEqual(seen, set(range(len(ticker.chunks))))
        self.assertEqual("".join(ticker.chunks), text)


MONITORS = [
    {"id": "DISPLAY1", "bounds": (0, 0, 1920, 1080), "taskbar": (1032, 48), "primary": True},
    {"id": "DISPLAY2", "bounds": (-3840, -523, 0, 1637), "taskbar": (1589, 48), "primary": False},
]


class InitialLayoutTests(unittest.TestCase):
    def test_real_occupied_rects_are_not_moved_and_first_gap_is_free(self):
        occupied = [(100, 1035, 700, 1077), (720, 1035, 920, 1077)]
        saved = list(occupied)
        state = notification.initial_notification_settings(MONITORS, occupied, 320)
        self.assertEqual(state, {"monitor_id": "DISPLAY1", "local_x": 928})
        self.assertEqual(occupied, saved)

    def test_full_primary_uses_negative_monitor_local_coordinates(self):
        state = notification.initial_notification_settings(
            MONITORS, [(0, 1032, 1920, 1080), (-3840, 1592, -3240, 1634)], 320)
        self.assertEqual(state, {"monitor_id": "DISPLAY2", "local_x": 608})

    def test_narrow_gap_uses_own_width_delta(self):
        monitor = [{"id": "narrow", "bounds": (0, 0, 800, 600),
                    "taskbar": (568, 32), "primary": True}]
        state = notification.initial_notification_settings(monitor, [(200, 568, 800, 600)], 320)
        self.assertEqual(state, {"monitor_id": "narrow", "local_x": 8, "width_delta": -174})

    def test_actual_y_intersection_not_saved_x_alone_determines_occupancy(self):
        state = notification.initial_notification_settings(MONITORS, [(0, 100, 1920, 200)], 320)
        self.assertEqual(state, {"monitor_id": "DISPLAY1", "local_x": 8})


SCAN_METHODS = {
    "current_group_index_value", "reset_disconnect_detected_names", "ensure_disconnect_group_context",
    "remember_disconnect_detected_window", "disconnect_detected_summary", "set_disconnect_scan_status",
    "scan_disconnect_once", "poll_disconnect_detect", "toggle_disconnect_detect",
}
scan_node = ast.ClassDef(name="ScanHarness", bases=[ast.Name(id="Panel", ctx=ast.Load())],
                         keywords=[], decorator_list=[], body=[copy.deepcopy(node) for node in APP_NODE.body
                         if isinstance(node, ast.FunctionDef) and node.name in SCAN_METHODS])
SCAN_NAMESPACE = {"__name__": __name__, "Panel": Panel, "user32": MagicMock()}
exec(compile(ast.fix_missing_locations(ast.Module(body=[scan_node], type_ignores=[])),
             str(ROOT / "flash_sync_v02.py"), "exec"), SCAN_NAMESPACE)
ScanHarness = SCAN_NAMESPACE["ScanHarness"]


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.app = ScanHarness()
        self.app.groups = [Group(name="合成測試組")]
        self.app.active_group_index = Value(0)
        self.app.disconnect_detect_enabled = Value(True)
        self.app.disconnect_restore_minimized = Value(True)
        self.app.disconnect_detect_after_id = None
        self.app.notification_bar = None
        self.app.after_cancel = MagicMock()
        self.app.pending_notification_bar_settings = {}

    def test_detector_entry_points_are_fixed_false_and_never_schedule(self):
        self.app.scan_disconnect_once(False)
        self.assertFalse(self.app.disconnect_detect_enabled.get())
        self.app.disconnect_detect_enabled.set(True)
        self.app.poll_disconnect_detect()
        self.assertFalse(self.app.disconnect_detect_enabled.get())
        self.assertIsNone(self.app.disconnect_detect_after_id)

    def test_toggle_cancels_only_stale_detector_timer_and_stays_false(self):
        self.app.disconnect_detect_after_id = "synthetic-detector"
        self.app.toggle_disconnect_detect()
        self.assertFalse(self.app.disconnect_detect_enabled.get())
        self.app.after_cancel.assert_called_once_with("synthetic-detector")
        self.assertIsNone(self.app.disconnect_detect_after_id)

    def test_notification_settings_roundtrip_verbatim_without_live_panel(self):
        state = {"font_size": 17, "width": 777, "future_key": {"keep": True}}
        self.app.load_notification_bar_settings(state)
        self.assertIsNot(self.app.pending_notification_bar_settings, state)
        self.assertEqual(self.app.notification_bar_settings(), state)
        self.app.load_notification_bar_settings("malformed")
        self.assertEqual(self.app.notification_bar_settings(), {})

    def test_create_and_close_never_construct_or_destroy_notification_bar(self):
        self.app.notification_bar = MagicMock()
        old = self.app.notification_bar
        self.app.create_notification_bar()
        self.assertIsNone(self.app.notification_bar)
        old.destroy.assert_not_called()
        self.app.notification_bar = MagicMock()
        old = self.app.notification_bar
        self.app.close_notification_bar()
        old.destroy.assert_not_called()
        self.assertIsNone(self.app.notification_bar)


class BarContractTests(unittest.TestCase):
    def make_bar(self):
        bar = notification.NotificationBar.__new__(notification.NotificationBar)
        bar.app = SimpleNamespace(refresh_notification_context=MagicMock())
        bar.model = notification.NotificationModel()
        bar.model.sync_context(SimpleNamespace(name="合成組"), False)
        bar.rpg_font_family = "synthetic-font"
        bar.floating_status_font_size = 14
        bar.floating_status_layout_cache = None
        bar.floating_status_applied_size = (420, 42)
        bar.render_key = None
        bar.marquee = notification.Marquee()
        bar.canvas = MagicMock()
        bar.canvas.create_text.side_effect = range(1, 100000)
        bar.text_items = {}
        bar.destroyed = False
        bar.timer_ids = set()
        bar.window = MagicMock()
        bar._tk_after = MagicMock(side_effect=lambda _delay, _callback: f"timer-{len(bar.timer_ids)}")
        bar.place_floating_status_default = MagicMock()
        bar.floating_status_geometry = MagicMock(return_value=(420, 42, 8, 1035))
        bar.layout = {"key": ("synthetic-font", 14), "font": SimpleNamespace(measure=len),
                      "content_width": 320, "text": bar.model.text()}
        bar.applied_layout_key = bar.layout["key"]
        return bar

    def test_long_frame_changes_only_canvas_no_native_geometry_or_stable_key_reset(self):
        bar = self.make_bar()
        bar.model.set_phase("continuous")
        bar.model.remember(101, "合成長名" * 500)
        bar.layout["text"] = bar.model.text()
        with patch.object(bar, "measure_floating_status_layout", return_value=bar.layout):
            bar.animate()
            initial_key = bar.render_key
            for _ in range(100):
                bar.model.set_phase("continuous")
                bar.animate()
        self.assertEqual(bar.render_key, initial_key)
        self.assertGreater(bar.marquee.offset, 0)
        bar.place_floating_status_default.assert_not_called()
        bar.floating_status_geometry.assert_not_called()
        self.assertEqual(sum(call.args == ("notification_text",) for call in bar.canvas.delete.call_args_list), 1)

    def test_font_or_width_change_resets_own_viewport_content_does_not_grow_bar(self):
        bar = self.make_bar()
        bar.model.remember(101, "合成長名" * 1000)
        bar.layout["text"] = bar.model.text()
        with patch.object(bar, "measure_floating_status_layout", return_value=bar.layout):
            bar.update_floating_status()
            first_key = bar.render_key
            bar.marquee.offset = 100
            bar.floating_status_geometry.return_value = (460, 42, 8, 1035)
            bar.update_floating_status()
            self.assertEqual(bar.marquee.viewport, 398)
            self.assertEqual(bar.marquee.offset, 0)
            self.assertNotEqual(bar.render_key, first_key)
            bar.layout["key"] = ("synthetic-font", 16)
            bar.update_floating_status()
        self.assertEqual(bar.layout["content_width"], 320)

    def test_real_font_measure_fit_short_disabled_and_caps_long_viewport(self):
        bar = self.make_bar()
        bar.model.context.name = "160"
        measure = MagicMock(side_effect=lambda text: len(text) * 14)
        font = SimpleNamespace(measure=measure)
        with patch.object(notification.tkfont, "Font", return_value=font) as constructor:
            compact = bar.measure_floating_status_layout()
            self.assertEqual(compact["text"], "")
            self.assertEqual(compact["content_width"], 24)
            self.assertLess(compact["content_width"] + 38, 200)
            calls = measure.call_count
            self.assertIs(bar.measure_floating_status_layout(), compact)
            self.assertEqual(measure.call_count, calls)
            bar.model.remember(101, "合成超長視窗" * 1000)
            long = bar.measure_floating_status_layout()
            self.assertEqual(long["content_width"], 320)
            constructor.assert_called_once()
            self.assertTrue(all(len(call.args[0]) <= 128 for call in measure.call_args_list))
            bar.floating_status_font_size = 16
            bar.measure_floating_status_layout()
            self.assertEqual(constructor.call_count, 2)

    def test_all_owned_timers_cancel_and_destroy_is_idempotent(self):
        bar = self.make_bar()
        callbacks = []
        bar._tk_after.side_effect = lambda _delay, callback: callbacks.append(callback) or f"timer-{len(callbacks)}"
        fired = MagicMock()
        bar.schedule_timer(40, fired)
        bar.schedule_timer(1000, fired)
        callbacks[0]()
        fired.assert_called_once()
        self.assertEqual(bar.timer_ids, {"timer-2"})
        bar.destroy()
        bar.destroy()
        bar.window.after_cancel.assert_called_once_with("timer-2")
        bar.window.destroy.assert_called_once()
        callbacks[1]()
        fired.assert_called_once()
        self.assertFalse(bar.timer_ids)

    def test_constructor_shared_native_methods_independent_states_and_dark_palette(self):
        app = Panel()
        app.clock_bar = SimpleNamespace(window=object())
        app.floating_status_window = object()
        app.rpg_font_family = "synthetic"
        app.floating_status_monitors = lambda: MONITORS
        app.save_launch_config = MagicMock()
        app.restore_from_tray = MagicMock()
        fake_tk = SimpleNamespace(Toplevel=MagicMock(), Menu=MagicMock(), Button=MagicMock(), Canvas=MagicMock())
        fake_tk.Toplevel.return_value.after.return_value = "owned-timer"
        with patch.object(notification, "tk", fake_tk), \
                patch.object(notification.NotificationBar, "update_floating_status"), \
                patch.object(Panel, "keep_floating_status_topmost"), \
                patch.dict(NAMESPACE, {"user32": MagicMock()}):
            bar = notification.NotificationBar(app, {"monitor_id": "DISPLAY2", "local_x": 123},
                                               notification.NotificationModel())
        self.assertIsNot(bar.window, app.floating_status_window)
        self.assertIsNot(bar.window, app.clock_bar.window)
        for name in ClockBar.SHARED_METHODS:
            if name != "keep_floating_status_topmost":
                self.assertIs(getattr(bar, name).__func__, getattr(Panel, name), name)
        self.assertIsNone(bar.notification_bar)
        self.assertIsNone(bar.clock_bar)
        self.assertEqual(bar.floating_status_local_x, 123)
        self.assertEqual(fake_tk.Canvas.call_args.kwargs["bg"], "#121317")
        self.assertEqual(len(fake_tk.Menu.return_value.add_command.call_args_list), 5)
        self.assertIn((("-toolwindow", True), {}), fake_tk.Toplevel.return_value.attributes.call_args_list)
        self.assertEqual(bar.timer_ids, {"owned-timer"})
        self.assertEqual(bar.model.text(), "")
        fake_tk.Toplevel.return_value.deiconify.assert_called_once_with()
        fake_tk.Button.return_value.place.assert_called_once_with(
            relx=1.0, x=-38, y=3, width=35, relheight=1.0, height=-6)

    def test_removed_notification_is_not_part_of_scoped_input_exclusion(self):
        app = Panel()
        app.clock_bar = None
        app.floating_status_window = None
        app.floating_status_menu_thread_id = 0
        app.notification_bar = SimpleNamespace(floating_status_contains_point=MagicMock(return_value=True))
        self.assertFalse(app.floating_status_contains_point(-1000, 1592))
        app.notification_bar.floating_status_contains_point.assert_not_called()

    def test_notification_drag_uses_original_xy_during_timer_then_docks_on_release(self):
        bar = self.make_bar()
        bar.floating_drag_offset = (10, 5)
        bar.floating_drag_position = (-500, -200)
        bar.floating_drag_pointer = (-490, -195)
        with patch.object(bar, "measure_floating_status_layout", return_value=bar.layout):
            for _ in range(3):
                bar.animate()
        self.assertEqual(bar.floating_drag_position, (-500, -200))
        bar.place_floating_status_default.assert_not_called()
        # Original shared geometry, not a copied implementation, is used by keep-topmost.
        bar.floating_status_monitor = lambda **kwargs: MONITORS[1]
        bar.floating_status_settings = lambda: {"width_delta": 0, "local_x": 8}
        bar.floating_status_width_range = MethodType(Panel.floating_status_width_range, bar)
        self.assertEqual(Panel.floating_status_geometry(bar, bar.layout), (358, 42, -500, -200))

    def test_first_construction_measures_compact_width_and_uses_real_old_rects(self):
        app = Panel()
        app.clock_bar = SimpleNamespace(floating_status_native_rect=MagicMock(return_value=(150, 1035, 300, 1077)))
        app.floating_status_native_rect = MagicMock(return_value=(0, 1035, 142, 1077))
        app.rpg_font_family = "synthetic"
        app.floating_status_monitors = lambda: MONITORS
        app.save_launch_config = MagicMock()
        app.restore_from_tray = MagicMock()
        model = notification.NotificationModel()
        model.sync_context(SimpleNamespace(name="160"), False)
        fake_tk = SimpleNamespace(Toplevel=MagicMock(), Menu=MagicMock(), Button=MagicMock(), Canvas=MagicMock())
        with patch.object(notification, "tk", fake_tk), \
                patch.object(notification.tkfont, "Font", return_value=SimpleNamespace(measure=lambda text: len(text) * 14)), \
                patch.object(notification.NotificationBar, "update_floating_status"), \
                patch.object(Panel, "keep_floating_status_topmost"), \
                patch.dict(NAMESPACE, {"user32": MagicMock()}):
            bar = notification.NotificationBar(app, {}, model)
        app.floating_status_native_rect.assert_called_once()
        app.clock_bar.floating_status_native_rect.assert_called_once()
        self.assertEqual(bar.floating_status_local_x, 308)
        self.assertLess(bar.measure_floating_status_layout()["content_width"] + 38, 200)


class ProtectedSourceTests(unittest.TestCase):
    def test_runtime_has_no_notification_import_creation_or_hit_test(self):
        imports = {node.module for node in TREE.body if isinstance(node, ast.ImportFrom)}
        self.assertNotIn("v02_notification_bar", imports)
        methods = {node.name: node for node in APP_NODE.body if isinstance(node, ast.FunctionDef)}
        init_dump = ast.dump(methods["__init__"])
        self.assertNotIn("create_notification_bar", init_dump)
        self.assertNotIn("schedule_disconnect_detect", init_dump)
        self.assertNotIn("notification_bar", ast.dump(methods["floating_status_contains_point"]))
        build_strings = {node.value for node in ast.walk(methods["_build_ui"])
                         if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        self.assertNotIn("斷線偵測", build_strings)
        self.assertNotIn("單次掃描", build_strings)
        self.assertNotIn("打開縮小並掃描", build_strings)

    @unittest.skipUnless((ROOT / "SOURCE_MANIFEST.json").exists() is False,
                         "historical outputs tree is intentionally absent from sanitized closure")
    def test_relogin_and_reconnect_methods_match_fixed_batch_baseline_exactly(self):
        baseline = ast.parse(subprocess.check_output(
            ["git", "show", "10fa86d0beb82ce60a6747172be881876fc9562e:outputs/flash_sync_v02.py"],
            cwd=ROOT, encoding="utf-8",
        ))
        baseline_app = next(node for node in baseline.body
                            if isinstance(node, ast.ClassDef) and node.name == "FlashSyncApp")
        before = {node.name: node for node in baseline_app.body
                  if isinstance(node, ast.FunctionDef)}
        after = {node.name: node for node in APP_NODE.body if isinstance(node, ast.FunctionDef)}
        protected = {
            name for name in before
            if "relogin" in name or "reconnect" in name or "restore_fishing" in name
        }
        self.assertTrue(protected)
        self.assertEqual(protected, protected & after.keys())
        for name in protected:
            self.assertEqual(ast.dump(before[name], include_attributes=False),
                             ast.dump(after[name], include_attributes=False), name)

    @unittest.skipUnless((ROOT / "SOURCE_MANIFEST.json").exists() is False,
                         "historical outputs tree is intentionally absent from sanitized closure")
    def test_clock_model_and_reader_bytes_unchanged(self):
        for name in ("v02_game_clock.py", "v02_game_clock_reader.py"):
            self.assertEqual(baseline_file(name), (ROOT / name).read_text(encoding="utf-8"), name)

    @unittest.skipUnless((ROOT / "SOURCE_MANIFEST.json").exists() is False,
                         "historical outputs tree is intentionally absent from sanitized closure")
    def test_clock_bar_only_exact_palette_changes_no_model_tick_or_layout_changes(self):
        old = baseline_file("v02_game_clock_bar.py")
        expected = old.replace('self.window.configure(bg="#121317")', 'self.window.configure(bg="#ffffff")')
        expected = expected.replace('bg="#263040", fg="#cad6ea", activebackground="#404c60",\n            activeforeground="#ffffff"',
                                    'bg="#ffffff", fg="#000000", activebackground="#e5e5e5",\n            activeforeground="#000000"')
        expected = expected.replace('bg="#121317", fg="#ffffff", takefocus=False)',
                                    'bg="#ffffff", fg="#000000", takefocus=False)')
        replacements = (
            (
                "        self.app = app\n        self.clock_bar = None",
                "        self.app = app\n        self._destroyed = False\n        self.clock_bar = None",
            ),
            (
                "    def update(self, text):\n"
                "        value = text if text else \"尚未校正\"\n"
                "        if self.value != value:\n"
                "            self.value = value\n"
                "            self.update_floating_status()\n",
                "    def update(self, text):\n"
                "        if getattr(self, \"_destroyed\", False):\n"
                "            return\n"
                "        window = getattr(self, \"window\", None)\n"
                "        if window is not None:\n"
                "            try:\n"
                "                if not window.winfo_exists():\n"
                "                    self._destroyed = True\n"
                "                    return\n"
                "            except tk.TclError:\n"
                "                self._destroyed = True\n"
                "                return\n"
                "        value = text if text else \"尚未校正\"\n"
                "        if self.value != value:\n"
                "            self.value = value\n"
                "            self.update_floating_status()\n",
            ),
            (
                "    def destroy(self):\n        self.window.destroy()\n",
                "    def destroy(self):\n"
                "        if self._destroyed:\n"
                "            return\n"
                "        self._destroyed = True\n"
                "        self.window.destroy()\n",
            ),
        )
        for before, after in replacements:
            self.assertEqual(expected.count(before), 1)
            expected = expected.replace(before, after, 1)
        self.assertEqual(ast.dump(ast.parse(expected)),
                         ast.dump(ast.parse((ROOT / "v02_game_clock_bar.py").read_text(encoding="utf-8"))))


if __name__ == "__main__":
    unittest.main(verbosity=2)
