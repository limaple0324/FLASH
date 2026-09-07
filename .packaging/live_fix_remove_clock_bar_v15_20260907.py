from pathlib import Path

ROOT = Path("legacy/fu-v02-reconnect-preview")
APP = ROOT / "flash_sync_v02.py"
TEST = ROOT / "test_fu_reconnect_integration.py"

app = APP.read_text(encoding="utf-8")

# Exact scope: only remove the startup invocation that recreates the standalone
# floating time display.  Do not alter the game-time backend, timed-key feature,
# quick-code floating control, sync/status floating control, or any section UI.
clock_call = "        self.create_clock_bar()\n"
count = app.count(clock_call)
if count != 1:
    for lineno, line in enumerate(app.splitlines(), 1):
        if "clock_bar" in line or "ClockBar" in line or "create_clock" in line:
            print(f"V15_CLOCK_CONTEXT {lineno}: {line!r}")
    raise SystemExit(f"v15 standalone clock startup call count={count}")
app = app.replace(clock_call, "", 1)
APP.write_text(app, encoding="utf-8", newline="\n")

text = TEST.read_text(encoding="utf-8")
anchor = '''class ExistingWindowReattachTests(unittest.TestCase):\n'''
case = '''class RemovedTimeDisplayRegressionTests(unittest.TestCase):\n    def test_startup_does_not_recreate_removed_clock_bar(self):\n        source = Path(__file__).with_name("flash_sync_v02.py").read_text(encoding="utf-8")\n        self.assertNotIn("        self.create_clock_bar()\\n", source)\n\n    def test_time_backend_and_other_floating_controls_are_untouched(self):\n        source = Path(__file__).with_name("flash_sync_v02.py").read_text(encoding="utf-8")\n        self.assertIn("def create_clock_bar(self) -> None:", source)\n        self.assertIn("def schedule_game_time_tick(self) -> None:", source)\n        self.assertIn("self.create_floating_status_window()", source)\n        self.assertIn("QuickCodeFloatingControl(", source)\n        self.assertIn('self.make_section("定時按下", True)', source)\n\n\n'''
if text.count(anchor) != 1:
    raise SystemExit(f"v15 test anchor count={text.count(anchor)}")
text = text.replace(anchor, case + anchor, 1)
TEST.write_text(text, encoding="utf-8", newline="\n")

print("LIVE_FIX_V15_APPLIED removed standalone time display stays removed; no other UI changed")
