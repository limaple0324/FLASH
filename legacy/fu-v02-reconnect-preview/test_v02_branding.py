"""Visible branding and exact 006 source-boundary regression guards."""
from __future__ import annotations

import ast
import copy
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent
BASELINE = "594e3a0b1acdb8b96d76fd5070af2e0f78b7cfc8"
BATCH_BASELINE = "37a10d878453f0ae9787119a9eb9bb3a80fdfd7c"
FIXED_BATCH_BASELINE = "10fa86d0beb82ce60a6747172be881876fc9562e"
SHUTDOWN_LIFECYCLE_PARENT = "308681214d80ef7e2b950fc4fb3c973795c56594"
SHUTDOWN_LIFECYCLE_METHODS = {
    "update_estimated_game_time_label", "poll_game_time_tick", "on_close",
}

ALLOWED_BATCH_METHOD_CHANGES = {
    "__init__", "create_floating_status_window", "create_notification_bar",
    "notification_bar_settings", "refresh_notification_context",
    "reset_notification_context", "notify_disconnect_window", "set_notification_phase",
    "close_notification_bar", "draw_floating_status_cards",
    "floating_status_contains_point", "load_launch_config", "save_launch_config",
    "_build_ui", "window_size_values",
    "_set_timed_click_point_from_cursor", "enable_timed_click", "poll_timed_click",
    "fire_timed_click", "send_timed_click_once", "apply_disconnect_restore_minimized",
    "toggle_disconnect_detect", "scan_disconnect_once_visible",
    "scan_disconnect_once_restore", "scan_disconnect_once", "schedule_disconnect_detect",
    "poll_disconnect_detect", "detect_disconnect_prompt",
    "start_sync", "poll_input", "_start_worker", "install_mouse_wheel_hook",
    "uninstall_mouse_wheel_hook", "on_close", "configure_keyboard_sync_keys",
    "run_sync_action",
}
EXPECTED_NEW_BATCH_METHODS = {
    "timed_click_target_failure", "clear_timed_click_overlay", "show_timed_click_point",
    "poll_wheel_hook_queues", "poll_worker_errors",
}
EXPECTED_REMOVED_BATCH_METHODS = {"load_master_window_size", "low_level_mouse_proc"}


def baseline_file(name):
    return subprocess.check_output(["git", "show", f"{BASELINE}:outputs/{name}"],
                                   cwd=ROOT, encoding="utf-8")


def batch_baseline_file(name):
    return subprocess.check_output(["git", "show", f"{BATCH_BASELINE}:outputs/{name}"],
                                   cwd=ROOT, encoding="utf-8")


def app_class(tree):
    return next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "FlashSyncApp")


def dump(node):
    return ast.dump(node, include_attributes=False)


def replace_exact(tree, before, after, *, count=1):
    """Replace a complete exact AST node, never substring attributes/methods."""
    old = ast.parse(before).body[0]
    new = ast.parse(after).body[0] if after else None
    matched = 0
    class Change(ast.NodeTransformer):
        def visit(self, node):
            nonlocal matched
            if dump(node) == dump(old):
                matched += 1
                return copy.deepcopy(new)
            return super().visit(node)
    result = Change().visit(tree)
    if matched != count:
        raise AssertionError((before, "exact replacement count", matched, count))
    return result


# Four reimplemented clock entry points are checked against these COMPLETE
# expected definitions before normalization. No method is blindly excluded.
CLOCK_METHODS = '''
def init_game_clock(self) -> None:
    self.game_clock = GameClock()
    self.game_clock_reader = GameClockReader()
    self.game_clock_source = AutoClockSource(
        self.game_clock, self.game_clock_reader,
        lambda cancel: enumerate_source_windows(user32, EnumWindowsProc, is_flash_window, cancel),
    )

def invalidate_game_clock_source(self, *_args, force=False, reason="來源已改變") -> None:
    if force:
        self.game_clock_source.invalidate(reason)
        self.game_time_source_text.set(self.game_clock_source.status)

def game_time_source_token(self):
    return self.game_clock_source.token

def poll_game_clock_acquisition(self) -> None:
    if self.closing_app:
        return
    self.game_clock_source.poll()
    self.game_time_source_text.set(self.game_clock_source.status)
'''


