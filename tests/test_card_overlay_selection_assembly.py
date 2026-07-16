from datetime import datetime, timedelta, timezone

from cards.service import CardService
from cards.view_state import CardViewItem, CardViewState
from services.card_overlay_selection_assembly import (
    build_windows_card_overlay_selection_coordinator,
)
from services.card_preview_selection_service import CardPreviewSelectionService
from ui.card_overlay import CardSize, WorkArea
from ui.card_preview_settings import CardPreviewCatalog, CardPreviewProfile
from ui.tk_card_presenter import TkCardTextSettings


def _profile(profile_id: str, width: int, background: str) -> CardPreviewProfile:
    return CardPreviewProfile(
        profile_id=profile_id,
        display_name=f"{profile_id} 預覽",
        card_size=CardSize(width, 120),
        right_margin=16,
        bottom_margin=16,
        gap=12,
        text=TkCardTextSettings(
            background=background,
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
        CardPreviewCatalog(
            (
                _profile("compact", 320, "#102030"),
                _profile("roomy", 400, "#304050"),
            )
        )
    )


def _card_state() -> CardViewState:
    shown_at = datetime(2026, 7, 17, tzinfo=timezone.utc)
    return CardViewState(
        cards=(
            CardViewItem(
                card_id="guard",
                group_id="14-windows",
                group_name="14支",
                activity_id="guard",
                activity_name="守紀",
                current_progress="守紀中斷",
                affected_character_ids=(),
                daily_summary=None,
                requires_player_action=True,
                next_step="返回競技場繼續守紀",
                priority_reason="斷線",
                priority_level=1,
                shown_at=shown_at,
                expires_at=shown_at + timedelta(seconds=30),
            ),
        )
    )


class FixedCardState:
    def snapshot(self) -> CardViewState:
        return _card_state()


class RecordingWorkArea:
    def __init__(self) -> None:
        self.calls = 0

    def read(self) -> WorkArea:
        self.calls += 1
        return WorkArea(0, 0, 1920, 1040)


class FakeWindow:
    def __init__(self) -> None:
        self.operations = []

    def overrideredirect(self, enabled) -> None:
        self.operations.append(("overrideredirect", enabled))

    def attributes(self, option, value) -> None:
        self.operations.append(("attributes", option, value))

    def geometry(self, geometry) -> None:
        self.operations.append(("geometry", geometry))

    def destroy(self) -> None:
        self.operations.append(("destroy",))


class RecordingWindowFactory:
    def __init__(self) -> None:
        self.windows = []

    def __call__(self, _master):
        window = FakeWindow()
        self.windows.append(window)
        return window


class FakeWidget:
    def __init__(self, options) -> None:
        self.options = dict(options)

    def configure(self, **options) -> None:
        self.options.update(options)

    def pack(self, **_options) -> None:
        pass

    def pack_forget(self) -> None:
        pass


class RecordingWidgetFactory:
    def __init__(self) -> None:
        self.widgets = []

    def frame(self, _parent, **options):
        widget = FakeWidget(options)
        self.widgets.append(widget)
        return widget

    def label(self, _parent, **options):
        widget = FakeWidget(options)
        self.widgets.append(widget)
        return widget


def _assembly(selection):
    work_area = RecordingWorkArea()
    windows = RecordingWindowFactory()
    widgets = RecordingWidgetFactory()
    coordinator = build_windows_card_overlay_selection_coordinator(
        object(),
        CardService(),
        selection,
        FixedCardState(),
        work_area,
        window_factory=windows,
        widget_factory=widgets,
    )
    return coordinator, work_area, windows, widgets


def test_unselected_assembly_starts_silently_without_windows_api_or_window() -> None:
    coordinator, work_area, windows, _widgets = _assembly(_selection())

    coordinator.start()

    assert coordinator.active_profile_id is None
    assert work_area.calls == 0
    assert windows.windows == []


def test_selected_profile_builds_complete_positioned_window_and_content() -> None:
    selection = _selection()
    selection.select("compact")
    coordinator, work_area, windows, widgets = _assembly(selection)

    coordinator.start()

    assert coordinator.active_profile_id == "compact"
    assert work_area.calls == 1
    assert windows.windows[0].operations[2] == ("geometry", "320x120+1584+904")
    assert widgets.widgets[0].options["background"] == "#102030"
    assert [widget.options["text"] for widget in widgets.widgets[1:]] == [
        "14支",
        "守紀",
        "守紀中斷",
        "返回競技場繼續守紀",
    ]


def test_switching_selection_replaces_window_with_new_profile_settings() -> None:
    selection = _selection()
    selection.select("compact")
    coordinator, _work_area, windows, widgets = _assembly(selection)
    coordinator.start()
    previous = windows.windows[0]

    selection.select("roomy")

    assert previous.operations[-1] == ("destroy",)
    assert coordinator.active_profile_id == "roomy"
    assert windows.windows[1].operations[2] == ("geometry", "400x120+1504+904")
    assert widgets.widgets[5].options["background"] == "#304050"
