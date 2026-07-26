import hashlib
from pathlib import Path

from PIL import Image

from adapters.game_screen_recognizer import (
    CHARACTER_ENTER_CLICK_POINT,
    CHARACTER_LEVEL_REGIONS,
    CHARACTER_SLOT_CLICK_POINTS,
    DEFAULT_SCREEN_TEMPLATES,
    ROUTE_DIGIT_REFERENCE_REGION,
    LINE_ROUTE_CLICK_POINTS,
    ReferenceScreenRecognizer,
)
from adapters.windows_background_capture import CaptureSample
from core.reconnect_policy import ReconnectScreenState
from domain.character import CharacterImportance


REFERENCE_DIR = Path("assets") / "reconnect_reference"


def test_all_confirmed_full_window_references_are_present_and_unique():
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)

    assert recognizer.ready is True
    assert recognizer.missing_references == ()
    assert len(DEFAULT_SCREEN_TEMPLATES) == 9
    assert len({item.filename for item in DEFAULT_SCREEN_TEMPLATES}) == 9
    assert len({item.state for item in DEFAULT_SCREEN_TEMPLATES}) == 9


def test_all_user_reference_images_match_the_fixed_sha256_manifest():
    manifest = {}
    for line in (REFERENCE_DIR / "SHA256SUMS.txt").read_text(
        encoding="utf-8"
    ).splitlines():
        digest, filename = line.split(maxsplit=1)
        manifest[filename.strip()] = digest

    images = sorted(REFERENCE_DIR.glob("*.png"))

    assert len(images) == 16
    assert set(manifest) == {image.name for image in images}
    for image in images:
        assert hashlib.sha256(image.read_bytes()).hexdigest() == manifest[image.name]


def test_each_confirmed_reference_classifies_to_its_declared_state():
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)

    for definition in DEFAULT_SCREEN_TEMPLATES:
        with Image.open(REFERENCE_DIR / definition.filename) as image:
            result = recognizer.recognize_image(image)
        assert result.state is definition.state
        assert result.score == 0.0
        assert result.reference_name == definition.filename
        if definition.state is ReconnectScreenState.LINE_SELECTION:
            assert result.line_number == 8
            assert result.click_point == LINE_ROUTE_CLICK_POINTS[8]
        elif definition.state is ReconnectScreenState.CHARACTER_SELECTION:
            assert result.character_level == 100
            assert result.character_importance is CharacterImportance.SECONDARY
            assert result.character_slot_index == 0
            assert result.character_slot_selected is True
            assert result.click_point == CHARACTER_ENTER_CLICK_POINT
        else:
            assert result.click_point == definition.click_point


def _paste_level_reference(
    recognizer,
    candidate,
    *,
    slot_index,
    level,
):
    region = CHARACTER_LEVEL_REGIONS[slot_index]
    box = (
        round(candidate.width * region[0]),
        round(candidate.height * region[1]),
        round(candidate.width * region[2]),
        round(candidate.height * region[3]),
    )
    reference = recognizer._reference(f"character_level_{level}.png")
    candidate.paste(
        reference.resize(
            (box[2] - box[0], box[3] - box[1]),
            Image.Resampling.BILINEAR,
        ),
        box,
    )


def test_character_login_prefers_confirmed_primary_role_over_leftmost_secondary():
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)
    with Image.open(REFERENCE_DIR / "05_character_selection.png") as source:
        candidate = source.convert("RGB")
    _paste_level_reference(
        recognizer,
        candidate,
        slot_index=1,
        level=120,
    )

    point, level, importance, slot_index, selected = (
        recognizer._character_selection_target(candidate)
    )

    assert level == 120
    assert importance is CharacterImportance.PRIMARY
    assert slot_index == 1
    assert selected is False
    assert point == CHARACTER_SLOT_CLICK_POINTS[1]


def test_character_login_prefers_higher_level_inside_primary_role():
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)
    with Image.open(REFERENCE_DIR / "05_character_selection.png") as source:
        candidate = source.convert("RGB")
    _paste_level_reference(
        recognizer,
        candidate,
        slot_index=1,
        level=120,
    )
    _paste_level_reference(
        recognizer,
        candidate,
        slot_index=2,
        level=160,
    )

    point, level, importance, slot_index, selected = (
        recognizer._character_selection_target(candidate)
    )

    assert level == 160
    assert importance is CharacterImportance.PRIMARY
    assert slot_index == 2
    assert selected is False
    assert point == CHARACTER_SLOT_CLICK_POINTS[2]


