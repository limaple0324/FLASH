"""離線自動戰鬥畫面證據。

這個模組只辨識圖片；沒有滑鼠、視窗或遊戲輸入能力。真正的輸入
必須由智慧重連的既有最終授權邊界執行。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageStat


@dataclass(frozen=True, slots=True)
class AutoBattleEvidence:
    disabled: bool
    enabled: bool
    entry_visible: bool
    red_x_box: tuple[int, int, int, int] | None = None
    battle_button_box: tuple[int, int, int, int] | None = None

    @property
    def red_x_center(self) -> tuple[float, float] | None:
        if self.red_x_box is None:
            return None
        left, top, right, bottom = self.red_x_box
        return ((left + right) / 2, (top + bottom) / 2)

    @property
    def battle_button_center(self) -> tuple[float, float] | None:
        if self.battle_button_box is None:
            return None
        left, top, right, bottom = self.battle_button_box
        return ((left + right) / 2, (top + bottom) / 2)


class AutoBattleRecognizer:
    """以使用者保存的真圖驗證，未取得完整匹配框時一律拒絕。"""

    # 已由右下入口真圖確認的限定搜尋區；這是區域限制，不是點擊座標。
    _SEARCH_LEFT = 0.80
    _SEARCH_TOP = 0.68
    _BATTLE_SOURCE_SIZE = (1336, 858)
    _BATTLE_SEARCH_REGION = (0.84, 0.54, 0.97, 0.68)
    _ENABLED_PANEL_REGION = (0.738, 0.0, 1.0, 0.22)
    _ENABLED_PANEL_ANCHORS = (
        (0.02, 0.02, 0.98, 0.28),
        (0.02, 0.28, 0.12, 0.98),
        (0.88, 0.28, 0.98, 0.98),
        (0.02, 0.80, 0.98, 0.98),
        (0.25, 0.55, 0.78, 0.84),
        (0.14, 0.28, 0.86, 0.52),
    )
    _ENABLED_CONTEXT_REGIONS = (
        (0.02, 0.28, 0.35, 0.65),
        (0.32, 0.25, 0.68, 0.62),
        (0.02, 0.67, 0.48, 0.98),
        (0.52, 0.65, 0.98, 0.98),
    )
    _ENABLED_MAXIMUM_COLOUR_SCORE = 25.0
    _ENABLED_MAXIMUM_EDGE_SCORE = 55.0
    _ENABLED_MINIMUM_CONTEXT_EDGE_RATIO = 0.03

    def __init__(self, reference_dir: Path) -> None:
        self.reference_dir = Path(reference_dir)
        self._disabled = self._load("disabled_red_x_with_context.png")
        self._entry = self._load("entry_icon.png")
        self._battle_button = self._load("battle_auto_button.png")
        self._enabled = tuple(
            item
            for item in (
                self._load("enabled_full_panel.png"),
                self._load("enabled_battle_full_panel.png"),
                self._load("enabled_start_full_panel.png"),
            )
            if item is not None
        )

    def _load(self, name: str) -> Image.Image | None:
        path = self.reference_dir / name
        if not path.is_file():
            return None
        with Image.open(path) as image:
            return image.convert("RGB")

    @property
    def ready(self) -> bool:
        return bool(
            self._disabled is not None
            and self._battle_button is not None
            and self._enabled
        )

    @staticmethod
    def _same(first: Image.Image, second: Image.Image) -> bool:
        return first.size == second.size and ImageChops.difference(first, second).getbbox() is None

    @staticmethod
    def _crop(
        image: Image.Image,
        region: tuple[float, float, float, float],
    ) -> Image.Image:
        left, top, right, bottom = region
        return image.crop(
            (
                round(image.width * left),
                round(image.height * top),
                round(image.width * right),
                round(image.height * bottom),
            )
        )

    @classmethod
    def _context_has_structure(cls, source: Image.Image) -> bool:
        for region in cls._ENABLED_CONTEXT_REGIONS:
            candidate = cls._crop(source, region).convert("L").filter(
                ImageFilter.FIND_EDGES
            )
            if candidate.width > 4 and candidate.height > 4:
                candidate = candidate.crop(
                    (2, 2, candidate.width - 2, candidate.height - 2)
                )
            histogram = candidate.histogram()
            total = candidate.width * candidate.height
            if total <= 0:
                return False
            edge_ratio = sum(histogram[25:]) / total
            if edge_ratio < cls._ENABLED_MINIMUM_CONTEXT_EDGE_RATIO:
                return False
        return True

    @classmethod
    def _enabled_panel_matches(
        cls,
        source: Image.Image,
        reference: Image.Image,
    ) -> bool:
        candidate_panel = cls._crop(source, cls._ENABLED_PANEL_REGION)
        reference_panel = cls._crop(reference, cls._ENABLED_PANEL_REGION)
        if min(candidate_panel.size) <= 0 or min(reference_panel.size) <= 0:
            return False
        for region in cls._ENABLED_PANEL_ANCHORS:
            candidate = cls._crop(candidate_panel, region).convert("RGB")
            expected = cls._crop(reference_panel, region).convert("RGB")
            expected = expected.resize(
                candidate.size,
                Image.Resampling.BILINEAR,
            )
            colour_score = sum(
                ImageStat.Stat(
                    ImageChops.difference(candidate, expected)
                ).mean
            ) / 3.0
            edge_score = float(
                ImageStat.Stat(
                    ImageChops.difference(
                        candidate.convert("L").filter(
                            ImageFilter.FIND_EDGES
                        ),
                        expected.convert("L").filter(
                            ImageFilter.FIND_EDGES
                        ),
                    )
                ).mean[0]
            )
            if (
                colour_score > cls._ENABLED_MAXIMUM_COLOUR_SCORE
                or edge_score > cls._ENABLED_MAXIMUM_EDGE_SCORE
            ):
                return False
        return True

    def _enabled_full_panel(self, source: Image.Image) -> bool:
        if (
            source.width < 800
            or source.height < 500
            or not 1.45 <= source.width / source.height <= 1.65
            or not self._context_has_structure(source)
        ):
            return False
        return any(
            self._enabled_panel_matches(source, reference)
            for reference in self._enabled
        )

    def _find_red_x(self, source: Image.Image) -> tuple[int, int, int, int] | None:
        """在核定右下區搜尋完整上下文模板，不縮放、不猜座標。"""
        template = self._disabled
        if template is None or source.width < template.width or source.height < template.height:
            return None
        left_bound = max(0, int(source.width * self._SEARCH_LEFT))
        top_bound = max(0, int(source.height * self._SEARCH_TOP))
        pixels = source.load()
        template_pixels = template.load()
        for top in range(top_bound, source.height - template.height + 1):
            for left in range(left_bound, source.width - template.width + 1):
                if pixels[left, top] != template_pixels[0, 0]:
                    continue
                if self._same(source.crop((left, top, left + template.width, top + template.height)), template):
                    return (left, top, left + template.width, top + template.height)
        return None

    def _find_battle_button(
        self,
        source: Image.Image,
    ) -> tuple[int, int, int, int] | None:
        """Find one complete button only inside the confirmed right panel."""

        template = self._battle_button
        if template is None or source.size != self._BATTLE_SOURCE_SIZE:
            return None
        left_ratio, top_ratio, right_ratio, bottom_ratio = (
            self._BATTLE_SEARCH_REGION
        )
        left_bound = round(source.width * left_ratio)
        top_bound = round(source.height * top_ratio)
        right_bound = round(source.width * right_ratio)
        bottom_bound = round(source.height * bottom_ratio)
        if (
            right_bound - left_bound < template.width
            or bottom_bound - top_bound < template.height
        ):
            return None
        pixels = source.load()
        template_pixels = template.load()
        matches: list[tuple[int, int, int, int]] = []
        for top in range(
            top_bound,
            bottom_bound - template.height + 1,
        ):
            for left in range(
                left_bound,
                right_bound - template.width + 1,
            ):
                if pixels[left, top] != template_pixels[0, 0]:
                    continue
                box = (
                    left,
                    top,
                    left + template.width,
                    top + template.height,
                )
                if self._same(source.crop(box), template):
                    matches.append(box)
                    if len(matches) > 1:
                        return None
        return matches[0] if len(matches) == 1 else None

    def read(self, image: Image.Image) -> AutoBattleEvidence:
        if not self.ready or not isinstance(image, Image.Image):
            return AutoBattleEvidence(False, False, False)
        source = image.convert("RGB")
        enabled = self._enabled_full_panel(source)
        red_x_box = None if enabled else self._find_red_x(source)
        battle_button_box = (
            None
            if enabled or red_x_box is not None
            else self._find_battle_button(source)
        )
        entry = self._entry is not None and self._same(source, self._entry)
        return AutoBattleEvidence(
            red_x_box is not None,
            enabled,
            entry,
            red_x_box,
            battle_button_box,
        )
