from datetime import datetime, timezone

from cards.models import GroupCard
from cards.priority import CardPriorityReason
from cards.service import CardService
from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.group import CharacterGroup
from services.card_overlay_layout_service import (
    CardOverlayLayoutService,
    PositionedCard,
)
from services.card_overlay_runtime import build_windows_card_overlay_runtime
from services.card_view_state_service import CardViewStateService
from ui.card_content_renderer import CardContent
from ui.card_overlay import CardPlacement, CardSize, WorkArea
from ui.tk_card_presenter import TkCardTextSettings
from ui.windows_card_overlay import WindowsCardOverlayPort


def _card(card_id: str = "daily") -> GroupCard:
    return GroupCard(
        card_id=card_id,
        group=CharacterGroup(group_id="group-120", name="120"),
        activity=ActivityDefinition(
            activity_id="demon-hall",
            name="諸魔殿",
            activity_type=ActivityType.DAILY,
            reset_rule=ResetRule.DAILY_MIDNIGHT,
        ),
        current_progress="尚未完成",
        next_step="12:55 前準備",
        priority_reason=CardPriorityReason.ACTIVITY,
    )


class FixedWorkArea:
    def read(self):
        return WorkArea(0, 0, 1920, 1040)


class FakeWindow:
    def __init__(self):
        self.calls = []
        self.destroyed = False

    def overrideredirect(self, value):
        self.calls.append(("overrideredirect", value))

    def attributes(self, option, value):
        self.calls.append(("attributes", option, value))

    def geometry(self, value):
        self.calls.append(("geometry", value))

    def destroy(self):
        self.destroyed = True


def test_overlay_window_is_topmost_borderless_and_uses_exact_geometry() -> None:
    cards = CardService()
    cards.upsert(_card(), shown_at=datetime.now(timezone.utc))
    item = CardViewStateService(cards).snapshot().cards[0]
    positioned = PositionedCard(item, CardPlacement(0, 1540, 880, 360, 140))
    window = FakeWindow()
    rendered = []
    port = WindowsCardOverlayPort(
        object(),
        lambda target, card: rendered.append((target, card.card_id)),
        window_factory=lambda _master: window,
    )

    port.open(positioned)

    assert ("overrideredirect", True) in window.calls
    assert ("attributes", "-topmost", True) in window.calls
    assert ("attributes", "-alpha", 1.0) in window.calls
    assert ("geometry", "360x140+1540+880") in window.calls
    assert rendered == [(window, "daily")]
    port.close("daily")
    assert window.destroyed is True


def test_card_change_opens_and_removal_closes_overlay() -> None:
    cards = CardService()
    state = CardViewStateService(cards)
    layout = CardOverlayLayoutService(
        state,
        FixedWorkArea(),
        CardSize(360, 140),
        right_margin=20,
        bottom_margin=20,
        gap=12,
    )
    windows = []
    runtime = build_windows_card_overlay_runtime(
        object(),
        cards,
        layout,
        TkCardTextSettings(
            background="#FFFFFF",
            foreground="#182433",
            muted_foreground="#617083",
            accent="#2474C6",
        ),
        window_factory=lambda _master: windows.append(FakeWindow()) or windows[-1],
        widget_factory=_FakeWidgetFactory(),
    )

    runtime.start()
    assert runtime.running is True
    cards.upsert(_card(), shown_at=datetime.now(timezone.utc))
    assert runtime.last_error is None
    assert len(windows) == 1

    cards.remove("daily")
    assert windows[0].destroyed is True
    runtime.stop()
    assert runtime.running is False


def test_name_only_activity_card_can_be_closed_without_opening_details() -> None:
    cards = CardService()
    state = CardViewStateService(cards)
    layout = CardOverlayLayoutService(
        state,
        FixedWorkArea(),
        CardSize(360, 140),
        right_margin=20,
        bottom_margin=20,
        gap=12,
    )
    widgets = _FakeWidgetFactory()
    windows = []
    runtime = build_windows_card_overlay_runtime(
        object(),
        cards,
        layout,
        TkCardTextSettings(
            background="#FFFFFF",
            foreground="#182433",
            muted_foreground="#617083",
            accent="#2474C6",
        ),
        window_factory=lambda _master: windows.append(FakeWindow()) or windows[-1],
        widget_factory=widgets,
    )
    reminder = _card("activity-reminder")
    reminder = GroupCard(
        card_id=reminder.card_id,
        group=reminder.group,
        activity=reminder.activity,
        current_progress=reminder.activity.name,
        priority_reason=CardPriorityReason.ACTIVITY,
        name_only=True,
    )

    runtime.start()
    cards.upsert(reminder, shown_at=datetime.now(timezone.utc))
    button = widgets.widgets[1]
    labels = widgets.widgets[2:6]

    assert [widget.options.get("text") for widget in labels] == [
        None,
        "諸魔殿",
        None,
        None,
    ]
    assert callable(button.options["command"])
    button.options["command"]()
    assert cards.cards == ()
    assert windows[0].destroyed is True
    runtime.stop()


