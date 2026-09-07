from pathlib import Path

ROOT = Path("legacy/fu-v02-reconnect-preview")
APP = ROOT / "flash_sync_v02.py"
TEST = ROOT / "test_fu_reconnect_integration.py"

app = APP.read_text(encoding="utf-8")
old = '''        self.update_window_title()\n        self.create_floating_status_window()\n        self.create_clock_bar()\n        self.setup_tray_icon()\n'''
new = '''        self.update_window_title()\n        self.create_floating_status_window()\n        # The standalone time display was intentionally removed from the product UI.\n        # Keep the game-time backend available for existing automation contracts,\n        # but never recreate the separate floating clock bar at startup.\n        self.setup_tray_icon()\n'''
if app.count(old) != 1:
    raise SystemExit(f"v15 startup clock-bar anchor count={app.count(old)}")
app = app.replace(old, new, 1)
APP.write_text(app, encoding="utf-8", newline="\n")

text = TEST.read_text(encoding="utf-8")
anchor = '''class ExistingWindowReattachTests(unittest.TestCase):\n'''
case = '''class RemovedTimeDisplayRegressionTests(unittest.TestCase):\n    def test_startup_does_not_recreate_removed_clock_bar(self):\n        source = Path(__file__).with_name("flash_sync_v02.py").read_text(encoding="utf-8")\n        init_tail = source.split("        self.update_window_title()", 1)[1].split(\n            "        self._start_worker()", 1\n        )[0]\n        self.assertIn("self.create_floating_status_window()", init_tail)\n        self.assertNotIn("self.create_clock_bar()", init_tail)\n        self.assertIn("self.setup_tray_icon()", init_tail)\n\n    def test_clock_backend_is_not_deleted_by_ui_fix(self):\n        source = Path(__file__).with_name("flash_sync_v02.py").read_text(encoding="utf-8")\n        self.assertIn("def create_clock_bar(self) -> None:", source)\n        self.assertIn("def schedule_game_time_tick(self) -> None:", source)\n\n\n'''
if text.count(anchor) != 1:
    raise SystemExit(f"v15 test anchor count={text.count(anchor)}")
text = text.replace(anchor, case + anchor, 1)
TEST.write_text(text, encoding="utf-8", newline="\n")

print("LIVE_FIX_V15_APPLIED removed standalone time display stays removed; no other UI changed")
