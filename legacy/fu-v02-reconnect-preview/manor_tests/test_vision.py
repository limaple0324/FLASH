from __future__ import annotations

import unittest

import cv2

from manor_assistant.vision import VisionEngine, asset_root


class VisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.vision = VisionEngine()
        cls.assets = asset_root()

    def test_nonbattle_evidence(self) -> None:
        frame = cv2.imread(str(self.assets / "main_nonbattle.png"))
        found, evidence = self.vision.nonbattle_evidence(frame)
        self.assertTrue(found)
        self.assertGreater(evidence["manor"], 0.9)
        self.assertGreater(evidence["line"], 0.9)

    def test_all_empty_plots(self) -> None:
        frame = cv2.imread(str(self.assets / "manor_empty_full.png"))
        action = self.vision.locate_action_bar(frame)
        self.assertIsNotNone(action)
        states = self.vision.classify_plots(frame, action)
        self.assertEqual([state.index for state in states if state.empty], list(range(1, 17)))

    def test_all_occupied_plots(self) -> None:
        frame = cv2.imread(str(self.assets / "manor_occupied.png"))
        action = self.vision.locate_action_bar(frame)
        self.assertIsNotNone(action)
        states = self.vision.classify_plots(frame, action)
        self.assertEqual([state.index for state in states if not state.empty], list(range(1, 17)))

    def test_shop_mapping_reference(self) -> None:
        frame = cv2.imread(str(self.assets / "shop_panel.png"))
        shop = self.vision.locate_shop(frame)
        self.assertIsNotNone(shop)
        self.assertGreater(shop.score, 0.99)


if __name__ == "__main__":
    unittest.main()
