"""Generate the minimal reconnect level references from user-provided evidence.

The output contains only the three level labels required by the confirmed
100/120/160 templates. It intentionally excludes names and other game pixels.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parent.parent
REFERENCE_DIR = ROOT / "assets" / "reconnect_reference"
EVIDENCE_DIR = ROOT / "docs" / "evidence" / "activity_templates_pending"

SOURCES = {
    100: (
        REFERENCE_DIR / "05_character_selection.png",
        (498, 658, 568, 700),
    ),
    120: (
        EVIDENCE_DIR / "02_120等主號_一.png",
        (90, 0, 145, 55),
    ),
    160: (
        EVIDENCE_DIR / "04_160等主號_一.png",
        (55, 5, 90, 38),
    ),
}


def main() -> int:
    for level, (source, box) in SOURCES.items():
        with Image.open(source) as image:
            crop = image.convert("RGB").crop(box)
        crop.save(
            REFERENCE_DIR / f"character_level_{level}.png",
            optimize=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
