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


NormalizedRect = tuple[float, float, float, float]
NormalizedPoint = tuple[float, float]
FORCE_LOGIN_CLICK_POINT: NormalizedPoint = (0.505, 0.856)
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
        maximum_score=31.0,
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
class ScreenRecognition:
    state: ReconnectScreenState
    score: float | None
    click_point: NormalizedPoint | None
    reference_name: str | None
    line_number: int | None = None

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
        return screen_references + digit_references

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

    def _route_digit_crop(self, candidate: Image.Image) -> Image.Image:
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
            return self._crop(candidate, ROUTE_DIGIT_REGION)
        prefix_score, left, top = min(matches)
        if prefix_score > 30.0:
            return self._crop(candidate, ROUTE_DIGIT_REGION)

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
        candidate_signature = self._digit_signature(
            self._route_digit_crop(candidate)
        )
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

    @staticmethod
    def _flat_pixels(image: Image.Image) -> list[tuple[int, int, int]]:
        getter = getattr(image, "get_flattened_data", None)
        if callable(getter):
            return list(getter())
        return list(image.getdata())

    @classmethod
    def _disconnect_overlay_score(
        cls,
        candidate: Image.Image,
        reference: Image.Image,
    ) -> float:
        """Compare only the stable cyan/text pixels of the centered dialog."""
        region = (0.323, 0.477, 0.677, 0.607)
        signature_size = (162, 41)
        reference_crop = cls._crop(reference, region).resize(
            signature_size,
            Image.Resampling.BILINEAR,
        )
        reference_pixels = cls._flat_pixels(reference_crop)
        mask = [
            index
            for index, (red, green, blue) in enumerate(reference_pixels)
            if (
                (green > 115 and blue > 115 and blue > red * 1.03)
                or (red > 175 and green > 175 and blue > 175)
                or (red > 170 and green > 145 and blue < 125)
            )
        ]
        if not mask:
            return 255.0

        left, top, right, bottom = region
        width, height = candidate.size
        base_box = (
            round(width * left),
            round(height * top),
            round(width * right),
            round(height * bottom),
        )
        offset_x = max(2, round(width * 0.007))
        offset_y = max(2, round(height * 0.010))
        best = 255.0
        y_offsets = sorted(
            set(range(-offset_y, offset_y + 1, 2)) | {0}
        )
        x_offsets = sorted(
            set(range(-offset_x, offset_x + 1, 2)) | {0}
        )
        for delta_y in y_offsets:
            for delta_x in x_offsets:
                box = (
                    base_box[0] + delta_x,
                    base_box[1] + delta_y,
                    base_box[2] + delta_x,
                    base_box[3] + delta_y,
                )
                candidate_crop = candidate.crop(box).resize(
                    signature_size,
                    Image.Resampling.BILINEAR,
                )
                candidate_pixels = cls._flat_pixels(candidate_crop)
                total = 0.0
                for index in mask:
                    reference_pixel = reference_pixels[index]
                    candidate_pixel = candidate_pixels[index]
                    total += (
                        abs(reference_pixel[0] - candidate_pixel[0])
                        + abs(reference_pixel[1] - candidate_pixel[1])
                        + abs(reference_pixel[2] - candidate_pixel[2])
                    ) / 3.0
                best = min(best, total / len(mask))
        return best

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
        if disconnected_score <= 42.0:
            return ScreenRecognition(
                state=ReconnectScreenState.DISCONNECTED,
                score=round(disconnected_score, 3),
                click_point=disconnected.click_point,
                reference_name=disconnected.filename,
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
        line_selection_match = next(
            (
                item
                for item in valid_scored
                if item[1].state is ReconnectScreenState.LINE_SELECTION
            ),
            None,
        )
        score, definition = (
            line_selection_match
            if line_selection_match is not None
            else min(valid_scored, key=lambda item: item[0])
        )
        line_number = None
        click_point = definition.click_point
        if definition.state is ReconnectScreenState.LINE_SELECTION:
            line_number, _route_score = self._recognize_route_number(candidate)
            click_point = (
                LINE_ROUTE_CLICK_POINTS.get(line_number)
                if line_number is not None
                else None
            )
        return ScreenRecognition(
            state=definition.state,
            score=round(score, 3),
            click_point=click_point,
            reference_name=definition.filename,
            line_number=line_number,
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
