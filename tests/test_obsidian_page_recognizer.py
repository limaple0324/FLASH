from dataclasses import replace
from datetime import datetime, timedelta, timezone
from inspect import signature
from pathlib import Path

from PIL import Image, ImageDraw

from adapters.obsidian_page_recognizer import (
    DEFAULT_OBSIDIAN_PAGE_DEFINITIONS,
    ObsidianPageRecognizer,
)
from adapters.windows_background_capture import CaptureSample
from services.character_game_data_capture_service import GameDataPageKind


REFERENCE_DIR = (
    Path(__file__).resolve().parents[1]
    / "assets"
    / "game_data_reference"
    / "obsidian"
)
FIXED_TIME = datetime(2026, 8, 3, 12, 0, tzinfo=timezone(timedelta(hours=8)))
EXPECTED_COUNTS = {
    1: (2, 0, "階段一／完成"),
    2: (6, 0, "階段一／完成"),
    3: (9, 0, "階段一／完成"),
    4: (15, 0, "階段一／完成"),
    5: (20, 0, "階段一／完成"),
    6: (16, 0, "階段一／完成"),
    7: (14, 0, "階段一／完成"),
    8: (16, 0, "階段一／完成"),
    9: (12, 0, "階段一／完成"),
    10: (12, 6, "激活"),
}


def _reference_path(page: int) -> Path:
    return REFERENCE_DIR / f"page_{page:02d}.png"


def _sample_from_image(image: Image.Image) -> CaptureSample:
    rgba = image.convert("RGBA")
    return CaptureSample(
        rgba.width,
        rgba.height,
        rgba.tobytes("raw", "BGRA"),
        True,
    )


def _sample(page: int) -> CaptureSample:
    with Image.open(_reference_path(page)) as image:
        return _sample_from_image(image)


def _recognizer(**kwargs) -> ObsidianPageRecognizer:
    return ObsidianPageRecognizer(
        reference_dir=REFERENCE_DIR,
        clock=lambda: FIXED_TIME,
        **kwargs,
    )


