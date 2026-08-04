"""Reference-image recognition for the confirmed Flash reconnect flow.

The recognizer compares only stable, normalized UI regions from the user-
provided full-window images.  It never sends input and never persists captured
game pixels.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any, Iterable

from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat

from adapters.windows_background_capture import CaptureSample
from adapters.windows_role_id_ocr import ROLE_ID_MODEL
from core.reconnect_policy import ReconnectScreenState
from domain.character import CharacterImportance


NormalizedRect = tuple[float, float, float, float]
NormalizedPoint = tuple[float, float]
FORCE_LOGIN_CLICK_POINT: NormalizedPoint = (0.505, 0.856)
# Mouse delivery is relative to the Flash client area, while reference
# screenshots include the 45-pixel Windows title bar. This targets the centre
# of the confirmed "?? button in 14_force_login_timeout.png.
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
# The visible character-name line is read only after the selection template and
# its level glyphs already match.  A trailing ellipsis remains part of the
# result so the controller can distinguish an abbreviated name from a full ID.
CHARACTER_NAME_REGIONS: tuple[NormalizedRect, ...] = (
    (460 / 1348, 640 / 937, 570 / 1348, 675 / 937),
    (660 / 1348, 640 / 937, 770 / 1348, 675 / 937),
    (865 / 1348, 640 / 937, 975 / 1348, 675 / 937),
)
CHARACTER_NAME_THRESHOLD = 150
CHARACTER_NAME_SCALE = 4
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
DISCONNECT_OVERLAY_PROBE_SIZE = (32, 8)
DISCONNECT_OVERLAY_FULL_MATCH_LIMIT = 64
DISCONNECT_OVERLAY_MINIMUM_MASKED_STDDEV = 12.0
# This is the text line in the confirmed disconnect dialog.  Template matching
# narrows the candidate first; the local reader then requires the core
# disconnect word before the reconnect flow can send any input.
DISCONNECT_TEXT_WITHIN_OVERLAY: NormalizedRect = (
    ((470 / 1349) - DISCONNECT_OVERLAY_REGION[0])
    / (DISCONNECT_OVERLAY_REGION[2] - DISCONNECT_OVERLAY_REGION[0]),
    ((480 / 936) - DISCONNECT_OVERLAY_REGION[1])
    / (DISCONNECT_OVERLAY_REGION[3] - DISCONNECT_OVERLAY_REGION[1]),
    ((890 / 1349) - DISCONNECT_OVERLAY_REGION[0])
    / (DISCONNECT_OVERLAY_REGION[2] - DISCONNECT_OVERLAY_REGION[0]),
    ((505 / 936) - DISCONNECT_OVERLAY_REGION[1])
    / (DISCONNECT_OVERLAY_REGION[3] - DISCONNECT_OVERLAY_REGION[1]),
)
DISCONNECT_TEXT_THRESHOLD = 160
DISCONNECT_TEXT_SCALE = 3
DISCONNECT_TEXT_PADDING = 8
# The second confirmed disconnect screenshot has the same state but a
# different stable dialog layout.  It remains an alternate reference for the
# same state; it does not introduce an additional screen state or action.
DISCONNECT_REFERENCE_FILES: tuple[str, ...] = (
    "01_disconnected_dialog.png",
    "15_disconnected_card_popup.png",
)
# These are the two confirmed full-window gameplay references.  They are
# evidence for rejecting an actionable dialog whose required button/selection
# region has been replaced by an ordinary gameplay crop.  This is a relative
# structural comparison, not a new colour threshold.
GAMEPLAY_EVIDENCE_REFERENCE_FILES: tuple[str, ...] = (
    "06_connected_gameplay.png",
    BATTLE_REFERENCE_FILE,
)
CONNECTED_CENTRAL_EVIDENCE_REGION: NormalizedRect = (
    0.400,
    0.400,
    0.600,
    0.600,
)
CENTRAL_MODAL_REFERENCE_FILES: tuple[str, ...] = (
    "03_line_selection_dialog.png",
    "07_post_login_activity_popup.png",
    "08_post_login_recommendation_popup.png",
    "12_post_login_auto_dungeon_popup.png",
)


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
            # The upper portion of the confirmed start panel is separate
            # evidence.  Without it, a replaced line-selection dialog can
            # partly overwrite this panel and fall through to LOGIN_START.
            (0.382, 0.730, 0.620, 0.795),
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
            # Online gameplay is never inferred from the narrow right panel
            # alone.  These four central reference regions make an unknown
            # modal, flat overlay, or partial background incapable of proving
            # that the player is safely online.
            (0.280, 0.250, 0.450, 0.400),
            (0.550, 0.250, 0.720, 0.400),
            (0.280, 0.580, 0.450, 0.730),
            (0.550, 0.580, 0.720, 0.730),
            # The confirmed connected reference also needs direct central
            # evidence.  A central unknown dialog can otherwise leave all
            # four surrounding regions intact and falsely prove CONNECTED.
            CONNECTED_CENTRAL_EVIDENCE_REGION,
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
        self._recognition_cache: OrderedDict[
            tuple[tuple[int, int], bytes],
            ScreenRecognition,
        ] = OrderedDict()
        self._full_reference_signatures: dict[int, Image.Image] = {}
        self._disconnect_overlay_reference: (
            tuple[
                Image.Image,
                Image.Image,
                Image.Image,
                Image.Image,
                Image.Image,
            ]
            | None
        ) = None
        self._disconnect_text_reader: Any | None = None
        self._disconnect_text_reader_loaded = False

    @property
    def missing_references(self) -> tuple[str, ...]:
        # Every file that recognition can load belongs to the ready contract.
        # The alternate disconnect layouts are consulted on every pass even
        # though only the primary layout appears in the default definitions.
        # Keep the returned list deterministic and free of duplicates so a
        # missing alternative fails before any capture is classified.
        required = tuple(
            dict.fromkeys(
                (
                    *(
                        definition.filename
                        for definition in self.definitions
                    ),
                    *DISCONNECT_REFERENCE_FILES,
                    *ROUTE_DIGIT_TEMPLATES.values(),
                    *CHARACTER_LEVEL_TEMPLATE_FILES.values(),
                    BATTLE_REFERENCE_FILE,
                )
            )
        )
        return tuple(
            filename
            for filename in required
            if not (self.reference_dir / filename).is_file()
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
        *,
        read_identity: bool = False,
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
            identity = (
                self._character_selection_identity(
                    image,
                    CHARACTER_NAME_REGIONS[index],
                )
                if read_identity
                else None
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
                    identity=identity,
                )
            )
        return tuple(choices)

    def _character_selection_identity(
        self,
        image: Image.Image,
        region: NormalizedRect,
    ) -> str | None:
        text = self._read_local_text(
            self._crop(image, region),
            threshold=CHARACTER_NAME_THRESHOLD,
            scale=CHARACTER_NAME_SCALE,
        )
        if text:
            return text
        return "\u2026"

    def _character_selection_target(
        self,
        image: Image.Image,
        *,
        candidates: tuple[CharacterSelectionCandidate, ...] | None = None,
    ) -> tuple[
        NormalizedPoint | None,
        int | None,
        CharacterImportance | None,
        int | None,
        bool | None,
    ]:
        if candidates is None:
            candidates = self._character_selection_candidates(
                image,
                read_identity=True,
            )
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

    @classmethod
    def _region_has_nonuniform_structure(
        cls,
        candidate: Image.Image,
        reference: Image.Image,
        region: NormalizedRect,
    ) -> bool:
        """Reject a flat replacement where a confirmed reference has detail.

        This is deliberately a structural existence check, not a new colour
        threshold: a reference region that contains detail requires the
        candidate region to contain at least some luminance and edge detail as
        well.  Per-region template matching then supplies the calibrated
        similarity threshold from the confirmed reference definition.
        """
        return cls._image_has_nonuniform_structure(
            cls._crop(candidate, region),
            cls._crop(reference, region),
        )

    @classmethod
    def _region_has_matching_structure(
        cls,
        candidate: Image.Image,
        reference: Image.Image,
        region: NormalizedRect,
        maximum_score: float,
    ) -> bool:
        """Require each evidence region to retain its reference structure."""
        candidate_edges = cls._signature(
            cls._crop(candidate, region)
            .convert("L")
            .filter(ImageFilter.FIND_EDGES)
        )
        reference_edges = cls._signature(
            cls._crop(reference, region)
            .convert("L")
            .filter(ImageFilter.FIND_EDGES)
        )
        edge_difference = ImageChops.difference(
            candidate_edges,
            reference_edges,
        )
        return (
            float(ImageStat.Stat(edge_difference).mean[0])
            <= maximum_score
        )

    @classmethod
    def _edge_difference(
        cls,
        candidate_region: Image.Image,
        reference_region: Image.Image,
    ) -> float:
        """Return one normalized edge-structure difference score."""
        candidate_edges = cls._signature(
            candidate_region.convert("L").filter(ImageFilter.FIND_EDGES)
        )
        reference_edges = cls._signature(
            reference_region.convert("L").filter(ImageFilter.FIND_EDGES)
        )
        return float(
            ImageStat.Stat(
                ImageChops.difference(candidate_edges, reference_edges)
            ).mean[0]
        )

    def _region_is_closer_to_confirmed_references(
        self,
        candidate_region: Image.Image,
        reference_region: Image.Image,
        region: NormalizedRect,
        reference_filenames: Iterable[str],
    ) -> bool:
        """Return whether a region is closer to another confirmed context.

        This compares only reference-relative edge evidence.  The caller
        chooses the confirmed alternative contexts appropriate for its own
        fail-closed decision.
        """
        reference_edge_difference = self._edge_difference(
            candidate_region,
            reference_region,
        )
        return any(
            self._edge_difference(
                candidate_region,
                self._crop(self._reference(filename), region),
            )
            < reference_edge_difference
            for filename in reference_filenames
        )

    def _region_is_confirmed_gameplay_replacement(
        self,
        candidate_region: Image.Image,
        reference_region: Image.Image,
        region: NormalizedRect,
    ) -> bool:
        """Reject a required dialog region replaced by known gameplay.

        A dark but intact dialog may have a broad luminance difference from
        its reference.  It must not be rejected merely for being darker.  A
        replacement with a confirmed gameplay crop is distinguishable because
        its edge structure is closer to that gameplay crop than to the
        required dialog evidence.  The comparison uses only existing,
        confirmed reference images and has no independently guessed limit.
        """
        return self._region_is_closer_to_confirmed_references(
            candidate_region,
            reference_region,
            region,
            GAMEPLAY_EVIDENCE_REFERENCE_FILES,
        )

    def _connected_central_region_has_confirmed_modal(
        self,
        candidate: Image.Image,
        reference: Image.Image,
    ) -> bool:
        """Reject a modal pasted into the stable connected central evidence."""
        region = CONNECTED_CENTRAL_EVIDENCE_REGION
        return self._region_is_closer_to_confirmed_references(
            self._crop(candidate, region),
            self._crop(reference, region),
            region,
            CENTRAL_MODAL_REFERENCE_FILES,
        )

    @staticmethod
    def _image_has_nonuniform_structure(
        candidate_region: Image.Image,
        reference_region: Image.Image,
    ) -> bool:
        """Require detail where the confirmed evidence region has detail."""
        reference_region = reference_region.convert("L")
        reference_edges = ImageStat.Stat(
            reference_region.filter(ImageFilter.FIND_EDGES)
        ).mean[0]
        reference_stddev = ImageStat.Stat(reference_region).stddev[0]
        if reference_edges == 0.0 and reference_stddev == 0.0:
            return True
        candidate_region = candidate_region.convert("L")
        return (
            ImageStat.Stat(candidate_region).stddev[0] > 0.0
            and ImageStat.Stat(
                candidate_region.filter(ImageFilter.FIND_EDGES)
            ).mean[0]
            > 0.0
        )

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
    ) -> tuple[
        Image.Image,
        Image.Image,
        Image.Image,
        Image.Image,
    ] | None:
        prepared = self._disconnect_overlay_reference
        if prepared is not None and prepared[0] is reference:
            return prepared[1], prepared[2], prepared[3], prepared[4]

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
        probe_reference = reference_crop.resize(
            DISCONNECT_OVERLAY_PROBE_SIZE,
            Image.Resampling.BILINEAR,
        )
        probe_mask = mask.resize(
            DISCONNECT_OVERLAY_PROBE_SIZE,
            Image.Resampling.NEAREST,
        )
        self._disconnect_overlay_reference = (
            reference,
            reference_crop,
            mask,
            probe_reference,
            probe_mask,
        )
        return reference_crop, mask, probe_reference, probe_mask

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
    ) -> tuple[
        float,
        Image.Image | None,
        tuple[int, int, int, int] | None,
    ]:
        """Return the best bounded position/scale match and its exact crop."""
        prepared = self._prepared_disconnect_overlay_reference(reference)
        if prepared is None:
            return 255.0, None, None
        reference_crop, mask, probe_reference, probe_mask = prepared

        best = 255.0
        best_crop = None
        best_box = None
        probe_matches: list[tuple[float, int, tuple[int, int, int, int]]] = []
        for index, box in enumerate(
            self._disconnect_overlay_candidate_boxes(candidate)
        ):
            probe_crop = candidate.crop(box).resize(
                DISCONNECT_OVERLAY_PROBE_SIZE,
                Image.Resampling.BILINEAR,
            )
            probe_difference = ImageChops.difference(
                probe_crop,
                probe_reference,
            )
            probe_means = ImageStat.Stat(
                probe_difference,
                mask=probe_mask,
            ).mean
            probe_matches.append(
                (sum(probe_means) / len(probe_means), index, box)
            )
        for _probe_score, _index, box in sorted(probe_matches)[
            :DISCONNECT_OVERLAY_FULL_MATCH_LIMIT
        ]:
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
                best_box = box
            if best == 0.0:
                break
        return best, best_crop, best_box

    def _disconnect_overlay_score(
        self,
        candidate: Image.Image,
        reference: Image.Image,
    ) -> float:
        """Compare stable dialog pixels across bounded position/scale drift."""
        score, _crop, _box = self._best_disconnect_overlay_match(
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
        _reference_crop, mask, _probe_reference, _probe_mask = prepared
        _score, candidate_crop, _box = self._best_disconnect_overlay_match(
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

    def _disconnect_required_regions_match(
        self,
        candidate: Image.Image,
        reference: Image.Image,
        dialog_box: tuple[int, int, int, int] | None,
        definition: ScreenTemplateDefinition,
    ) -> bool:
        """Validate every required disconnect region at the matched overlay.

        The disconnect dialog is allowed a bounded position/scale drift, so
        its required full-window regions are mapped through the selected
        overlay box before being compared.  A full overlay score alone is not
        permission to click when one required evidence region is absent.
        """
        if dialog_box is None:
            return False
        candidate_overlay = candidate.crop(dialog_box)
        reference_overlay = self._crop(
            reference,
            DISCONNECT_OVERLAY_REGION,
        )
        if (
            candidate_overlay.width <= 0
            or candidate_overlay.height <= 0
            or reference_overlay.width <= 0
            or reference_overlay.height <= 0
        ):
            return False
        overlay_left, overlay_top, overlay_right, overlay_bottom = (
            DISCONNECT_OVERLAY_REGION
        )
        overlay_width = overlay_right - overlay_left
        overlay_height = overlay_bottom - overlay_top
        for left, top, right, bottom in definition.regions:
            relative = (
                (left - overlay_left) / overlay_width,
                (top - overlay_top) / overlay_height,
                (right - overlay_left) / overlay_width,
                (bottom - overlay_top) / overlay_height,
            )
            if not (
                0.0 <= relative[0] < relative[2] <= 1.0
                and 0.0 <= relative[1] < relative[3] <= 1.0
            ):
                return False
            current_region = self._crop(candidate_overlay, relative)
            reference_region = self._crop(reference_overlay, relative)
            if current_region.width < 1 or current_region.height < 1:
                return False
            difference = ImageChops.difference(
                self._signature(current_region),
                self._signature(reference_region),
            )
            score = float(ImageStat.Stat(difference).mean[0])
            if (
                score > definition.maximum_score
                or not self._image_has_nonuniform_structure(
                    current_region,
                    reference_region,
                )
                or self._region_is_confirmed_gameplay_replacement(
                    current_region,
                    reference_region,
                    (left, top, right, bottom),
                )
                or ImageStat.Stat(
                    current_region.convert("L")
                ).stddev[0]
                < DISCONNECT_OVERLAY_MINIMUM_MASKED_STDDEV
            ):
                return False
        return True

    def _best_disconnect_reference_match(
        self,
        candidate: Image.Image,
        primary_filename: str,
    ) -> tuple[float, Image.Image, tuple[int, int, int, int] | None]:
        """Choose the one confirmed disconnect layout with the best overlay."""
        filenames = tuple(
            dict.fromkeys((primary_filename, *DISCONNECT_REFERENCE_FILES))
        )
        matches = []
        for filename in filenames:
            reference = self._reference(filename)
            score, _crop, box = self._best_disconnect_overlay_match(
                candidate,
                reference,
            )
            matches.append((score, reference, box))
        return min(matches, key=lambda item: item[0])

    @staticmethod
    def _definition_can_send_input(
        definition: ScreenTemplateDefinition,
    ) -> bool:
        return bool(
            definition.click_point is not None
            or definition.state is ReconnectScreenState.LINE_SELECTION
        )

    def _full_image_score(
        self,
        candidate: Image.Image,
        reference: Image.Image,
        candidate_signature: Image.Image | None = None,
    ) -> float:
        reference_key = id(reference)
        reference_signature = self._full_reference_signatures.get(
            reference_key
        )
        if reference_signature is None:
            reference_signature = self._signature(reference)
            self._full_reference_signatures[reference_key] = (
                reference_signature
            )
        difference = ImageChops.difference(
            (
                candidate_signature
                if candidate_signature is not None
                else self._signature(candidate)
            ),
            reference_signature,
        )
        return float(ImageStat.Stat(difference).mean[0])

    def _has_incomplete_actionable_template(
        self,
        candidate: Image.Image,
        scored: Iterable[
            tuple[float, ScreenTemplateDefinition, tuple[float, ...]]
        ],
        selected: ScreenTemplateDefinition,
        candidate_signature: Image.Image,
    ) -> bool:
        """Reject a fallback state after a near-match action screen is damaged."""
        selected_reference = self._reference(selected.filename)
        selected_full_score = self._full_image_score(
            candidate,
            selected_reference,
            candidate_signature,
        )
        for _average_score, definition, scores in scored:
            if (
                definition.state is selected.state
                or not self._definition_can_send_input(definition)
            ):
                continue
            reference = self._reference(definition.filename)
            actionable_full_score = self._full_image_score(
                candidate,
                reference,
                candidate_signature,
            )
            # Only a nearer actionable template can cause a fallback.  Avoid
            # repeatedly evaluating every action template for ordinary
            # CONNECTED gameplay, which is already farther from each dialog.
            if actionable_full_score >= selected_full_score:
                continue
            evidence = tuple(
                score <= definition.maximum_score
                and self._region_has_nonuniform_structure(
                    candidate,
                    reference,
                    region,
                )
                and self._region_has_matching_structure(
                    candidate,
                    reference,
                    region,
                    definition.maximum_score,
                )
                for score, region in zip(scores, definition.regions)
            )
            gameplay_replacements = tuple(
                self._region_is_confirmed_gameplay_replacement(
                    self._crop(candidate, region),
                    self._crop(reference, region),
                    region,
                )
                for region in definition.regions
            )
            # A damaged actionable screen must not fall back to another
            # actionable state (or LOGIN_START) when its remaining required
            # region(s) still prove which dialog it was.  Ordinary connected
            # gameplay has no surviving dialog evidence and therefore does
            # not trip this guard.
            if (
                any(gameplay_replacements)
                and any(evidence)
                and actionable_full_score < selected_full_score
            ):
                return True
            if (
                not all(evidence)
                and actionable_full_score < selected_full_score
            ):
                return True
        return False

    @staticmethod
    def _local_model_path() -> Path:
        bundle_root = getattr(sys, "_MEIPASS", None)
        root = (
            Path(bundle_root)
            if bundle_root
            else Path(__file__).resolve().parents[1]
        )
        return root / ROLE_ID_MODEL

    def _disconnect_reader(self) -> Any | None:
        if self._disconnect_text_reader_loaded:
            return self._disconnect_text_reader
        self._disconnect_text_reader_loaded = True
        try:
            from rapidocr_onnxruntime.ch_ppocr_rec.text_recognize import (
                TextRecognizer,
            )

            model_path = self._local_model_path()
            if not model_path.is_file():
                return None
            self._disconnect_text_reader = TextRecognizer(
                {
                    "intra_op_num_threads": -1,
                    "inter_op_num_threads": -1,
                    "use_cuda": False,
                    "use_dml": False,
                    "model_path": str(model_path),
                    "rec_img_shape": [3, 48, 320],
                    "rec_batch_num": 6,
                }
            )
        except (ImportError, OSError, RuntimeError, ValueError):
            return None
        return self._disconnect_text_reader

    def _disconnect_text_has_words(
        self,
        candidate: Image.Image,
        dialog_box: tuple[int, int, int, int] | None,
    ) -> bool:
        if dialog_box is None:
            return False
        text_region = self._crop(
            candidate.crop(dialog_box),
            DISCONNECT_TEXT_WITHIN_OVERLAY,
        )
        text = self._read_local_text(
            text_region,
            threshold=DISCONNECT_TEXT_THRESHOLD,
            scale=DISCONNECT_TEXT_SCALE,
        )
        if '銝剜' in text:
            return True
        if not text:
            return bool(self._binary_text(text_region).getbbox())
        return False

    def _read_local_text(
        self,
        image: Image.Image,
        *,
        threshold: int,
        scale: int,
    ) -> str:
        reader = self._disconnect_reader()
        if reader is None:
            return ""
        glyphs = image.convert("L").point(
            lambda value: 255 if value >= threshold else 0
        ).convert("RGB")
        prepared = Image.new(
            "RGB",
            (
                glyphs.width + DISCONNECT_TEXT_PADDING * 2,
                glyphs.height + DISCONNECT_TEXT_PADDING * 2,
            ),
            "black",
        )
        prepared.paste(
            glyphs,
            (DISCONNECT_TEXT_PADDING, DISCONNECT_TEXT_PADDING),
        )
        try:
            from numpy import asarray

            result, _elapsed = reader(
                asarray(
                    prepared.resize(
                        (
                            prepared.width * scale,
                            prepared.height * scale,
                        ),
                        Image.Resampling.NEAREST,
                    )
                )
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return ""
        parts = [
            item[0]
            for item in result
            if isinstance(item, (list, tuple))
            and item
            and isinstance(item[0], str)
        ]
        return "".join(parts).replace(" ", "")

    def recognize_image(self, image: Image.Image) -> ScreenRecognition:
        candidate = image if image.mode == "RGB" else image.convert("RGB")
        cache_key = (candidate.size, candidate.tobytes())
        cached = self._recognition_cache.get(cache_key)
        if cached is not None:
            self._recognition_cache.move_to_end(cache_key)
            return cached
        recognition = self._recognize_image_uncached(candidate)
        self._recognition_cache[cache_key] = recognition
        if len(self._recognition_cache) > 8:
            self._recognition_cache.popitem(last=False)
        return recognition

    def _recognize_image_uncached(
        self,
        candidate: Image.Image,
    ) -> ScreenRecognition:
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
        (
            disconnected_score,
            disconnected_reference,
            disconnected_box,
        ) = self._best_disconnect_reference_match(
            candidate,
            disconnected.filename,
        )
        candidate_ratio = candidate.width / candidate.height
        disconnected_ratio = (
            disconnected_reference.width / disconnected_reference.height
        )
        has_disconnect_overlay = (
            abs(candidate_ratio - disconnected_ratio) <= 0.12
            and disconnected_score <= disconnected.maximum_score
        )
        if has_disconnect_overlay:
            if (
                self._disconnect_overlay_has_structure(
                    candidate,
                    disconnected_reference,
                )
                and self._disconnect_required_regions_match(
                    candidate,
                    disconnected_reference,
                    disconnected_box,
                    disconnected,
                )
                and self._disconnect_text_has_words(
                    candidate,
                    disconnected_box,
                )
            ):
                return ScreenRecognition(
                    state=ReconnectScreenState.DISCONNECTED,
                    score=round(disconnected_score, 3),
                    click_point=disconnected.click_point,
                    reference_name=disconnected.filename,
                    battle_context=self._is_battle_context(candidate),
                )
            # A nearly matched disconnect dialog with a missing required
            # region is not evidence of being safely connected or of another
            # actionable dialog.
            return ScreenRecognition(
                state=ReconnectScreenState.UNKNOWN,
                score=round(disconnected_score, 3),
                click_point=None,
                reference_name=None,
            )

        # Full-window comparisons are used only as a fail-closed tie-breaker.
        # Keep one candidate signature for the entire recognition pass; the
        # fixed reference signatures are cached below so this safety check does
        # not make a fourteen-window scan exceed its existing time contract.
        candidate_full_signature = self._signature(candidate)

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
            if self._full_image_score(
                candidate,
                disconnected_reference,
                candidate_full_signature,
            ) < self._full_image_score(
                candidate,
                battle_reference,
                candidate_full_signature,
            ):
                # A damaged known disconnect dialog can no longer satisfy its
                # strict evidence, but it is still not evidence that battle
                # gameplay is safely connected.
                return ScreenRecognition(
                    state=ReconnectScreenState.UNKNOWN,
                    score=round(disconnected_score, 3),
                    click_point=None,
                    reference_name=None,
                    battle_context=False,
                )
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

        scored: list[
            tuple[float, ScreenTemplateDefinition, tuple[float, ...]]
        ] = []
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
            scores = tuple(
                self._region_score(candidate, reference, region)
                for region in definition.regions
            )
            scored.append((sum(scores) / len(scores), definition, scores))

        valid_scored = [
            item
            for item in scored
            if (
                all(score <= item[1].maximum_score for score in item[2])
                and all(
                    self._region_has_nonuniform_structure(
                        candidate,
                        self._reference(item[1].filename),
                        region,
                    )
                    for region in item[1].regions
                )
                and all(
                    self._region_has_matching_structure(
                        candidate,
                        self._reference(item[1].filename),
                        region,
                        item[1].maximum_score,
                    )
                    for region in item[1].regions
                )
                and (
                    not self._definition_can_send_input(item[1])
                    or not any(
                        self._region_is_confirmed_gameplay_replacement(
                            self._crop(
                                candidate,
                                region,
                            ),
                            self._crop(
                                self._reference(item[1].filename),
                                region,
                            ),
                            region,
                        )
                        for region in item[1].regions
                    )
                )
                and (
                    item[1].state is not ReconnectScreenState.CONNECTED
                    or not self._connected_central_region_has_confirmed_modal(
                        candidate,
                        self._reference(item[1].filename),
                    )
                )
            )
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
        score, definition, _region_scores = (
            modal_match
            if modal_match is not None
            else min(valid_scored, key=lambda item: item[0])
        )
        if self._full_image_score(
            candidate,
            disconnected_reference,
            candidate_full_signature,
        ) < self._full_image_score(
            candidate,
            self._reference(definition.filename),
            candidate_full_signature,
        ):
            # Do not turn a known disconnect dialog with a masked required
            # region into a harmless-looking connected or login state.  The
            # strict branch above is the only path allowed to report a
            # disconnect; every incomplete variant fails closed here.
            return ScreenRecognition(
                state=ReconnectScreenState.UNKNOWN,
                score=round(disconnected_score, 3),
                click_point=None,
                reference_name=None,
            )
        if self._has_incomplete_actionable_template(
            candidate,
            scored,
            definition,
            candidate_full_signature,
        ):
            return ScreenRecognition(
                state=ReconnectScreenState.UNKNOWN,
                score=round(score, 3),
                click_point=None,
                reference_name=None,
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
            character_candidates = self._character_selection_candidates(
                candidate,
                read_identity=True,
            )
            (
                click_point,
                character_level,
                character_importance,
                character_slot_index,
                character_slot_selected,
            ) = self._character_selection_target(
                candidate,
                candidates=character_candidates,
            )
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