def test_card_actions_are_rendered_and_dispatched_without_opening_a_page() -> None:
    cards = CardService()
    state = CardViewStateService(cards)
    layout = CardOverlayLayoutService(
        state,
        FixedWorkArea(),
        CardSize(160, 75),
        right_margin=12,
        bottom_margin=12,
        gap=6,
    )
    widgets = _FakeWidgetFactory()
    actions = []
    runtime = build_windows_card_overlay_runtime(
        object(),
        cards,
        layout,
        TkCardTextSettings(
            background="#80591F",
            foreground="#FFF2CF",
            muted_foreground="#FFF2CF",
            accent="#FFF2CF",
        ),
        window_factory=lambda _master: FakeWindow(),
        widget_factory=widgets,
        on_action=lambda card_id, action_id: actions.append(
            (card_id, action_id)
        ),
    )
    base = _card("preference")
    from cards.models import CardAction

    cards_with_action = GroupCard(
        card_id=base.card_id,
        group=base.group,
        activity=base.activity,
        current_progress="建議保存",
        priority_reason=CardPriorityReason.PREFERENCE,
        actions=(CardAction("adopt", "採用"),),
    )

    runtime.start()
    cards.upsert(cards_with_action, shown_at=datetime.now(timezone.utc))
    action_button = widgets.widgets[6]

    assert action_button.options["text"] == "採用"
    assert callable(action_button.options["command"])
    action_button.options["command"]()
    assert actions == [("preference", "adopt")]
    runtime.stop()


def test_card_content_uses_only_confirmed_player_fields() -> None:
    cards = CardService()
    cards.upsert(_card(), shown_at=datetime.now(timezone.utc))
    item = CardViewStateService(cards).snapshot().cards[0]

    content = CardContent.from_card(item)

    assert content == CardContent(
        card_id="daily",
        group_name="120",
        activity_name="諸魔殿",
        current_progress="尚未完成",
        next_step="12:55 前準備",
        name_only=False,
    )
    assert not hasattr(content, "affected_character_ids")


def test_long_card_text_keeps_width_and_grows_height() -> None:
    cards = CardService()
    long_card = GroupCard(
        card_id="long",
        group=CharacterGroup(group_id="group-120", name="120"),
        activity=ActivityDefinition(
            activity_id="long-activity",
            name="這是一個需要自動換行而且必須完整顯示的很長活動名稱",
            activity_type=ActivityType.DAILY,
            reset_rule=ResetRule.DAILY_MIDNIGHT,
        ),
        current_progress="這是一段需要完整顯示的很長進度內容",
        priority_reason=CardPriorityReason.ACTIVITY,
    )
    cards.upsert(long_card, shown_at=datetime.now(timezone.utc))
    layout = CardOverlayLayoutService(
        CardViewStateService(cards),
        FixedWorkArea(),
        CardSize(160, 75),
        right_margin=12,
        bottom_margin=12,
        gap=6,
    ).snapshot()

    assert layout.cards[0].placement.width == 160
    assert layout.cards[0].placement.height > 75


class _FakeWidget:
    def __init__(self, **options):
        self.options = options

    def configure(self, **options):
        self.options.update(options)

    def pack(self, **_options):
        return None

    def pack_forget(self):
        return None


class _FakeWidgetFactory:
    def __init__(self):
        self.widgets = []

    def _widget(self, **options):
        widget = _FakeWidget(**options)
        self.widgets.append(widget)
        return widget

    def frame(self, _parent, **options):
        return self._widget(**options)

    def label(self, _parent, **options):
        return self._widget(**options)

    def button(self, _parent, **options):
        return self._widget(**options)
