from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

import numpy as np

import smart_reconnect


class UnexpectedPanelCloseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.old_template_bank = smart_reconnect.TB
        smart_reconnect.TB = smart_reconnect.TemplateBank()

    @classmethod
    def tearDownClass(cls) -> None:
        smart_reconnect.TB = cls.old_template_bank

    def test_standard_panel_x_is_found_in_upper_game_area(self) -> None:
        frame = np.zeros((590, 900, 3), dtype=np.uint8)
        template = smart_reconnect.TemplateBank._read_image(
            smart_reconnect.RESOURCE_DIR / "manor_assets" / "manor_close.png"
        )
        self.assertIsNotNone(template)
        th, tw = template.shape[:2]
        frame[80 : 80 + th, 650 : 650 + tw] = template

        found = smart_reconnect.find_unexpected_panel_close(frame)

        self.assertIsNotNone(found)
        self.assertEqual(found[:2], (650 + tw // 2, 80 + th // 2))

    def test_bottom_right_auto_area_is_not_searched(self) -> None:
        frame = np.zeros((590, 900, 3), dtype=np.uint8)
        template = smart_reconnect.TB.data["彈窗關閉X_自動副本"]
        th, tw = template.shape[:2]
        frame[520 : 520 + th, 840 : 840 + tw] = template

        self.assertIsNone(smart_reconnect.find_unexpected_panel_close(frame))

    def test_already_red_current_tab_advances_without_click(self) -> None:
        worker = smart_reconnect.GameWorker.__new__(smart_reconnect.GameWorker)
        threading.Thread.__init__(worker)
        worker.name = "測試角色"
        worker.fishing_phase = "待選目前分頁"
        worker.fishing_last_tab_warning_at = 0.0
        worker.set_event = lambda _message: None
        frame = np.zeros((590, 900, 3), dtype=np.uint8)

        with patch.object(
            worker,
            "_detect_current_chat_tab_dual",
            return_value=("紅色已選", (84, 530), "logical/測試"),
        ), patch.object(smart_reconnect.WIO, "click_foreground_physical") as click:
            worker._select_current_chat_tab_step(frame, 10.0)

        click.assert_not_called()
        self.assertEqual(worker.fishing_phase, "待確認發送目前頻道")

    def test_manor_active_skips_generic_panel_close(self) -> None:
        worker = smart_reconnect.GameWorker.__new__(smart_reconnect.GameWorker)
        worker.hwnd = 123
        worker.last_unexpected_panel_click = 0.0
        frame = np.zeros((590, 900, 3), dtype=np.uint8)

        with patch.object(smart_reconnect.manor_runtime, "is_hwnd_active", return_value=True), patch.object(
            smart_reconnect, "find_unexpected_panel_close"
        ) as find:
            handled = worker._close_unexpected_panel_step(frame, 10.0)

        self.assertFalse(handled)
        find.assert_not_called()

    def test_visible_manor_panel_stays_protected_after_runtime_lock_ends(self) -> None:
        worker = smart_reconnect.GameWorker.__new__(smart_reconnect.GameWorker)
        worker.hwnd = 123
        worker.last_unexpected_panel_click = 0.0
        frame = np.zeros((590, 900, 3), dtype=np.uint8)
        action = smart_reconnect.TemplateBank._read_image(
            smart_reconnect.RESOURCE_DIR / "manor_assets" / "action_bar.png"
        )
        self.assertIsNotNone(action)
        height, width = action.shape[:2]
        frame[481 : 481 + height, 374 : 374 + width] = action

        with patch.object(smart_reconnect.manor_runtime, "is_hwnd_active", return_value=False), patch.object(
            smart_reconnect, "find_unexpected_panel_close"
        ) as find:
            handled = worker._close_unexpected_panel_step(frame, 10.0)

        self.assertFalse(handled)
        find.assert_not_called()


if __name__ == "__main__":
    unittest.main()