def expected_006():
    before = ast.parse(baseline_file("flash_sync_v02.py"))
    expected = copy.deepcopy(before)
    for old, new in (
        ('APP_DISPLAY_NAME = "輔V0.2"', 'APP_DISPLAY_NAME = "輔魔"'),
        ('APP_STABLE_BASELINE_NAME = "輔V0.2 架構整理基準"', 'APP_STABLE_BASELINE_NAME = "輔魔 架構整理基準"'),
        ('from v02_game_clock import GameClock, HEALTH_MAX_GAP_NS', 'from v02_game_clock import GameClock'),
        ('from v02_game_clock_reader import AcquisitionError, GameClockReader', 'from v02_game_clock_reader import GameClockReader'),
    ):
        replace_exact(expected, old, new)
    import_at = next(index for index, node in enumerate(expected.body)
                     if isinstance(node, ast.ImportFrom) and node.module == "v02_game_clock_reader")
    expected.body.insert(import_at + 1, ast.parse(
        "from v02_game_clock_source import AutoClockSource, enumerate_source_windows").body[0])
    methods = {node.name: node for node in app_class(expected).body if isinstance(node, ast.FunctionDef)}
    status_line = methods["floating_status_group_line"]
    replace_exact(status_line,
                  'key = self.floating_status_hotkey_text(group).replace("\\r", " ").replace("\\n", " ")',
                  "")
    replace_exact(status_line,
                  'return f"{name}｜{state}　快捷鍵：{key}　視窗：{live}/{expected}"',
                  'return f"{name}｜{state}　視窗：{live}/{expected}"')
    init = methods["__init__"]
    replace_exact(init, 'self.game_time_source_text = tk.StringVar(value="伺服器時間：尚未綁定來源")',
                  'self.game_time_source_text = tk.StringVar(value="伺服器時間：尚未校正（自動尋找已開啟的遊戲）")')
    replace_exact(init, 'self.active_group_index.trace_add("write", self.invalidate_game_clock_source)', "")
    replace_exact(init, 'self.tree.bind("<<TreeviewSelect>>", self.invalidate_game_clock_source, add="+")', "")
    # The only additional literal brand is a menu entry, not all occurrences of 輔.
    menu = methods["create_floating_status_window"]
    old_tuple = ast.parse('("關閉輔", self.close_from_tray)', mode="eval").body
    new_tuple = ast.parse('("關閉輔魔", self.close_from_tray)', mode="eval").body
    matches = [node for node in ast.walk(menu) if dump(node) == dump(old_tuple)]
    if len(matches) != 1:
        raise AssertionError("exact close-menu tuple changed")
    matches[0].elts = new_tuple.elts
    replacements = {node.name: node for node in ast.parse(CLOCK_METHODS).body}
    app_class(expected).body = [copy.deepcopy(replacements[node.name]) if getattr(node, "name", "") in replacements else node
                                for node in app_class(expected).body]
    close = methods["on_close"]
    cancel_positions = [index for index, node in enumerate(close.body)
                        if dump(node) == dump(ast.parse("self.game_clock_cancel.set()").body[0])]
    if len(cancel_positions) != 2:
        raise AssertionError("exact close cancellation count changed")
    close.body[cancel_positions[0]] = ast.parse("self.game_clock_source.shutdown()").body[0]
    del close.body[cancel_positions[1]]
    replace_exact(close, 'self.game_clock.invalidate("程式正在關閉")', "")
    replace_exact(close,
                  'if self.game_clock_thread is not None and self.game_clock_thread.is_alive():\n'
                  '    self.after(25, self.on_close)\n    return',
                  'if self.game_clock_source.is_busy():\n    self.after(25, self.on_close)\n    return')
    return before, expected


