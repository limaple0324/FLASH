import hashlib
from pathlib import Path
from time import perf_counter

from PIL import Image, ImageEnhance

from adapters.game_screen_recognizer import (
    BATTLE_CONTEXT_REGION,
    BATTLE_REFERENCE_FILE,
    BATTLE_SCREEN_EVIDENCE_REGION,
    CHARACTER_ENTER_CLICK_POINT,
    CHARACTER_LEVEL_REGIONS,
    DEFAULT_LINE_NUMBER,
    DEFAULT_SCREEN_TEMPLATES,
    DISCONNECT_OVERLAY_REGION,
    FORCE_LOGIN_TIMEOUT_CLICK_POINT,
    ROUTE_DIGIT_REFERENCE_REGION,
    ROUTE_PREFIX_SEARCH_REGION,
    LINE_ROUTE_CLICK_POINTS,
    ReferenceScreenRecognizer,
)
from adapters.windows_background_capture import CaptureSample
from core.reconnect_policy import ReconnectScreenState


REFERENCE_DIR = Path("assets") / "reconnect_reference"
FOURTEEN_WINDOW_RECOGNITION_LIMIT_SECONDS = 4.5


def test_all_confirmed_full_window_references_are_present_and_unique():
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)

    assert recognizer.ready is True
    assert recognizer.missing_references == ()
    assert len(DEFAULT_SCREEN_TEMPLATES) == 10
    assert len({item.filename for item in DEFAULT_SCREEN_TEMPLATES}) == 10
    assert len({item.state for item in DEFAULT_SCREEN_TEMPLATES}) == 10


def test_all_user_reference_images_match_the_fixed_sha256_manifest():
    manifest = {}
    for line in (REFERENCE_DIR / "SHA256SUMS.txt").read_text(
        encoding="utf-8"
    ).splitlines():
        digest, filename = line.split(maxsplit=1)
        manifest[filename.strip()] = digest

    images = sorted(REFERENCE_DIR.glob("*.png"))

    assert len(images) == 19
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
            assert result.character_importance is None
            assert result.character_slot_index == 0
            assert result.character_slot_selected is True
            assert result.click_point == CHARACTER_ENTER_CLICK_POINT
            assert len(result.character_candidates) == 1
            assert result.character_candidates[0].level == 100
        else:
            assert result.click_point == definition.click_point


def test_fourteen_connected_window_references_finish_within_time_limit():
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)
    with Image.open(REFERENCE_DIR / "06_connected_gameplay.png") as source:
        candidate = source.convert("RGB")

    recognizer.recognize_image(candidate)
    started = perf_counter()
    results = tuple(
        recognizer.recognize_image(candidate)
        for _index in range(14)
    )
    elapsed = perf_counter() - started

    assert all(
        result.state is ReconnectScreenState.CONNECTED
        for result in results
    )
    assert elapsed < FOURTEEN_WINDOW_RECOGNITION_LIMIT_SECONDS


def test_disconnect_overlay_accepts_confirmed_darker_game_rendering():
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)
    with Image.open(REFERENCE_DIR / "01_disconnected_dialog.png") as source:
        candidate = ImageEnhance.Brightness(
            source.convert("RGB")
        ).enhance(0.75)

    result = recognizer.recognize_image(candidate)

    assert result.state is ReconnectScreenState.DISCONNECTED
    assert result.score is not None
    assert 31.0 < result.score <= 38.0


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


def test_character_selection_target_only_uses_the_confirmed_selected_border():
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

    assert level == 100
    assert importance is None
    assert slot_index == 0
    assert selected is True
    assert point == CHARACTER_ENTER_CLICK_POINT


def test_character_selection_target_does_not_guess_when_no_border_is_unique(
    monkeypatch,
):
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)
    with Image.open(REFERENCE_DIR / "05_character_selection.png") as source:
        candidate = source.convert("RGB")
    monkeypatch.setattr(
        recognizer,
        "_selected_character_slot_index",
        lambda _image: None,
    )

    point, level, importance, slot_index, selected = (
        recognizer._character_selection_target(candidate)
    )

    assert (point, level, importance, slot_index, selected) == (
        None,
        None,
        None,
        None,
        None,
    )


