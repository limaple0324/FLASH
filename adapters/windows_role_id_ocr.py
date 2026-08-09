"""Read visible game-character names from passive client captures."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from PIL import Image

from adapters.windows_background_capture import CaptureSample


# The game's HUD paints the character name in a tiny bright bitmap font.  The
# local model is reliable only after that glyph is isolated from the blue HUD.
ROLE_ID_THRESHOLD = 160
ROLE_ID_MAX_CHANNEL_DELTA = 80
ROLE_ID_SCALE = 10
ROLE_ID_PADDING = 5
ROLE_ID_MODEL = Path("assets") / "role_id_ocr" / "ch_PP-OCRv5_rec_mobile.onnx"


def role_id_ocr_image(sample: CaptureSample) -> Image.Image | None:
    """Extract the bright HUD glyphs without using a shortcut-file name."""
    required = sample.width * sample.height * 4
    if (
        sample.width <= 0
        or sample.height <= 0
        or len(sample.pixels) < required
    ):
        return None

    source = Image.frombytes(
        "RGBA",
        (sample.width, sample.height),
        sample.pixels,
        "raw",
        "BGRA",
    ).convert("RGB")
    # The name is neutral bright text.  The envelope beside it is also bright
    # but coloured; excluding non-neutral pixels lets the sample cover long
    # names without treating that icon as an extra character.
    source_pixels = source.load()
    glyphs = Image.new("L", source.size)
    glyphs.putdata(
        [
            255
            if (
                min(red, green, blue) >= ROLE_ID_THRESHOLD
                and max(red, green, blue) - min(red, green, blue)
                <= ROLE_ID_MAX_CHANNEL_DELTA
            )
            else 0
            for red, green, blue in (
                source_pixels[x, y]
                for y in range(source.height)
                for x in range(source.width)
            )
        ]
    )
    bounds = glyphs.getbbox()
    if bounds is None:
        return None
    left, top, right, bottom = bounds
    glyphs = glyphs.crop(
        (
            max(0, left - 1),
            max(0, top - 1),
            # PP-OCR can discard the final bitmap glyph when it touches the
            # recognition crop edge.  Keep the clear HUD space before the
            # envelope icon as a right-side margin.
            min(sample.width, right + 7),
            min(sample.height, bottom + 1),
        )
    ).convert("RGB")
    prepared = Image.new(
        "RGB",
        (
            glyphs.width + ROLE_ID_PADDING * 2,
            glyphs.height + ROLE_ID_PADDING * 2,
        ),
        "black",
    )
    prepared.paste(glyphs, (ROLE_ID_PADDING, ROLE_ID_PADDING))
    return prepared.resize(
        (prepared.width * ROLE_ID_SCALE, prepared.height * ROLE_ID_SCALE),
        Image.Resampling.NEAREST,
    )


class WindowsRoleIdOcrReader:
    """Use the packaged local OCR reader without focusing a game window."""

    def __init__(self, *, engine: Any | None = None) -> None:
        self._engine = engine

    @staticmethod
    def _model_path() -> Path:
        bundle_root = getattr(sys, "_MEIPASS", None)
        root = (
            Path(bundle_root)
            if bundle_root
            else Path(__file__).resolve().parents[1]
        )
        return root / ROLE_ID_MODEL

    def _reader(self) -> Any | None:
        if self._engine is not None:
            return self._engine
        try:
            from rapidocr_onnxruntime.ch_ppocr_rec.text_recognize import (
                TextRecognizer,
            )

            model_path = self._model_path()
            if not model_path.is_file():
                return None
            self._engine = TextRecognizer(
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
        return self._engine

    @staticmethod
    def _text_from_result(result: Any) -> str:
        if not isinstance(result, (list, tuple)):
            return ""
        parts: list[str] = []
        for item in result:
            if not isinstance(item, (list, tuple)):
                continue
            if item and isinstance(item[0], str):
                parts.append(item[0])
            elif len(item) >= 2 and isinstance(item[1], str):
                parts.append(item[1])
        return "".join(parts).strip()

    def read(self, sample: CaptureSample) -> str:
        image = role_id_ocr_image(sample)
        engine = self._reader()
        if image is None or engine is None:
            return ""
        try:
            from numpy import asarray

            result, _elapsed = engine(asarray(image))
        except (OSError, RuntimeError, TypeError, ValueError):
            return ""
        return self._text_from_result(result)
