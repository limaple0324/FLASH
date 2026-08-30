from __future__ import annotations

import inspect
import threading
import unittest
from unittest.mock import patch

import numpy as np

import smart_reconnect
from user_activity_guard import UserActivityGuard
from manor_assistant.workflow import ManorWorkflow


class FishingBackgroundResumeTests(unittest.TestCase):
    def test_all_fishing_input_paths_are_background_only(self) -> None:
        methods = (
            "_fishing_prepare_step",
            "_select_current_chat_tab_step",
            "_select_sender_current_channel_step",
            "_fishing_send",
            "_fishing_click_current_link",
        )
        source = "\n".join(inspect.getsource(getattr(smart_reconnect.GameWorker, name)) for name in methods)
        self.assertNotIn("click_foreground_physical", source)
        self.assertNotIn("send_chat_message_foreground_physical", source)
        self.assertIn("send_chat_message_background", source)

        interactive = inspect.getsource(smart_reconnect.WindowIO.click_interactive)
        self.assertIn('transport_override="純背景"', interactive)
        self.assertNotIn('transport_override="互動同步"', interactive)
        self.assertNotIn("_humanized_mouse_click_attached", interactive)

    def test_interactive_click_prefers_native_client_coordinates(self) -> None:
        wio = smart_reconnect.WIO
        with (
            patch.object(wio, "clear_calibration_if_environment_changed"),
            patch.object(
                wio,
                "candidate_modes",
                return_value=["接收視窗實際尺寸映射", "目標原生邏輯", "視窗客戶區"],
            ),
            patch.object(wio, "click_mode", return_value=True) as click_mode,
        ):
            self.assertTrue(wio.click_interactive(123, 863, 406, "飛行／降落"))
        click_mode.assert_called_once_with(
            123,
            863,
            406,
            "目標原生邏輯",
            "飛行／降落",
            root=False,
            transport_override="純背景",
        )

    def test_pure_background_click_queues_complete_release_sequence(self) -> None:
        wio = smart_reconnect.WIO
        posted = []

        def record(_target, message, wparam, lparam):
            posted.append((message, wparam, lparam))
            return True

        with (
            patch.object(wio, "_send_message_timeout", return_value=True),
            patch.object(wio, "_post_message", side_effect=record),
            patch.object(smart_reconnect.time, "sleep"),
        ):
            self.assertTrue(wio._sync_mouse_click(456, 863, 406, 123))

        messages = [message for message, _wparam, _lparam in posted]
        self.assertEqual(messages.count(smart_reconnect.win32con.WM_LBUTTONDOWN), 1)
        self.assertEqual(messages.count(smart_reconnect.win32con.WM_LBUTTONUP), 3)
        self.assertLess(
            messages.index(smart_reconnect.win32con.WM_LBUTTONUP),
            messages.index(smart_reconnect.win32con.WM_LBUTTONDOWN),
        )
        self.assertEqual(messages[-1], 0x02A3)

    def test_stuck_button_recovery_never_sends_mouse_down(self) -> None:
        wio = smart_reconnect.WIO
        posted = []

        def record(_target, message, wparam, lparam):
            posted.append((message, wparam, lparam))
            return True

        with (
            patch.object(
                wio,
                "_message_point_candidates",
                return_value=(456, "ShockwaveFlash", (0, 0), [("目標原生邏輯", 863, 406)]),
            ),
            patch.object(wio, "_post_message", side_effect=record),
        ):
            self.assertTrue(wio.release_interactive(123, 863, 406, "解除按住"))

        messages = [message for message, _wparam, _lparam in posted]
        self.assertNotIn(smart_reconnect.win32con.WM_LBUTTONDOWN, messages)
        self.assertGreaterEqual(messages.count(smart_reconnect.win32con.WM_LBUTTONUP), 2)
        self.assertIn(0x001F, messages)
        self.assertIn(0x0215, messages)

    def test_missing_flight_templates_use_ocr_without_release_spam(self) -> None:
        source = inspect.getsource(smart_reconnect.GameWorker._fishing_prepare_step)
        self.assertIn("find_fishing_state_buttons_ocr", source)
        self.assertNotIn("release_interactive", source)
        self.assertIn("目前不發送輸入", source)

    def test_native_normalized_interactive_click_is_one_to_one(self) -> None:
        wio = smart_reconnect.WIO
        old_surface = wio.surface_mode.get(123)
        wio.surface_mode[123] = "native-normalized"
        try:
            with (
                patch.object(wio, "clear_calibration_if_environment_changed"),
                patch.object(
                    wio,
                    "candidate_modes",
                    return_value=["接收視窗實際尺寸映射", "邏輯畫布直送"],
                ),
                patch.object(wio, "click_mode", return_value=True) as click_mode,
            ):
                self.assertTrue(wio.click_interactive(123, 84, 520, "目前分頁"))
            click_mode.assert_called_once_with(
                123, 84, 520, "邏輯畫布直送", "目前分頁",
                root=False, transport_override="純背景",
            )
        finally:
            if old_surface is None:
                wio.surface_mode.pop(123, None)
            else:
                wio.surface_mode[123] = old_surface

    def test_preparation_does_not_skip_missing_flight_or_clear_evidence(self) -> None:
        source = inspect.getsource(smart_reconnect.GameWorker._fishing_prepare_step)
        self.assertIn("留在本步，不略過", source)
        self.assertIn("確認聊天清除", source)

    def test_active_fishing_never_toggles_auto_battle_x_between_steps(self) -> None:
        source = inspect.getsource(smart_reconnect.GameWorker._fishing_step)
        self.assertIn('"等待結果"', source)
        self.assertIn('"釣魚中"', source)
        self.assertIn("if not fishing_action_active and not self._maintain_no_x_auto_battle", source)

    def test_manor_harvest_uses_harvest_not_all(self) -> None:
        source = inspect.getsource(ManorWorkflow._harvest_pass)
        self.assertIn("action_point(action, 90, 33)", source)
        self.assertNotIn("action_point(action, 150, 33)", source)

    def test_current_live_flight_evidence_is_accepted_without_confusing_land(self) -> None:
        old_bank = smart_reconnect.TB
        try:
            smart_reconnect.TB = smart_reconnect.TemplateBank()
            frame = np.zeros((590, 900, 3), dtype=np.uint8)
            evidence = smart_reconnect.TemplateBank._read_image(
                smart_reconnect.TEMPLATE_DIR / "fishing_flight_button_current.png"
            )
            self.assertIsNotNone(evidence)
            height, width = evidence.shape[:2]
            frame[392 : 392 + height, 828 : 828 + width] = evidence

            self.assertIsNotNone(smart_reconnect.find_fishing_state_button(frame, "飛行"))
            self.assertIsNone(smart_reconnect.find_fishing_state_button(frame, "降落"))
        finally:
            smart_reconnect.TB = old_bank

    def test_120fu_live_land_and_right_menu_collapse_evidence(self) -> None:
        old_bank = smart_reconnect.TB
        try:
            smart_reconnect.TB = smart_reconnect.TemplateBank()
            frame = np.zeros((600, 895, 3), dtype=np.uint8)
            land = smart_reconnect.TemplateBank._read_image(
                smart_reconnect.TEMPLATE_DIR / "fishing_land_button_120fu.png"
            )
            collapse = smart_reconnect.TemplateBank._read_image(
                smart_reconnect.TEMPLATE_DIR / "fishing_menu_collapse_button_live.png"
            )
            self.assertIsNotNone(land)
            self.assertIsNotNone(collapse)
            lh, lw = land.shape[:2]
            ch, cw = collapse.shape[:2]
            frame[419 : 419 + lh, 834 : 834 + lw] = land
            frame[293 : 293 + ch, 817 : 817 + cw] = collapse

            self.assertIsNotNone(smart_reconnect.find_fishing_state_button(frame, "降落"))
            self.assertIsNotNone(smart_reconnect.find_fishing_menu_collapse(frame))
        finally:
            smart_reconnect.TB = old_bank

    def test_120fu_collapsed_menu_arrow_can_reopen_flight_controls(self) -> None:
        old_bank = smart_reconnect.TB
        try:
            smart_reconnect.TB = smart_reconnect.TemplateBank()
            frame = np.zeros((572, 900, 3), dtype=np.uint8)
            expand = smart_reconnect.TemplateBank._read_image(
                smart_reconnect.TEMPLATE_DIR / "fishing_menu_expand_button_120fu.png"
            )
            self.assertIsNotNone(expand)
            eh, ew = expand.shape[:2]
            frame[263 : 263 + eh, 881 : 881 + ew] = expand
            self.assertIsNotNone(smart_reconnect.find_fishing_menu_expand(frame))
            self.assertIsNone(smart_reconnect.find_fishing_menu_collapse(frame))
            source = inspect.getsource(smart_reconnect.GameWorker._fishing_prepare_step)
            self.assertIn("展開系統列以確認飛行／降落", source)
            self.assertIn('self.fishing_phase = "確認系統列展開"', source)
        finally:
            smart_reconnect.TB = old_bank

    def test_user_activity_pauses_only_target_window_and_preserves_fishing_deadlines(self) -> None:
        source = inspect.getsource(smart_reconnect.GameWorker.step)
        self.assertIn("USER_ACTIVITY_GUARD.remaining(self.hwnd)", source)
        self.assertIn("本視窗暫停所有自動輸入，其他視窗照常", source)
        self.assertIn("paused_for", source)
        self.assertIn("fishing_deadline", source)

    def test_real_input_extends_only_foreground_root_three_minutes(self) -> None:
        guard = object.__new__(UserActivityGuard)
        guard.quiet_seconds = 180.0
        guard._lock = threading.RLock()
        guard._busy_until = {}
        guard._last_input_tick = 100
        guard.root = lambda hwnd: int(hwnd)
        with (
            patch.object(guard, "_snapshot", return_value=(101, 111, 0)),
            patch("user_activity_guard.time.monotonic", return_value=10.0),
        ):
            guard.observe()
        self.assertEqual(guard._busy_until, {111: 190.0})
        with (
            patch.object(guard, "_snapshot", return_value=(102, 222, 0)),
            patch("user_activity_guard.time.monotonic", return_value=20.0),
        ):
            guard.observe()
        self.assertEqual(guard._busy_until[111], 190.0)
        self.assertEqual(guard._busy_until[222], 200.0)

    def test_fishing_status_requires_three_misses_before_reset(self) -> None:
        source = inspect.getsource(smart_reconnect.GameWorker._fishing_step)
        self.assertIn('need = max(3, min(5', source)

    def test_every_resend_reenters_chat_clear_before_channel_selection(self) -> None:
        source = inspect.getsource(smart_reconnect.GameWorker._begin_fishing_channel_selection)
        self.assertIn('intent != "reclick"', source)
        self.assertIn('self.fishing_phase = "準備聊天"', source)
        map_source = inspect.getsource(smart_reconnect.GameWorker._fishing_step)
        self.assertIn('"轉圖後找不到原訊息；重新發送同一組', map_source)

    def test_right_menu_toggle_retries_only_when_live_expanded_evidence_remains(self) -> None:
        source = inspect.getsource(smart_reconnect.GameWorker._fishing_prepare_step)
        confirm = source.split('if self.fishing_phase == "確認系統列收回":', 1)[1]
        confirm = confirm.split('if self.fishing_phase == "準備聊天":', 1)[0]
        self.assertNotIn("click_interactive", confirm)
        self.assertIn("find_fishing_menu_still_expanded", confirm)
        self.assertIn("self.fishing_menu_collapse_attempts < 2", confirm)

    def test_visual_link_evidence_accepts_strict_regular_row_without_ocr_text(self) -> None:
        frame = np.zeros((572, 900, 3), dtype=np.uint8)
        links = [{"label": value} for value in ("51", "52", "53", "54")]
        with (
            patch.object(smart_reconnect, "OCR", type("O", (), {"enabled": True})()),
            patch.object(smart_reconnect, "_ocr_frame_region", return_value=[]),
            patch.object(
                smart_reconnect,
                "_visual_fishing_link_points",
                return_value=[(393, 521), (415, 521), (437, 521), (459, 521)],
            ),
        ):
            points, evidence = smart_reconnect.locate_fishing_link_points(frame, links)
        self.assertEqual(len(points), 4)
        self.assertIn("OCR漏字", evidence)

    def test_visual_detector_rejects_wide_chat_tab_spacing(self) -> None:
        frame = np.zeros((572, 900, 3), dtype=np.uint8)
        hsv_color = np.uint8([[[95, 220, 220]]])
        bgr = tuple(int(v) for v in smart_reconnect.cv2.cvtColor(hsv_color, smart_reconnect.cv2.COLOR_HSV2BGR)[0, 0])
        for center in (21, 141, 305, 423):
            smart_reconnect.cv2.line(frame, (center - 5, 528), (center + 5, 528), bgr, 1)
        self.assertEqual(smart_reconnect._visual_fishing_link_points(frame, 4), [])

    def test_visual_detector_rejects_four_regular_bottom_toolbar_icons(self) -> None:
        frame = np.zeros((572, 900, 3), dtype=np.uint8)
        hsv_color = np.uint8([[[95, 220, 220]]])
        bgr = tuple(int(v) for v in smart_reconnect.cv2.cvtColor(hsv_color, smart_reconnect.cv2.COLOR_HSV2BGR)[0, 0])
        for center, y in ((365, 529), (387, 526), (411, 526), (434, 532)):
            smart_reconnect.cv2.line(frame, (center - 5, y), (center + 5, y), bgr, 1)
        self.assertEqual(smart_reconnect._visual_fishing_link_points(frame, 4), [])

    def test_yellow_fishing_codes_require_four_measured_bracket_pairs(self) -> None:
        frame = np.zeros((572, 900, 3), dtype=np.uint8)
        yellow_hsv = np.uint8([[[24, 255, 255]]])
        yellow = tuple(int(v) for v in smart_reconnect.cv2.cvtColor(yellow_hsv, smart_reconnect.cv2.COLOR_HSV2BGR)[0, 0])
        for left in (114, 134, 154, 174):
            smart_reconnect.cv2.line(frame, (left, 413), (left, 423), yellow, 1)
            smart_reconnect.cv2.line(frame, (left + 16, 413), (left + 16, 423), yellow, 1)
            smart_reconnect.cv2.line(frame, (left + 5, 415), (left + 9, 421), yellow, 1)
        points = smart_reconnect._visual_yellow_fishing_code_points(frame, 4)
        self.assertEqual([point[0] for point in points], [122, 142, 162, 182])

    def test_dim_antialiased_yellow_codes_work_across_flash_windows(self) -> None:
        frame = np.zeros((572, 900, 3), dtype=np.uint8)
        # This low-saturation/brightness sample was outside the old detector's
        # range but remains visibly yellow in normalized Flash captures.
        yellow_hsv = np.uint8([[[18, 110, 150]]])
        yellow = tuple(
            int(v)
            for v in smart_reconnect.cv2.cvtColor(
                yellow_hsv, smart_reconnect.cv2.COLOR_HSV2BGR
            )[0, 0]
        )
        for left in (114, 134, 154, 174):
            smart_reconnect.cv2.line(frame, (left, 413), (left, 423), yellow, 1)
            smart_reconnect.cv2.line(frame, (left + 16, 413), (left + 16, 423), yellow, 1)
            smart_reconnect.cv2.line(frame, (left + 5, 415), (left + 9, 421), yellow, 1)
        points = smart_reconnect._visual_yellow_fishing_code_points(frame, 4)
        self.assertEqual([point[0] for point in points], [122, 142, 162, 182])

    def test_chat_clear_button_is_pressed_four_times_before_send(self) -> None:
        source = inspect.getsource(smart_reconnect.GameWorker._fishing_prepare_step)
        clear_block = source.split('if self.fishing_phase == "準備聊天":', 1)[1]
        clear_block = clear_block.split('return self.fishing_phase == "前置完成"', 1)[0]
        self.assertIn("self.fishing_chat_clear_attempts += 1", clear_block)
        self.assertIn("self.fishing_chat_clear_attempts < 4", clear_block)
        self.assertIn("聊天框『－』已背景連按 4 次", clear_block)

    def test_link_timeout_keeps_message_and_yields_other_windows(self) -> None:
        source = inspect.getsource(smart_reconnect.GameWorker._fishing_step)
        wait_block = source.split('if self.fishing_phase == "等待連結":', 1)[1]
        wait_block = wait_block.split('if self.fishing_phase == "轉圖後等待連結":', 1)[0]
        self.assertNotIn('self.fishing_phase = "發送重試"', wait_block)
        self.assertIn("self.fishing_next_locate_at = now + retry", wait_block)
        self.assertIn("不重送、不推算點擊，先輪巡其他視窗", wait_block)

    def test_weak_current_sender_template_cannot_confirm_guild_as_current(self) -> None:
        source = inspect.getsource(smart_reconnect.detect_sender_chat_channel)
        self.assertIn('local_state != "目前已選" or local_score >= 0.90', source)


if __name__ == "__main__":
    unittest.main()