def test_recognition_survives_proportional_window_scaling():
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)
    definition = next(
        item
        for item in DEFAULT_SCREEN_TEMPLATES
        if item.state is ReconnectScreenState.DISCONNECTED
    )
    with Image.open(REFERENCE_DIR / definition.filename) as image:
        resized = image.resize(
            (round(image.width * 0.72), round(image.height * 0.72)),
            Image.Resampling.BILINEAR,
        )

    result = recognizer.recognize_image(resized)

    assert result.state is ReconnectScreenState.DISCONNECTED
    assert result.click_point == definition.click_point


def test_battle_disconnect_is_distinguished_from_normal_disconnect():
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)
    with Image.open(
        REFERENCE_DIR / "01_disconnected_dialog.png"
    ) as source:
        disconnected = source.convert("RGB")
    with Image.open(REFERENCE_DIR / "13_battle_gameplay.png") as source:
        battle = source.convert("RGB")

    candidate = Image.new("RGB", disconnected.size, "black")
    client_top = round(candidate.height * 0.05)
    client_bottom = round(candidate.height * 0.985)
    candidate.paste(
        battle.resize(
            (candidate.width, client_bottom - client_top),
            Image.Resampling.BILINEAR,
        ),
        (0, client_top),
    )
    overlay_region = (0.323, 0.477, 0.677, 0.607)
    overlay_box = (
        round(disconnected.width * overlay_region[0]),
        round(disconnected.height * overlay_region[1]),
        round(disconnected.width * overlay_region[2]),
        round(disconnected.height * overlay_region[3]),
    )
    candidate.paste(disconnected.crop(overlay_box), overlay_box[:2])

    normal_result = recognizer.recognize_image(disconnected)
    battle_result = recognizer.recognize_image(candidate)

    assert normal_result.state is ReconnectScreenState.DISCONNECTED
    assert normal_result.battle_context is False
    assert battle_result.state is ReconnectScreenState.DISCONNECTED
    assert battle_result.battle_context is True


def test_blank_or_wrong_aspect_image_is_unknown_and_has_no_click_target():
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)

    blank = recognizer.recognize_image(Image.new("RGB", (1351, 936), "black"))
    wrong_aspect = recognizer.recognize_image(Image.new("RGB", (300, 900), "teal"))

    assert blank.state is ReconnectScreenState.UNKNOWN
    assert blank.click_point is None
    assert wrong_aspect.state is ReconnectScreenState.UNKNOWN
    assert wrong_aspect.click_point is None


def test_capture_sample_is_decoded_as_top_down_bgra_without_persistence():
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)
    definition = next(
        item
        for item in DEFAULT_SCREEN_TEMPLATES
        if item.state is ReconnectScreenState.LOGIN_START
    )
    with Image.open(REFERENCE_DIR / definition.filename) as source:
        rgba = source.convert("RGBA")
        red, green, blue, alpha = rgba.split()
        bgra = Image.merge("RGBA", (blue, green, red, alpha)).tobytes()
        sample = CaptureSample(
            width=rgba.width,
            height=rgba.height,
            pixels=bgra,
            api_succeeded=True,
        )

    result = recognizer.recognize_capture(sample)

    assert result.state is ReconnectScreenState.LOGIN_START


def test_invalid_capture_is_unknown():
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)

    result = recognizer.recognize_capture(
        CaptureSample(width=10, height=10, pixels=b"", api_succeeded=True)
    )

    assert result.state is ReconnectScreenState.UNKNOWN
    assert result.click_point is None


def test_every_actionable_template_has_a_safe_client_relative_point():
    actionable = [
        item
        for item in DEFAULT_SCREEN_TEMPLATES
        if item.state
        not in {
            ReconnectScreenState.CONNECTED,
            ReconnectScreenState.LINE_SELECTION,
            ReconnectScreenState.RECONNECTING,
        }
    ]

    assert actionable
    for definition in actionable:
        assert definition.click_point is not None
        x, y = definition.click_point
        assert 0.0 <= x <= 1.0
        assert 0.0 <= y <= 1.0


def test_line_selection_uses_recent_route_seven_instead_of_top_line():
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)
    with Image.open(REFERENCE_DIR / "03_line_selection_dialog.png") as source:
        candidate = source.convert("RGB")
        route_box = (
            round(candidate.width * ROUTE_DIGIT_REFERENCE_REGION[0]),
            round(candidate.height * ROUTE_DIGIT_REFERENCE_REGION[1]),
            round(candidate.width * ROUTE_DIGIT_REFERENCE_REGION[2]),
            round(candidate.height * ROUTE_DIGIT_REFERENCE_REGION[3]),
        )
        with Image.open(REFERENCE_DIR / "10_route_digit_7.png") as digit:
            replacement = Image.new(
                "RGB",
                (route_box[2] - route_box[0], route_box[3] - route_box[1]),
                (29, 88, 111),
            )
            replacement.paste(
                digit.convert("RGB").resize(
                    replacement.size,
                    Image.Resampling.NEAREST,
                )
            )
        candidate.paste(replacement, route_box)

    result = recognizer.recognize_image(candidate)

    assert result.state is ReconnectScreenState.LINE_SELECTION
    assert result.line_number == 7
    assert result.click_point == LINE_ROUTE_CLICK_POINTS[7]