def test_user_reported_character_selection_keeps_real_levels_and_border():
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)
    with Image.open(
        REFERENCE_DIR / "16_character_selection_report.png"
    ) as source:
        split = source.width // 2
        left = source.crop((0, 0, split, source.height))
        right = source.crop((split, 0, source.width, source.height))

    left_result = recognizer.recognize_image(left)
    right_result = recognizer.recognize_image(right)

    assert left_result.state is ReconnectScreenState.CHARACTER_SELECTION
    assert [
        (item.level, item.digit_count, item.slot_index, item.selected)
        for item in left_result.character_candidates
    ] == [
        (None, 2, 0, True),
        (100, 3, 1, False),
        (None, 2, 2, False),
    ]
    assert right_result.state is ReconnectScreenState.CHARACTER_SELECTION
    assert [
        (item.level, item.digit_count, item.slot_index, item.selected)
        for item in right_result.character_candidates
    ] == [
        (120, 3, 0, False),
        (None, 2, 1, False),
        (120, 3, 2, True),
    ]
    assert right_result.character_slot_index == 2
    assert right_result.character_slot_selected is True
    assert right_result.click_point == CHARACTER_ENTER_CLICK_POINT


def test_user_reported_timeout_and_disconnect_are_actionable():
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)
    with Image.open(
        REFERENCE_DIR / "14_force_login_timeout.png"
    ) as source:
        timeout = recognizer.recognize_image(source)
    with Image.open(
        REFERENCE_DIR / "15_disconnected_card_popup.png"
    ) as source:
        disconnected = recognizer.recognize_image(source)

    assert timeout.state is ReconnectScreenState.FORCE_LOGIN_TIMEOUT
    assert timeout.click_point == FORCE_LOGIN_TIMEOUT_CLICK_POINT
    assert disconnected.state is ReconnectScreenState.DISCONNECTED
    assert disconnected.click_point == (0.5, 0.536)


def test_force_login_timeout_click_point_lands_inside_confirm_button():
    reference_height = 928
    client_top = 45
    client_height = reference_height - client_top
    click_y = client_top + round(
        (client_height - 1) * FORCE_LOGIN_TIMEOUT_CLICK_POINT[1]
    )

    assert 514 <= click_y <= 542


def test_character_level_ignores_unrelated_role_text_below_the_digits():
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)
    for level in (100, 120, 160):
        reference = recognizer._reference(
            f"character_level_{level}.png"
        )
        candidate = Image.new(
            "RGB",
            (reference.width, reference.height + 24),
            (29, 88, 111),
        )
        candidate.paste(reference, (0, 0))
        candidate.paste(
            (240, 240, 240),
            (4, reference.height + 8, reference.width - 4, reference.height + 14),
        )
        recognized, score = recognizer._recognize_character_level(
            candidate,
            (0.0, 0.0, 1.0, 1.0),
        )

        assert recognized == level
        assert score == 0.0


def test_character_level_uses_middle_digit_when_outer_glyph_style_differs(
    monkeypatch,
):
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)
    signatures = {
        level: recognizer._level_signature(
            recognizer._reference(f"character_level_{level}.png")
        )
        for level in (100, 120, 160)
    }
    assert all(signature is not None for signature in signatures.values())
    middle_zero = recognizer._level_glyph_signatures(signatures[100])[1]
    candidate = Image.new("L", (64, 32), 0)
    candidate.paste(Image.new("L", (12, 20), 255), (10, 6))
    candidate.paste(middle_zero.crop((2, 2, 14, 22)), (26, 6))
    candidate.paste(Image.new("L", (12, 20), 255), (42, 6))
    sequence = iter(
        (
            candidate,
            signatures[100],
            signatures[120],
            signatures[160],
        )
    )
    monkeypatch.setattr(
        recognizer,
        "_level_signature",
        lambda _image: next(sequence),
    )

    recognized, score = recognizer._recognize_character_level(
        Image.new("RGB", (64, 32)),
        (0.0, 0.0, 1.0, 1.0),
    )

    assert recognized == 100
    assert score is not None


