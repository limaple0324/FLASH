from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys

import cv2
import numpy as np


BASE_CLIENT_WIDTH = 897
BASE_CLIENT_HEIGHT = 573
ACTION_REFERENCE_ORIGIN = (374, 481)
PLOT_REFERENCE_POINTS = [
    (416, 222),
    (359, 254), (471, 258),
    (306, 287), (416, 290), (528, 294),
    (254, 318), (363, 323), (473, 326), (584, 330),
    (309, 353), (420, 359), (529, 362),
    (366, 390), (476, 395),
    (422, 425),
]

# 使用者畫面上的編號不是上到下逐橫排，而是沿左下斜列依序編號。
# 參考圖仍維持原本的橫排行序；所有種植／收穫動作必須透過此映射。
USER_PLOT_ORDER = (0, 1, 3, 6, 2, 4, 7, 10, 5, 8, 11, 13, 9, 12, 14, 15)


def user_plot_indices(quantity: int = 16) -> tuple[int, ...]:
    if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 16:
        raise ValueError("格數必須是 1–16 的整數")
    return USER_PLOT_ORDER[:quantity]


def asset_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "manor_assets"
    return Path(__file__).resolve().parents[1] / "manor_assets"


def read_image(path: Path) -> np.ndarray | None:
    """OpenCV on Windows may reject non-ASCII packaged paths; decode raw bytes first."""
    try:
        raw = np.fromfile(str(path), dtype=np.uint8)
        if raw.size:
            image = cv2.imdecode(raw, cv2.IMREAD_COLOR)
            if image is not None:
                return image
    except (OSError, ValueError):
        pass
    return cv2.imread(str(path), cv2.IMREAD_COLOR)


@dataclass(frozen=True)
class Match:
    x: int
    y: int
    width: int
    height: int
    score: float
    scale: float

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2


@dataclass(frozen=True)
class PlotState:
    index: int
    empty: bool
    similarity: float
    point: tuple[int, int]


