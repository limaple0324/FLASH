"""Deterministic status-panel tests: no Tk root, hook, game, or real config access."""
from __future__ import annotations

import ast
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import queue
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


SOURCE = Path(__file__).with_name("flash_sync_v02.py")
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
APP_NODE = next(node for node in TREE.body
                if isinstance(node, ast.ClassDef) and node.name == "FlashSyncApp")
INDEX_NODE = next(node for node in TREE.body
                  if isinstance(node, ast.AnnAssign)
                  and getattr(node.target, "id", "") == "MODULE_API_METHOD_INDEX_V02")
STATUS_METHODS = set(ast.literal_eval(INDEX_NODE.value)["StatusWindowAPI"])
METHODS = STATUS_METHODS | {
    "load_launch_config", "save_launch_config", "current_group", "running_groups",
    "int_from_text", "queue_wheel_sync_at_point", "check_mouse_buttons",
}
CONSTANTS = {
    "STATUS_PANEL_HEIGHT", "STATUS_PANEL_LEGACY_WIDTH", "STATUS_PANEL_DEFAULT_FONT_SIZE",
    "STATUS_PANEL_GEAR_WIDTH", "DEFAULT_FLASH_CLIENT_WIDTH", "DEFAULT_FLASH_CLIENT_HEIGHT",
    "DEFAULT_KEYBOARD_SYNC_KEYS", "FISHING_ROUTES", "GA_ROOT", "SWP_NOACTIVATE",
}
CLASSES = {"SyncGroup", "LaunchEntry", "MouseMirrorEvent", "MouseWheelMirrorEvent"}
nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
for node in TREE.body:
    if isinstance(node, ast.Assign) and any(getattr(t, "id", "") in CONSTANTS for t in node.targets):
        nodes.append(node)
    elif isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") in CONSTANTS:
        nodes.append(node)
    elif isinstance(node, ast.ClassDef) and node.name in CLASSES:
        nodes.append(node)
nodes.append(ast.ClassDef(name="PanelHarness", bases=[], keywords=[], decorator_list=[],
                          body=[node for node in APP_NODE.body
                                if isinstance(node, ast.FunctionDef) and node.name in METHODS]))
NAMESPACE = {
    "__name__": __name__, "ctypes": ctypes, "wintypes": wintypes, "RECT": wintypes.RECT,
    "dataclass": dataclass, "field": field, "json": json, "os": os,
    "tk": SimpleNamespace(), "tkfont": SimpleNamespace(), "user32": MagicMock(),
    "EnumWindowsProc": lambda callback: callback,
    "current_machine_id": lambda: "isolated-test-machine",
}
exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
             str(SOURCE), "exec"), NAMESPACE)
Panel = NAMESPACE["PanelHarness"]
Group = NAMESPACE["SyncGroup"]
Entry = NAMESPACE["LaunchEntry"]


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeCanvas:
    def __init__(self, width=562):
        self.width_provider = lambda: width
        self.left = 0
        self.texts = []
        self.polygons = []
        self.region = (0, 0, width, 42)

    @property
    def width(self):
        return self.width_provider()

    def winfo_exists(self):
        return True

    def winfo_width(self):
        return self.width

    def canvasx(self, _x):
        return self.left

    def delete(self, _tag):
        self.texts = []
        self.polygons = []

    def create_text(self, *args, **kwargs):
        self.texts.append((args, kwargs))

    def create_polygon(self, *args, **kwargs):
        self.polygons.append((args, kwargs))

    def configure(self, **kwargs):
        self.region = kwargs["scrollregion"]

    def xview_moveto(self, fraction):
        # The production canvas uses one-pixel xscrollincrement, not fractional pixels.
        self.left = round(max(0, min(self.region[2] - self.width, fraction * self.region[2])))

    def xview_scroll(self, units, mode):
        assert mode == "units"
        self.xview_moveto((self.left + units) / self.region[2])