def test_character_selection_match_without_level_card_yields_to_gameplay(
    monkeypatch,
):
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)
    with Image.open(REFERENCE_DIR / "06_connected_gameplay.png") as source:
        candidate = source.convert("RGB")
    reference_states = {
        id(recognizer._reference(definition.filename)): definition.state
        for definition in DEFAULT_SCREEN_TEMPLATES
    }

    monkeypatch.setattr(
        recognizer,
        "_disconnect_overlay_score",
        lambda *_args: 255.0,
    )
    monkeypatch.setattr(
        recognizer,
        "_battle_context_score",
        lambda *_args: 255.0,
    )
    monkeypatch.setattr(
        recognizer,
        "_popup_title_score",
        lambda *_args: 255.0,
    )
    monkeypatch.setattr(
        recognizer,
        "_region_score",
        lambda _candidate, reference, _region: {
            ReconnectScreenState.CHARACTER_SELECTION: 1.0,
            ReconnectScreenState.CONNECTED: 2.0,
        }.get(reference_states[id(reference)], 255.0),
    )
    monkeypatch.setattr(
        recognizer,
        "_character_selection_candidates",
        lambda _candidate: (),
    )

    result = recognizer.recognize_image(candidate)

    assert result.state is ReconnectScreenState.CONNECTED
    assert result.click_point is None


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
    assert battle_result.click_point == (0.5, 0.536)
    assert battle_result.battle_context is True


def test_battle_disconnect_remains_priority_after_bounded_capture_drift():
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)
    with Image.open(
        REFERENCE_DIR / "01_disconnected_dialog.png"
    ) as source:
        disconnected = source.convert("RGB")
    with Image.open(REFERENCE_DIR / BATTLE_REFERENCE_FILE) as source:
        battle = source.convert("RGB")

    base = Image.new("RGB", disconnected.size, "black")
    client_top = round(base.height * 0.05)
    client_bottom = round(base.height * 0.985)
    base.paste(
        battle.resize(
            (base.width, client_bottom - client_top),
            Image.Resampling.BILINEAR,
        ),
        (0, client_top),
    )
    left, top, right, bottom = DISCONNECT_OVERLAY_REGION
    overlay_box = (
        round(disconnected.width * left),
        round(disconnected.height * top),
        round(disconnected.width * right),
        round(disconnected.height * bottom),
    )
    overlay = disconnected.crop(overlay_box)

    for delta_y, scale in ((20, 1.0), (0, 0.90)):
        candidate = base.copy()
        scaled = overlay.resize(
            (
                round(overlay.width * scale),
                round(overlay.height * scale),
            ),
            Image.Resampling.BILINEAR,
        )
        position = (
            overlay_box[0] + (overlay.width - scaled.width) // 2,
            overlay_box[1]
            + delta_y
            + (overlay.height - scaled.height) // 2,
        )
        candidate.paste(scaled, position)

        result = recognizer.recognize_image(candidate)

        assert result.state is ReconnectScreenState.DISCONNECTED
        assert result.click_point == (0.5, 0.536)


def test_confirmed_battle_gameplay_is_connected_without_any_action():
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)
    with Image.open(REFERENCE_DIR / BATTLE_REFERENCE_FILE) as source:
        battle = source.convert("RGB")

    result = recognizer.recognize_image(battle)

    assert result.state is ReconnectScreenState.CONNECTED
    assert result.reference_name == BATTLE_REFERENCE_FILE
    assert result.click_point is None
    assert result.battle_context is True


