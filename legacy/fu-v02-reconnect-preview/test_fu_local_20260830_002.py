from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import flash_sync_v02
from flash_sync_v02 import FlashSyncApp, LaunchEntry, SyncGroup
from fu_reconnect_integration import EmbeddedAutomationController


class FakeVar:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeFrame:
    def __init__(self):
        self.packed = False

    def pack_forget(self):
        self.packed = False

    def pack(self, **_kwargs):
        self.packed = True


class FakeTree:
    def __init__(self):
        self.rows = {}
        self.selected = ()

    def selection(self):
        return self.selected

    def selection_set(self, entry_id):
        self.selected = (entry_id,)

    def get_children(self):
        return tuple(self.rows)

    def delete(self, *entry_ids):
        for entry_id in entry_ids:
            self.rows.pop(entry_id, None)
        if self.selected and self.selected[0] not in self.rows:
            self.selected = ()

    def insert(self, _parent, _index, *, iid, values):
        self.rows[iid] = values

    def exists(self, entry_id):
        return entry_id in self.rows


class SectionAndManorUiTests(unittest.TestCase):
    def test_automation_uses_common_section_menu_and_bulk_controls(self):
        build_source = inspect.getsource(FlashSyncApp._build_ui)
        menu_source = inspect.getsource(FlashSyncApp.rebuild_section_menu)
        self.assertIn('make_section("自動重連", True)', build_source)
        self.assertIn("self.section_order", menu_source)
        self.assertIn("add_checkbutton", menu_source)

        app = object.__new__(FlashSyncApp)
        app.required_sections = {"窗口"}
        app.section_order = ["窗口", "同步窗口", "自動重連"]
        app.section_visible_vars = {name: FakeVar(False) for name in app.section_order}
        app.section_frames = {name: FakeFrame() for name in app.section_order}
        app.section_expand = {}
        app.schedule_fit_window_to_content = lambda **_kwargs: None

        FlashSyncApp.show_all_sections(app)
        self.assertTrue(all(var.get() for var in app.section_visible_vars.values()))
        self.assertTrue(all(frame.packed for frame in app.section_frames.values()))

        FlashSyncApp.hide_all_sections(app)
        self.assertTrue(app.section_visible_vars["窗口"].get())
        self.assertFalse(app.section_visible_vars["同步窗口"].get())
        self.assertFalse(app.section_visible_vars["自動重連"].get())
        self.assertTrue(app.section_frames["窗口"].packed)
        self.assertFalse(app.section_frames["自動重連"].packed)

        app.section_visible_vars["自動重連"].set(True)
        FlashSyncApp.refresh_visible_sections(app)
        self.assertTrue(app.section_frames["自動重連"].packed)

    def test_section_visibility_persists_across_config_reload(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "launch.json"
            original_backup = flash_sync_v02.app_config_backup_path
            original_machine = flash_sync_v02.current_machine_id
            try:
                flash_sync_v02.app_config_backup_path = lambda: str(path)
                flash_sync_v02.current_machine_id = lambda: "test-machine"
                app = object.__new__(FlashSyncApp)
                app.groups = [SyncGroup(name="第1組")]
                app.active_group_index = FakeVar(0)
                app.section_visible_vars = {
                    "窗口": FakeVar(True), "同步窗口": FakeVar(False),
                    "自動重連": FakeVar(True),
                }
                app.legacy_disconnect_settings = {}
                app.pending_disconnect_detect_enabled = False
                app.pending_disconnect_restore_minimized = False
                app.pending_disconnect_detect_interval_ms = "3000"
                app.remember_main_window_geometry = lambda: "820x440+10+10"
                app.floating_status_settings = lambda: {}
                app.clock_bar_settings = lambda: {}
                app.notification_bar_settings = lambda: {}
                app.current_group = lambda: app.groups[0]
                app.launch_config_path = lambda: str(path)
                app.write_log = lambda _text: None
                FlashSyncApp.save_launch_config(app)

                saved = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(saved["app_state"]["section_visibility"]["自動重連"], True)
                self.assertEqual(saved["app_state"]["section_visibility"]["同步窗口"], False)

                fresh = object.__new__(FlashSyncApp)
                fresh.groups = [SyncGroup(name="第1組")]
                fresh.pending_section_visibility = {}
                fresh.pending_active_group_name = ""
                fresh.pending_active_group_index = 0
                fresh.pending_window_geometry = ""
                fresh.legacy_disconnect_settings = {}
                fresh.pending_disconnect_detect_enabled = False
                fresh.pending_disconnect_restore_minimized = False
                fresh.pending_disconnect_detect_interval_ms = "3000"
                fresh.active_group_index = FakeVar(0)
                fresh.launch_config_path = lambda: str(path)
                fresh.load_floating_status_settings = lambda _value: None
                fresh.load_clock_bar_settings = lambda _value: None
                fresh.load_notification_bar_settings = lambda _value: None
                FlashSyncApp.load_launch_config(fresh)
                fresh.required_sections = {"窗口"}
                fresh.section_visible_vars = {
                    "窗口": FakeVar(False), "同步窗口": FakeVar(True),
                    "自動重連": FakeVar(False),
                }
                FlashSyncApp.apply_pending_section_visibility(fresh)
                self.assertTrue(fresh.section_visible_vars["窗口"].get())
                self.assertFalse(fresh.section_visible_vars["同步窗口"].get())
                self.assertTrue(fresh.section_visible_vars["自動重連"].get())
            finally:
                flash_sync_v02.app_config_backup_path = original_backup
                flash_sync_v02.current_machine_id = original_machine

    def make_automation_app(self, controller, entries):
        app = object.__new__(FlashSyncApp)
        app.automation = controller
        app.automation_tree = FakeTree()
        app.automation_selected_entry_id = ""
        app.automation_manor_enabled = FakeVar(False)
        app.automation_crop = FakeVar(flash_sync_v02.CROP_OPTIONS[0].label)
        app.automation_quantity = FakeVar(16)
        app.automation_fishing_enabled = FakeVar(False)
        app.automation_fishing_profile = FakeVar("")
        app._automation_fishing_profiles = []
        app.closing_app = False
        app.after = lambda *_args: None
        app.current_group = lambda: SimpleNamespace(launch_entries=entries)
        app.launch_entry_display_name = lambda entry: Path(entry.path).stem
        app.write_log = lambda _text: None
        return app

    def test_saved_non_default_quantity_survives_refresh_role_roundtrip_and_restart(self):
        with tempfile.TemporaryDirectory() as td:
            settings_path = Path(td) / "automation.json"
            controller = EmbeddedAutomationController(settings_path, lambda _hwnd: False)
            self.addCleanup(controller.stop)
            entries = [
                LaunchEntry(path="A.lnk", entry_id="stable-a"),
                LaunchEntry(path="B.lnk", entry_id="stable-b"),
            ]
            app = self.make_automation_app(controller, entries)
            FlashSyncApp.refresh_automation_rows(app)

            app.automation_tree.selection_set("stable-a")
            FlashSyncApp.on_automation_selected(app)
            app.automation_manor_enabled.set(True)
            app.automation_quantity.set(7)
            FlashSyncApp.save_automation_selected(app)
            self.assertEqual(controller.setting("stable-a")["manor_quantity"], 7)

            app.automation_quantity.set(9)
            for _ in range(3):
                FlashSyncApp.poll_automation_rows(app)
                self.assertEqual(app.automation_quantity.get(), 9)
                self.assertEqual(app.automation_selected_entry_id, "stable-a")
                self.assertEqual(controller.setting("stable-a")["manor_quantity"], 7)

            FlashSyncApp.save_automation_selected(app)
            self.assertEqual(controller.setting("stable-a")["manor_quantity"], 9)
            FlashSyncApp.refresh_automation_rows(app)
            self.assertEqual(app.automation_quantity.get(), 9)

            controller.update_setting("stable-b", manor_quantity=3)
            app.automation_tree.selection_set("stable-b")
            FlashSyncApp.on_automation_selected(app)
            self.assertEqual(app.automation_quantity.get(), 3)
            app.automation_tree.selection_set("stable-a")
            FlashSyncApp.on_automation_selected(app)
            self.assertEqual(app.automation_quantity.get(), 9)
            self.assertEqual(controller.setting("stable-b")["manor_quantity"], 3)

            controller.stop()
            fresh_controller = EmbeddedAutomationController(settings_path, lambda _hwnd: False)
            self.addCleanup(fresh_controller.stop)
            fresh_app = self.make_automation_app(fresh_controller, entries)
            FlashSyncApp.refresh_automation_rows(fresh_app)
            fresh_app.automation_tree.selection_set("stable-a")
            FlashSyncApp.on_automation_selected(fresh_app)
            self.assertEqual(fresh_app.automation_quantity.get(), 9)
            self.assertEqual(fresh_controller.setting("stable-b")["manor_quantity"], 3)

    def test_missing_entry_id_is_atomically_backfilled_and_keeps_automation_link(self):
        with tempfile.TemporaryDirectory() as td:
            launch_path = Path(td) / "launch.json"
            settings_path = Path(td) / "automation.json"
            launch_path.write_text(json.dumps({
                "app_state": {"machine_id": "test-machine"},
                "groups": [{
                    "name": "第1組",
                    "launch_entries": [{"path": "legacy-role.lnk", "role": "同步窗口"}],
                }],
            }, ensure_ascii=False), encoding="utf-8")

            def loader():
                app = object.__new__(FlashSyncApp)
                app.groups = [SyncGroup(name="第1組")]
                app.pending_section_visibility = {}
                app.pending_active_group_name = ""
                app.pending_active_group_index = 0
                app.pending_window_geometry = ""
                app.legacy_disconnect_settings = {}
                app.pending_disconnect_detect_enabled = False
                app.pending_disconnect_restore_minimized = False
                app.pending_disconnect_detect_interval_ms = "3000"
                app.active_group_index = FakeVar(0)
                app.launch_config_path = lambda: str(launch_path)
                app.load_floating_status_settings = lambda _value: None
                app.load_clock_bar_settings = lambda _value: None
                app.load_notification_bar_settings = lambda _value: None
                app.write_log = lambda text: self.fail(text)
                FlashSyncApp.load_launch_config(app)
                return app

            with mock.patch.object(flash_sync_v02, "current_machine_id", return_value="test-machine"):
                first = loader()
                first_id = first.groups[0].launch_entries[0].entry_id
                self.assertTrue(first_id)
                persisted = json.loads(launch_path.read_text(encoding="utf-8"))
                self.assertEqual(persisted["groups"][0]["launch_entries"][0]["entry_id"], first_id)
                self.assertEqual(list(Path(td).glob("*.tmp")), [])

                controller = EmbeddedAutomationController(settings_path, lambda _hwnd: False)
                controller.update_setting(first_id, manor_quantity=7)
                controller.stop()

                second = loader()
                second_id = second.groups[0].launch_entries[0].entry_id
                self.assertEqual(second_id, first_id)
                fresh_controller = EmbeddedAutomationController(settings_path, lambda _hwnd: False)
                self.addCleanup(fresh_controller.stop)
                self.assertEqual(fresh_controller.setting(second_id)["manor_quantity"], 7)

    def test_no_selection_refresh_does_not_create_or_overwrite_any_setting(self):
        with tempfile.TemporaryDirectory() as td:
            controller = EmbeddedAutomationController(Path(td) / "automation.json", lambda _hwnd: False)
            self.addCleanup(controller.stop)
            controller.update_setting("stable-a", manor_quantity=7)
            app = self.make_automation_app(controller, [LaunchEntry(path="A.lnk", entry_id="stable-a")])
            app.automation_quantity.set(16)
            FlashSyncApp.refresh_automation_rows(app)
            self.assertEqual(controller.setting("stable-a")["manor_quantity"], 7)
            self.assertEqual(app.automation_quantity.get(), 16)


class IconContractTests(unittest.TestCase):
    def test_app_user_model_id_is_set_before_tk_window_and_icons_are_strongly_held(self):
        source = inspect.getsource(FlashSyncApp.__init__)
        self.assertLess(source.index("set_windows_app_user_model_id()"), source.index("super().__init__()"))
        self.assertIn('iconbitmap(app_resource_path("sync_plus_icon.ico"))', source)
        self.assertIn('file=app_resource_path("sync_plus_icon.png")', source)
        self.assertIn("self._app_icon_photo", source)
        self.assertIn("self.iconphoto(True, self._app_icon_photo)", source)

    def test_app_user_model_id_api_and_frozen_resource_path(self):
        calls = []
        fake_shell32 = SimpleNamespace(
            SetCurrentProcessExplicitAppUserModelID=lambda value: calls.append(value)
        )
        with mock.patch.object(flash_sync_v02.os, "name", "nt"), \
                mock.patch.object(flash_sync_v02.ctypes, "windll", SimpleNamespace(shell32=fake_shell32)):
            self.assertTrue(flash_sync_v02.set_windows_app_user_model_id())
        self.assertEqual(calls, [flash_sync_v02.STANDALONE_APP_USER_MODEL_ID])
        with mock.patch.object(flash_sync_v02.sys, "_MEIPASS", r"C:\bundle", create=True):
            self.assertEqual(
                flash_sync_v02.app_resource_path("sync_plus_icon.png"),
                r"C:\bundle\sync_plus_icon.png",
            )

    def test_spec_keeps_both_icon_data_and_exe_resource(self):
        spec = (Path(__file__).resolve().parent / "fu_preview.spec").read_text(encoding="utf-8")
        self.assertIn('(str(root / "sync_plus_icon.ico"), ".")', spec)
        self.assertIn('(str(root / "sync_plus_icon.png"), ".")', spec)
        self.assertIn('icon=str(root / "sync_plus_icon.ico")', spec)
        self.assertIn('name="輔V0.2_自動重連獨立版_v2.1"', spec)


if __name__ == "__main__":
    unittest.main()
