from pathlib import Path

ROOT = Path("legacy/fu-v02-reconnect-preview")
APP = ROOT / "flash_sync_v02.py"
TEST = ROOT / "test_fu_reconnect_integration.py"

app = APP.read_text(encoding="utf-8")

# V14 cumulative source already contains the user's intended UI state: the
# standalone time window is not instantiated.  This patch is therefore only a
# regression lock.  It must not add/remove any product UI or backend behavior.
if "        self.create_clock_bar()\n" in app:
    raise SystemExit("v15 regression: standalone time window startup call returned")
method_anchor = "    def create_clock_bar(self) -> None:\n"
if app.count(method_anchor) != 1:
    raise SystemExit(f"v15 create_clock_bar API anchor count={app.count(method_anchor)}")
method = app.split(method_anchor, 1)[1].split("    def clock_bar_settings", 1)[0]
if "self.clock_bar = None" not in method or "ClockBar(" in method:
    raise SystemExit("v15 regression: removed clock API is no longer disabled")

text = TEST.read_text(encoding="utf-8")
anchor = '''class ExistingWindowReattachTests(unittest.TestCase):\n'''
case = '''class RemovedTimeDisplayRegressionTests(unittest.TestCase):\n    def test_removed_standalone_time_window_stays_removed(self):\n        source = Path(__file__).with_name("flash_sync_v02.py").read_text(encoding="utf-8")\n        self.assertNotIn("        self.create_clock_bar()\\n", source)\n        method = source.split("    def create_clock_bar(self) -> None:\\n", 1)[1].split(\n            "    def clock_bar_settings", 1\n        )[0]\n        self.assertIn("self.clock_bar = None", method)\n        self.assertNotIn("ClockBar(", method)\n\n    def test_code_and_sync_status_floating_controls_are_untouched(self):\n        source = Path(__file__).with_name("flash_sync_v02.py").read_text(encoding="utf-8")\n        self.assertIn("self.create_floating_status_window()", source)\n        self.assertIn("QuickCodeFloatingControl(", source)\n\n\n'''
if text.count(anchor) != 1:
    raise SystemExit(f"v15 test anchor count={text.count(anchor)}")
text = text.replace(anchor, case + anchor, 1)
TEST.write_text(text, encoding="utf-8", newline="\n")

print("LIVE_FIX_V15_ASSERTED standalone time display remains removed; product UI unchanged")
