from __future__ import annotations

import unittest
from unittest.mock import patch

import numpy as np

import smart_reconnect


class FishingOpenCVCompatibilityTests(unittest.TestCase):
    def test_hough_lines_accepts_opencv4_and_opencv5_shapes(self) -> None:
        frame = np.zeros((600, 900, 3), dtype=np.uint8)
        opencv4 = np.asarray([[[20, 10, 400, 10]], [[30, 20, 420, 20]]], dtype=np.int32)
        opencv5 = opencv4.reshape(-1, 4)

        with patch.object(smart_reconnect.cv2, "HoughLinesP", return_value=opencv4):
            result4 = smart_reconnect._chat_horizontal_lines(frame)
        with patch.object(smart_reconnect.cv2, "HoughLinesP", return_value=opencv5):
            result5 = smart_reconnect._chat_horizontal_lines(frame)

        self.assertEqual(result4, result5)
        self.assertEqual(result5, [(20, 382, 400, 382), (30, 392, 420, 392)])


if __name__ == "__main__":
    unittest.main()
