from pathlib import Path

ROOT = Path("legacy/fu-v02-reconnect-preview")
SOURCE = ROOT / "smart_reconnect.py"
TEST = ROOT / "manor_tests" / "test_fishing_background_resume.py"

source = SOURCE.read_text(encoding="utf-8")

old = '''        self.fishing_menu_collapse_attempts = 0\n        self.fishing_menu_expand_attempts = 0\n        self.fishing_menu_expand_next_at = 0.0\n        self.fishing_message_group_index = 0\n'''
new = '''        self.fishing_menu_collapse_attempts = 0\n        self.fishing_menu_expand_attempts = 0\n        self.fishing_menu_expand_next_at = 0.0\n        self.fishing_flight_confirm_failures = 0\n        self.fishing_message_group_index = 0\n'''
if source.count(old) != 2:
    raise SystemExit(f"fishing init/reset anchor count={source.count(old)}")
source = source.replace(old, new, 2)

old = '''            if flying:\n                self.fishing_phase = "準備收回系統列"\n                self.set_event("釣魚前置：已由『降落』按鈕確認目前正在飛行")\n                return False\n'''
new = '''            if flying:\n                self.fishing_flight_confirm_failures = 0\n                self.fishing_phase = "準備收回系統列"\n                self.set_event("釣魚前置：已由『降落』按鈕確認目前正在飛行")\n                return False\n'''
if source.count(old) != 1:
    raise SystemExit(f"prepare flying-positive anchor count={source.count(old)}")
source = source.replace(old, new, 1)

old = '''            if flying:\n                self.fishing_phase = "準備收回系統列"\n                self.set_event("釣魚前置：已看到『降落』，飛行狀態確認完成")\n            elif now >= self.fishing_prepare_deadline:\n                self.fishing_phase = "準備飛行"\n                self.set_event("釣魚前置：未確認『降落』，重新辨識飛行狀態")\n            return False\n'''
new = '''            if flying:\n                self.fishing_flight_confirm_failures = 0\n                self.fishing_phase = "準備收回系統列"\n                self.set_event("釣魚前置：已看到『降落』，飛行狀態確認完成")\n            elif now >= self.fishing_prepare_deadline:\n                self.fishing_flight_confirm_failures += 1\n                limit = max(2, min(4, int(CONFIG.get("釣魚飛行確認失敗上限", 2))))\n                if self.fishing_flight_confirm_failures >= limit:\n                    retry_s = max(5.0, float(CONFIG.get("釣魚不可飛行重試秒", 10.0)))\n                    self.fishing_phase = "等待可飛行場景"\n                    self.fishing_prepare_deadline = now + retry_s\n                    self.set_event(\n                        f"釣魚暫停：目前場景拒絕飛行；其他監控照常，約 {int(retry_s)} 秒後再探測"\n                    )\n                    LOG.warning(\n                        "[%s] 連續 %d 次點『飛行』後都未出現『降落』；判定目前場景暫時不可飛行。"\n                        "釣魚進入等待，不再卡住自動戰鬥／斷線監控；%.1f 秒後再探測。",\n                        self.name, self.fishing_flight_confirm_failures, retry_s,\n                    )\n                else:\n                    self.fishing_phase = "準備飛行"\n                    self.set_event(\n                        f"釣魚前置：未確認『降落』 {self.fishing_flight_confirm_failures}/{limit}，再重試一次"\n                    )\n            return False\n'''
if source.count(old) != 1:
    raise SystemExit(f"confirm-flight anchor count={source.count(old)}")
source = source.replace(old, new, 1)

old = '''        if not fishing_action_active and not self._maintain_no_x_auto_battle(frame, now):\n            return\n\n        if self.fishing_phase == "待無X後重送":\n'''
new = '''        if not fishing_action_active and not self._maintain_no_x_auto_battle(frame, now):\n            return\n\n        if self.fishing_phase == "等待可飛行場景":\n            if now < self.fishing_prepare_deadline:\n                remaining = max(1, int(self.fishing_prepare_deadline - now + 0.999))\n                self.set_event(\n                    f"釣魚暫停：目前場景不可飛行；其他監控照常，約 {remaining} 秒後再探測"\n                )\n                return\n            self.fishing_phase = "準備飛行"\n            self.fishing_prepare_deadline = 0.0\n            self.set_event("釣魚：重新探測目前場景是否已可飛行")\n            return\n\n        if self.fishing_phase == "待無X後重送":\n'''
if source.count(old) != 1:
    raise SystemExit(f"wait-dispatch anchor count={source.count(old)}")
source = source.replace(old, new, 1)

SOURCE.write_text(source, encoding="utf-8", newline="\n")

test = TEST.read_text(encoding="utf-8")
anchor = '''    def test_native_normalized_interactive_click_is_one_to_one(self) -> None:\n'''
case = '''    def test_repeated_flight_refusal_waits_without_blocking_no_x_monitoring(self) -> None:\n        worker = self.fishing_worker()\n        frame = np.zeros((572, 900, 3), dtype=np.uint8)\n        worker.fishing_profile = {"name": "五級魚"}\n        worker.fishing_phase = "確認飛行"\n        worker.fishing_prepare_deadline = 99.0\n        worker.fishing_state_ocr_at = 0.0\n        worker.fishing_flight_confirm_failures = 1\n        with (\n            patch.object(smart_reconnect, "find_fishing_state_button", return_value=None),\n            patch.object(smart_reconnect, "find_fishing_state_buttons_ocr", return_value={}),\n            patch.dict(\n                smart_reconnect.CONFIG,\n                {"釣魚飛行確認失敗上限": 2, "釣魚不可飛行重試秒": 10.0},\n                clear=False,\n            ),\n        ):\n            self.assertFalse(worker._fishing_prepare_step(frame, 100.0))\n        self.assertEqual(worker.fishing_phase, "等待可飛行場景")\n        self.assertEqual(worker.fishing_prepare_deadline, 110.0)\n\n        worker.fishing_prerequisites_ready = True\n        worker.startup_login_probe_until = 0.0\n        worker.state = "監看"\n        worker.flow_started_at = 0.0\n        worker._refresh_fishing_profile = unittest.mock.Mock()\n        worker._maintain_no_x_auto_battle = unittest.mock.Mock(return_value=True)\n        with patch.object(smart_reconnect.manor_runtime, "is_hwnd_active", return_value=False):\n            worker._fishing_step(frame, 101.0)\n        worker._maintain_no_x_auto_battle.assert_called_once_with(frame, 101.0)\n        self.assertEqual(worker.fishing_phase, "等待可飛行場景")\n\n\n'''
if test.count(anchor) != 1:
    raise SystemExit(f"test insertion anchor count={test.count(anchor)}")
test = test.replace(anchor, case + anchor, 1)
TEST.write_text(test, encoding="utf-8", newline="\n")

print("LIVE_FIX_APPLIED arena/no-flight bounded wait with no-X monitoring preserved")