def test_reference_assets_hashes_and_source_description_are_consistent() -> None:
    expected_hashes = {
        definition.filename: definition.source_sha256
        for definition in DEFAULT_OBSIDIAN_PAGE_DEFINITIONS
    }
    manifest = {}
    for line in (REFERENCE_DIR / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        digest, filename = line.split("  ", maxsplit=1)
        manifest[filename] = digest

    assert manifest == expected_hashes
    for filename, expected_hash in expected_hashes.items():
        import hashlib

        assert hashlib.sha256((REFERENCE_DIR / filename).read_bytes()).hexdigest() == expected_hash

    source = (REFERENCE_DIR / "SOURCE.md").read_text(encoding="utf-8")
    assert "依使用者已確認的順序" in source
    assert "灰色格為未亮" in source
    assert "| 1 | 2 | 0 | 階段一／完成 |" in source
    assert "| 10 | 12 | 6 | 激活 |" in source


def test_each_reference_is_recognized_by_central_shape_not_candidate_filename() -> None:
    recognizer = _recognizer()

    assert recognizer.ready
    assert recognizer.missing_references == ()
    assert tuple(signature(ObsidianPageRecognizer.read).parameters) == ("self", "sample")
    for page_number, (opened, unlit, status) in EXPECTED_COUNTS.items():
        page = recognizer.read(_sample(page_number))

        assert page is not None
        assert not hasattr(page, "character_id")
        assert page.page_kind is GameDataPageKind.OBSIDIAN
        assert page.logical_page_id == f"obsidian-page-{page_number}"
        assert page.data.opened_page == page_number
        assert page.data.opened_nodes == opened
        assert page.data.unlit_nodes == unlit
        assert page.data.stage == status
        assert page.data.updated_at == FIXED_TIME.isoformat(timespec="seconds")
        assert page.data.page_shape_signature


def test_tenth_reference_has_six_gray_unlit_nodes() -> None:
    page = _recognizer().read(_sample(10))

    assert page is not None
    assert page.data.opened_nodes == 12
    assert page.data.unlit_nodes == 6


def test_covering_a_required_node_fails_closed() -> None:
    definition = DEFAULT_OBSIDIAN_PAGE_DEFINITIONS[0]
    node = definition.topology[0]
    with Image.open(_reference_path(1)) as source:
        original = source.convert("RGB")
    center_x = round(node.x * original.width)
    center_y = round(node.y * original.height)

    for fill in ((0, 0, 0), (255, 0, 0)):
        covered = original.copy()
        drawer = ImageDraw.Draw(covered)
        drawer.rectangle(
            (center_x - 18, center_y - 18, center_x + 18, center_y + 18),
            fill=fill,
        )

        assert _recognizer().read(_sample_from_image(covered)) is None


def test_reference_definition_order_does_not_change_recognition() -> None:
    recognizer = _recognizer(
        definitions=tuple(reversed(DEFAULT_OBSIDIAN_PAGE_DEFINITIONS))
    )

    page = recognizer.read(_sample(7))

    assert recognizer.ready
    assert page is not None
    assert page.data.opened_page == 7
    assert page.data.opened_nodes == 14


def test_status_must_match_the_recognized_central_shape() -> None:
    with Image.open(_reference_path(10)) as source:
        mixed = source.convert("RGB")
    with Image.open(_reference_path(1)) as source:
        completed = source.convert("RGB").resize(mixed.size)
    start = round(mixed.height * 0.70)
    mixed.paste(completed.crop((0, start, mixed.width, mixed.height)), (0, start))

    assert _recognizer().read(_sample_from_image(mixed)) is None


def test_completed_page_requires_visible_stage_evidence() -> None:
    with Image.open(_reference_path(1)) as source:
        masked = source.convert("RGB")
    ImageDraw.Draw(masked).rectangle((70, 625, 170, 668), fill=(0, 0, 0))

    assert _recognizer().read(_sample_from_image(masked)) is None


def test_completed_page_requires_visible_completion_evidence() -> None:
    with Image.open(_reference_path(1)) as source:
        masked = source.convert("RGB")
    ImageDraw.Draw(masked).rectangle((70, 670, 170, 717), fill=(0, 0, 0))

    assert _recognizer().read(_sample_from_image(masked)) is None


def test_blank_low_confidence_and_missing_reference_fail_closed(tmp_path) -> None:
    with Image.open(_reference_path(1)) as source:
        blank = Image.new("RGB", source.size, "black")

    recognizer = _recognizer()
    missing = ObsidianPageRecognizer(
        reference_dir=tmp_path,
        clock=lambda: FIXED_TIME,
    )

    assert recognizer.read(_sample_from_image(blank)) is None
    assert not missing.ready
    assert missing.missing_references
    assert missing.read(_sample(1)) is None


def test_equal_shape_scores_fail_closed() -> None:
    duplicate_first = replace(
        DEFAULT_OBSIDIAN_PAGE_DEFINITIONS[0],
        page_number=2,
    )
    definitions = (
        DEFAULT_OBSIDIAN_PAGE_DEFINITIONS[0],
        duplicate_first,
        *DEFAULT_OBSIDIAN_PAGE_DEFINITIONS[2:],
    )
    recognizer = _recognizer(definitions=definitions)

    assert recognizer.ready
    assert recognizer.read(_sample(1)) is None


def test_right_resources_and_star_background_do_not_change_content_signature() -> None:
    recognizer = _recognizer()
    original = recognizer.read(_sample(1))
    with Image.open(_reference_path(1)) as source:
        changed = source.convert("RGB")
    drawer = ImageDraw.Draw(changed)
    drawer.rectangle(
        (
            round(changed.width * 0.76),
            round(changed.height * 0.20),
            round(changed.width * 0.93),
            round(changed.height * 0.25),
        ),
        fill=(255, 255, 255),
    )
    drawer.rectangle(
        (
            round(changed.width * 0.08),
            round(changed.height * 0.52),
            round(changed.width * 0.11),
            round(changed.height * 0.55),
        ),
        fill=(255, 255, 255),
    )
    reread = recognizer.read(_sample_from_image(changed))

    assert original is not None
    assert reread is not None
    assert reread.data.opened_page == original.data.opened_page
    assert reread.data.opened_nodes == original.data.opened_nodes
    assert reread.data.unlit_nodes == original.data.unlit_nodes
    assert reread.content_signature == original.content_signature


def test_recognizer_has_no_game_input_or_foreground_operations() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "adapters"
        / "obsidian_page_recognizer.py"
    ).read_text(encoding="utf-8")

    for forbidden in (
        "activate(",
        "click(",
        "send_keys(",
        "SetForegroundWindow",
        "keybd_event",
    ):
        assert forbidden not in source
