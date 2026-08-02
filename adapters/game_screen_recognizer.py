"""Reference-image recognition for the confirmed Flash reconnect flow.

The recognizer compares only stable, normalized UI regions from the user-
provided full-window images.  It never sends input and never persists captured
game pixels.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat

from adapters.windows_background_capture import CaptureSample
from core.reconnect_policy import ReconnectScreenState
from domain.character import CharacterImportance


NormalizedRect = tuple[float, float, float, float]
NormalizedPoint = tuple[float, float]
FORCE_LOGIN_CLICK_POINT: NormalizedPoint = (0.505, 0.856)
# Mouse delivery is relative to the Flash client area, while reference
# screenshots include the 45-pixel Windows title bar. This targets the centre
# of the confirmed "是" button in 14_force_login_timeout.png.
FORCE_LOGIN_TIMEOUT_CLICK_POINT: NormalizedPoint = (0.500, 0.547)
# Legacy fixed digit area retained only as a fail-closed fallback.  The route
# status line is centered as a whole, so different character-name lengths move
# the number horizontally.
ROUTE_DIGIT_REGION: NormalizedRect = (0.449, 0.311, 0.462, 0.342)
ROUTE_PREFIX_REFERENCE_REGION: NormalizedRect = (
    555 / 1351,
    293 / 936,
    608 / 1351,
    320 / 936,
)
ROUTE_PREFIX_SEARCH_REGION: NormalizedRect = (0.350, 0.295, 0.500, 0.335)
ROUTE_DIGIT_REFERENCE_REGION: NormalizedRect = (
    608 / 1351,
    288 / 936,
    621 / 1351,
    313 / 936,
)
ROUTE_DIGIT_TEMPLATES = {
    7: "10_route_digit_7.png",
    8: "11_route_digit_8.png",
}
DEFAULT_LINE_NUMBER = 1
LINE_ROUTE_CLICK_POINTS: dict[int, NormalizedPoint] = {
    1: (0.500, 0.327),
    2: (0.500, 0.385),
    3: (0.500, 0.442),
    4: (0.500, 0.497),
    5: (0.500, 0.553),
    6: (0.500, 0.609),
    7: (0.500, 0.665),
    8: (0.500, 0.722),
}
POPUP_TITLE_REGIONS: dict[ReconnectScreenState, NormalizedRect] = {
    ReconnectScreenState.POST_LOGIN_ACTIVITY: (0.400, 0.130, 0.600, 0.190),
    ReconnectScreenState.POST_LOGIN_RECOMMENDATION: (
        0.400,
        0.190,
        0.600,
        0.250,
    ),
    ReconnectScreenState.POST_LOGIN_AUTO_DUNGEON: (
        0.400,
        0.140,
        0.600,
        0.210,
    ),
}
POPUP_TITLE_MAXIMUM_SCORE = 45.0
CHARACTER_ENTER_CLICK_POINT: NormalizedPoint = (0.353, 0.854)
CHARACTER_LEVEL_TEMPLATE_FILES = {
    100: "character_level_100.png",
    120: "character_level_120.png",
    160: "character_level_160.png",
}
# The three login slots use the same internal layout. These regions contain
# only the displayed level digits, never the character name.
CHARACTER_LEVEL_REGIONS: tuple[NormalizedRect, ...] = (
    (498.1 / 1348, 657.9 / 937, 568.1 / 1348, 699.9 / 937),
    (698.1 / 1348, 657.9 / 937, 768.1 / 1348, 699.9 / 937),
    (903.1 / 1348, 657.9 / 937, 973.1 / 1348, 699.9 / 937),
)
CHARACTER_SLOT_REGIONS: tuple[NormalizedRect, ...] = (
    (0.282, 0.665, 0.426, 0.783),
    (0.430, 0.665, 0.568, 0.783),
    (0.582, 0.665, 0.720, 0.783),
)
CHARACTER_SLOT_CLICK_POINTS: tuple[NormalizedPoint, ...] = (
    (0.355, 0.706),
    (0.500, 0.706),
    (0.651, 0.706),
)
CHARACTER_LEVEL_MAXIMUM_SCORE = 48.0
CHARACTER_LEVEL_MINIMUM_MARGIN = 0.75
CHARACTER_SELECTED_MINIMUM_SCORE = 100.0
CHARACTER_SELECTED_MINIMUM_MARGIN = 60.0
CHARACTER_SELECTED_BORDER_REGIONS: tuple[NormalizedRect, ...] = (
    (0.285789474, 0.665, 0.422210526, 0.677872727),
    (0.433631579, 0.665, 0.564368421, 0.677872727),
    (0.585631579, 0.665, 0.716368421, 0.677872727),
)
BATTLE_REFERENCE_FILE = "13_battle_gameplay.png"
BATTLE_CONTEXT_REGION: NormalizedRect = (0.73, 0.0, 1.0, 0.22)
BATTLE_CONTEXT_MAXIMUM_SCORE = 28.0
BATTLE_SCREEN_EVIDENCE_REGION: NormalizedRect = (0.08, 0.08, 0.92, 0.92)
BATTLE_SCREEN_EVIDENCE_MAXIMUM_SCORE = 23.0
BATTLE_SCREEN_STRUCTURE_REGION: NormalizedRect = (0.25, 0.25, 0.75, 0.75)
BATTLE_SCREEN_MINIMUM_STDDEV = 30.0
BATTLE_SCREEN_MINIMUM_EDGE_MEAN = 12.0
DISCONNECT_OVERLAY_REGION: NormalizedRect = (0.323, 0.477, 0.677, 0.607)
DISCONNECT_OVERLAY_SIGNATURE_SIZE = (162, 41)
DISCONNECT_OVERLAY_MINIMUM_MASKED_STDDEV = 12.0


@dataclass(frozen=True, slots=True)
class ScreenTemplateDefinition:
    state: ReconnectScreenState
    filename: str
    regions: tuple[NormalizedRect, ...]
    maximum_score: float
    click_point: NormalizedPoint | None

    def validate(self) -> None:
        if not self.filename.strip():
            raise ValueError("Template filename must not be empty")
        if not self.regions:
            raise ValueError("At least one recognition region is required")
        if self.maximum_score <= 0:
            raise ValueError("maximum_score must be positive")
        for left, top, right, bottom in self.regions:
            if not (0.0 <= left < right <= 1.0):
                raise ValueError(f"Invalid template horizontal region: {(left, right)}")
            if not (0.0 <= top < bottom <= 1.0):
                raise ValueError(f"Invalid template vertical region: {(top, bottom)}")
        if self.click_point is not None:
            x, y = self.click_point
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise ValueError(f"Invalid normalized click point: {self.click_point}")


# Recognition rectangles use full-window coordinates because PrintWindow captures
# the complete frame.  Click points use Flash client-area coordinates because
# WM_LBUTTONDOWN/UP expects client coordinates.
DEFAULT_SCREEN_TEMPLATES: tuple[ScreenTemplateDefinition, ...] = (
    ScreenTemplateDefinition(
        state=ReconnectScreenState.DISCONNECTED,
        filename="01_disconnected_dialog.png",
        regions=(
            (0.328, 0.486, 0.672, 0.598),
            (0.455, 0.535, 0.545, 0.584),
        ),
        maximum_score=38.0,
        click_point=(0.500, 0.536),
    ),
    ScreenTemplateDefinition(
        state=ReconnectScreenState.LOGIN_START,
        filename="02_login_start_screen.png",
        regions=(
            (0.382, 0.730, 0.620, 0.922),
            (0.355, 0.070, 0.670, 0.250),
        ),
        maximum_score=32.0,
        click_point=(0.500, 0.786),
    ),
    ScreenTemplateDefinition(
        state=ReconnectScreenState.RECONNECTING,
        filename="09_force_login_progress.png",
        regions=(
            (0.330, 0.430, 0.675, 0.645),
            (0.385, 0.530, 0.625, 0.595),
        ),
        maximum_score=25.0,
        click_point=None,
    ),
    ScreenTemplateDefinition(
        state=ReconnectScreenState.FORCE_LOGIN_TIMEOUT,
        filename="14_force_login_timeout.png",
        regions=(
            (0.329, 0.475, 0.676, 0.617),
            (0.455, 0.548, 0.545, 0.598),
        ),
        maximum_score=15.0,
        click_point=FORCE_LOGIN_TIMEOUT_CLICK_POINT,
    ),
    ScreenTemplateDefinition(
        state=ReconnectScreenState.LINE_SELECTION,
        filename="03_line_selection_dialog.png",
        regions=(
            (0.378, 0.272, 0.625, 0.795),
            (0.400, 0.298, 0.605, 0.390),
        ),
        maximum_score=32.0,
        click_point=None,
    ),
    ScreenTemplateDefinition(
        state=ReconnectScreenState.CHARACTER_SELECTION,
        filename="05_character_selection.png",
        regions=(
            (0.260, 0.615, 0.750, 0.905),
            (0.294, 0.662, 0.430, 0.790),
        ),
        maximum_score=34.0,
        click_point=(0.353, 0.854),
    ),
    ScreenTemplateDefinition(
        state=ReconnectScreenState.POST_LOGIN_ACTIVITY,
        filename="07_post_login_activity_popup.png",
        regions=(
            (0.110, 0.130, 0.890, 0.242),
            (0.108, 0.835, 0.895, 0.968),
        ),
        maximum_score=27.0,
        click_point=(0.865, 0.116),
    ),
    ScreenTemplateDefinition(
        state=ReconnectScreenState.POST_LOGIN_RECOMMENDATION,
        filename="08_post_login_recommendation_popup.png",
        regions=(
            (0.108, 0.192, 0.830, 0.305),
            (0.120, 0.286, 0.825, 0.430),
        ),
        maximum_score=27.0,
        click_point=(0.809, 0.182),
    ),
    ScreenTemplateDefinition(
        state=ReconnectScreenState.POST_LOGIN_AUTO_DUNGEON,
        filename="12_post_login_auto_dungeon_popup.png",
        regions=(
            (0.118, 0.135, 0.905, 0.235),
            (0.118, 0.842, 0.885, 0.965),
        ),
        maximum_score=27.0,
        click_point=(0.880, 0.129),
    ),
    ScreenTemplateDefinition(
        state=ReconnectScreenState.CONNECTED,
        filename="06_connected_gameplay.png",
        regions=(
            (0.920, 0.280, 0.998, 0.742),
        ),
        maximum_score=38.0,
        click_point=None,
    ),
)


@dataclass(frozen=True, slots=True)
class CharacterSelectionCandidate:
    level: int | None
    importance: CharacterImportance | None
    slot_index: int
    selected: bool
    click_point: NormalizedPoint
    digit_count: int | None = None
    identity: str | None = None


@dataclass(frozen=True, slots=True)
class ScreenRecognition:
    state: ReconnectScreenState
    score: float | None
    click_point: NormalizedPoint | None
    reference_name: str | None
    line_number: int | None = None
    character_level: int | None = None
    character_importance: CharacterImportance | None = None
    character_slot_index: int | None = None
    character_slot_selected: bool | None = None
    character_identity: str | None = None
    character_candidates: tuple[CharacterSelectionCandidate, ...] = ()
    battle_context: bool = False

    @property
    def recognized(self) -> bool:
        return self.state is not ReconnectScreenState.UNKNOWN


class ReferenceScreenRecognizer:
    """Classify one capture against the confirmed screen references."""

    SIGNATURE_SIZE = (32, 16)

    def __init__(
        self,
        reference_dir: Path,
        definitions: Iterable[ScreenTemplateDefinition] = DEFAULT_SCREEN_TEMPLATES,
    ):
        self.reference_dir = Path(reference_dir)
        self.definitions = tuple(definitions)
        if not self.definitions:
            raise ValueError("At least one screen template is required")
        for definition in self.definitions:
            definition.validate()
        states = [definition.state for definition in self.definitions]
        if len(states) != len(set(states)):
            raise ValueError("Screen template states must be unique")
        self._references: dict[str, Image.Image] = {}
        self._disconnect_overlay_reference: (
            tuple[Image.Image, Image.Image, Image.Image] | None
        ) = None

    @property
    def missing_references(self) -> tuple[str, ...]:
        screen_references = tuple(
            definition.filename
            for definition in self.definitions
            if not (self.reference_dir / definition.filename).is_file()
        )
        digit_references = tuple(
            filename
            for filename in ROUTE_DIGIT_TEMPLATES.values()
            if not (self.reference_dir / filename).is_file()
        )
        character_level_references = tuple(
            filename
            for filename in CHARACTER_LEVEL_TEMPLATE_FILES.values()
            if not (self.reference_dir / filename).is_file()
        )
        battle_references = (
            ()
            if (self.reference_dir / BATTLE_REFERENCE_FILE).is_file()
            else (BATTLE_REFERENCE_FILE,)
        )
        return (
            screen_references
            + digit_references
            + character_level_references
            + battle_references
        )

    @property
    def ready(self) -> bool:
        return not self.missing_references

    def _reference(self, filename: str) -> Image.Image:
        cached = self._references.get(filename)
        if cached is not None:
            return cached
        path = self.reference_dir / filename
        with Image.open(path) as image:
            loaded = image.convert("RGB")
        self._references[filename] = loaded
        return loaded

    @staticmethod
    def _crop(image: Image.Image, region: NormalizedRect) -> Image.Image:
        left, top, right, bottom = region
        width, height = image.size
        box = (
            max(0, min(width - 1, round(width * left))),
            max(0, min(height - 1, round(height * top))),
            max(1, min(width, round(width * right))),
            max(1, min(height, round(height * bottom))),
        )
        return image.crop(box)

    @classmethod
    def _signature(cls, image: Image.Image) -> Image.Image:
        grayscale = ImageOps.grayscale(image).filter(
            ImageFilter.GaussianBlur(radius=2.0)
        )
        return ImageOps.fit(
            grayscale,
            cls.SIGNATURE_SIZE,
            method=Image.Resampling.BILINEAR,
        )

    @staticmethod
    def _digit_signature(image: Image.Image) -> Image.Image | None:
        grayscale = ImageOps.grayscale(image)
        binary = grayscale.point(lambda value: 255 if value >= 170 else 0)
        bounds = binary.getbbox()
        if bounds is None:
            return None
        glyph = binary.crop(bounds)
        glyph.thumbnail((14, 22), Image.Resampling.NEAREST)
        signature = Image.new("L", (16, 24), 0)
        left = (signature.width - glyph.width) // 2
        top = (signature.height - glyph.height) // 2
        signature.paste(glyph, (left, top))
        return signature

    @staticmethod
    def _binary_text(image: Image.Image) -> Image.Image:
        return ImageOps.grayscale(image).point(
            lambda value: 255 if value >= 170 else 0
        )

    @classmethod
    def _text_signature(cls, image: Image.Image) -> Image.Image:
        return ImageOps.fit(
            cls._binary_text(image),
            (128, 32),
            method=Image.Resampling.BILINEAR,
        )

    @classmethod
    def _gold_title_signature(cls, image: Image.Image) -> Image.Image:
        pixels = image.convert("RGB")
        red, green, blue = pixels.split()
        mask = Image.new("L", pixels.size, 0)
        mask.putdata(
            [
                255
                if r >= 145
                and g >= 105
                and b <= 150
                and r + g >= (2 * b) + 65
                else 0
                for r, g, b in zip(
                    red.get_flattened_data(),
                    green.get_flattened_data(),
                    blue.get_flattened_data(),
                )
            ]
        )
        bounds = mask.getbbox()
        if bounds is None:
            return Image.new("L", (128, 32), 0)
        glyphs = mask.crop(bounds)
        glyphs.thumbnail((124, 28), Image.Resampling.NEAREST)
        signature = Image.new("L", (128, 32), 0)
        signature.paste(
            glyphs,
            (
                (signature.width - glyphs.width) // 2,
                (signature.height - glyphs.height) // 2,
            ),
        )
        return signature

    @classmethod
    def _popup_title_score(
        cls,
        candidate: Image.Image,
        reference: Image.Image,
        region: NormalizedRect,
    ) -> float:
        candidate_signature = cls._gold_title_signature(
            cls._crop(candidate, region)
        )
        if candidate_signature.getbbox() is None:
            return 255.0
        difference = ImageChops.difference(
            candidate_signature,
            cls._gold_title_signature(cls._crop(reference, region)),
        )
        return float(ImageStat.Stat(difference).mean[0])

    def _route_digit_crop(
        self,
        candidate: Image.Image,
    ) -> Image.Image | None:
        route_definition = next(
            definition
            for definition in self.definitions
            if definition.state is ReconnectScreenState.LINE_SELECTION
        )
        route_reference = self._reference(route_definition.filename)
        prefix_reference = self._crop(
            route_reference,
            ROUTE_PREFIX_REFERENCE_REGION,
        )
        scale_x = candidate.width / route_reference.width
        scale_y = candidate.height / route_reference.height
        prefix = prefix_reference.resize(
            (
                max(1, round(prefix_reference.width * scale_x)),
                max(1, round(prefix_reference.height * scale_y)),
            ),
            Image.Resampling.BILINEAR,
        )
        prefix_binary = self._binary_text(prefix)
        search_left = round(candidate.width * ROUTE_PREFIX_SEARCH_REGION[0])
        search_top = round(candidate.height * ROUTE_PREFIX_SEARCH_REGION[1])
        search_right = round(candidate.width * ROUTE_PREFIX_SEARCH_REGION[2])
        search_bottom = round(candidate.height * ROUTE_PREFIX_SEARCH_REGION[3])
        matches: list[tuple[float, int, int]] = []
        for top in range(search_top, max(search_top + 1, search_bottom)):
            for left in range(search_left, max(search_left + 1, search_right)):
                if (
                    left + prefix.width > candidate.width
                    or top + prefix.height > candidate.height
                ):
                    continue
                sample = candidate.crop(
                    (
                        left,
                        top,
                        left + prefix.width,
                        top + prefix.height,
                    )
                )
                difference = ImageChops.difference(
                    self._binary_text(sample),
                    prefix_binary,
                )
                matches.append(
                    (
                        float(ImageStat.Stat(difference).mean[0]),
                        left,
                        top,
                    )
                )
        if not matches:
            return None
        prefix_score, left, top = min(matches)
        if prefix_score > 30.0:
            return None

        reference_prefix_left = round(
            route_reference.width * ROUTE_PREFIX_REFERENCE_REGION[0]
        )
        reference_prefix_top = round(
            route_reference.height * ROUTE_PREFIX_REFERENCE_REGION[1]
        )
        reference_digit_left = round(
            route_reference.width * ROUTE_DIGIT_REFERENCE_REGION[0]
        )
        reference_digit_top = round(
            route_reference.height * ROUTE_DIGIT_REFERENCE_REGION[1]
        )
        reference_digit_right = round(
            route_reference.width * ROUTE_DIGIT_REFERENCE_REGION[2]
        )
        reference_digit_bottom = round(
            route_reference.height * ROUTE_DIGIT_REFERENCE_REGION[3]
        )
        digit_width = max(
            1,
            round((reference_digit_right - reference_digit_left) * scale_x),
        )
        digit_height = max(
            1,
            round((reference_digit_bottom - reference_digit_top) * scale_y),
        )
        digit_left = left + round(
            (reference_digit_left - reference_prefix_left) * scale_x
        )
        digit_top = top + round(
            (reference_digit_top - reference_prefix_top) * scale_y
        )
        return candidate.crop(
            (
                digit_left,
                digit_top,
                min(candidate.width, digit_left + digit_width),
                min(candidate.height, digit_top + digit_height),
            )
        )

    def _recognize_route_number(
        self,
        candidate: Image.Image,
    ) -> tuple[int | None, float | None]:
        digit_crop = self._route_digit_crop(candidate)
        if digit_crop is None:
            return None, None
        candidate_signature = self._digit_signature(digit_crop)
        if candidate_signature is None:
            return None, None
        scores: list[tuple[float, int]] = []
        for route_number, filename in ROUTE_DIGIT_TEMPLATES.items():
            reference_signature = self._digit_signature(
                self._reference(filename)
            )
            if reference_signature is None:
                continue
            difference = ImageChops.difference(
                candidate_signature,
                reference_signature,
            )
            scores.append(
                (float(ImageStat.Stat(difference).mean[0]), route_number)
            )
        if not scores:
            return None, None
        scores.sort()
        score, route_number = scores[0]
        runner_up = scores[1][0] if len(scores) > 1 else 255.0
        if score > 65.0 or runner_up - score < 8.0:
            return None, round(score, 3)
        return route_number, round(score, 3)

    @staticmethod
    def _level_signature(image: Image.Image) -> Image.Image | None:
        pixels = image.convert("RGB")
        mask = Image.new("L", pixels.size, 0)
        mask.putdata(
            [
                255
                if min(red, green, blue) >= 150
                and max(red, green, blue) - min(red, green, blue) < 90
                else 0
                for red, green, blue in pixels.get_flattened_data()
            ]
        )
        # A selected role card draws a bright vertical border through this
        # narrow crop. Remove only full-height edge components so the border
        # cannot be mistaken for another level digit.
        width, height = mask.size
        data = bytearray(mask.tobytes())
        seen: set[int] = set()
        for start, value in enumerate(data):
            if not value or start in seen:
                continue
            stack = [start]
            seen.add(start)
            component: list[int] = []
            while stack:
                current = stack.pop()
                component.append(current)
                x = current % width
                y = current // width
                for delta_x, delta_y in (
                    (1, 0),
                    (-1, 0),
                    (0, 1),
                    (0, -1),
                    (1, 1),
                    (-1, -1),
                    (1, -1),
                    (-1, 1),
                ):
                    neighbour_x = x + delta_x
                    neighbour_y = y + delta_y
                    if not (
                        0 <= neighbour_x < width
                        and 0 <= neighbour_y < height
                    ):
                        continue
                    neighbour = neighbour_y * width + neighbour_x
                    if data[neighbour] and neighbour not in seen:
                        seen.add(neighbour)
                        stack.append(neighbour)
            component_x = tuple(item % width for item in component)
            component_y = tuple(item // width for item in component)
            touches_side = (
                min(component_x) == 0 or max(component_x) == width - 1
            )
            component_height = max(component_y) - min(component_y) + 1
            if touches_side and component_height >= round(height * 0.6):
                for item in component:
                    data[item] = 0
        mask = Image.frombytes("L", mask.size, bytes(data))
        row_counts = [
            sum(
                1
                for value in mask.crop(
                    (0, row, mask.width, row + 1)
                ).get_flattened_data()
                if value
            )
            for row in range(mask.height)
        ]
        bands: list[tuple[int, int, int]] = []
        band_start: int | None = None
        last_occupied: int | None = None
        for row, count in enumerate(row_counts):
            if count:
                if band_start is None:
                    band_start = row
                last_occupied = row
                continue
            if (
                band_start is not None
                and last_occupied is not None
                and row - last_occupied > 2
            ):
                bands.append(
                    (
                        band_start,
                        last_occupied + 1,
                        sum(row_counts[band_start : last_occupied + 1]),
                    )
                )
                band_start = None
                last_occupied = None
        if band_start is not None and last_occupied is not None:
            bands.append(
                (
                    band_start,
                    last_occupied + 1,
                    sum(row_counts[band_start : last_occupied + 1]),
                )
            )
        meaningful_bands = [
            band
            for band in bands
            if band[1] - band[0] >= 5 and band[2] >= 20
        ]
        interior_bands = [
            band
            for band in meaningful_bands
            if band[0] >= 4 and band[1] <= mask.height - 3
        ]
        if interior_bands:
            meaningful_bands = interior_bands
        if not meaningful_bands:
            return None
        top, bottom, _pixel_count = meaningful_bands[0]
        digit_line = mask.crop((0, top, mask.width, bottom))
        bounds = digit_line.getbbox()
        if bounds is None:
            return None
        glyphs = digit_line.crop(bounds).resize(
            (48, 24),
            Image.Resampling.NEAREST,
        )
        signature = Image.new("L", (64, 32), 0)
        signature.paste(
            glyphs,
            (8, 4),
        )
        return signature

    @staticmethod
    def _level_glyph_signatures(
        signature: Image.Image,
    ) -> tuple[Image.Image, ...]:
        """Normalize each level digit independently across window scales."""
        content = signature.convert("L").crop((8, 4, 56, 28))
        occupied_columns = [
            any(content.getpixel((column, row)) for row in range(content.height))
            for column in range(content.width)
        ]
        spans: list[tuple[int, int]] = []
        start: int | None = None
        for column, occupied in enumerate(occupied_columns + [False]):
            if occupied and start is None:
                start = column
            elif not occupied and start is not None:
                if column - start >= 2:
                    spans.append((start, column))
                start = None
        glyphs: list[Image.Image] = []
        for left, right in spans:
            glyph = content.crop((left, 0, right, content.height))
            bounds = glyph.getbbox()
            if bounds is None:
                continue
            normalized = glyph.crop(bounds).resize(
                (12, 20),
                Image.Resampling.NEAREST,
            )
            canvas = Image.new("L", (16, 24), 0)
            canvas.paste(normalized, (2, 2))
            glyphs.append(canvas)
        return tuple(glyphs)

    def _recognize_character_level(
        self,
        image: Image.Image,
        region: NormalizedRect,
    ) -> tuple[int | None, float | None]:
        candidate = self._level_signature(self._crop(image, region))
        if candidate is None:
            return None, None
        candidate_glyphs = self._level_glyph_signatures(candidate)
        if len(candidate_glyphs) != 3:
            return None, None
        scores: list[tuple[float, int]] = []
        for level, filename in CHARACTER_LEVEL_TEMPLATE_FILES.items():
            reference = self._level_signature(self._reference(filename))
            if reference is None:
                continue
            reference_glyphs = self._level_glyph_signatures(reference)
            # All supported levels are three digits and differ at the middle
            # digit. Comparing that digit independently avoids treating
            # different Flash window scales/fonts as a different level.
            if len(reference_glyphs) != 3:
                continue
            difference = ImageChops.difference(
                candidate_glyphs[1],
                reference_glyphs[1],
            )
            scores.append(
                (float(ImageStat.Stat(difference).mean[0]), level)
            )
        if not scores:
            return None, None
        scores.sort()
        score, level = scores[0]
        runner_up = scores[1][0] if len(scores) > 1 else 255.0
        if (
            score > CHARACTER_LEVEL_MAXIMUM_SCORE
            or runner_up - score < CHARACTER_LEVEL_MINIMUM_MARGIN
        ):
            return None, round(score, 3)
        return level, round(score, 3)

    @classmethod
    def _selected_character_slot_index(
        cls,
        image: Image.Image,
    ) -> int | None:
        scores: list[tuple[float, int]] = []
        for index, region in enumerate(
            CHARACTER_SELECTED_BORDER_REGIONS
        ):
            crop = cls._crop(image, region).convert("RGB")
            pixels = tuple(crop.get_flattened_data())
            if not pixels:
                return None
            score = sum(
                min(red, green, blue)
                for red, green, blue in pixels
            ) / len(pixels)
            scores.append((score, index))
        scores.sort(reverse=True)
        winner_score, winner_index = scores[0]
        runner_up_score = scores[1][0]
        if (
            winner_score < CHARACTER_SELECTED_MINIMUM_SCORE
            or winner_score - runner_up_score
            < CHARACTER_SELECTED_MINIMUM_MARGIN
        ):
            return None
        return winner_index

    def _character_selection_candidates(
        self,
        image: Image.Image,
    ) -> tuple[CharacterSelectionCandidate, ...]:
        selected_slot = self._selected_character_slot_index(image)
        choices: list[CharacterSelectionCandidate] = []
        for index, level_region in enumerate(CHARACTER_LEVEL_REGIONS):
            signature = self._level_signature(
                self._crop(image, level_region)
            )
            if signature is None:
                continue
            digit_count = len(self._level_glyph_signatures(signature))
            level, _score = self._recognize_character_level(
                image,
                level_region,
            )
            choices.append(
                CharacterSelectionCandidate(
                    level=level,
                    importance=None,
                    slot_index=index,
                    selected=index == selected_slot,
                    click_point=(
                        CHARACTER_ENTER_CLICK_POINT
                        if index == selected_slot
                        else CHARACTER_SLOT_CLICK_POINTS[index]
                    ),
                    digit_count=digit_count,
                )
            )
        return tuple(choices)

    def _character_selection_target(
        self,
        image: Image.Image,
    ) -> tuple[
        NormalizedPoint | None,
        int | None,
        CharacterImportance | None,
        int | None,
        bool | None,
    ]:
        candidates = self._character_selection_candidates(image)
        if not candidates:
            return None, None, None, None, None
        selected_candidates = tuple(
            candidate for candidate in candidates if candidate.selected
        )
        if len(selected_candidates) != 1:
            return None, None, None, None, None
        selected_candidate = selected_candidates[0]
        return (
            selected_candidate.click_point,
            selected_candidate.level,
            selected_candidate.importance,
            selected_candidate.slot_index,
            selected_candidate.selected,
        )

    @classmethod
    def _region_score(
        cls,
        candidate: Image.Image,
        reference: Image.Image,
        region: NormalizedRect,
    ) -> float:
        candidate_signature = cls._signature(cls._crop(candidate, region))
        reference_signature = cls._signature(cls._crop(reference, region))
        difference = ImageChops.difference(candidate_signature, reference_signature)
        return float(ImageStat.Stat(difference).mean[0])

    def _battle_context_score(self, candidate: Image.Image) -> float:
        """Score the stable top-right auto-battle panel outside any dialog."""
        reference = self._reference(BATTLE_REFERENCE_FILE)
        variants = [candidate]
        if candidate.height >= 100:
            variants.append(
                candidate.crop(
                    (
                        0,
                        round(candidate.height * 0.05),
                        candidate.width,
                        round(candidate.height * 0.985),
                    )
                )
            )
        score = min(
            self._region_score(
                variant,
                reference,
                BATTLE_CONTEXT_REGION,
            )
            for variant in variants
        )
        return score

    def _battle_screen_evidence_score(self, candidate: Image.Image) -> float:
        """Require broad gameplay evidence before reporting battle as online."""
        reference = self._reference(BATTLE_REFERENCE_FILE)
        variants = [candidate]
        if candidate.height >= 100:
            variants.append(
                candidate.crop(
                    (
                        0,
                        round(candidate.height * 0.05),
                        candidate.width,
                        round(candidate.height * 0.985),
                    )
                )
            )
        reference_signature = ImageOps.fit(
            self._crop(
                reference,
                BATTLE_SCREEN_EVIDENCE_REGION,
            ).filter(ImageFilter.GaussianBlur(radius=2.0)),
            self.SIGNATURE_SIZE,
            method=Image.Resampling.BILINEAR,
        )
        return min(
            sum(
                ImageStat.Stat(
                    ImageChops.difference(
                        ImageOps.fit(
                            self._crop(
                                variant,
                                BATTLE_SCREEN_EVIDENCE_REGION,
                            ).filter(
                                ImageFilter.GaussianBlur(radius=2.0)
                            ),
                            self.SIGNATURE_SIZE,
                            method=Image.Resampling.BILINEAR,
                        ),
                        reference_signature,
                    )
                ).mean
            )
            / 3.0
            for variant in variants
        )

    @classmethod
    def _battle_screen_has_structure(cls, candidate: Image.Image) -> bool:
        """Reject a stable battle panel shown above a large unknown overlay."""
        centre = cls._crop(
            candidate,
            BATTLE_SCREEN_STRUCTURE_REGION,
        ).convert("L")
        structure = ImageStat.Stat(centre)
        edge_mean = ImageStat.Stat(
            centre.filter(ImageFilter.FIND_EDGES)
        ).mean[0]
        return (
            structure.stddev[0] >= BATTLE_SCREEN_MINIMUM_STDDEV
            and edge_mean >= BATTLE_SCREEN_MINIMUM_EDGE_MEAN
        )

    def _is_battle_context(self, candidate: Image.Image) -> bool:
        return (
            self._battle_context_score(candidate)
            <= BATTLE_CONTEXT_MAXIMUM_SCORE
        )

    @staticmethod
    def _flat_pixels(image: Image.Image) -> list[tuple[int, int, int]]:
        getter = getattr(image, "get_flattened_data", None)
        if callable(getter):
            return list(getter())
        return list(image.getdata())

    def _prepared_disconnect_overlay_reference(
        self,
        reference: Image.Image,
    ) -> tuple[Image.Image, Image.Image] | None:
        prepared = self._disconnect_overlay_reference
        if prepared is not None and prepared[0] is reference:
            return prepared[1], prepared[2]

        reference_crop = self._crop(
            reference,
            DISCONNECT_OVERLAY_REGION,
        ).resize(
            DISCONNECT_OVERLAY_SIGNATURE_SIZE,
            Image.Resampling.BILINEAR,
        )
        mask = Image.new("L", reference_crop.size, 0)
        mask.putdata(
            [
                255
                if (
                    (green > 115 and blue > 115 and blue > red * 1.03)
                    or (red > 175 and green > 175 and blue > 175)
                    or (red > 170 and green > 145 and blue < 125)
                )
                else 0
                for red, green, blue in self._flat_pixels(reference_crop)
            ]
        )
        if mask.getbbox() is None:
            return None
        self._disconnect_overlay_reference = (
            reference,
            reference_crop,
            mask,
        )
        return reference_crop, mask

    @staticmethod
    def _disconnect_overlay_candidate_boxes(
        candidate: Image.Image,
    ) -> tuple[tuple[int, int, int, int], ...]:
        left, top, right, bottom = DISCONNECT_OVERLAY_REGION
        width, height = candidate.size
        base_box = (
            round(width * left),
            round(height * top),
            round(width * right),
            round(height * bottom),
        )
        base_width = base_box[2] - base_box[0]
        base_height = base_box[3] - base_box[1]
        center_x = (base_box[0] + base_box[2]) / 2.0
        center_y = (base_box[1] + base_box[3]) / 2.0
        offset_x = max(2, round(width * 0.007))
        offset_y = max(2, round(height * 0.010))
        boxes: list[tuple[int, int, int, int]] = []

        # Preserve the original fine translation search for normal title-bar
        # and capture-border differences.
        for delta_y in sorted(
            set(range(-offset_y, offset_y + 1, 2)) | {0}
        ):
            for delta_x in sorted(
                set(range(-offset_x, offset_x + 1, 2)) | {0}
            ):
                boxes.append(
                    (
                        base_box[0] + delta_x,
                        base_box[1] + delta_y,
                        base_box[2] + delta_x,
                        base_box[3] + delta_y,
                    )
                )

        # A Flash client captured through another route can move the modal by
        # roughly one title-bar height or scale it independently of the outer
        # window. Search a bounded coarse grid before battle-online evidence is
        # allowed to win.
        for scale in (0.90, 0.95, 1.0, 1.05, 1.10):
            scaled_width = max(1, round(base_width * scale))
            scaled_height = max(1, round(base_height * scale))
            for y_multiplier in (-2, -1, 0, 1, 2):
                for x_multiplier in (-2, -1, 0, 1, 2):
                    shifted_x = center_x + x_multiplier * offset_x
                    shifted_y = center_y + y_multiplier * offset_y
                    crop_left = round(shifted_x - scaled_width / 2.0)
                    crop_top = round(shifted_y - scaled_height / 2.0)
                    boxes.append(
                        (
                            crop_left,
                            crop_top,
                            crop_left + scaled_width,
                            crop_top + scaled_height,
                        )
                    )
        return tuple(dict.fromkeys(boxes))

    def _best_disconnect_overlay_match(
        self,
        candidate: Image.Image,
        reference: Image.Image,
    ) -> tuple[float, Image.Image | None]:
        """Return the best bounded position/scale match and its exact crop."""
        prepared = self._prepared_disconnect_overlay_reference(reference)
        if prepared is None:
            return 255.0, None
        reference_crop, mask = prepared

        best = 255.0
        best_crop = None
        for box in self._disconnect_overlay_candidate_boxes(candidate):
            candidate_crop = candidate.crop(box).resize(
                DISCONNECT_OVERLAY_SIGNATURE_SIZE,
                Image.Resampling.BILINEAR,
            )
            difference = ImageChops.difference(
                candidate_crop,
                reference_crop,
            )
            channel_means = ImageStat.Stat(
                difference,
                mask=mask,
            ).mean
            score = sum(channel_means) / len(channel_means)
            if score < best:
                best = score
                best_crop = candidate_crop
            if best == 0.0:
                break
        return best, best_crop

    def _disconnect_overlay_score(
        self,
        candidate: Image.Image,
        reference: Image.Image,
    ) -> float:
        """Compare stable dialog pixels across bounded position/scale drift."""
        score, _crop = self._best_disconnect_overlay_match(
            candidate,
            reference,
        )
        return score

    def _disconnect_overlay_has_structure(
        self,
        candidate: Image.Image,
        reference: Image.Image,
    ) -> bool:
        prepared = self._prepared_disconnect_overlay_reference(reference)
        if prepared is None:
            return False
        _reference_crop, mask = prepared
        _score, candidate_crop = self._best_disconnect_overlay_match(
            candidate,
            reference,
        )
        if candidate_crop is None:
            return False
        channel_stddev = ImageStat.Stat(
            candidate_crop,
            mask=mask,
        ).stddev
        return (
            sum(channel_stddev) / len(channel_stddev)
            >= DISCONNECT_OVERLAY_MINIMUM_MASKED_STDDEV
        )

    def recognize_image(self, image: Image.Image) -> ScreenRecognition:
        candidate = image.convert("RGB")
        if candidate.width < 64 or candidate.height < 64:
            return ScreenRecognition(
                state=ReconnectScreenState.UNKNOWN,
                score=None,
                click_point=None,
                reference_name=None,
            )

        disconnected = next(
            definition
            for definition in self.definitions
            if definition.state is ReconnectScreenState.DISCONNECTED
        )
        disconnected_reference = self._reference(disconnected.filename)
        disconnected_score = self._disconnect_overlay_score(
            candidate,
            disconnected_reference,
        )
        candidate_ratio = candidate.width / candidate.height
        disconnected_ratio = (
            disconnected_reference.width / disconnected_reference.height
        )
        if (
            abs(candidate_ratio - disconnected_ratio) <= 0.12
            and disconnected_score <= disconnected.maximum_score
            and self._disconnect_overlay_has_structure(
                candidate,
                disconnected_reference,
            )
        ):
            return ScreenRecognition(
                state=ReconnectScreenState.DISCONNECTED,
                score=round(disconnected_score, 3),
                click_point=disconnected.click_point,
                reference_name=disconnected.filename,
                battle_context=self._is_battle_context(candidate),
            )

        # A normal auto-battle screen uses a dedicated full-window reference
        # because its right-side layout differs from ordinary gameplay.  The
        # disconnect overlay above always wins first; this branch is therefore
        # read-only evidence that the game remains connected and never exposes
        # a click target.
        battle_reference = self._reference(BATTLE_REFERENCE_FILE)
        battle_ratio = battle_reference.width / battle_reference.height
        battle_score = self._battle_context_score(candidate)
        if (
            abs(candidate_ratio - battle_ratio) <= 0.12
            and battle_score <= BATTLE_CONTEXT_MAXIMUM_SCORE
        ):
            battle_evidence_score = self._battle_screen_evidence_score(
                candidate
            )
            if (
                battle_evidence_score
                <= BATTLE_SCREEN_EVIDENCE_MAXIMUM_SCORE
                and self._battle_screen_has_structure(candidate)
            ):
                return ScreenRecognition(
                    state=ReconnectScreenState.CONNECTED,
                    score=round(
                        max(battle_score, battle_evidence_score),
                        3,
                    ),
                    click_point=None,
                    reference_name=BATTLE_REFERENCE_FILE,
                    battle_context=True,
                )
            # The stable auto-battle panel may remain visible underneath an
            # unrecognized modal. Do not let another broad gameplay template
            # turn that incomplete evidence into a false online result.
            return ScreenRecognition(
                state=ReconnectScreenState.UNKNOWN,
                score=round(battle_evidence_score, 3),
                click_point=None,
                reference_name=None,
                battle_context=False,
            )

        scored: list[tuple[float, ScreenTemplateDefinition]] = []
        for definition in self.definitions:
            if definition.state is ReconnectScreenState.DISCONNECTED:
                continue
            reference = self._reference(definition.filename)
            candidate_ratio = candidate.width / candidate.height
            reference_ratio = reference.width / reference.height
            if abs(candidate_ratio - reference_ratio) > 0.12:
                continue
            popup_title_region = POPUP_TITLE_REGIONS.get(definition.state)
            if (
                popup_title_region is not None
                and self._popup_title_score(
                    candidate,
                    reference,
                    popup_title_region,
                )
                > POPUP_TITLE_MAXIMUM_SCORE
            ):
                continue
            scores = [
                self._region_score(candidate, reference, region)
                for region in definition.regions
            ]
            scored.append((sum(scores) / len(scores), definition))

        valid_scored = [
            item
            for item in scored
            if item[0] <= item[1].maximum_score
        ]
        character_candidates: tuple[CharacterSelectionCandidate, ...] = ()
        if any(
            item[1].state is ReconnectScreenState.CHARACTER_SELECTION
            for item in valid_scored
        ):
            character_candidates = self._character_selection_candidates(
                candidate
            )
            if not character_candidates:
                # Normal gameplay can share broad background colors with the
                # role-selection template. A role-selection state is valid
                # only when at least one supported level card is actually
                # present; otherwise allow the connected template to win.
                valid_scored = [
                    item
                    for item in valid_scored
                    if item[1].state
                    is not ReconnectScreenState.CHARACTER_SELECTION
                ]
        if not valid_scored:
            return ScreenRecognition(
                state=ReconnectScreenState.UNKNOWN,
                score=(
                    round(min(scored, key=lambda item: item[0])[0], 3)
                    if scored
                    else None
                ),
                click_point=None,
                reference_name=None,
            )

        # The route chooser is a modal overlay on top of the login screen.
        # Its background therefore remains an excellent LOGIN_START match.
        # Prefer the confirmed modal whenever its own score is inside the
        # validated threshold, even when the underlying screen scores lower.
        modal_match = next(
            (
                item
                for item in valid_scored
                if item[1].state
                in {
                    ReconnectScreenState.FORCE_LOGIN_TIMEOUT,
                    ReconnectScreenState.LINE_SELECTION,
                }
            ),
            None,
        )
        score, definition = (
            modal_match
            if modal_match is not None
            else min(valid_scored, key=lambda item: item[0])
        )
        line_number = None
        character_level = None
        character_importance = None
        character_slot_index = None
        character_slot_selected = None
        click_point = definition.click_point
        if definition.state is ReconnectScreenState.LINE_SELECTION:
            line_number, _route_score = self._recognize_route_number(candidate)
            if line_number is None:
                line_number = DEFAULT_LINE_NUMBER
            click_point = LINE_ROUTE_CLICK_POINTS[line_number]
        elif definition.state is ReconnectScreenState.CHARACTER_SELECTION:
            (
                click_point,
                character_level,
                character_importance,
                character_slot_index,
                character_slot_selected,
            ) = self._character_selection_target(candidate)
        return ScreenRecognition(
            state=definition.state,
            score=round(score, 3),
            click_point=click_point,
            reference_name=definition.filename,
            line_number=line_number,
            character_level=character_level,
            character_importance=character_importance,
            character_slot_index=character_slot_index,
            character_slot_selected=character_slot_selected,
            character_candidates=character_candidates,
        )

    def recognize_capture(self, sample: CaptureSample | None) -> ScreenRecognition:
        if (
            sample is None
            or not sample.api_succeeded
            or sample.width <= 0
            or sample.height <= 0
            or len(sample.pixels) != sample.width * sample.height * 4
        ):
            return ScreenRecognition(
                state=ReconnectScreenState.UNKNOWN,
                score=None,
                click_point=None,
                reference_name=None,
            )
        image = Image.frombytes(
            "RGBA",
            (sample.width, sample.height),
            sample.pixels,
            "raw",
            "BGRA",
        ).convert("RGB")
        return self.recognize_image(image)
