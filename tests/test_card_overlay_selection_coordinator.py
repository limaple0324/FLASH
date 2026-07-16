import pytest

from services.card_overlay_selection_coordinator import (
    CardOverlaySelectionCoordinator,
)
from services.card_preview_selection_service import CardPreviewSelectionService
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


def _selection() -> CardPreviewSelectionService:
    return CardPreviewSelectionService(
        CardPreviewCatalog((_profile("compact"), _profile("roomy")))
    )


class FakeRuntime:
    def __init__(self, profile_id: str, *, fail_start: bool = False) -> None:
        self.profile_id = profile_id
        self.fail_start = fail_start
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        if self.fail_start:
            raise RuntimeError("overlay start failed")

    def stop(self) -> None:
        self.stop_calls += 1


class RecordingFactory:
    def __init__(self) -> None:
        self.created: list[FakeRuntime] = []
        self.fail_profile_id: str | None = None
        self.raise_profile_id: str | None = None

    def __call__(self, profile: CardPreviewProfile) -> FakeRuntime:
        if profile.profile_id == self.raise_profile_id:
            raise RuntimeError("factory failed")
        runtime = FakeRuntime(
            profile.profile_id,
            fail_start=profile.profile_id == self.fail_profile_id,
        )
        self.created.append(runtime)
        return runtime


def test_start_without_selection_does_not_create_an_overlay() -> None:
    selection = _selection()
    factory = RecordingFactory()
    coordinator = CardOverlaySelectionCoordinator(selection, factory)

    changed = coordinator.start()

    assert changed is False
    assert coordinator.started is True
    assert coordinator.active_profile_id is None
    assert factory.created == []


def test_explicit_selection_starts_its_overlay() -> None:
    selection = _selection()
    selection.select("compact")
    factory = RecordingFactory()
    coordinator = CardOverlaySelectionCoordinator(selection, factory)

    changed = coordinator.start()

    assert changed is True
    assert coordinator.active_profile_id == "compact"
    assert factory.created[0].start_calls == 1


def test_switch_stops_old_overlay_and_starts_selected_replacement() -> None:
    selection = _selection()
    selection.select("compact")
    factory = RecordingFactory()
    coordinator = CardOverlaySelectionCoordinator(selection, factory)
    coordinator.start()
    previous = factory.created[0]

    selection.select("roomy")
    changed = coordinator.sync_selection()

    assert changed is True
    assert previous.stop_calls == 1
    assert factory.created[1].profile_id == "roomy"
    assert factory.created[1].start_calls == 1
    assert coordinator.active_profile_id == "roomy"


def test_same_selection_does_not_rebuild_overlay() -> None:
    selection = _selection()
    selection.select("compact")
    factory = RecordingFactory()
    coordinator = CardOverlaySelectionCoordinator(selection, factory)
    coordinator.start()

    changed = coordinator.sync_selection()

    assert changed is False
    assert len(factory.created) == 1


def test_clear_selection_stops_overlay_without_creating_a_replacement() -> None:
    selection = _selection()
    selection.select("compact")
    factory = RecordingFactory()
    coordinator = CardOverlaySelectionCoordinator(selection, factory)
    coordinator.start()
    previous = factory.created[0]

    selection.clear()
    changed = coordinator.sync_selection()

    assert changed is True
    assert previous.stop_calls == 1
    assert len(factory.created) == 1
    assert coordinator.active_profile_id is None


def test_replacement_factory_failure_preserves_running_overlay() -> None:
    selection = _selection()
    selection.select("compact")
    factory = RecordingFactory()
    coordinator = CardOverlaySelectionCoordinator(selection, factory)
    coordinator.start()
    previous = factory.created[0]

    selection.select("roomy")
    factory.raise_profile_id = "roomy"
    with pytest.raises(RuntimeError, match="factory failed"):
        coordinator.sync_selection()

    assert previous.stop_calls == 0
    assert coordinator.active_profile_id == "compact"


def test_failed_replacement_start_is_cleaned_and_leaves_overlay_disabled() -> None:
    selection = _selection()
    selection.select("compact")
    factory = RecordingFactory()
    coordinator = CardOverlaySelectionCoordinator(selection, factory)
    coordinator.start()
    previous = factory.created[0]
    factory.fail_profile_id = "roomy"
    selection.select("roomy")

    with pytest.raises(RuntimeError, match="overlay start failed"):
        coordinator.sync_selection()

    failed = factory.created[1]
    assert previous.stop_calls == 1
    assert failed.stop_calls == 1
    assert coordinator.active_profile_id is None


def test_stop_is_idempotent_and_prevents_future_sync_until_restarted() -> None:
    selection = _selection()
    selection.select("compact")
    factory = RecordingFactory()
    coordinator = CardOverlaySelectionCoordinator(selection, factory)
    coordinator.start()

    assert coordinator.stop() is True
    assert coordinator.stop() is False
    selection.select("roomy")
    assert coordinator.sync_selection() is False
    assert len(factory.created) == 1
