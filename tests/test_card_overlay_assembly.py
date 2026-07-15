from dataclasses import replace
from datetime import datetime, timedelta, timezone

from cards.view_state import CardViewItem
from services.card_overlay_assembly import build_windows_card_overlay_lifecycle
from services.card_overlay_layout_service import CardOverlayLayout, PositionedCard
from ui.card_overlay import CardPlacement
from ui.tk_card_presenter import TkCardTextSettings


def _item(*, progress="守紀中斷", next_step="返回競技場繼續守紀"):
    shown_at = datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc)
    return PositionedCard(
        card=CardViewItem(
            card_id="guard",
            group_id="14-windows",
            group_name="14支",
            activity_id="guard",
            activity_name="守紀",
            current_progress=progress,
            affected_character_ids=("120-old",),
            daily_summary="今日守紀尚未完成",
            requires_player_action=True,
            next_step=next_step,
            priority_reason="斷線",
            priority_level=1,
            shown_at=shown_at,
            expires_at=shown_at + timedelta(seconds=30),
        ),
        placement=CardPlacement(
            slot=0,
            x=1544,
            y=904,
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
        self.masters = []
        self.windows = []

    def __call__(self, master):
        window = FakeWindow()
        self.masters.append(master)
        self.windows.append(window)
        return window


class FakeWidget:
    def __init__(self, kind, parent, options):
        self.kind = kind
        self.parent = parent
        self.options = dict(options)
        self.visible = False

    def configure(self, **options):
        self.options.update(options)

    def pack(self, **options):
        self.visible = True
        self.pack_options = options

    def pack_forget(self):
        self.visible = False


class RecordingWidgetFactory:
    def __init__(self):
        self.widgets = []

    def _create(self, kind, parent, options):
        widget = FakeWidget(kind, parent, options)
        self.widgets.append(widget)
        return widget

    def frame(self, parent, **options):
        return self._create("frame", parent, options)

    def label(self, parent, **options):
        return self._create("label", parent, options)


def _assembly():
    master = object()
    windows = RecordingWindowFactory()
    widgets = RecordingWidgetFactory()
    lifecycle = build_windows_card_overlay_lifecycle(
        master,
        _settings(),
        window_factory=windows,
        widget_factory=widgets,
    )
    return lifecycle, windows, widgets, master


def test_assembly_opens_positioned_window_and_renders_real_card_content():
    lifecycle, windows, widgets, master = _assembly()
    item = _item()

    lifecycle.sync(CardOverlayLayout(cards=(item,)))

    window = windows.windows[0]
    assert windows.masters == [master]
    assert window.operations == [
        ("overrideredirect", True),
        ("attributes", "-topmost", True),
        ("geometry", "360x120+1544+904"),
    ]
    frame, *labels = widgets.widgets
    assert frame.options == {"background": "caller-background"}
    assert [label.options["text"] for label in labels] == [
        "14支",
        "守紀",
        "守紀中斷",
        "返回競技場繼續守紀",
    ]
    assert all(label.options["foreground"] == "caller-foreground" for label in labels)
    assert lifecycle.visible_card_ids == ("guard",)


def test_assembly_updates_existing_window_and_reuses_text_widgets():
    lifecycle, windows, widgets, _ = _assembly()
    first = _item()
    lifecycle.sync(CardOverlayLayout(cards=(first,)))
    original_widgets = tuple(widgets.widgets)
    changed = replace(
        first,
        card=replace(
            first.card,
            current_progress="已恢復登入",
            next_step="返回守紀畫面",
        ),
        placement=replace(first.placement, y=760),
    )

    lifecycle.sync(CardOverlayLayout(cards=(changed,)))

    assert len(windows.windows) == 1
    assert tuple(widgets.widgets) == original_widgets
    assert windows.windows[0].operations[-1] == (
        "geometry",
        "360x120+1544+760",
    )
    assert widgets.widgets[3].options["text"] == "已恢復登入"
    assert widgets.widgets[4].options["text"] == "返回守紀畫面"


def test_assembly_closes_window_when_card_leaves_layout():
    lifecycle, windows, _, _ = _assembly()
    lifecycle.sync(CardOverlayLayout(cards=(_item(),)))

    lifecycle.sync(CardOverlayLayout())

    assert windows.windows[0].destroyed is True
    assert lifecycle.visible_card_ids == ()
