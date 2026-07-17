from dataclasses import FrozenInstanceError

import pytest

from services.card_preview_selection_service import (
    CardPreviewChoice,
    CardPreviewSelectionService,
    CardPreviewSelectionState,
)
from ui.card_overlay import CardSize
from ui.card_preview_settings import CardPreviewCatalog, CardPreviewProfile
from ui.tk_card_presenter import TkCardTextSettings


def _profile(profile_id: str) -> CardPreviewProfile:
    return CardPreviewProfile(
        profile_id=profile_id,
        display_name=f"{profile_id} 預覽",
        card_size=CardSize(360, 120),
        right_margin=16,
        bottom_margin=16,
        gap=12,
        text=TkCardTextSettings(
            background="#102030",
            foreground="#ffffff",
            font_family="Microsoft JhengHei UI",
            font_size=12,
            horizontal_padding=12,
            vertical_padding=8,
            line_spacing=4,
        ),
    )


def _service() -> CardPreviewSelectionService:
    return CardPreviewSelectionService(
        CardPreviewCatalog((_profile("compact"), _profile("roomy")))
    )


def test_initial_state_has_no_selection_and_keeps_overlay_disabled() -> None:
    service = _service()

    assert service.snapshot() == CardPreviewSelectionState()
    assert service.snapshot().overlay_enabled is False
    assert service.selected_profile() is None


def test_available_choices_expose_only_ordered_player_facing_metadata() -> None:
    choices = _service().available_choices()

    assert choices == (
        CardPreviewChoice("compact", "compact 預覽", False),
        CardPreviewChoice("roomy", "roomy 預覽", False),
    )
    assert not hasattr(choices[0], "card_size")
    assert not hasattr(choices[0], "text")


def test_available_choices_mark_only_the_current_selection() -> None:
    service = _service()
    before = service.available_choices()

    service.select("roomy")
    after = service.available_choices()

    assert [choice.selected for choice in before] == [False, False]
    assert [choice.selected for choice in after] == [False, True]


def test_available_choice_items_are_immutable() -> None:
    choice = _service().available_choices()[0]

    with pytest.raises(FrozenInstanceError):
        choice.display_name = "已被修改"


def test_explicit_catalog_selection_enables_overlay() -> None:
    service = _service()

    state = service.select("compact")

    assert state.selected_profile_id == "compact"
    assert state.overlay_enabled is True
    assert service.selected_profile().profile_id == "compact"


def test_player_can_switch_to_another_catalog_profile() -> None:
    service = _service()
    service.select("compact")

    service.select("roomy")

    assert service.snapshot().selected_profile_id == "roomy"
    assert service.selected_profile().profile_id == "roomy"


def test_unknown_selection_is_rejected_without_losing_current_choice() -> None:
    service = _service()
    original = service.select("compact")

    with pytest.raises(KeyError):
        service.select("unknown")

    assert service.snapshot() is original
    assert service.selected_profile().profile_id == "compact"


def test_clear_removes_selection_and_disables_overlay() -> None:
    service = _service()
    service.select("roomy")

    state = service.clear()

    assert state.selected_profile_id is None
    assert state.overlay_enabled is False
    assert service.selected_profile() is None


def test_selection_snapshot_is_immutable() -> None:
    state = _service().select("compact")

    with pytest.raises(FrozenInstanceError):
        state.selected_profile_id = "roomy"


def test_service_rejects_non_catalog_input() -> None:
    with pytest.raises(TypeError, match="catalog"):
        CardPreviewSelectionService(object())


def test_successful_select_switch_and_clear_notify_current_state() -> None:
    service = _service()
    states = []
    service.subscribe(lambda: states.append(service.snapshot()))

    service.select("compact")
    service.select("roomy")
    service.clear()

    assert [state.selected_profile_id for state in states] == [
        "compact",
        "roomy",
        None,
    ]


def test_unknown_selection_does_not_notify_listener() -> None:
    service = _service()
    notifications = []
    service.subscribe(lambda: notifications.append(True))

    with pytest.raises(KeyError):
        service.select("unknown")

    assert notifications == []


def test_unsubscribe_stops_future_selection_notifications() -> None:
    service = _service()
    notifications = []

    def record_change() -> None:
        notifications.append(service.snapshot().selected_profile_id)

    service.subscribe(record_change)
    service.select("compact")
    service.unsubscribe(record_change)
    service.select("roomy")

    assert notifications == ["compact"]


def test_selection_listener_must_be_callable() -> None:
    with pytest.raises(TypeError, match="listener"):
        _service().subscribe(object())