class VisionEngine:
    def __init__(self, assets: Path | None = None) -> None:
        self.assets = assets or asset_root()
        names = [
            "manor_button",
            "network_status",
            "action_bar",
            "shop_panel",
            "manor_close",
        ]
        self.templates: dict[str, np.ndarray] = {}
        for name in names:
            image = read_image(self.assets / f"{name}.png")
            if image is None:
                raise FileNotFoundError(self.assets / f"{name}.png")
            self.templates[name] = image
        self.empty_patches = []
        for index in range(1, 17):
            patch = read_image(self.assets / "empty_plots" / f"plot_{index:02d}.png")
            if patch is None:
                raise FileNotFoundError(f"empty plot reference {index}")
            self.empty_patches.append(patch)
        self.harvest_cooldown = read_image(self.assets / "harvest_cooldown.png")
        if self.harvest_cooldown is None:
            raise FileNotFoundError(self.assets / "harvest_cooldown.png")
        self._resize_cache: dict[tuple[str, int], np.ndarray] = {}

    def _scales_for(self, frame: np.ndarray) -> list[float]:
        height, width = frame.shape[:2]
        expected = min(width / BASE_CLIENT_WIDTH, height / BASE_CLIENT_HEIGHT)
        factors = [0.78, 0.84, 0.9, 0.94, 0.97, 1.0, 1.03, 1.06, 1.1, 1.16, 1.24]
        values = {round(expected * factor, 3) for factor in factors}
        values.update({0.75, 0.85, 1.0, 1.15, 1.25, 1.5, 1.75, 2.0})
        return sorted(value for value in values if 0.45 <= value <= 2.5)

    def _resized_gray(self, name: str, scale: float) -> np.ndarray:
        key = (name, round(scale * 1000))
        if key not in self._resize_cache:
            original = self.templates[name]
            width = max(8, round(original.shape[1] * scale))
            height = max(8, round(original.shape[0] * scale))
            resized = cv2.resize(original, (width, height), interpolation=cv2.INTER_AREA)
            self._resize_cache[key] = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        return self._resize_cache[key]

    def locate(
        self,
        frame: np.ndarray,
        name: str,
        threshold: float = 0.76,
        scales: list[float] | None = None,
    ) -> Match | None:
        if frame is None or frame.size == 0:
            return None
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        best: Match | None = None
        for scale in scales or self._scales_for(frame):
            template = self._resized_gray(name, scale)
            th, tw = template.shape[:2]
            if th > gray.shape[0] or tw > gray.shape[1]:
                continue
            result = cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED)
            _, score, _, location = cv2.minMaxLoc(result)
            candidate = Match(location[0], location[1], tw, th, float(score), scale)
            if best is None or candidate.score > best.score:
                best = candidate
        return best if best is not None and best.score >= threshold else None

    def locate_action_bar(self, frame: np.ndarray) -> Match | None:
        return self.locate(frame, "action_bar", threshold=0.74)

    def locate_shop(self, frame: np.ndarray) -> Match | None:
        return self.locate(frame, "shop_panel", threshold=0.7)

    def locate_manor_button(self, frame: np.ndarray) -> Match | None:
        return self.locate(frame, "manor_button", threshold=0.78)

    def locate_close(self, frame: np.ndarray) -> Match | None:
        return self.locate(frame, "manor_close", threshold=0.72)

    def nonbattle_evidence(self, frame: np.ndarray) -> tuple[bool, dict[str, float]]:
        manor = self.locate(frame, "manor_button", threshold=0.72)
        line = self.locate(frame, "network_status", threshold=0.68)
        return bool(manor and line), {
            "manor": manor.score if manor else 0.0,
            "line": line.score if line else 0.0,
        }

    @staticmethod
    def action_point(action: Match, x: float, y: float) -> tuple[int, int]:
        return round(action.x + x * action.scale), round(action.y + y * action.scale)

    def plot_points(self, action: Match) -> list[tuple[int, int]]:
        origin_x, origin_y = ACTION_REFERENCE_ORIGIN
        return [
            (
                round(action.x + (x - origin_x) * action.scale),
                round(action.y + (y - origin_y) * action.scale),
            )
            for x, y in PLOT_REFERENCE_POINTS
        ]

    def harvest_points(self, action: Match) -> list[tuple[int, int]]:
        """Return harvest targets at crop/bed centres in the user's 1..16 order."""
        origin_x, origin_y = ACTION_REFERENCE_ORIGIN
        return [
            self.action_point(
                action,
                PLOT_REFERENCE_POINTS[index][0] - origin_x - 28,
                PLOT_REFERENCE_POINTS[index][1] - origin_y,
            )
            for index in USER_PLOT_ORDER
        ]

    def classify_plots(self, frame: np.ndarray, action: Match) -> list[PlotState]:
        states: list[PlotState] = []
        height, width = frame.shape[:2]
        for index, ((cx, cy), reference) in enumerate(
            zip(self.plot_points(action), self.empty_patches), start=1
        ):
            left = round(cx - 23 * action.scale)
            top = round(cy - 49 * action.scale)
            right = round(cx + 24 * action.scale)
            bottom = round(cy + 9 * action.scale)
            if left < 0 or top < 0 or right > width or bottom > height or right <= left or bottom <= top:
                states.append(PlotState(index, False, 0.0, (cx, cy)))
                continue
            region = frame[top:bottom, left:right]
            resized = cv2.resize(region, (reference.shape[1], reference.shape[0]))
            similarity = float(
                cv2.matchTemplate(resized, reference, cv2.TM_CCOEFF_NORMED)[0, 0]
            )
            states.append(PlotState(index, similarity >= 0.78, similarity, (cx, cy)))
        return states

    def harvest_plot_evidence(self, frame: np.ndarray, action: Match, index: int) -> str:
        """Return a positive visual result for one plot after a harvest pass."""
        if frame is None or not 0 <= index < 16:
            return "unknown"
        states = self.classify_plots(frame, action)
        if states[index].empty:
            return "empty"

        sign_x, cy = self.plot_points(action)[index]
        cx = round(sign_x - 28 * action.scale)
        radius = round(33 * action.scale)
        if (
            cx - radius < 0
            or cy - radius < 0
            or cx + radius > frame.shape[1]
            or cy + radius > frame.shape[0]
        ):
            return "unknown"
        region = frame[cy - radius : cy + radius, cx - radius : cx + radius]
        for factor in (0.96, 1.0, 1.04):
            template = cv2.resize(
                self.harvest_cooldown,
                None,
                fx=action.scale * factor,
                fy=action.scale * factor,
            )
            if template.shape[0] > region.shape[0] or template.shape[1] > region.shape[1]:
                continue
            scores = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
            if float(np.max(scores)) >= 0.84:
                return "cooldown"
        return "unknown"

    def diagnostic(self, frame: np.ndarray) -> dict[str, object]:
        action = self.locate_action_bar(frame)
        shop = self.locate_shop(frame)
        manor = self.locate_manor_button(frame)
        nonbattle, evidence = self.nonbattle_evidence(frame)
        result: dict[str, object] = {
            "size": f"{frame.shape[1]}x{frame.shape[0]}",
            "manor_button": round(manor.score, 3) if manor else 0,
            "action_bar": round(action.score, 3) if action else 0,
            "shop": round(shop.score, 3) if shop else 0,
            "nonbattle": nonbattle,
            "evidence": evidence,
        }
        if action:
            states = self.classify_plots(frame, action)
            result["empty_plots"] = [state.index for state in states if state.empty]
            result["occupied_plots"] = [state.index for state in states if not state.empty]
        return result
