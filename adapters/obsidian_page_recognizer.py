"""黑曜石頁面的唯讀、失敗關閉辨識器。"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image, ImageOps

from adapters.windows_background_capture import CaptureSample
from domain.character_game_data import ObsidianSnapshot
from services.character_game_data_capture_service import (
    GameDataPageKind,
    VerifiedGameDataPage,
)


_SELECTION_TEXT_SIGNATURE_SIZE = (80, 80)
_STAGE_EVIDENCE_REGION = (0.10, 0.14, 0.90, 0.47)
_STATUS_EVIDENCE_REGION = (0.10, 0.48, 0.90, 0.90)
_PANEL_ASPECT_MINIMUM = 1.34
_PANEL_ASPECT_MAXIMUM = 1.48
_PANEL_REFERENCE_ASPECT = 1.414
_PANEL_SCAN_WIDTH = 512
_PANEL_HEADER_RUN_MINIMUM_RATIO = 0.70
_PANEL_MINIMUM_VISIBLE_HEIGHT_RATIO = 0.94
_SELECTION_TEXT_TRANSLATION_RADIUS = 2


@dataclass(frozen=True, slots=True)
class ObsidianNodeTopology:
    """原圖人工核對後的固定格位中心與已亮／灰基準。"""

    x: float
    y: float
    expected_lit: bool

    def __post_init__(self) -> None:
        if not isinstance(self.x, float) or not 0.0 < self.x < 1.0:
            raise ValueError("x must be a normalized coordinate.")
        if not isinstance(self.y, float) or not 0.0 < self.y < 1.0:
            raise ValueError("y must be a normalized coordinate.")
        if not isinstance(self.expected_lit, bool):
            raise TypeError("expected_lit must be bool.")


@dataclass(frozen=True, slots=True)
class ObsidianPageDefinition:
    """不依候選檔名的單一可靠中央形狀參考。"""

    page_number: int
    filename: str
    source_sha256: str
    status_text: str
    topology: tuple[ObsidianNodeTopology, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.page_number, bool)
            or not isinstance(self.page_number, int)
            or not 1 <= self.page_number <= 10
        ):
            raise ValueError("page_number must be between 1 and 10.")
        if not isinstance(self.filename, str) or not self.filename.strip():
            raise ValueError("filename must be a non-empty string.")
        digest = self.source_sha256.lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("source_sha256 must be a SHA-256 digest.")
        if self.status_text not in {"階段一／完成", "激活"}:
            raise ValueError("status_text must be a verified visible status.")
        if not self.topology:
            raise ValueError("topology must contain at least one node.")
        if any(not isinstance(node, ObsidianNodeTopology) for node in self.topology):
            raise TypeError("topology must contain ObsidianNodeTopology values.")
        object.__setattr__(self, "filename", self.filename.strip())
        object.__setattr__(self, "source_sha256", digest)

    @property
    def expected_opened_nodes(self) -> int:
        return sum(node.expected_lit for node in self.topology)

    @property
    def expected_unlit_nodes(self) -> int:
        return len(self.topology) - self.expected_opened_nodes

    @property
    def page_shape_signature(self) -> str:
        payload = json.dumps(
            {
                "page": self.page_number,
                "topology": [
                    (round(node.x, 5), round(node.y, 5))
                    for node in self.topology
                ],
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"obsidian-shape-{self.page_number}-{digest}"


def _nodes(
    points: Iterable[tuple[float, float]],
    *,
    unlit_count: int = 0,
) -> tuple[ObsidianNodeTopology, ...]:
    normalized = tuple(points)
    if unlit_count < 0 or unlit_count > len(normalized):
        raise ValueError("unlit_count must fit the topology.")
    lit_count = len(normalized) - unlit_count
    return tuple(
        ObsidianNodeTopology(x, y, index < lit_count)
        for index, (x, y) in enumerate(normalized)
    )


DEFAULT_OBSIDIAN_PAGE_DEFINITIONS = (
    ObsidianPageDefinition(
        1,
        "page_01.png",
        "ea613be92723ad5c50807d23df2495a0b9fad116c03d8472ff5d2e1a8ad6d86c",
        "階段一／完成",
        _nodes(((0.35490, 0.42712), (0.35400, 0.33333))),
    ),
    ObsidianPageDefinition(
        2,
        "page_02.png",
        "dcdcb69bbeda8cfdbe6dbc1b6ab8a1f90324d77f430de543d02a67a02fbaaea3",
        "階段一／完成",
        _nodes(
            (
                (0.35817, 0.37834),
                (0.42101, 0.52866),
                (0.29713, 0.52866),
                (0.35817, 0.28153),
                (0.41741, 0.43185),
                (0.29533, 0.43185),
            )
        ),
    ),
    ObsidianPageDefinition(
        3,
        "page_03.png",
        "bc7e1f115183e85bb53d72a6e8e81eaeb481e08ae748b34e0409c70285f7d29d",
        "階段一／完成",
        _nodes(
            (
                (0.35336, 0.52881),
                (0.35247, 0.42894),
                (0.35426, 0.22407),
                (0.41256, 0.58003),
                (0.28879, 0.58131),
                (0.41614, 0.37260),
                (0.29058, 0.37516),
                (0.41435, 0.27657),
                (0.28969, 0.27657),
            )
        ),
    ),
    ObsidianPageDefinition(
        4,
        "page_04.png",
        "2c08443f0cb6f75a010c99aef6f5990cb37350698a3fd12d0e0e3f99254478be",
        "階段一／完成",
        _nodes(
            (
                (0.50134, 0.52273),
                (0.24261, 0.65025),
                (0.42704, 0.50631),
                (0.48612, 0.65025),
                (0.42704, 0.59975),
                (0.36705, 0.45455),
                (0.24799, 0.15657),
                (0.48791, 0.15657),
                (0.42435, 0.21591),
                (0.30528, 0.21591),
                (0.42704, 0.30934),
                (0.30618, 0.30682),
                (0.30260, 0.50379),
                (0.36616, 0.35985),
                (0.30707, 0.59848),
            )
        ),
    ),
    ObsidianPageDefinition(
        5,
        "page_05.png",
        "e39ab6ea14e2f45d1aada4adf652cee4085631b4be0bc9731a2163e705e80754",
        "階段一／完成",
        _nodes(
            (
                (0.48297, 0.36224),
                (0.42294, 0.21684),
                (0.29928, 0.21684),
                (0.23925, 0.36480),
                (0.11828, 0.65944),
                (0.24104, 0.45918),
                (0.54480, 0.60587),
                (0.18100, 0.60459),
                (0.36201, 0.16327),
                (0.48477, 0.45918),
                (0.60484, 0.65689),
                (0.54391, 0.51020),
                (0.36201, 0.65689),
                (0.48118, 0.65816),
                (0.42294, 0.60459),
                (0.17921, 0.50893),
                (0.30197, 0.60714),
                (0.24014, 0.65689),
                (0.42294, 0.31122),
                (0.30197, 0.31122),
            )
        ),
    ),
    ObsidianPageDefinition(
        6,
        "page_06.png",
        "7202f50b8d342b2331eb84b5ebaf44bfefba8d3e00412e8279a45e4de2445dff",
        "階段一／完成",
        _nodes(
            (
                (0.48617, 0.60792),
                (0.24532, 0.60792),
                (0.30330, 0.45722),
                (0.36664, 0.60920),
                (0.24086, 0.21711),
                (0.42284, 0.66028),
                (0.48350, 0.31162),
                (0.24264, 0.31162),
                (0.30419, 0.65900),
                (0.36218, 0.21839),
                (0.54237, 0.66028),
                (0.42462, 0.45722),
                (0.50312, 0.51469),
                (0.18198, 0.65900),
                (0.36574, 0.31162),
                (0.48171, 0.21967),
            )
        ),
    ),
    ObsidianPageDefinition(
        7,
        "page_07.png",
        "2793685803eeb0cec4b10268c066ac65b723128930f5c9de707d3e7db1fa8e36",
        "階段一／完成",
        _nodes(
            (
                (0.48822, 0.31226),
                (0.60960, 0.60303),
                (0.36051, 0.51075),
                (0.12047, 0.60303),
                (0.35960, 0.21365),
                (0.36322, 0.60051),
                (0.48460, 0.50948),
                (0.24004, 0.50948),
                (0.24094, 0.30973),
                (0.36141, 0.30973),
                (0.48551, 0.60303),
                (0.60688, 0.50948),
                (0.24094, 0.60430),
                (0.11775, 0.50948),
            )
        ),
    ),
    ObsidianPageDefinition(
        8,
        "page_08.png",
        "ebd472671034e0e700811926dd03f2d9a1d49ca967f4fd976257bba58581ecf7",
        "階段一／完成",
        _nodes(
            (
                (0.40072, 0.45949),
                (0.21377, 0.61013),
                (0.33721, 0.61139),
                (0.58318, 0.61139),
                (0.52236, 0.66076),
                (0.45975, 0.51646),
                (0.15206, 0.16835),
                (0.21199, 0.21899),
                (0.46064, 0.61139),
                (0.21288, 0.31139),
                (0.33810, 0.51646),
                (0.27370, 0.66203),
                (0.15027, 0.36582),
                (0.15295, 0.45949),
                (0.21199, 0.51519),
                (0.52415, 0.53418),
            )
        ),
    ),
    ObsidianPageDefinition(
        9,
        "page_09.png",
        "57642714dc8688c2eeccfd1d088e7165f4b8cfb127f7fbb74d18e2107224bcca",
        "階段一／完成",
        _nodes(
            (
                (0.35996, 0.20607),
                (0.35727, 0.50316),
                (0.35996, 0.30594),
                (0.36176, 0.60556),
                (0.48115, 0.50569),
                (0.24057, 0.30341),
                (0.29892, 0.45386),
                (0.42101, 0.35651),
                (0.48294, 0.30088),
                (0.23609, 0.50822),
                (0.42280, 0.45259),
                (0.29713, 0.35777),
            )
        ),
    ),
    ObsidianPageDefinition(
        10,
        "page_10.png",
        "262924e496079e06beabccc68a85b91e839bc4405b69d6e839ced53001375582",
        "激活",
        _nodes(
            (
                (0.43024, 0.65013),
                (0.25203, 0.30789),
                (0.49325, 0.30789),
                (0.30963, 0.65013),
                (0.36994, 0.20738),
                (0.49415, 0.59288),
                (0.25293, 0.59288),
                (0.37444, 0.59160),
                (0.49055, 0.49873),
                (0.24932, 0.49873),
                (0.55266, 0.65267),
                (0.18812, 0.65267),
                (0.43474, 0.15903),
                (0.55356, 0.15903),
                (0.24752, 0.20738),
                (0.31143, 0.16031),
                (0.18992, 0.16031),
                (0.49325, 0.20611),
            ),
            unlit_count=6,
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class _ReferenceFrame:
    definition: ObsidianPageDefinition
    image: Image.Image
    stage_signature: bytes | None
    status_signature: bytes


def _default_reference_dir() -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    root = Path(bundle_root) if bundle_root else Path(__file__).resolve().parents[1]
    return root / "assets" / "game_data_reference" / "obsidian"


def _normalized_crop(
    image: Image.Image,
    region: tuple[float, float, float, float],
) -> Image.Image:
    left_ratio, top_ratio, right_ratio, bottom_ratio = region
    width, height = image.size
    left = max(0, min(width - 1, round(width * left_ratio)))
    top = max(0, min(height - 1, round(height * top_ratio)))
    right = max(left + 1, min(width, round(width * right_ratio)))
    bottom = max(top + 1, min(height, round(height * bottom_ratio)))
    return image.crop((left, top, right, bottom))


def _binary_signature(
    image: Image.Image,
    region: tuple[float, float, float, float],
    size: tuple[int, int],
) -> bytes:
    cropped = _normalized_crop(image, region)
    fitted = ImageOps.fit(
        ImageOps.grayscale(cropped),
        size,
        method=Image.Resampling.BILINEAR,
    )
    flattened = getattr(fitted, "get_flattened_data", fitted.getdata)
    return bytes(1 if value >= 170 else 0 for value in flattened())


def _jaccard_score(left: bytes, right: bytes) -> float:
    if len(left) != len(right):
        return 0.0
    intersection = 0
    union = 0
    for first, second in zip(left, right):
        if first or second:
            union += 1
            if first and second:
                intersection += 1
    return intersection / union if union else 0.0


def _translated_jaccard_score(
    left: bytes,
    right: bytes,
    *,
    size: tuple[int, int] = _SELECTION_TEXT_SIGNATURE_SIZE,
    radius: int = _SELECTION_TEXT_TRANSLATION_RADIUS,
) -> float:
    """容許擷取縮放造成的少量文字位移，但不放寬文字內容。"""

    width, height = size
    if len(left) != width * height or len(right) != width * height:
        return 0.0
    left_points = {
        (index % width, index // width)
        for index, enabled in enumerate(left)
        if enabled
    }
    right_points = {
        (index % width, index // width)
        for index, enabled in enumerate(right)
        if enabled
    }
    if not left_points and not right_points:
        return 1.0
    best = 0.0
    for shift_y in range(-radius, radius + 1):
        for shift_x in range(-radius, radius + 1):
            shifted = {
                (x + shift_x, y + shift_y)
                for x, y in right_points
                if 0 <= x + shift_x < width and 0 <= y + shift_y < height
            }
            union = left_points | shifted
            score = len(left_points & shifted) / len(union) if union else 0.0
            best = max(best, score)
    return best


class ObsidianPageRecognizer:
    """只憑目前擷取畫面確認黑曜石頁面，不操作遊戲。"""

    def __init__(
        self,
        *,
        reference_dir: Path | str | None = None,
        definitions: Iterable[ObsidianPageDefinition] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._reference_dir = (
            Path(reference_dir) if reference_dir is not None else _default_reference_dir()
        )
        self._definitions = tuple(
            definitions if definitions is not None else DEFAULT_OBSIDIAN_PAGE_DEFINITIONS
        )
        if not self._definitions:
            raise ValueError("definitions must contain at least one page.")
        if any(
            not isinstance(item, ObsidianPageDefinition)
            for item in self._definitions
        ):
            raise TypeError("definitions must contain ObsidianPageDefinition values.")
        numbers = [item.page_number for item in self._definitions]
        if len(numbers) != len(set(numbers)):
            raise ValueError("definition page numbers must be unique.")
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._frames: tuple[_ReferenceFrame, ...] = ()
        self._missing_references: tuple[str, ...] = ()
        self._stage_minimum_score = 1.0
        self._status_minimum_score = 1.0
        self._status_minimum_margin = 1.0
        self._lit_brightness_boundary = 255.0
        self._gray_brightness_boundary = 0.0
        self._chroma_boundary = 255.0
        self._node_presence_brightness_minimum = 255.0
        self._node_presence_structure_minimum = 255.0
        self._load_references()

    @property
    def missing_references(self) -> tuple[str, ...]:
        return self._missing_references

    @property
    def ready(self) -> bool:
        return bool(self._frames) and not self._missing_references

    @staticmethod
    def _longest_cyan_run(image: Image.Image, y: int) -> tuple[int, int, int]:
        """回傳指定列最長的青藍色面板邊框區段。"""

        pixels = image.load()
        best_length = 0
        best_start = 0
        best_end = -1
        start: int | None = None
        for x in range(image.width):
            red, green, blue = pixels[x, y]
            is_cyan = (
                green >= 105
                and blue >= 105
                and green - red >= 15
                and blue - red >= 15
                and abs(green - blue) <= 100
            )
            if is_cyan and start is None:
                start = x
            if (not is_cyan or x == image.width - 1) and start is not None:
                end = x if is_cyan and x == image.width - 1 else x - 1
                length = end - start + 1
                if length > best_length:
                    best_length = length
                    best_start = start
                    best_end = end
                start = None
        return best_length, best_start, best_end

    @classmethod
    def _panel_from_capture(cls, image: Image.Image) -> Image.Image | None:
        """從完整遊戲畫面定位黑曜石面板；裁切不足時失敗關閉。"""

        width, height = image.size
        if width <= 0 or height <= 0:
            return None
        aspect = width / height
        if _PANEL_ASPECT_MINIMUM <= aspect <= _PANEL_ASPECT_MAXIMUM:
            return image

        scale = min(1.0, _PANEL_SCAN_WIDTH / width)
        scan = image.resize(
            (max(1, round(width * scale)), max(1, round(height * scale))),
            Image.Resampling.BILINEAR,
        ).convert("RGB")
        minimum_run = round(scan.width * _PANEL_HEADER_RUN_MINIMUM_RATIO)
        runs = [
            (length, y, start, end)
            for y in range(scan.height)
            for length, start, end in (cls._longest_cyan_run(scan, y),)
            if length >= minimum_run
        ]
        if not runs:
            return None

        widest = max(item[0] for item in runs)
        strong = [item for item in runs if item[0] >= widest * 0.90]
        anchor_y = min(item[1] for item in strong)
        header_band = [
            item
            for item in strong
            if anchor_y <= item[1] <= anchor_y + round(scan.height * 0.10)
        ]
        if not header_band:
            return None

        scale_x = width / scan.width
        scale_y = height / scan.height
        left = max(0, int(min(item[2] for item in header_band) * scale_x))
        right = min(
            width,
            int(max(item[3] + 1 for item in header_band) * scale_x + 0.999999),
        )
        top = max(0, int(anchor_y * scale_y))
        panel_width = right - left
        if panel_width <= 0:
            return None
        expected_height = round(panel_width / _PANEL_REFERENCE_ASPECT)
        available_height = height - top
        if expected_height <= 0 or available_height <= 0:
            return None
        if available_height < expected_height:
            if available_height / expected_height < _PANEL_MINIMUM_VISIBLE_HEIGHT_RATIO:
                return None
            bottom = height
        else:
            bottom = top + expected_height
        panel = image.crop((left, top, right, bottom))
        panel_aspect = panel.width / panel.height
        if not _PANEL_ASPECT_MINIMUM <= panel_aspect <= _PANEL_ASPECT_MAXIMUM:
            return None
        return panel

    @staticmethod
    def _gold_panel(image: Image.Image) -> Image.Image | None:
        width, height = image.size
        top = round(height * 0.70)
        bottom = round(height * 0.97)
        right = round(width * 0.72)
        if top >= bottom or right <= 0:
            return None
        region = image.crop((0, top, right, bottom)).convert("RGB")
        region_width, region_height = region.size
        pixels = region.load()
        gold = bytearray(region_width * region_height)
        for y in range(region_height):
            for x in range(region_width):
                red, green, blue = pixels[x, y]
                gold[y * region_width + x] = int(
                    red >= 150
                    and green >= 90
                    and blue <= 110
                    and red >= green
                    and red - blue >= 80
                )
        visited = bytearray(len(gold))
        best: tuple[int, int, int, int, int] | None = None
        for start, enabled in enumerate(gold):
            if not enabled or visited[start]:
                continue
            pending = [start]
            visited[start] = 1
            count = 0
            min_x = max_x = start % region_width
            min_y = max_y = start // region_width
            while pending:
                current = pending.pop()
                x = current % region_width
                y = current // region_width
                count += 1
                min_x = min(min_x, x)
                max_x = max(max_x, x)
                min_y = min(min_y, y)
                max_y = max(max_y, y)
                for next_x, next_y in (
                    (x - 1, y),
                    (x + 1, y),
                    (x, y - 1),
                    (x, y + 1),
                ):
                    if not (0 <= next_x < region_width and 0 <= next_y < region_height):
                        continue
                    next_index = next_y * region_width + next_x
                    if gold[next_index] and not visited[next_index]:
                        visited[next_index] = 1
                        pending.append(next_index)
            if best is None or count > best[0]:
                best = (count, min_x, min_y, max_x, max_y)
        if best is None:
            return None
        _, min_x, min_y, max_x, max_y = best
        if (
            max_x - min_x + 1 < round(width * 0.10)
            or max_y - min_y + 1 < round(height * 0.12)
        ):
            return None
        return region.crop((min_x, min_y, max_x + 1, max_y + 1))

    @classmethod
    def _selection_text_signature(
        cls,
        image: Image.Image,
        region: tuple[float, float, float, float],
    ) -> bytes | None:
        panel = cls._gold_panel(image)
        if panel is None:
            return None
        return _binary_signature(
            panel,
            region,
            _SELECTION_TEXT_SIGNATURE_SIZE,
        )

    @classmethod
    def _stage_signature(cls, image: Image.Image) -> bytes | None:
        return cls._selection_text_signature(image, _STAGE_EVIDENCE_REGION)

    @classmethod
    def _status_signature(cls, image: Image.Image) -> bytes | None:
        return cls._selection_text_signature(image, _STATUS_EVIDENCE_REGION)

    @staticmethod
    def _node_metrics(
        image: Image.Image,
        node: ObsidianNodeTopology,
    ) -> tuple[float, float, float] | None:
        width, height = image.size
        radius = max(1, round(min(width, height) * 0.0115))
        center_x = round(node.x * width)
        center_y = round(node.y * height)
        left = max(0, center_x - radius)
        top = max(0, center_y - radius)
        right = min(width, center_x + radius + 1)
        bottom = min(height, center_y + radius + 1)
        if left >= right or top >= bottom:
            return None
        cropped = image.crop((left, top, right, bottom)).convert("RGB")
        flattened = getattr(cropped, "get_flattened_data", cropped.getdata)
        values = tuple(flattened())
        if not values:
            return None
        brightness_values = tuple(
            max(red, green, blue)
            for red, green, blue in values
        )
        brightness = sum(brightness_values) / len(brightness_values)
        chroma = sum(
            max(red, green, blue) - min(red, green, blue)
            for red, green, blue in values
        ) / len(values)
        local_structure = (
            sum(
                (value - brightness) ** 2
                for value in brightness_values
            )
            / len(brightness_values)
        ) ** 0.5
        return brightness, chroma, local_structure

    def _load_references(self) -> None:
        frames: list[_ReferenceFrame] = []
        missing: list[str] = []
        for definition in self._definitions:
            path = self._reference_dir / definition.filename
            if not path.is_file():
                missing.append(definition.filename)
                continue
            if hashlib.sha256(path.read_bytes()).hexdigest() != definition.source_sha256:
                missing.append(definition.filename)
                continue
            try:
                with Image.open(path) as source:
                    image = source.convert("RGB")
            except (OSError, ValueError):
                missing.append(definition.filename)
                continue
            status_signature = self._status_signature(image)
            stage_signature = (
                self._stage_signature(image)
                if definition.status_text == "階段一／完成"
                else None
            )
            if status_signature is None or (
                definition.status_text == "階段一／完成"
                and stage_signature is None
            ):
                missing.append(definition.filename)
                continue
            frames.append(
                _ReferenceFrame(
                    definition=definition,
                    image=image,
                    stage_signature=stage_signature,
                    status_signature=status_signature,
                )
            )
        self._missing_references = tuple(missing)
        if missing:
            self._frames = ()
            return
        self._frames = tuple(frames)
        self._calibrate()

    def _calibrate(self) -> None:
        completed_stages = [
            frame.stage_signature
            for frame in self._frames
            if frame.definition.status_text == "階段一／完成"
            and frame.stage_signature is not None
        ]
        completed_statuses = [
            frame.status_signature
            for frame in self._frames
            if frame.definition.status_text == "階段一／完成"
        ]
        activated = [
            frame.status_signature
            for frame in self._frames
            if frame.definition.status_text == "激活"
        ]
        if not completed_stages or not completed_statuses or not activated:
            self._missing_references = ("階段一／完成或激活狀態參考",)
            self._frames = ()
            return
        status_cross = max(
            _translated_jaccard_score(first, second)
            for first in completed_statuses
            for second in activated
        )
        # 上行「階段一」的零像素簽章代表文字完全缺失；門檻取其與
        # 原圖自比對分數的中點，不能由下行「完成」補造。
        self._stage_minimum_score = (
            1.0
            + max(
                _jaccard_score(signature, bytes(len(signature)))
                for signature in completed_stages
            )
        ) / 2.0
        # 完整遊戲視窗原圖及十張可靠面板的縮放反例校準：相同文字容許
        # 兩像素位移後仍至少 0.35；不同狀態最高交叉分數遠低於此值。
        self._status_minimum_score = max(0.35, status_cross + 0.20)
        self._status_minimum_margin = max(0.30, status_cross + 0.20)

        lit_metrics: list[tuple[float, float, float]] = []
        gray_metrics: list[tuple[float, float, float]] = []
        for frame in self._frames:
            for node in frame.definition.topology:
                metrics = self._node_metrics(frame.image, node)
                if metrics is None:
                    self._missing_references = (frame.definition.filename,)
                    self._frames = ()
                    return
                if node.expected_lit:
                    lit_metrics.append(metrics)
                else:
                    gray_metrics.append(metrics)
        if not lit_metrics or not gray_metrics:
            self._missing_references = ("亮格或灰格校準參考",)
            self._frames = ()
            return
        self._lit_brightness_boundary = (
            min(item[0] for item in lit_metrics)
            + max(item[0] for item in gray_metrics)
        ) / 2.0
        self._gray_brightness_boundary = self._lit_brightness_boundary
        self._chroma_boundary = (
            min(item[1] for item in lit_metrics)
            + max(item[1] for item in gray_metrics)
        ) / 2.0
        all_node_metrics = (*lit_metrics, *gray_metrics)
        # 門檻是原圖最弱可靠格位與「純色、無格位」零證據間的中點；
        # 因此缺格、全黑或任何單一純色覆蓋都不能被誤判為灰色未亮。
        self._node_presence_brightness_minimum = (
            min(item[0] for item in all_node_metrics) / 2.0
        )
        self._node_presence_structure_minimum = (
            min(item[2] for item in all_node_metrics) / 2.0
        )

    @staticmethod
    def _image_from_sample(sample: CaptureSample) -> Image.Image | None:
        required = sample.width * sample.height * 4
        if (
            not sample.api_succeeded
            or sample.width <= 0
            or sample.height <= 0
            or len(sample.pixels) != required
        ):
            return None
        try:
            return Image.frombytes(
                "RGBA",
                (sample.width, sample.height),
                sample.pixels,
                "raw",
                "BGRA",
            ).convert("RGB")
        except (ValueError, OSError):
            return None

    def _recognized_definition(
        self,
        image: Image.Image,
    ) -> tuple[ObsidianPageDefinition, tuple[bool, ...]] | None:
        matched: list[tuple[ObsidianPageDefinition, tuple[bool, ...]]] = []
        for frame in self._frames:
            states = self._node_states(image, frame.definition)
            expected = tuple(
                node.expected_lit
                for node in frame.definition.topology
            )
            if states == expected:
                matched.append((frame.definition, states))
        if not matched:
            return None
        longest = max(len(item[0].topology) for item in matched)
        strongest = [item for item in matched if len(item[0].topology) == longest]
        if len(strongest) != 1:
            return None
        return strongest[0]

    def _recognized_status(
        self,
        image: Image.Image,
        definition: ObsidianPageDefinition,
    ) -> str | None:
        status_candidate = self._status_signature(image)
        if status_candidate is None:
            return None
        completed_status_score = max(
            (
                _translated_jaccard_score(status_candidate, frame.status_signature)
                for frame in self._frames
                if frame.definition.status_text == "階段一／完成"
            ),
            default=0.0,
        )
        active_score = max(
            (
                _translated_jaccard_score(status_candidate, frame.status_signature)
                for frame in self._frames
                if frame.definition.status_text == "激活"
            ),
            default=0.0,
        )
        if definition.status_text == "階段一／完成":
            stage_candidate = self._stage_signature(image)
            stage_score = max(
                (
                    _translated_jaccard_score(stage_candidate, frame.stage_signature)
                    for frame in self._frames
                    if frame.definition.status_text == "階段一／完成"
                    and frame.stage_signature is not None
                ),
                default=0.0,
            ) if stage_candidate is not None else 0.0
            if (
                stage_score < self._stage_minimum_score
                or completed_status_score < self._status_minimum_score
                or completed_status_score - active_score < self._status_minimum_margin
            ):
                return None
            return "階段一／完成"
        if definition.status_text == "激活" and (
            active_score >= self._status_minimum_score
            and active_score - completed_status_score >= self._status_minimum_margin
        ):
            return "激活"
        return None

    def _node_states(
        self,
        image: Image.Image,
        definition: ObsidianPageDefinition,
    ) -> tuple[bool, ...] | None:
        states: list[bool] = []
        for node in definition.topology:
            metrics = self._node_metrics(image, node)
            if metrics is None:
                return None
            brightness, chroma, local_structure = metrics
            if (
                brightness < self._node_presence_brightness_minimum
                or local_structure < self._node_presence_structure_minimum
            ):
                return None
            if (
                brightness >= self._lit_brightness_boundary
                and chroma >= self._chroma_boundary
            ):
                states.append(True)
            elif (
                brightness <= self._gray_brightness_boundary
                and chroma <= self._chroma_boundary
            ):
                states.append(False)
            else:
                return None
        return tuple(states)

    def read(self, sample: CaptureSample) -> VerifiedGameDataPage | None:
        """只在中央形狀、選取狀態與全部固定格位一致時回傳。"""

        if not self.ready or not isinstance(sample, CaptureSample):
            return None
        captured = self._image_from_sample(sample)
        if captured is None:
            return None
        image = self._panel_from_capture(captured)
        if image is None:
            return None
        recognized = self._recognized_definition(image)
        if recognized is None:
            return None
        definition, states = recognized
        status_text = self._recognized_status(image, definition)
        if status_text != definition.status_text:
            return None
        now = self._clock()
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            return None
        opened_nodes = sum(states)
        snapshot = ObsidianSnapshot(
            opened_page=definition.page_number,
            opened_nodes=opened_nodes,
            unlit_nodes=len(states) - opened_nodes,
            stage=status_text,
            page_shape_signature=definition.page_shape_signature,
            updated_at=now.isoformat(timespec="seconds"),
        )
        content_signature = hashlib.sha256(
            json.dumps(
                {
                    "page": snapshot.opened_page,
                    "stage": snapshot.stage,
                    "opened_nodes": snapshot.opened_nodes,
                    "unlit_nodes": snapshot.unlit_nodes,
                    "page_shape_signature": snapshot.page_shape_signature,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return VerifiedGameDataPage(
            page_kind=GameDataPageKind.OBSIDIAN,
            logical_page_id=f"obsidian-page-{definition.page_number}",
            content_signature=content_signature,
            data=snapshot,
        )
