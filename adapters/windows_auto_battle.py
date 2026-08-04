"""離線自動戰鬥畫面證據。

這個模組只辨識圖片；沒有滑鼠、視窗或遊戲輸入能力。真正的輸入
必須由智慧重連的既有最終授權邊界執行。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops


@dataclass(frozen=True, slots=True)
class AutoBattleEvidence:
    disabled: bool
    enabled: bool
    entry_visible: bool
    red_x_box: tuple[int, int, int, int] | None = None

    @property
    def red_x_center(self) -> tuple[float, float] | None:
        if self.red_x_box is None:
            return None
        left, top, right, bottom = self.red_x_box
        return ((left + right) / 2, (top + bottom) / 2)


class AutoBattleRecognizer:
    """以使用者保存的真圖驗證，未取得完整匹配框時一律拒絕。"""

    # 已由右下入口真圖確認的限定搜尋區；這是區域限制，不是點擊座標。
    _SEARCH_LEFT = 0.80
    _SEARCH_TOP = 0.68

    def __init__(self, reference_dir: Path) -> None:
        self.reference_dir = Path(reference_dir)
        self._disabled = self._load("disabled_red_x_with_context.png")
        self._entry = self._load("entry_icon.png")
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
        return self._disabled is not None and bool(self._enabled)

    @staticmethod
    def _same(first: Image.Image, second: Image.Image) -> bool:
        return first.size == second.size and ImageChops.difference(first, second).getbbox() is None

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

    def read(self, image: Image.Image) -> AutoBattleEvidence:
        if not self.ready or not isinstance(image, Image.Image):
            return AutoBattleEvidence(False, False, False)
        source = image.convert("RGB")
        enabled = any(self._same(source, reference) for reference in self._enabled)
        red_x_box = None if enabled else self._find_red_x(source)
        entry = self._entry is not None and self._same(source, self._entry)
        return AutoBattleEvidence(red_x_box is not None, enabled, entry, red_x_box)
