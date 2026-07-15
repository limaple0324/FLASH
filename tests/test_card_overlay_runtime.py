from dataclasses import replace
from datetime import datetime, timedelta, timezone

from cards.models import GroupCard
from cards.service import CardService
from cards.view_state import CardViewItem
from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.group import CharacterGroup
from services.card_overlay_layout_service import CardOverlayLayout, PositionedCard
from services.card_overlay_runtime import build_windows_card_overlay_runtime
from ui.card_overlay import CardPlacement
from ui.tk_card_presenter import TkCardTextSettings


def _group_card(progress="守紀中斷"):
    return GroupCard(
        card_id="guard",
        group=CharacterGroup(group_id="14-windows", name="14支"),
        activity=ActivityDefinition(
            activity_id="guard",
            name="守紀",
            activity_type=ActivityType.DAILY,
            reset_rule=ResetRule.DAILY_MIDNIGHT,
        ),
        current_progress=progress,
        next_step="返回競技場繼續守紀",
    )


def _positioned_card(progress="守紀中斷", *, y=904):
    shown_at = datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc)
    return PositionedCard(
        card=CardViewItem(
            card_id="guard",
            group_id="14-windows",
            group_name="14支",
            activity_id="guard",
            activity_name="守紀",
            current_progress=progress,
            affected_character_ids=(),
            daily_summary=None,
            requires_player_action=True,
            next_step="返回競技場繼續守紀",
            priority_reason="斷線",
            priority_level=1,
            shown_at=shown_at,
            expires_at=shown_at + timedelta(seconds=30),
        ),
        placement=CardPlacement(
            slot=0,
            x=1544,
            y=y,
            width=360,
            height=120,
        ),
    )


def _settings():
    return TkCardTextSettings(
        background="caller-background",
        foreground="caller-foreground",
        font_family="caller-font",
        font_size=12,
        horizontal_padding=10,
        vertical_padding=8,
        line_spacing=4,
    )


class MutableLayoutSource:
    def __init__(self, layout=CardOverlayLayout()):
        self.layout = layout
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        return self.layout


class FakeWindow:
    def __init__(self):
        self.operations = []
        self.destroyed = False

    def overrideredirect(self, enabled):
        self.operations.append(("overrideredirect", enabled))

    def attributes(self, option, value):
        self.operations.append(("attributes", option, value))

    def geometry(self, geometry):
        self.operations.append(("geometry", geometry))

    def destroy(self):
        self.operations.append(("destroy",))
        self.destroyed = True


class RecordingWindowFactory:
    def __init__(self):
        self.windows = []

    def __call__(self, master):
        window = FakeWindow()
        self.windows.append(window)
        return window


class FakeWidget:
    def __init__(self, kind, options):
        self.kind = kind
        self.options = dict(options)
        self.visible = False

    def configure(self, **options):
        self.options.update(options)

    def pack(self, **options):
        self.visible = True

    def pack_forget(self):
        self.visible = False


class RecordingWidgetFactory:
    def __init__(self):
        self.widgets = []

    def frame(self, parent, **options):
        widget = FakeWidget("frame", options)
        self.widgets.append(widget)
        return widget

    def label(self, parent, **options):
        widget = FakeWidget("label", options)
        self.widgets.append(widget)
        return widget


def _runtime(layout):
    cards = CardService()
    windows = RecordingWindowFactory()
    widgets = RecordingWidgetFactory()
    service = build_windows_card_overlay_runtime(
        object(),
        cards,
        layout,
        _settings(),
        window_factory=windows,
        widget_factory=widgets,
    )
    return service, cards, windows, widgets


def test_runtime_waits_for_start_then_performs_initial_sync():
    layout = MutableLayoutSource(
        CardOverlayLayout(cards=(_positioned_card(),))
    )
    service, _, windows, widgets = _runtime(layout)

    assert service.running is False
    assert windows.windows == []
    service.start()

    assert service.running is True
    assert layout.calls == 1
    assert windows.windows[0].operations[:3] == [
        ("overrideredirect", True),
        ("attributes", "-topmost", True),
        ("geometry", "360x120+1544+904"),
    ]
    assert [widget.options["text"] for widget in widgets.widgets[1:]] == [
        "14支",
        "守紀",
        "守紀中斷",
        "返回競技場繼續守紀",
    ]


def test_card_change_refreshes_existing_runtime_window():
    first = _positioned_card()
    layout = MutableLayoutSource(CardOverlayLayout(cards=(first,)))
    service, cards, windows, widgets = _runtime(layout)
    service.start()
    original_widgets = tuple(widgets.widgets)
    changed = replace(
        first,
        card=replace(first.card, current_progress="已恢復登入"),
        placement=replace(first.placement, y=760),
    )
    layout.layout = CardOverlayLayout(cards=(changed,))

    cards.upsert(_group_card("已恢復登入"))

    assert len(windows.windows) == 1
    assert tuple(widgets.widgets) == original_widgets
    assert windows.windows[0].operations[-1] == (
        "geometry",
        "360x120+1544+760",
    )
    assert widgets.widgets[3].options["text"] == "已恢復登入"


def test_stop_closes_windows_and_unsubscribes_from_future_changes():
    layout = MutableLayoutSource(
        CardOverlayLayout(cards=(_positioned_card(),))
    )
    service, cards, windows, _ = _runtime(layout)
    service.start()
    calls_before_stop = layout.calls

    service.stop()
    layout.layout = CardOverlayLayout()
    cards.upsert(_group_card())
    service.stop()

    assert service.running is False
    assert windows.windows[0].destroyed is True
    assert layout.calls == calls_before_stop