def test_confirmed_battle_layout_allows_bounded_scene_variation():
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)
    with Image.open(REFERENCE_DIR / BATTLE_REFERENCE_FILE) as source:
        candidate = source.convert("RGB")
    left, top, right, bottom = BATTLE_SCREEN_EVIDENCE_REGION
    box = (
        round(candidate.width * left),
        round(candidate.height * top),
        round(candidate.width * right),
        round(candidate.height * bottom),
    )
    scene = candidate.crop(box)
    scene = ImageEnhance.Brightness(scene).enhance(1.28)
    candidate.paste(scene, box[:2])

    result = recognizer.recognize_image(candidate)

    assert result.state is ReconnectScreenState.CONNECTED
    assert result.reference_name == BATTLE_REFERENCE_FILE
    assert result.click_point is None
    assert result.battle_context is True


def test_battle_gameplay_requires_the_stable_auto_battle_panel():
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)
    with Image.open(REFERENCE_DIR / BATTLE_REFERENCE_FILE) as source:
        candidate = source.convert("RGB")
    left, top, right, bottom = BATTLE_CONTEXT_REGION
    box = (
        round(candidate.width * left),
        round(candidate.height * top),
        round(candidate.width * right),
        round(candidate.height * bottom),
    )
    candidate.paste(
        Image.new(
            "RGB",
            (box[2] - box[0], box[3] - box[1]),
            "black",
        ),
        box[:2],
    )

    result = recognizer.recognize_image(candidate)

    assert result.reference_name != BATTLE_REFERENCE_FILE
    assert result.battle_context is False


def test_battle_panel_alone_or_under_unknown_modal_is_not_online():
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)
    with Image.open(REFERENCE_DIR / BATTLE_REFERENCE_FILE) as source:
        battle = source.convert("RGB")
    left, top, right, bottom = BATTLE_CONTEXT_REGION
    panel_box = (
        round(battle.width * left),
        round(battle.height * top),
        round(battle.width * right),
        round(battle.height * bottom),
    )

    panel_only = Image.new("RGB", battle.size, "black")
    panel_only.paste(battle.crop(panel_box), panel_box[:2])
    unknown_modal = battle.copy()
    modal_box = (
        round(battle.width * 0.25),
        round(battle.height * 0.25),
        round(battle.width * 0.75),
        round(battle.height * 0.75),
    )
    unknown_modal.paste(
        Image.new(
            "RGB",
            (
                modal_box[2] - modal_box[0],
                modal_box[3] - modal_box[1],
            ),
            (90, 120, 130),
        ),
        modal_box[:2],
    )

    panel_only_result = recognizer.recognize_image(panel_only)
    modal_result = recognizer.recognize_image(unknown_modal)

    assert panel_only_result.state is ReconnectScreenState.UNKNOWN
    assert panel_only_result.click_point is None
    assert modal_result.state is ReconnectScreenState.UNKNOWN
    assert modal_result.click_point is None


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


def test_route_number_is_not_used_without_a_reliable_route_prefix():
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)
    with Image.open(REFERENCE_DIR / "03_line_selection_dialog.png") as source:
        candidate = source.convert("RGB")
        prefix_box = (
            round(candidate.width * ROUTE_PREFIX_SEARCH_REGION[0]),
            round(candidate.height * ROUTE_PREFIX_SEARCH_REGION[1]),
            round(candidate.width * ROUTE_PREFIX_SEARCH_REGION[2]),
            round(candidate.height * ROUTE_PREFIX_SEARCH_REGION[3]),
        )
        candidate.paste((29, 88, 111), prefix_box)

    line_number, score = recognizer._recognize_route_number(candidate)

    assert line_number is None
    assert score is None


def test_line_selection_defaults_to_line_one_when_original_line_is_unreadable(
    monkeypatch,
):
    recognizer = ReferenceScreenRecognizer(REFERENCE_DIR)
    monkeypatch.setattr(
        recognizer,
        "_recognize_route_number",
        lambda _candidate: (None, 23.242),
    )
    with Image.open(REFERENCE_DIR / "03_line_selection_dialog.png") as source:
        result = recognizer.recognize_image(source.convert("RGB"))

    assert result.state is ReconnectScreenState.LINE_SELECTION
    assert result.line_number == DEFAULT_LINE_NUMBER == 1
    assert result.click_point == LINE_ROUTE_CLICK_POINTS[1]


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
