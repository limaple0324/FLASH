from pathlib import Path

ROOT = Path('legacy/fu-v02-reconnect-preview')
INTEGRATION = ROOT / 'fu_reconnect_integration.py'
TEST = ROOT / 'test_fu_reconnect_integration.py'
APP = ROOT / 'flash_sync_v02.py'

source = INTEGRATION.read_text(encoding='utf-8')
anchor = '''    def authorize_launch_transaction(\n'''
helper = '''    @staticmethod\n    def _identityless_existing_reattach_hwnd(\n        entry_id: str,\n        identity: str,\n        allow_existing_reattach: bool,\n        existing_reattach_hwnds: set[int],\n        existing_reattach_bindings: dict[str, int],\n    ) -> int:\n        """Return the exact pre-proven HWND allowed for blank-identity reattach.\n\n        This never authorizes a normal new launch. It only recognizes the host's\n        explicit entry_id->HWND proof when that HWND is also in the immutable\n        pre-existing reattach allow-list.\n        """\n        if identity or not allow_existing_reattach or not entry_id:\n            return 0\n        try:\n            hwnd = int(existing_reattach_bindings.get(str(entry_id), 0) or 0)\n        except (TypeError, ValueError):\n            return 0\n        if not hwnd or hwnd not in existing_reattach_hwnds:\n            return 0\n        return hwnd\n\n'''
if source.count(anchor) != 1:
    raise SystemExit(f'identityless-helper method anchor count={source.count(anchor)}')
source = source.replace(anchor, helper + anchor, 1)

old = '''                explicit_reattach_hwnd = int(\n                    existing_reattach_bindings.get(eid, 0) or 0\n                ) if eid else 0\n                identityless_reattach = bool(\n                    allow_existing_reattach\n                    and eid\n                    and not identity\n                    and explicit_reattach_hwnd\n                    and explicit_reattach_hwnd in existing_reattach_hwnds\n                )\n'''
new = '''                explicit_reattach_hwnd = self._identityless_existing_reattach_hwnd(\n                    eid, identity, allow_existing_reattach,\n                    existing_reattach_hwnds, existing_reattach_bindings,\n                )\n                identityless_reattach = bool(explicit_reattach_hwnd)\n'''
if source.count(old) != 1:
    raise SystemExit(f'identityless-helper call anchor count={source.count(old)}')
source = source.replace(old, new, 1)
INTEGRATION.write_text(source, encoding='utf-8', newline='\n')

# Replace the heavyweight controller fixture/tests with direct tests of the
# proof helper actually used by authorize_launch_transaction. Existing V8 tests
# continue to exercise the PID/creation-time candidate-reason gate.
test = TEST.read_text(encoding='utf-8')
class_pos = test.find('class ExistingWindowReattachTests(unittest.TestCase):')
if class_pos < 0:
    raise SystemExit('identityless-helper test class missing')
start = test.find('    @staticmethod\n    def identityless_controller', class_pos)
end = test.find('    def test_preexisting_candidate_requires_explicit_snapshot_whitelist', start)
if start < 0 or end < 0 or end <= start:
    raise SystemExit(f'identityless-helper test slice invalid start={start} end={end}')
replacement = '''    def test_explicit_existing_binding_allows_blank_account_identity(self):\n        hwnd = EmbeddedAutomationController._identityless_existing_reattach_hwnd(\n            "entry-a", "", True, {1001}, {"entry-a": 1001}\n        )\n        self.assertEqual(hwnd, 1001)\n\n    def test_blank_identity_without_exact_entry_hwnd_binding_is_rejected(self):\n        hwnd = EmbeddedAutomationController._identityless_existing_reattach_hwnd(\n            "entry-a", "", True, {1001}, {}\n        )\n        self.assertEqual(hwnd, 0)\n\n    def test_blank_identity_stays_rejected_for_normal_new_launch(self):\n        self.assertEqual(\n            EmbeddedAutomationController._identityless_existing_reattach_hwnd(\n                "entry-a", "", False, {1001}, {"entry-a": 1001}\n            ),\n            0,\n        )\n        self.assertEqual(\n            EmbeddedAutomationController._identityless_existing_reattach_hwnd(\n                "entry-a", "nonblank-identity", True, {1001}, {"entry-a": 1001}\n            ),\n            0,\n        )\n\n    def test_host_passes_exact_entry_hwnd_binding_and_notice_strip_is_removed(self):\n        app = (Path(__file__).parent / "flash_sync_v02.py").read_text(encoding="utf-8")\n        self.assertIn('"existing_reattach_bindings"', app)\n        self.assertNotIn(\n            "身份唯一且生命週期可驗證的既有視窗可安全接回；只有缺少或身份衝突的視窗才需要重新開啟。",\n            app,\n        )\n\n'''
test = test[:start] + replacement + test[end:]
TEST.write_text(test, encoding='utf-8', newline='\n')

print('LIVE_FIX_APPLIED identityless reattach proof helper + focused regression coverage')
