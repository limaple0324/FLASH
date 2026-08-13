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
    def __init__(
        self,
        profile_id: str,
        *,
        fail_start: bool = False,
        record_start_error: bool = False,
        stop_result: bool | None = None,
    ) -> None:
        self.profile_id = profile_id
        self.fail_start = fail_start
        self.record_start_error = record_start_error
        self.stop_result = stop_result
        self.start_calls = 0
        self.stop_calls = 0
        self.last_error: Exception | None = None

    def start(self) -> None:
        self.start_calls += 1
        if self.fail_start:
            raise RuntimeError("overlay start failed")
        if self.record_start_error:
            self.last_error = RuntimeError("overlay refresh failed")

    def stop(self) -> bool | None:
        self.stop_calls += 1
        return self.stop_result


class RecordingFactory:
    def __init__(self) -> None:
        self.created: list[FakeRuntime] = []
        self.fail_profile_id: str | None = None
        self.raise_profile_id: str | None = None
        self.record_error_profile_id: str | None = None

    def __call__(self, profile: CardPreviewProfile) -> FakeRuntime:
        if profile.profile_id == self.raise_profile_id:
            raise RuntimeError("factory failed")
        runtime = FakeRuntime(
            profile.profile_id,
            fail_start=profile.profile_id == self.fail_profile_id,
            record_start_error=(
                profile.profile_id == self.record_error_profile_id
            ),
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
    assert factory.created == []


def test_explicit_selection_starts_its_overlay() -> None:
    selection = _selection()
    selection.select("compact")
    factory = RecordingFactory()
    coordinator = CardOverlaySelectionCoordinator(selection, factory)

    changed = coordinator.start()

    assert changed is True
    assert factory.created[0].profile_id == "compact"
    assert factory.created[0].start_calls == 1


def test_switch_stops_old_overlay_and_starts_selected_replacement() -> None:
    selection = _selection()
    selection.select("compact")
    factory = RecordingFactory()
    coordinator = CardOverlaySelectionCoordinator(selection, factory)
    coordinator.start()
    previous = factory.created[0]

    selection.select("roomy")

    assert previous.stop_calls == 1
    assert factory.created[1].profile_id == "roomy"
    assert factory.created[1].start_calls == 1


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

    assert previous.stop_calls == 1
    assert len(factory.created) == 1


def test_replacement_factory_failure_preserves_running_overlay() -> None:
    selection = _selection()
    selection.select("compact")
    factory = RecordingFactory()
    coordinator = CardOverlaySelectionCoordinator(selection, factory)
    coordinator.start()
    previous = factory.created[0]

    factory.raise_profile_id = "roomy"
    with pytest.raises(RuntimeError, match="factory failed"):
        selection.select("roomy")

    assert previous.stop_calls == 0
    assert selection.snapshot().selected_profile_id == "compact"


def test_failed_replacement_start_is_cleaned_and_preserves_previous_overlay() -> None:
    selection = _selection()
    selection.select("compact")
    factory = RecordingFactory()
    coordinator = CardOverlaySelectionCoordinator(selection, factory)
    coordinator.start()
    previous = factory.created[0]
    factory.fail_profile_id = "roomy"

    with pytest.raises(RuntimeError, match="overlay start failed"):
        selection.select("roomy")

    failed = factory.created[1]
    assert previous.stop_calls == 0
    assert failed.stop_calls == 1
    assert selection.snapshot().selected_profile_id == "compact"


def test_saved_selection_start_failure_keeps_coordinator_available_for_retry() -> None:
    selection = _selection()
    selection.select("compact")
    factory = RecordingFactory()
    factory.fail_profile_id = "compact"
    coordinator = CardOverlaySelectionCoordinator(selection, factory)

    with pytest.raises(RuntimeError, match="overlay start failed"):
        coordinator.start()

    failed = factory.created[0]
    assert coordinator.started is True
    assert failed.start_calls == 1
    assert failed.stop_calls == 1

    factory.fail_profile_id = None
    selection.select("compact")

    assert len(factory.created) == 2
    assert factory.created[-1].profile_id == "compact"
    assert factory.created[-1].start_calls == 1


def test_runtime_recorded_refresh_error_is_treated_as_start_failure() -> None:
    selection = _selection()
    selection.select("compact")
    factory = RecordingFactory()
    factory.record_error_profile_id = "compact"
    coordinator = CardOverlaySelectionCoordinator(selection, factory)

    with pytest.raises(RuntimeError, match="overlay refresh failed"):
        coordinator.start()

    failed = factory.created[0]
    assert failed.stop_calls == 1
    assert failed.last_error is not None
    assert coordinator.started is True


def test_stop_is_idempotent_and_prevents_future_sync_until_restarted() -> None:
    selection = _selection()
    selection.select("compact")
    factory = RecordingFactory()
    coordinator = CardOverlaySelectionCoordinator(selection, factory)
    coordinator.start()

    assert coordinator.stop() is True
    assert coordinator.stop() is False
    selection.select("roomy")
    assert len(factory.created) == 1


def test_stopped_coordinator_unsubscribes_from_selection_changes() -> None:
    selection = _selection()
    factory = RecordingFactory()
    coordinator = CardOverlaySelectionCoordinator(selection, factory)
    coordinator.start()
    coordinator.stop()

    selection.select("compact")

    assert factory.created == []


def test_failed_stop_keeps_active_runtime_for_a_safe_retry() -> None:
    selection = _selection()
    selection.select("compact")
    factory = RecordingFactory()
    coordinator = CardOverlaySelectionCoordinator(selection, factory)
    coordinator.start()
    runtime = factory.created[0]
    runtime.stop_result = False

    with pytest.raises(RuntimeError, match="overlay stop failed"):
        coordinator.stop()

    assert coordinator.started is True
    assert selection.snapshot().selected_profile_id == "compact"
    runtime.stop_result = True
    assert coordinator.stop() is True
    assert runtime.stop_calls == 2
    assert coordinator.started is False