def normalize_006(tree):
    """Exact reverse mapping for the older 005 protection test, with no skips."""
    before, expected = expected_006()
    result = copy.deepcopy(tree)
    old_methods = {node.name: node for node in app_class(before).body if isinstance(node, ast.FunctionDef)}
    expected_methods = {node.name: node for node in app_class(expected).body if isinstance(node, ast.FunctionDef)}
    changed = {name for name in old_methods if dump(old_methods[name]) != dump(expected_methods[name])}
    if changed != {"__init__", "create_floating_status_window", "floating_status_group_line",
                   "init_game_clock", "invalidate_game_clock_source", "game_time_source_token",
                   "poll_game_clock_acquisition", "on_close"}:
        raise AssertionError(("unexpected source boundary", changed))
    for index, node in enumerate(app_class(result).body):
        if getattr(node, "name", "") in changed:
            if dump(node) != dump(expected_methods[node.name]):
                raise AssertionError(("unexpected statement inside 006 method", node.name))
            app_class(result).body[index] = copy.deepcopy(old_methods[node.name])
    # Exact full top-level nodes, including aliases and import ordering.
    for old, new in (
        ('APP_DISPLAY_NAME = "輔魔"', 'APP_DISPLAY_NAME = "輔V0.2"'),
        ('APP_STABLE_BASELINE_NAME = "輔魔 架構整理基準"', 'APP_STABLE_BASELINE_NAME = "輔V0.2 架構整理基準"'),
        ('from v02_game_clock import GameClock', 'from v02_game_clock import GameClock, HEALTH_MAX_GAP_NS'),
        ('from v02_game_clock_reader import GameClockReader', 'from v02_game_clock_reader import AcquisitionError, GameClockReader'),
        ('from v02_game_clock_source import AutoClockSource, enumerate_source_windows', ''),
    ):
        replace_exact(result, old, new)
    return result


def expected_notification_text_change():
    before = ast.parse(baseline_file("v02_notification_bar.py"))
    expected = copy.deepcopy(before)
    model = next(node for node in expected.body
                 if isinstance(node, ast.ClassDef) and node.name == "NotificationModel")
    text_method = next(node for node in model.body
                       if isinstance(node, ast.FunctionDef) and node.name == "text")
    replace_exact(
        text_method,
        'if not self.events:\n'
        '    if self.phase in ("disabled", "waiting"):\n'
        '        return prefix\n'
        '    return f"{prefix}｜尚無偵測紀錄"',
        'if not self.events:\n    return ""',
    )
    return before, expected


