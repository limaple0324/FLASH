from pathlib import Path

ROOT = Path("legacy/fu-v02-reconnect-preview")
INTEGRATION = ROOT / "fu_reconnect_integration.py"
TEST = ROOT / "test_fu_reconnect_integration.py"

source = INTEGRATION.read_text(encoding="utf-8")
old = '''        self._validated_at: dict[int, float] = {}\n        self._load_settings()\n        self._install_policy()\n        self._monitor_thread = threading.Thread(target=self._monitor_loop, name="fu-strict-identity-monitor", daemon=True)\n'''
new = '''        self._validated_at: dict[int, float] = {}\n        self._load_settings()\n        self._ensure_recognition_runtime()\n        self._install_policy()\n        self._monitor_thread = threading.Thread(target=self._monitor_loop, name="fu-strict-identity-monitor", daemon=True)\n'''
if source.count(old) != 1:
    raise SystemExit(f"integration init anchor count={source.count(old)}")
source = source.replace(old, new, 1)

anchor = '''    @staticmethod\n    def _parse_utc_iso(value: object) -> datetime | None:\n'''
helper = '''    @staticmethod\n    def _ensure_recognition_runtime() -> None:\n        """Initialize smart_reconnect recognition services for embedded mode.\n\n        smart_reconnect.main() normally owns this initialization, but the embedded\n        controller never enters main(). Keep the shared services singleton-like\n        and fail at controller startup instead of letting every managed worker hit\n        ``None.match`` later in the recognition loop.\n        """\n        if sr.OCR is None:\n            sr.OCR = sr.OCRReader(bool(sr.CONFIG.get("啟用OCR", True)))\n        if sr.TB is None:\n            sr.TB = sr.TemplateBank()\n\n'''
if source.count(anchor) != 1:
    raise SystemExit(f"integration helper anchor count={source.count(anchor)}")
source = source.replace(anchor, helper + anchor, 1)
INTEGRATION.write_text(source, encoding="utf-8", newline="\n")

text = TEST.read_text(encoding="utf-8")
test_anchor = '''class StrictRegistryTests(unittest.TestCase):\n'''
test_case = '''class EmbeddedRecognitionRuntimeTests(unittest.TestCase):\n    def test_controller_initializes_shared_recognition_runtime_for_embedded_mode(self):\n        fake_ocr = object()\n        fake_tb = object()\n        with tempfile.TemporaryDirectory() as td, \\\n             mock.patch.object(sr, "OCR", None), \\\n             mock.patch.object(sr, "TB", None), \\\n             mock.patch.object(sr, "OCRReader", return_value=fake_ocr) as ocr_ctor, \\\n             mock.patch.object(sr, "TemplateBank", return_value=fake_tb) as tb_ctor:\n            ctl = EmbeddedAutomationController(Path(td) / "settings.json", lambda _hwnd: True)\n            try:\n                self.assertIs(sr.OCR, fake_ocr)\n                self.assertIs(sr.TB, fake_tb)\n                ocr_ctor.assert_called_once_with(bool(sr.CONFIG.get("啟用OCR", True)))\n                tb_ctor.assert_called_once_with()\n            finally:\n                ctl.stop()\n\n\n'''
if text.count(test_anchor) != 1:
    raise SystemExit(f"test anchor count={text.count(test_anchor)}")
text = text.replace(test_anchor, test_case + test_anchor, 1)
TEST.write_text(text, encoding="utf-8", newline="\n")

print("LIVE_FIX_APPLIED embedded recognition runtime initialization")
