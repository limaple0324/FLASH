from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

import cv2

from manor_assistant.models import Profile
from manor_assistant.vision import VisionEngine, asset_root
from manor_assistant.workflow import ManorWorkflow


class FakeWindowSession:
    vision = VisionEngine()
    main = cv2.imread(str(asset_root() / "main_nonbattle.png"))
    manor_empty = cv2.imread(str(asset_root() / "manor_empty_full.png"))
    manor_one = cv2.imread(str(asset_root() / "manor_one.png"))
    shop = cv2.imread(str(asset_root() / "shop_panel.png"))

    def __init__(self, _hwnd: int, _cancelled: threading.Event | None = None) -> None:
        self.frame = self.main.copy()
        self.capture_method = "模擬背景"
        self.offscreen = False
        self.harvest_mode = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def capture(self):
        return self.frame.copy()

    def promote_offscreen(self) -> bool:
        self.offscreen = True
        return True

    def move(self, _x: int, _y: int) -> bool:
        return True

    def click(self, x: int, y: int) -> bool:
        shop = self.vision.locate_shop(self.frame)
        if shop:
            self.frame = self.manor_empty.copy()
            return True
        action = self.vision.locate_action_bar(self.frame)
        if not action:
            if self.vision.locate_manor_button(self.frame):
                self.frame = self.manor_empty.copy()
            return True
        # Action buttons sit below all plots.
        if y >= action.y:
            relative_x = (x - action.x) / action.scale
            if relative_x < 70:
                self.frame = self.shop.copy()
            elif 55 <= relative_x <= 115:
                self.harvest_mode = True
            return True
        if self.harvest_mode:
            self.frame = self.manor_empty.copy()
        else:
            self.frame = self.manor_one.copy()
        return True


class FastWorkflow(ManorWorkflow):
    @staticmethod
    def _wait(cancelled: threading.Event, _seconds: float) -> bool:
        return not cancelled.is_set()


class WorkflowTests(unittest.TestCase):
    def test_complete_flow_uses_background_messages(self) -> None:
        messages: list[str] = []
        workflow = FastWorkflow(VisionEngine(), lambda _profile, message: messages.append(message))
        profile = Profile(
            shortcut_path=r"C:\Game\角色一.lnk",
            shortcut_name="角色一",
            crop_key="normal_rock",
            quantity=1,
        )
        with patch("manor_assistant.workflow.BackgroundWindowSession", FakeWindowSession):
            result = workflow.run(profile, 1234, threading.Event())
        self.assertEqual(result.kind, "success", result.message)
        self.assertTrue(any("非戰鬥畫面確認" in message for message in messages))
        self.assertTrue(any("已種植第 1 格" in message for message in messages))
        self.assertTrue(any("點擊「收穫」" in message for message in messages))
        self.assertFalse(any("點擊「全部」" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