class StatusWindowTests(unittest.TestCase):
    def setUp(self):
        self.win32 = MagicMock()
        self.win32.IsWindow.side_effect = lambda hwnd: hwnd in {11, 12, 21}
        self.win32.GetAncestor.side_effect = lambda hwnd, _root: hwnd
        self.win32.GetWindowLongW.return_value = 0x00040000
        self.environment = patch.dict(NAMESPACE, {"user32": self.win32})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.app = Panel()
        self.app.groups = [Group(name="第1組"), Group(name="第2組", custom_key_display="F2")]
        self.app.active_group_index = Value(0)
        self.app.floating_status_font_size = 14
        self.app.floating_status_width_delta = 0
        self.app.floating_status_monitor_id = ""
        self.app.floating_status_local_x = 8
        self.app.floating_status_window = None
        self.app.floating_status_menu = None
        self.app.floating_status_menu_thread_id = 0
        self.app.floating_status_canvas = None
        self.app.floating_status_content_width = 0
        self.app.floating_status_render_key = None
        self.app.floating_status_layout_cache = None
        self.app.floating_status_applied_size = None
        self.app.floating_status_text = Value("")
        self.app.floating_master_text = Value("")
        self.app.floating_drag_offset = None
        self.app.floating_drag_position = None
        self.app.floating_drag_pointer = None
        self.app.floating_resize_state = None
        self.app.floating_drag_bindtag = "test-panel"
        self.app.floating_drag_class_bound = False
        self.app.rpg_font_family = "Microsoft JhengHei UI"
        self.app.rpg_panel = "#ead3a0"
        self.app.rpg_panel_light = "#fff0ca"
        self.app.rpg_ink = "#2b1a0a"
        self.app.rpg_border = "#80591f"
        self.app.rpg_button = "#edd08e"
        self.app.rpg_button_active = "#8a5a24"
        self.app.rpg_select_text = "#fff2cf"
        self.monitors = [
            {"id": "DISPLAY1", "bounds": (0, 0, 1920, 1080), "work": (0, 0, 1920, 1032),
             "taskbar": (1032, 48), "primary": True},
            {"id": "DISPLAY2", "bounds": (-3840, -523, 0, 1637), "work": (-3840, -523, 0, 1589),
             "taskbar": (1589, 48), "primary": False},
        ]
        self.app.floating_status_monitors = lambda: self.monitors
        self.app.save_launch_config = MagicMock()
        self.app.update_tray_tooltip = MagicMock()
        self.font_measure_calls = []
        def font_factory(**kwargs):
            size = -kwargs["size"]
            def measure(text):
                self.font_measure_calls.append((text, size))
                return len(text) * size
            return SimpleNamespace(measure=measure)
        self.font_patch = patch.dict(NAMESPACE, {"tkfont": SimpleNamespace(Font=font_factory)})
        self.font_patch.start()
        self.addCleanup(self.font_patch.stop)

    def prepare_window(self):
        window = MagicMock()
        window.winfo_exists.return_value = True
        window.winfo_x.return_value = 100
        window.winfo_id.return_value = 101
        self.app.floating_status_window = window
        self.native_rect = [100, 1035, 700, 1077]
        def get_rect(_hwnd, pointer):
            pointer._obj.left, pointer._obj.top, pointer._obj.right, pointer._obj.bottom = self.native_rect
            return True
        def set_position(_hwnd, _after, x, y, width, height, flags):
            old_width = self.native_rect[2] - self.native_rect[0]
            old_height = self.native_rect[3] - self.native_rect[1]
            if flags & 2:
                x, y = self.native_rect[:2]
            if flags & 1:
                width, height = old_width, old_height
            self.native_rect[:] = [x, y, x + width, y + height]
            return True
        self.win32.GetWindowRect.side_effect = get_rect
        self.win32.SetWindowPos.side_effect = set_position
        return window

    def prepare_canvas(self):
        canvas = FakeCanvas()
        canvas.width_provider = lambda: self.app.floating_status_geometry()[0] - 38
        self.app.floating_status_canvas = canvas
        return canvas

    def fit_width(self):
        return self.app.measure_floating_status_layout()["content_width"] + 38

    def test_formatter_exact_idle_and_dynamic_counts(self):
        group = self.app.groups[0]
        group.launch_entries = [Entry("a"), Entry("b"), Entry("c")]
        group.master_hwnd, group.followers = 11, [12, 999, 11]
        self.assertEqual(self.app.floating_status_group_line(group),
                         "第1組｜未同步　視窗：2/3")
        group.followers = []
        self.assertEqual(self.app.floating_status_group_line(group),
                         "第1組｜未同步　視窗：1/3")

    def test_formatter_exact_running_and_no_extra_labels(self):
        group = self.app.groups[1]
        group.running, group.master_hwnd = True, 21
        self.assertEqual(self.app.floating_status_group_line(group),
                         "第2組｜同步中　視窗：1/1")
        group.master_hwnd = None
        self.assertEqual(self.app.floating_status_group_line(group),
                         "第2組｜同步中　視窗：0/0")

    def test_formatter_sanitizes_line_breaks_without_shortening_long_names(self):
        group = self.app.groups[0]
        group.name = "長名稱" * 300 + "\r\n尾"
        group.custom_key_display = "F\n2"
        line = self.app.floating_status_group_line(group)
        self.assertTrue(line.startswith("長名稱" * 300 + "  尾｜"))
        self.assertNotIn("\n", line)
        self.assertNotIn("\r", line)
        self.assertEqual(group.custom_key_display, "F\n2")
        self.assertEqual(self.app.floating_status_hotkey_text(group), "F\n2")
        group.custom_key_display = ""
        self.assertEqual(self.app.floating_status_hotkey_text(group), "未設定")

    def test_selection_idle_current_and_running_only(self):
        self.app.active_group_index.set(1)
        self.assertEqual(self.app.floating_status_display_groups(), [self.app.groups[1]])
        self.app.groups[0].running = True
        self.assertEqual(self.app.floating_status_display_groups(), [self.app.groups[0]])
        self.app.groups[1].running = True
        self.assertEqual(self.app.floating_status_display_groups(), self.app.groups)

    def test_canvas_single_row_full_text_and_rounded_cards(self):
        canvas = self.prepare_canvas()
        for group in self.app.groups:
            group.running = True
        self.app.groups[0].name *= 100
        self.app.draw_floating_status_cards()
        self.assertEqual(len(canvas.texts), 2)
        self.assertEqual({args[1] for args, _kw in canvas.texts}, {21})
        self.assertTrue(all(kw["width"] == 0 for _args, kw in canvas.texts))
        self.assertEqual(canvas.texts[0][1]["text"], self.app.floating_status_group_line(self.app.groups[0]))
        self.assertGreater(canvas.texts[1][0][0], canvas.width)
        self.assertGreater(canvas.region[2], canvas.width)
        self.assertTrue(all(kw["smooth"] for _args, kw in canvas.polygons))
        self.assertEqual(canvas.region[3], 42)

    def test_wheel_overflow_scrolls_both_directions_and_clamps(self):
        canvas = self.prepare_canvas()
        self.app.groups[0].name *= 100
        self.app.draw_floating_status_cards()
        running = [group.running for group in self.app.groups]
        self.assertEqual(self.app.scroll_floating_status(SimpleNamespace(delta=-120)), "break")
        self.assertEqual(canvas.left, 24)
        self.app.scroll_floating_status(SimpleNamespace(delta=120))
        self.assertEqual(canvas.left, 0)
        self.app.scroll_floating_status(SimpleNamespace(delta=-12000000))
        self.assertEqual(canvas.left + canvas.width, canvas.region[2])
        self.assertEqual([group.running for group in self.app.groups], running)

    def test_no_overflow_or_zero_delta_never_scrolls(self):
        canvas = self.prepare_canvas()
        canvas.xview_scroll = MagicMock()
        self.app.floating_status_content_width = canvas.width
        self.app.scroll_floating_status(SimpleNamespace(delta=-120))
        self.app.floating_status_content_width += 1
        self.app.scroll_floating_status(SimpleNamespace(delta=0))
        canvas.xview_scroll.assert_not_called()

    def test_update_preserves_viewport_and_refreshes_changed_counts(self):
        canvas = self.prepare_canvas()
        self.app.groups[0].name *= 100
        self.app.update_floating_status()
        self.app.scroll_floating_status(SimpleNamespace(delta=-120))
        self.app.update_floating_status()
        self.assertEqual(canvas.left, 24)
        self.app.groups[0].master_hwnd = 11
        self.app.update_floating_status()
        self.assertIn("視窗：1/1", canvas.texts[0][1]["text"])
        self.assertEqual(canvas.left, 24)
        self.app.update_tray_tooltip.assert_called()

    def test_regular_timer_refreshes_lost_follower_without_manual_update(self):
        self.prepare_window()
        canvas = self.prepare_canvas()
        group = self.app.groups[0]
        group.master_hwnd, group.followers = 11, [12]
        group.launch_entries = [Entry("a"), Entry("b")]
        self.app.keep_floating_status_topmost()
        self.assertIn("視窗：2/2", canvas.texts[0][1]["text"])
        self.win32.IsWindow.side_effect = lambda hwnd: hwnd == 11
        for _ in range(3):
            self.app.keep_floating_status_topmost()
        self.assertIn("視窗：1/2", canvas.texts[0][1]["text"])
        self.assertIn("視窗：1/2", self.app.floating_status_text.get())
        self.assertEqual(self.app.floating_status_window.after.call_count, 4)

    def test_settings_clamp_and_malformed_fallback(self):
        defaults = {"font_size": 14, "width_delta": 0, "monitor_id": "", "local_x": 8}
        for state in ({}, [], "old", {"font_size": None, "width": "bad", "x": []},
                      {"font_size": float("inf"), "width": False, "x": float("nan")}):
            self.assertEqual(self.app.floating_status_settings(state), defaults)
        self.assertEqual(self.app.floating_status_settings({"font_size": -1, "width": 90000, "x": -9}),
                         {"font_size": 10, "width_delta": 1000, "monitor_id": "", "local_x": 0})
        self.assertEqual(self.app.floating_status_settings({"font_size": 999, "width": 1, "x": 999999}),
                         {"font_size": 20, "width_delta": -360, "monitor_id": "", "local_x": 100000})
        self.assertEqual(self.app.floating_status_settings({"width_delta": -90000, "monitor_id": []}),
                         {**defaults, "width_delta": -10000})
        self.app.floating_status_font_size = 20
        self.app.load_floating_status_settings(None)
        self.assertEqual(self.app.floating_status_settings(), defaults)

    def test_content_fit_uses_exact_shared_measurement_without_fixed_blank_width(self):
        canvas = self.prepare_canvas()
        layout = self.app.measure_floating_status_layout()
        width = self.app.floating_status_geometry(layout)[0]
        self.assertEqual(width, 3 + sum(layout["widths"]) + 6 * len(layout["lines"]) + 38)
        self.assertNotEqual(width, 600)
        measured = len(self.font_measure_calls)
        self.app.draw_floating_status_cards(layout)
        self.app.floating_status_geometry()
        self.assertEqual(len(self.font_measure_calls), measured)
        self.assertIs(canvas.texts[0][1]["font"], layout["font"])
        self.assertEqual(canvas.width, layout["content_width"])

    def test_fit_remeasures_font_name_state_counts_and_running_collection(self):
        group = self.app.groups[0]
        first = self.app.measure_floating_status_layout()
        group.name += "增加長組名"
        renamed = self.app.measure_floating_status_layout()
        self.assertGreater(renamed["content_width"], first["content_width"])
        group.running = True
        running = self.app.measure_floating_status_layout()
        self.assertIsNot(running, renamed)
        self.assertIn("同步中", running["lines"][0])
        group.launch_entries = [Entry(str(index)) for index in range(10)]
        counted = self.app.measure_floating_status_layout()
        self.assertGreater(counted["content_width"], running["content_width"])
        self.app.groups[1].running = True
        multiple = self.app.measure_floating_status_layout()
        self.assertEqual(len(multiple["lines"]), 2)
        self.assertGreater(multiple["content_width"], counted["content_width"])
        self.app.adjust_floating_status_font(1)
        self.assertGreater(self.app.measure_floating_status_layout()["content_width"], multiple["content_width"])

    def test_legacy_width_migrates_to_delta_and_new_delta_takes_precedence(self):
        for legacy_width, delta in ((600, 0), (800, 200), (400, -200)):
            self.app.load_floating_status_settings({"width": legacy_width, "x": 123, "font_size": 16})
            self.assertEqual(self.app.floating_status_width_delta, delta)
            self.assertEqual(self.app.floating_status_local_x, 123)
            self.assertEqual(self.app.floating_status_geometry()[0], self.fit_width() + delta)
        self.app.load_floating_status_settings({"width": 800, "width_delta": -40, "local_x": 90})
        self.assertEqual(self.app.floating_status_width_delta, -40)
        self.assertEqual(self.app.floating_status_local_x, 90)
        self.app.adjust_floating_status_width(40)
        self.assertEqual(self.app.floating_status_geometry()[0], self.fit_width())

    def test_manual_shrink_immediately_changes_overflowing_content_width(self):
        self.app.groups[0].name *= 500
        self.app.floating_status_monitor_id = "DISPLAY2"
        self.assertGreater(self.fit_width(), 20000)
        self.assertEqual(self.app.floating_status_geometry()[0], 3832)
        self.app.adjust_floating_status_width(-40)
        self.assertEqual(self.app.floating_status_geometry()[0], 3792)
        self.assertEqual(self.app.floating_status_width_delta, -40)
        settings = self.app.floating_status_settings()
        self.app.load_floating_status_settings(settings)
        self.assertEqual(self.app.floating_status_geometry()[0], 3792)
        self.app.floating_status_monitor_id = "DISPLAY1"
        self.assertEqual(self.app.floating_status_geometry()[0], 1872)
        self.app.reset_floating_status_size()
        self.assertEqual(self.app.floating_status_geometry()[0], 1912)
        self.assertEqual(self.app.floating_status_width_delta, 0)

    def test_upper_width_saturation_does_not_accumulate_reverse_dead_zone(self):
        for _ in range(80):
            self.app.adjust_floating_status_width(40)
        self.assertEqual(self.app.floating_status_geometry()[0], 1912)
        self.assertEqual(self.app.floating_status_width_delta, 1912 - self.fit_width())
        self.app.adjust_floating_status_width(-40)
        self.assertEqual(self.app.floating_status_geometry()[0], 1872)
        # Historical saturated settings are absorbed by the first reverse action too.
        self.app.floating_status_width_delta = 10000
        self.app.adjust_floating_status_width(-40)
        self.assertEqual(self.app.floating_status_geometry()[0], 1872)

    def test_lower_width_saturation_does_not_accumulate_reverse_dead_zone(self):
        for _ in range(80):
            self.app.adjust_floating_status_width(-40)
        self.assertEqual(self.app.floating_status_geometry()[0], 86)
        self.assertEqual(self.app.floating_status_width_delta, 86 - self.fit_width())
        self.app.adjust_floating_status_width(40)
        self.assertEqual(self.app.floating_status_geometry()[0], 126)
        self.app.floating_status_width_delta = -10000
        self.app.adjust_floating_status_width(40)
        self.assertEqual(self.app.floating_status_geometry()[0], 126)

    def test_monitor_selection_negative_stacked_and_gaps(self):
        self.assertEqual(self.app.floating_status_monitor(point=(-3000, -400))["id"], "DISPLAY2")
        self.assertEqual(self.app.floating_status_monitor(point=(100, 100))["id"], "DISPLAY1")
        above = {"id": "ABOVE", "bounds": (0, -1400, 1920, -320), "taskbar": (-368, 48), "primary": False}
        below = {"id": "BELOW", "bounds": (0, 2000, 1920, 3080), "taskbar": (3032, 48), "primary": False}
        right = {"id": "RIGHT", "bounds": (2400, 0, 4320, 1080), "taskbar": (1032, 48), "primary": False}
        self.monitors.extend([above, below, right])
        self.assertEqual(self.app.floating_status_monitor(point=(200, -700))["id"], "ABOVE")
        self.assertEqual(self.app.floating_status_monitor(point=(100, 2200))["id"], "BELOW")
        self.assertEqual(self.app.floating_status_monitor(point=(2500, 500))["id"], "RIGHT")
        self.assertEqual(self.app.floating_status_monitor(point=(2390, 500))["id"], "RIGHT")

    def test_saved_monitor_origin_change_and_missing_monitor_fallback(self):
        self.app.load_floating_status_settings({"monitor_id": "DISPLAY2", "local_x": 345})
        self.assertEqual(self.app.floating_status_geometry()[2:], (-3495, 1592))
        self.monitors[1] = {**self.monitors[1], "bounds": (2000, -1000, 5840, 1160),
                            "taskbar": (1112, 48)}
        self.assertEqual(self.app.floating_status_geometry()[2:], (2345, 1115))
        self.monitors.pop()
        self.assertEqual(self.app.floating_status_geometry()[2:], (345, 1035))
        self.assertEqual(self.app.floating_status_monitor_id, "DISPLAY1")

    def test_timer_keeps_free_drag_xy_then_drop_clamps_on_negative_screen(self):
        self.prepare_window()
        self.prepare_canvas()
        self.app.start_floating_status_drag(SimpleNamespace(x_root=115, y_root=1040))
        self.app.drag_floating_status(SimpleNamespace(x_root=-3838, y_root=-400))
        for _ in range(3):
            self.app.keep_floating_status_topmost()
            self.assertEqual(self.native_rect[:2], [-3853, -405])
        self.app.stop_floating_status_drag()
        self.assertEqual(self.native_rect[:2], [-3840, 1592])
        self.assertEqual(self.app.floating_status_local_x, 0)
        self.assertEqual(self.native_rect[3] - self.native_rect[1], 42)

    def test_negative_placement_uses_native_absolute_coordinates_not_tk_offsets(self):
        window = self.prepare_window()
        self.app.floating_status_monitor_id = "DISPLAY2"
        self.app.floating_status_local_x = 500
        self.app.place_floating_status_default()
        self.assertEqual(self.native_rect[:2], [-3340, 1592])
        self.assertEqual(window.geometry.call_args.args, (f"{self.fit_width()}x42",))
        call = self.win32.SetWindowPos.call_args.args
        self.assertEqual(call[2:6], (-3340, 1592, self.fit_width(), 42))
        self.assertEqual(call[-1] & 0x10, 0x10)

    def test_native_monitor_query_reads_both_real_rects_and_work_fallback(self):
        def get_info(handle, pointer):
            data = self.monitors[int(handle) - 1]
            info = pointer._obj
            info.szDevice = data["id"]
            info.dwFlags = 1 if data["primary"] else 0
            for rect, coords in ((info.rcMonitor, data["bounds"]), (info.rcWork, data["work"])):
                rect.left, rect.top, rect.right, rect.bottom = coords
            return True
        def enum(_dc, _rect, callback, _param):
            callback(1, None, None, 0)
            callback(2, None, None, 0)
            return True
        def find(_parent, after, kind, _title):
            return None if after else {"Shell_TrayWnd": 90, "Shell_SecondaryTrayWnd": 91}[kind]
        def bar_rect(hwnd, pointer):
            coords = {90: (0, 1032, 1920, 1080), 91: (-3840, 1589, 0, 1637)}[hwnd]
            pointer._obj.left, pointer._obj.top, pointer._obj.right, pointer._obj.bottom = coords
            return True
        self.win32.GetMonitorInfoW.side_effect = get_info
        self.win32.EnumDisplayMonitors.side_effect = enum
        self.win32.FindWindowExW.side_effect = find
        self.win32.GetWindowRect.side_effect = bar_rect
        actual = Panel.floating_status_monitors(self.app)
        self.assertEqual(actual, self.monitors)
        self.win32.FindWindowExW.side_effect = lambda *_args: None
        self.assertEqual(Panel.floating_status_monitors(self.app), self.monitors)

    def test_overflow_uses_destination_width_and_retains_entire_content(self):
        canvas = self.prepare_canvas()
        self.app.groups[0].name *= 500
        self.app.floating_status_monitor_id = "DISPLAY2"
        self.app.update_floating_status()
        self.assertEqual(self.app.floating_status_geometry()[0], 3832)
        self.assertEqual(canvas.width, 3832 - 38)
        self.assertEqual(canvas.texts[0][1]["text"], self.app.floating_status_group_line(self.app.groups[0]))
        self.app.scroll_floating_status(SimpleNamespace(delta=-12000000))
        self.assertEqual(canvas.left + canvas.width, canvas.region[2])

    def test_geometry_primary_bottom_clamps_and_keeps_height_y(self):
        fit = self.fit_width()
        self.assertEqual(self.app.floating_status_geometry(), (fit, 42, 8, 1035))
        self.app.floating_status_local_x = 9999
        self.assertEqual(self.app.floating_status_geometry(), (fit, 42, 1920 - fit, 1035))
        self.monitors = [{"id": "small", "bounds": (0, 0, 320, 600), "taskbar": (568, 32), "primary": True}]
        self.app.floating_status_width_delta = 1600
        self.assertEqual(self.app.floating_status_geometry(), (312, 32, 8, 568))

    def test_drag_follows_xy_and_docks_on_destination_only_on_release(self):
        self.prepare_window()
        self.app.start_floating_status_drag(SimpleNamespace(x_root=115, y_root=1040))
        self.app.drag_floating_status(SimpleNamespace(x_root=-1000, y_root=-200))
        self.assertEqual(self.native_rect[:2], [-1015, -205])
        self.app.save_launch_config.assert_not_called()
        self.app.stop_floating_status_drag()
        self.assertEqual(self.native_rect[:2], [-1015, 1592])
        self.assertEqual(self.app.floating_status_monitor_id, "DISPLAY2")
        self.assertEqual(self.app.floating_status_local_x, 2825)
        self.app.save_launch_config.assert_called_once()
        self.app.stop_floating_status_drag()
        self.app.save_launch_config.assert_called_once()

    def test_font_width_reset_keep_height_y_and_reset_keeps_saved_x(self):
        window = self.prepare_window()
        initial_fit = self.fit_width()
        self.app.floating_status_local_x = 333
        self.app.adjust_floating_status_font(100)
        self.assertEqual(self.app.floating_status_font_size, 20)
        larger_fit = self.fit_width()
        self.assertGreater(larger_fit, initial_fit)
        self.app.adjust_floating_status_width(40)
        window.geometry.assert_called_with(f"{larger_fit + 40}x42")
        self.assertEqual(self.native_rect[:2], [333, 1035])
        self.app.reset_floating_status_size()
        self.assertEqual((self.app.floating_status_font_size, self.app.floating_status_width_delta,
                          self.app.floating_status_local_x), (14, 0, 333))
        window.geometry.assert_called_with(f"{initial_fit}x42")
        self.assertEqual(self.app.save_launch_config.call_count, 3)

    def test_legacy_resize_entry_points_cannot_resize_vertical(self):
        window = self.prepare_window()
        self.app.start_floating_status_resize(SimpleNamespace(x_root=100, y_root=100))
        self.app.resize_floating_status(SimpleNamespace(x_root=140, y_root=10000))
        window.geometry.assert_called_with(f"{self.fit_width() + 40}x42")
        self.assertEqual(self.native_rect[:2], [8, 1035])
        self.app.stop_floating_status_resize()
        self.app.save_launch_config.assert_called_once()

    def test_win32_primary_taskbar_and_noactivate_toolwindow(self):
        self.assertEqual(Panel.floating_status_taskbar_bounds(self.app), (1920, 1032, 48))
        window = self.prepare_window()
        self.app.apply_floating_status_window_style()
        self.win32.SetWindowLongW.assert_called_with(101, -20, 0x08000080)
        self.app.keep_floating_status_topmost()
        self.assertEqual(self.win32.SetWindowPos.call_args.args[-1] & 0x10, 0x10)
        window.after.assert_called_once_with(1000, self.app.keep_floating_status_topmost)

    def test_create_menu_exact_wiring_fixed_gear_and_canvas_bindings(self):
        window, menu, gear, canvas = [MagicMock() for _ in range(4)]
        fake_tk = SimpleNamespace(Toplevel=MagicMock(return_value=window), Menu=MagicMock(return_value=menu),
                                  Button=MagicMock(return_value=gear), Canvas=MagicMock(return_value=canvas))
        self.app.launch_current_group_files = MagicMock()
        self.app.close_from_tray = MagicMock()
        self.app.bind_floating_status_drag = MagicMock()
        self.app.keep_floating_status_topmost = MagicMock()
        self.app.update_floating_status = MagicMock()
        self.app.apply_floating_status_window_style = MagicMock()
        self.app.adjust_floating_status_font = MagicMock()
        self.app.adjust_floating_status_width = MagicMock()
        self.app.reset_floating_status_size = MagicMock()
        with patch.dict(NAMESPACE, {"tk": fake_tk}):
            self.app.create_floating_status_window()
        actions = {call.kwargs["label"]: call.kwargs["command"] for call in menu.add_command.call_args_list}
        self.assertEqual(list(actions), ["字體縮小", "字體放大", "寬度縮短", "寬度加長",
                                         "恢復預設尺寸", "整理目前組別", "關閉輔魔"])
        for command in actions.values():
            command()
        self.assertEqual(self.app.adjust_floating_status_font.call_args_list[0].args, (-1,))
        self.assertEqual(self.app.adjust_floating_status_font.call_args_list[1].args, (1,))
        self.assertEqual(self.app.adjust_floating_status_width.call_args_list[0].args, (-40,))
        self.assertEqual(self.app.adjust_floating_status_width.call_args_list[1].args, (40,))
        self.app.launch_current_group_files.assert_called_once()
        self.app.close_from_tray.assert_called_once()
        self.app.reset_floating_status_size.assert_called_once()
        window.overrideredirect.assert_called_once_with(True)
        self.assertIn((("-toolwindow", True), {}), window.attributes.call_args_list)
        window.configure.assert_called_once_with(bg=self.app.rpg_panel)
        self.assertEqual(fake_tk.Button.call_args.args[0], window)
        self.assertEqual(fake_tk.Button.call_args.kwargs["bg"], self.app.rpg_button)
        self.assertEqual(fake_tk.Button.call_args.kwargs["fg"], self.app.rpg_ink)
        self.assertEqual(fake_tk.Canvas.call_args.args[0], window)
        self.assertEqual(fake_tk.Canvas.call_args.kwargs["bg"], self.app.rpg_panel)
        self.assertEqual(gear.place.call_args.kwargs["relx"], 1.0)
        self.assertEqual(canvas.place.call_args.kwargs["width"], -38)
        self.app.bind_floating_status_drag.assert_called_once_with(canvas)
        self.assertIn((("<MouseWheel>", self.app.scroll_floating_status), {}), canvas.bind.call_args_list)

    def test_double_click_restores_main_and_menu_releases_grab(self):
        self.app.bind_class = MagicMock()
        self.app.restore_from_tray = MagicMock()
        canvas = MagicMock()
        canvas.bindtags.return_value = ("canvas",)
        canvas.winfo_children.return_value = []
        self.app.bind_floating_status_drag(canvas)
        handlers = {call.args[1]: call.args[2] for call in self.app.bind_class.call_args_list}
        handlers["<Double-Button-1>"](None)
        self.app.restore_from_tray.assert_called_once()
        self.app.floating_status_gear = MagicMock()
        self.app.floating_status_menu = MagicMock()
        self.app.show_floating_status_menu()
        self.app.floating_status_menu.grab_release.assert_called_once()

    def test_hit_test_only_panel_and_own_visible_menu(self):
        panel, menu = self.prepare_window(), MagicMock()
        self.app.floating_status_menu = menu
        menu.winfo_id.return_value = 102
        menu.winfo_ismapped.return_value = 0  # Real Windows Tk native-menu behavior.
        self.app.floating_status_menu_thread_id = 77
        def rect_for(hwnd, pointer):
            coords = {101: (100, 1035, 700, 1077), 102: (0, 0, 1, 1),
                      103: (650, 800, 850, 1035)}[hwnd]
            pointer._obj.left, pointer._obj.top, pointer._obj.right, pointer._obj.bottom = coords
            return True
        def enumerate_thread(thread_id, callback, _parameter):
            self.assertEqual(thread_id, 77)
            callback(103, 0)
        def class_name(_hwnd, buffer, _length):
            buffer.value = "#32768"
            return 6
        self.win32.GetWindowRect.side_effect = rect_for
        self.win32.EnumThreadWindows.side_effect = enumerate_thread
        self.win32.GetClassNameW.side_effect = class_name
        self.assertTrue(self.app.floating_status_contains_point(200, 1040))
        self.assertTrue(self.app.floating_status_contains_point(700, 900))
        self.assertFalse(self.app.floating_status_contains_point(900, 900))
        self.assertFalse(self.app.floating_status_contains_point(700, 1077))
        self.app.floating_status_menu_thread_id = 0
        self.assertFalse(self.app.floating_status_contains_point(700, 900))
        panel.winfo_ismapped.return_value = False
        self.assertFalse(self.app.floating_status_contains_point(200, 1040))
        menu.winfo_ismapped.assert_not_called()

    def test_native_menu_scope_rejects_unrelated_threads_windows_and_closed_menu(self):
        self.app.floating_status_menu_thread_id = 77
        def enumerate_thread(thread_id, callback, _parameter):
            # Thread 88 has another app's native menu at the same screen point.
            for hwnd in ({77: [103], 88: [104]}[thread_id]):
                callback(hwnd, 0)
        def rect_for(_hwnd, pointer):
            pointer._obj.left, pointer._obj.top = 650, 800
            pointer._obj.right, pointer._obj.bottom = 850, 1035
            return True
        def class_name(_hwnd, buffer, _length):
            buffer.value = "TkTopLevel"
            return 10
        self.win32.EnumThreadWindows.side_effect = enumerate_thread
        self.win32.GetWindowRect.side_effect = rect_for
        self.win32.GetClassNameW.side_effect = class_name
        self.assertFalse(self.app.floating_status_contains_point(700, 900))
        self.win32.EnumThreadWindows.assert_called_once()
        self.assertEqual(self.win32.EnumThreadWindows.call_args.args[0], 77)
        self.win32.GetWindowRect.assert_not_called()
        self.win32.IsWindowVisible.return_value = False
        self.assertFalse(self.app.floating_status_contains_point(700, 900))
        self.app.floating_status_menu_thread_id = 0
        self.win32.EnumThreadWindows.reset_mock()
        self.assertFalse(self.app.floating_status_contains_point(700, 900))
        self.win32.EnumThreadWindows.assert_not_called()

    def test_native_menu_tracking_lifetime_clears_even_on_popup_failure(self):
        self.app.floating_status_gear = MagicMock()
        menu = MagicMock()
        menu.winfo_id.return_value = 102
        menu.winfo_ismapped.return_value = 0
        self.app.floating_status_menu = menu
        self.win32.GetWindowThreadProcessId.return_value = 77
        def tracking(*_args):
            self.assertEqual(self.app.floating_status_menu_thread_id, 77)
        menu.tk_popup.side_effect = tracking
        self.app.show_floating_status_menu()
        self.assertEqual(self.app.floating_status_menu_thread_id, 0)
        menu.tk_popup.side_effect = RuntimeError("popup failure")
        with self.assertRaisesRegex(RuntimeError, "popup failure"):
            self.app.show_floating_status_menu()
        self.assertEqual(self.app.floating_status_menu_thread_id, 0)
        self.assertEqual(menu.grab_release.call_count, 2)

    def test_unmapped_tk_menu_native_popup_blocks_wheel_only_while_own_menu_open(self):
        self.app.floating_status_menu = MagicMock()
        self.app.floating_status_menu.winfo_ismapped.return_value = 0
        group = self.app.groups[0]
        group.running, group.master_hwnd = True, 11
        self.app.events = queue.Queue()
        self.app.floating_status_menu_thread_id = 77
        self.win32.EnumThreadWindows.side_effect = lambda _thread, callback, _param: callback(103, 0)
        def class_name(_hwnd, buffer, _length):
            buffer.value = "#32768"
            return 6
        def rect_for(_hwnd, pointer):
            pointer._obj.left, pointer._obj.top = 1482, 899
            pointer._obj.right, pointer._obj.bottom = 1598, 1038
            return True
        self.win32.GetClassNameW.side_effect = class_name
        self.win32.GetWindowRect.side_effect = rect_for
        point = MagicMock(return_value=(True, 612, 285))
        with patch.dict(NAMESPACE, {"point_in_client": point}):
            self.app.queue_wheel_sync_at_point(1540, 968, -120)
            self.assertTrue(self.app.events.empty())
            point.assert_not_called()
            # An unrelated menu at the same coordinates must not be excluded.
            self.app.floating_status_menu_thread_id = 0
            self.app.queue_wheel_sync_at_point(1540, 968, -120)
        event = self.app.events.get_nowait()
        self.assertEqual((event.x, event.y, event.delta), (612, 285, -120))

    def test_wheel_guard_blocks_overlay_preserves_normal_game_path(self):
        group = self.app.groups[0]
        group.running, group.master_hwnd = True, 11
        self.app.events = queue.Queue()
        self.app.floating_status_contains_point = MagicMock(return_value=True)
        point = MagicMock(return_value=(True, 10, 20))
        with patch.dict(NAMESPACE, {"point_in_client": point}):
            self.app.queue_wheel_sync_at_point(200, 1040, -120)
            self.assertTrue(self.app.events.empty())
            point.assert_not_called()
            self.app.floating_status_contains_point.return_value = False
            self.app.queue_wheel_sync_at_point(200, 1040, -120)
        event = self.app.events.get_nowait()
        self.assertEqual((event.group_index, event.x, event.y, event.delta), (0, 10, 20, -120))
        self.assertTrue(group.running)

    def test_mouse_guard_blocks_panel_press_but_retains_release_pairing(self):
        group = self.app.groups[0]
        group.running, group.master_hwnd = True, 11
        group.button_state["left"] = False
        self.app.events = queue.Queue()
        self.app.enabled_buttons = lambda _g: [("left", 1, 100, 101, 0)]
        self.app.is_button_down = lambda _vk: True
        self.app.floating_status_contains_point = lambda _x, _y: True
        with patch.dict(NAMESPACE, {"cursor_point_in_client": lambda _h: (True, 10, 20),
                                   "get_cursor_pos_raw": lambda: SimpleNamespace(x=200, y=1040)}):
            self.app.check_mouse_buttons()
            self.assertTrue(self.app.events.empty())
            group.button_state["left"] = False
            self.app.floating_status_contains_point = lambda _x, _y: False
            self.app.check_mouse_buttons()
            self.assertEqual(self.app.events.get_nowait().message, 100)
            self.app.floating_status_contains_point = lambda _x, _y: True
            self.app.is_button_down = lambda _vk: False
            self.app.check_mouse_buttons()
            self.assertEqual(self.app.events.get_nowait().message, 101)
            self.assertEqual(group.active_buttons, set())

    def test_config_roundtrip_uses_only_temp_paths_and_preserves_other_settings(self):
        self.app.remember_main_window_geometry = lambda: "820x440+80+80"
        self.app.disconnect_detect_enabled = Value(True)
        self.app.disconnect_restore_minimized = Value(False)
        self.app.disconnect_detect_interval_ms_text = Value("3000")
        self.app.pending_disconnect_detect_enabled = True
        self.app.pending_disconnect_restore_minimized = False
        self.app.pending_disconnect_detect_interval_ms = "4321"
        self.app.legacy_disconnect_settings = {
            "disconnect_detect_enabled": True,
            "disconnect_restore_minimized": True,
            "disconnect_detect_interval_ms": "4321",
        }
        self.app.section_visible_vars = {"窗口": Value(True)}
        self.app.write_log = MagicMock()
        self.app.floating_status_local_x = 345
        self.app.floating_status_monitor_id = "DISPLAY2"
        self.app.floating_status_font_size = 18
        self.app.floating_status_width_delta = 200
        self.app.pending_clock_bar_settings = {"font_size": 15, "width_delta": 0,
                                               "monitor_id": "DISPLAY1", "local_x": 1494}
        self.app.pending_notification_bar_settings = {"font_size": 16, "width_delta": 80,
                                                      "monitor_id": "DISPLAY2", "local_x": 1900}
        self.app.groups[0].launch_entries = [Entry("isolated-game-reference.exe")]
        with tempfile.TemporaryDirectory(prefix="fu-status-test-") as directory:
            config, backup = Path(directory) / "sync_launch_config.json", Path(directory) / "backup.json"
            self.app.launch_config_path = lambda: str(config)
            with patch.dict(os.environ, {"APPDATA": directory}), patch.dict(
                    NAMESPACE, {"app_config_backup_path": lambda: str(backup)}):
                Panel.save_launch_config(self.app)
                saved = json.loads(config.read_text(encoding="utf-8"))
                self.assertEqual(config.read_bytes(), backup.read_bytes())
                self.assertEqual(saved["app_state"]["notification_window"], self.app.pending_notification_bar_settings)
                self.assertEqual(saved["app_state"]["clock_window"], self.app.pending_clock_bar_settings)
                self.assertEqual(
                    {key: saved["app_state"][key] for key in self.app.legacy_disconnect_settings},
                    self.app.legacy_disconnect_settings,
                )
                self.app.load_floating_status_settings({})
                self.app.load_launch_config()
                self.assertEqual(self.app.pending_notification_bar_settings, saved["app_state"]["notification_window"])
                self.assertEqual(self.app.floating_status_settings(),
                                 {"font_size": 18, "width_delta": 200, "monitor_id": "DISPLAY2", "local_x": 345})
                self.assertEqual(self.app.floating_status_geometry()[2:], (-3495, 1592))
                Panel.save_launch_config(self.app)
                self.assertEqual(saved, json.loads(config.read_text(encoding="utf-8")))
                # Old/malformed panel settings safely default; root corruption is ignored.
                saved["app_state"]["status_window"] = "broken"
                config.write_text(json.dumps(saved), encoding="utf-8")
                self.app.load_launch_config()
                self.assertEqual(self.app.floating_status_settings(),
                                 {"font_size": 14, "width_delta": 0, "monitor_id": "", "local_x": 8})
                for broken in ("[]", "null", "{bad"):
                    config.write_text(broken, encoding="utf-8")
                    self.app.load_launch_config()
        self.app.write_log.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