def test_route_number_follows_centered_prefix_when_character_name_shifts():
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)
    with Image.open(REFERENCE_DIR / "03_line_selection_dialog.png") as source:
        candidate = source.convert("RGB")
        line_box = (
            round(candidate.width * 0.400),
            round(candidate.height * 0.306),
            round(candidate.width * 0.474),
            round(candidate.height * 0.350),
        )
        shifted_line = candidate.crop(line_box)
        candidate.paste((29, 88, 111), line_box)
        candidate.paste(
            shifted_line,
            (line_box[0] + 34, line_box[1]),
        )

    line_number, score = recognizer._recognize_route_number(candidate)

    assert line_number == 8
    assert score is not None
    assert score <= 65.0


def test_popup_title_guard_rejects_a_similar_generic_window_frame():
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)
    with Image.open(REFERENCE_DIR / "07_post_login_activity_popup.png") as source:
        candidate = source.convert("RGB")
        title_box = (
            round(candidate.width * 0.400),
            round(candidate.height * 0.130),
            round(candidate.width * 0.600),
            round(candidate.height * 0.190),
        )
        candidate.paste((29, 88, 111), title_box)

    result = recognizer.recognize_image(candidate)

    assert result.state not in {
        ReconnectScreenState.POST_LOGIN_ACTIVITY,
        ReconnectScreenState.POST_LOGIN_RECOMMENDATION,
    }
    assert result.click_point is None


def test_line_selection_modal_takes_priority_over_login_background(monkeypatch):
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)
    login_definition = next(
        item
        for item in DEFAULT_SCREEN_TEMPLATES
        if item.state is ReconnectScreenState.LOGIN_START
    )
    line_definition = next(
        item
        for item in DEFAULT_SCREEN_TEMPLATES
        if item.state is ReconnectScreenState.LINE_SELECTION
    )
    login_reference = recognizer._reference(login_definition.filename)
    line_reference = recognizer._reference(line_definition.filename)
    monkeypatch.setattr(
        recognizer,
        "_disconnect_overlay_score",
        lambda _candidate, _reference: 255.0,
    )
    monkeypatch.setattr(
        recognizer,
        "_region_score",
        lambda _candidate, reference, _region: (
            8.0
            if reference is login_reference
            else 19.0
            if reference is line_reference
            else 100.0
        ),
    )

    result = recognizer.recognize_image(line_reference)

    assert result.state is ReconnectScreenState.LINE_SELECTION
    assert result.line_number == 8
    assert result.click_point == LINE_ROUTE_CLICK_POINTS[8]


def test_invalid_lower_score_does_not_mask_valid_connected_match(monkeypatch):
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)
    auto_dungeon_definition = next(
        item
        for item in DEFAULT_SCREEN_TEMPLATES
        if item.state is ReconnectScreenState.POST_LOGIN_AUTO_DUNGEON
    )
    connected_definition = next(
        item
        for item in DEFAULT_SCREEN_TEMPLATES
        if item.state is ReconnectScreenState.CONNECTED
    )
    auto_dungeon_reference = recognizer._reference(
        auto_dungeon_definition.filename
    )
    connected_reference = recognizer._reference(
        connected_definition.filename
    )
    monkeypatch.setattr(
        recognizer,
        "_disconnect_overlay_score",
        lambda _candidate, _reference: 255.0,
    )
    monkeypatch.setattr(
        recognizer,
        "_popup_title_score",
        lambda _candidate, _reference, _region: 0.0,
    )
    monkeypatch.setattr(
        recognizer,
        "_region_score",
        lambda _candidate, reference, _region: (
            28.0
            if reference is auto_dungeon_reference
            else 30.0
            if reference is connected_reference
            else 90.0
        ),
    )

    result = recognizer.recognize_image(connected_reference)

    assert auto_dungeon_definition.maximum_score == 27.0
    assert connected_definition.maximum_score == 38.0
    assert result.state is ReconnectScreenState.CONNECTED
    assert result.score == 30.0
