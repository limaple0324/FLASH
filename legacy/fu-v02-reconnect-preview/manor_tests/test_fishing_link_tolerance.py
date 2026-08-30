from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

import fishing_profiles
import smart_reconnect


def ocr_item(text: str, left: int, top: int, right: int, bottom: int):
    box = np.asarray(
        [[left, top], [right, top], [right, bottom], [left, bottom]],
        dtype=np.float32,
    )
    return smart_reconnect.OCRItem(text=text, score=0.95, box=box)


class FishingLinkToleranceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.frame = np.zeros((600, 900, 3), dtype=np.uint8)
        self.links = fishing_profiles.parse_message_links(
            "[@N|1189|11][@N|1190|12][@N|1191|13]"
        )

    def test_merged_two_digit_labels_are_split_left_to_right(self) -> None:
        item = ocr_item("11 12 13", 100, 400, 280, 420)
        with patch.object(smart_reconnect, "OCR", SimpleNamespace(enabled=True)), patch.object(
            smart_reconnect, "_ocr_frame_region", return_value=[item]
        ):
            points, route = smart_reconnect.locate_fishing_link_points(self.frame, self.links)
        self.assertEqual(len(points), 3)
        self.assertEqual(points, sorted(points))
        self.assertTrue(route.startswith("OCR:"), route)

    def test_common_one_glyph_variants_are_normalized(self) -> None:
        self.assertEqual(smart_reconnect.normalize_fishing_link_text("1l l2 I3"), "111213")

    def test_partial_ocr_can_anchor_exact_count_visual_underlines(self) -> None:
        item = ocr_item("11", 95, 400, 130, 420)
        expected = [(110, 410), (170, 410), (230, 410)]
        with patch.object(smart_reconnect, "OCR", SimpleNamespace(enabled=True)), patch.object(
            smart_reconnect, "_ocr_frame_region", return_value=[item]
        ), patch.object(smart_reconnect, "_visual_fishing_link_points", return_value=expected):
            points, route = smart_reconnect.locate_fishing_link_points(self.frame, self.links)
        self.assertEqual(points, expected)
        self.assertTrue(route.startswith("OCR+青色底線容錯:"), route)

    def test_single_ambiguous_digit_never_becomes_three_clicks(self) -> None:
        item = ocr_item("1", 100, 400, 112, 420)
        with patch.object(smart_reconnect, "OCR", SimpleNamespace(enabled=True)), patch.object(
            smart_reconnect, "_ocr_frame_region", return_value=[item]
        ), patch.object(smart_reconnect, "_visual_fishing_link_points", return_value=[]):
            points, _route = smart_reconnect.locate_fishing_link_points(self.frame, self.links)
        self.assertEqual(points, [])

    def test_tail_tolerance_stays_right_of_sender_name(self) -> None:
        links = fishing_profiles.parse_message_links(
            "[@N|1212|51][@N|1213|52][@N|1214|53][@N|1215|54]"
        )
        item = ocr_item("目前[嘻の百二射手]:1][52[53][54", 0, 440, 200, 462)
        with patch.object(smart_reconnect, "OCR", SimpleNamespace(enabled=True)), patch.object(
            smart_reconnect, "_ocr_frame_region", return_value=[item]
        ), patch.object(smart_reconnect, "_visual_fishing_link_points", return_value=[]):
            points, route = smart_reconnect.locate_fishing_link_points(self.frame, links)
        self.assertEqual(len(points), 4)
        self.assertTrue(route.startswith("OCR尾端等寬:"), route)
        self.assertGreater(points[0][0], 100)
        self.assertEqual(points, sorted(points))


if __name__ == "__main__":
    unittest.main()