class BrandingTests(unittest.TestCase):
    def test_all_visible_brand_entries_and_unchanged_internal_identity(self):
        source = (ROOT / "flash_sync_v02.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        assignments = {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
                       if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                       and node.targets[0].id.startswith("APP_")}
        self.assertEqual(assignments, {
            "APP_DISPLAY_NAME": "輔魔", "APP_VERSION": "V.02", "APP_VERSION_CODE": "v0.2",
            "APP_STABLE_BASELINE_NAME": "輔魔 架構整理基準", "APP_DATA_DIR_NAME": "輔V0.2_自動重連獨立版",
            "APP_CONFIG_FILENAME": "sync_launch_config_reconnect_standalone.json",
            "APP_OUTPUT_CONFIG_BACKUP_FILENAME": "sync_launch_config_reconnect_standalone_backup.json",
        })
        branded = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
                   and isinstance(node.value, str) and "輔" in node.value]
        self.assertEqual(sorted(branded), sorted([
            "輔魔", "輔魔 架構整理基準", "輔V0.2_自動重連獨立版", "輔V0.2", "關閉輔魔",
            "由輔本次啟動且身份唯一的視窗固定監管。重啟輔後既有視窗不納管，須由輔重新開啟。",
            "自動重連：程序快照失敗，本次不納管；輔原啟動與同步流程繼續。",
            "自動重連：程序快照失敗，本次不納管；輔原視窗綁定流程繼續。",
        ]))
        methods = {node.name: node for node in app_class(tree).body if isinstance(node, ast.FunctionDef)}
        name_counts = {name: sum(isinstance(node, ast.Name) and node.id == "APP_DISPLAY_NAME"
                                 for node in ast.walk(method)) for name, method in methods.items()}
        self.assertEqual({name: count for name, count in name_counts.items() if count}, {
            "__init__": 1, "tray_tip_text": 2, "show_tray_menu": 2,
            "export_launch_config": 1, "import_launch_config": 2, "update_window_title": 1,
        })
        self.assertNotIn('"關閉輔"', source)

    def test_every_unauthorized_method_and_top_level_function_matches_fixed_baseline(self):
        baseline = ast.parse(subprocess.check_output(
            ["git", "show", f"{FIXED_BATCH_BASELINE}:outputs/flash_sync_v02.py"],
            cwd=ROOT, encoding="utf-8",
        ))
        actual = ast.parse((ROOT / "flash_sync_v02.py").read_text(encoding="utf-8"))
        before_methods = {node.name: node for node in app_class(baseline).body
                          if isinstance(node, ast.FunctionDef)}
        after_methods = {node.name: node for node in app_class(actual).body
                         if isinstance(node, ast.FunctionDef)}
        self.assertEqual(set(after_methods) - set(before_methods), EXPECTED_NEW_BATCH_METHODS)
        self.assertEqual(set(before_methods) - set(after_methods), EXPECTED_REMOVED_BATCH_METHODS)
        parent = ast.parse(subprocess.check_output(
            ["git", "show", f"{SHUTDOWN_LIFECYCLE_PARENT}:outputs/flash_sync_v02.py"],
            cwd=ROOT, encoding="utf-8",
        ))
        parent_methods = {node.name: node for node in app_class(parent).body
                          if isinstance(node, ast.FunctionDef)}
        self.assertEqual(SHUTDOWN_LIFECYCLE_METHODS,
                         SHUTDOWN_LIFECYCLE_METHODS & parent_methods.keys())

        update = copy.deepcopy(parent_methods["update_estimated_game_time_label"])
        old_update_tail = ast.parse(
            "if self.clock_bar is not None:\n"
            "    self.clock_bar.update(estimated)\n"
        ).body[0]
        self.assertEqual(dump(old_update_tail), dump(update.body[-1]))
        update.body = [
            ast.parse(
                "if getattr(self, 'closing_app', False):\n"
                "    return\n"
            ).body[0],
            *update.body[:-1],
            *ast.parse(
                "clock_bar = self.clock_bar\n"
                "if clock_bar is not None and getattr(clock_bar, '_destroyed', False) is not True:\n"
                "    clock_bar.update(estimated)\n"
            ).body,
        ]

        poll = copy.deepcopy(parent_methods["poll_game_time_tick"])
        old_poll_body = ast.parse(
            "self.game_time_tick_after_id = None\n"
            "self.poll_game_clock_acquisition()\n"
            "self.update_estimated_game_time_label()\n"
            "if not self.closing_app:\n"
            "    self.schedule_game_time_tick()\n"
        ).body
        self.assertEqual([dump(node) for node in old_poll_body],
                         [dump(node) for node in poll.body])
        poll.body = ast.parse(
            "self.game_time_tick_after_id = None\n"
            "if self.closing_app:\n"
            "    return\n"
            "self.poll_game_clock_acquisition()\n"
            "self.update_estimated_game_time_label()\n"
            "self.schedule_game_time_tick()\n"
        ).body

        close = copy.deepcopy(parent_methods["on_close"])
        def is_self_attr(node, name):
            return (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "self" and node.attr == name)
        tick_indices = [index for index, node in enumerate(close.body)
                        if isinstance(node, ast.If)
                        and is_self_attr(node.test, "game_time_tick_after_id")]
        self.assertEqual(tick_indices, [tick_indices[0]] if tick_indices else [])
        self.assertEqual(len(tick_indices), 1)
        tick_block = close.body.pop(tick_indices[0])
        clock_indices = [index for index, node in enumerate(close.body)
                         if isinstance(node, ast.If)
                         and isinstance(node.test, ast.Compare)
                         and is_self_attr(node.test.left, "clock_bar")]
        self.assertEqual(len(clock_indices), 1)
        close.body.insert(clock_indices[0], tick_block)

        exact_shutdown = {
            "update_estimated_game_time_label": update,
            "poll_game_time_tick": poll,
            "on_close": close,
        }
        for name in set(before_methods) - ALLOWED_BATCH_METHOD_CHANGES - EXPECTED_REMOVED_BATCH_METHODS:
            expected = exact_shutdown.get(name, before_methods[name])
            self.assertEqual(dump(expected), dump(after_methods[name]), name)
        before_top = {node.name: node for node in baseline.body if isinstance(node, ast.FunctionDef)}
        after_top = {node.name: node for node in actual.body if isinstance(node, ast.FunctionDef)}
        self.assertEqual(before_top.keys(), after_top.keys())
        for name in before_top:
            self.assertEqual(dump(before_top[name]), dump(after_top[name]), name)
        def protected_top_level(tree, current):
            result = []
            for node in tree.body:
                if isinstance(node, ast.ClassDef) and node.name == "FlashSyncApp":
                    continue
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                    # Module documentation changed only to remove the stale
                    # claim that wheel synchronization has no low-level hook.
                    continue
                if isinstance(node, ast.ClassDef) and node.name == "MSLLHOOKSTRUCT":
                    continue
                if (not current and isinstance(node, ast.ImportFrom)
                        and node.module == "v02_notification_bar"):
                    continue
                if (current and isinstance(node, ast.ImportFrom)
                        and node.module == "v02_wheel_hook"):
                    continue
                assigned = {target.id for target in getattr(node, "targets", [])
                             if isinstance(target, ast.Name)}
                if assigned & {"WH_MOUSE_LL", "HC_ACTION", "LowLevelMouseProc"}:
                    continue
                attribute_targets = {
                    target.value.attr
                    for target in getattr(node, "targets", [])
                    if isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Attribute)
                    and isinstance(target.value.value, ast.Name)
                    and target.value.value.id == "user32"
                }
                if attribute_targets & {
                    "SetWindowsHookExW", "CallNextHookEx", "UnhookWindowsHookEx",
                }:
                    continue
                if current and assigned & {
                    "RESTORED_FLASH_CLIENT_WIDTH", "RESTORED_FLASH_CLIENT_HEIGHT",
                    "TIMED_CLICK_COUNT", "TIMED_CLICK_INTERVAL_MS",
                }:
                    continue
                result.append(node)
            return ast.Module(body=result, type_ignores=[])
        self.assertEqual(dump(protected_top_level(baseline, False)),
                         dump(protected_top_level(actual, True)))

    def test_mutations_inside_changed_and_protected_methods_are_not_masked(self):
        baseline = ast.parse(subprocess.check_output(
            ["git", "show", f"{FIXED_BATCH_BASELINE}:outputs/flash_sync_v02.py"],
            cwd=ROOT, encoding="utf-8",
        ))
        actual = ast.parse((ROOT / "flash_sync_v02.py").read_text(encoding="utf-8"))
        before = {node.name: node for node in app_class(baseline).body
                  if isinstance(node, ast.FunctionDef)}
        for method_name in ("poll_hotkey", "finish_capture_custom_input", "schedule_relogin_flow"):
            mutated = copy.deepcopy(actual)
            method = next(node for node in app_class(mutated).body
                          if getattr(node, "name", "") == method_name)
            method.body.append(ast.parse("self.unapproved_mutation = True").body[0])
            changed = {node.name for node in app_class(mutated).body
                       if isinstance(node, ast.FunctionDef) and node.name in before
                       and dump(node) != dump(before[node.name])
                       and node.name not in ALLOWED_BATCH_METHOD_CHANGES}
            self.assertIn(method_name, changed)

    def test_clock_and_notification_support_files_match_fixed_batch_baseline(self):
        for name in ("v02_game_clock.py", "v02_game_clock_reader.py",
                     "v02_game_clock_source.py", "v02_notification_bar.py"):
            expected = subprocess.check_output(
                ["git", "show", f"{FIXED_BATCH_BASELINE}:outputs/{name}"],
                cwd=ROOT, encoding="utf-8",
            )
            self.assertEqual(expected, (ROOT / name).read_text(encoding="utf-8"), name)
        expected = subprocess.check_output(
            ["git", "show", f"{FIXED_BATCH_BASELINE}:outputs/v02_game_clock_bar.py"],
            cwd=ROOT, encoding="utf-8",
        )
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
        self.assertEqual(expected, (ROOT / "v02_game_clock_bar.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
