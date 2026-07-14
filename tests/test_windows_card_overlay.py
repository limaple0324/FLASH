from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from cards.view_state import CardViewItem
from services.card_overlay_layout_service import PositionedCard
from ui.card_overlay import CardPlacement
from ui.windows_card_overlay import WindowsCardOverlayPort


def _item(
    card_id: str = "guard",
    *,
    x: int = 1544,
    y: int = 904,
    width: int = 360,
    height: int = 120,
) -> PositionedCard:
    shown_at = datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc)
    return PositionedCard(
        card=CardViewItem(
            card_id=card_id,
            group_id="14-windows",
            group_name="14支",
            activity_id="guard",
            activity_name="守紀",
            current_progress="守紀中斷",
            affected_character_ids=("120-old",),
            daily_summary="今日守紀尚未完成",
            requires_player_action=True,
            next_step="返回競技場繼續守紀",
            priority_reason="斷線",
            priority_level=1,
            shown_at=shown_at,
            expires_at=shown_at + timedelta(seconds=30),
        ),
        placement=CardPlacement(
            slot=0,
            x=x,
            y=y,
            width=width,
            height=height,
        ),
    )


class FakeWindow:
    def __init__(self) -> None:
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


class RecordingFactory:
    def __init__(self) -> None:
        self.masters = []
        self.windows = []

    def __call__(self, master):
        window = FakeWindow()
        self.masters.append(master)
        self.windows.append(window)
        return window


class RecordingRenderer:
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, window, card):
        self.calls.append((window, card))


def _port(renderer=None):
    factory = RecordingFactory()
    renderer = renderer or RecordingRenderer()
    master = object()
    port = WindowsCardOverlayPort(
        master,
        renderer,
        window_factory=factory,
    )
    return port, factory, renderer, master


def test_open_creates_topmost_borderless_window_at_position():
    port, factory, renderer, master = _port()
    item = _item()

    port.open(item)

    window = factory.windows[0]
    assert factory.masters == [master]
    assert window.operations == [
        ("overrideredirect", True),
        ("attributes", "-topmost", True),
        ("geometry", "360x120+1544+904"),
    ]
    assert renderer.calls == [(window, item.card)]
    assert port.open_card_ids == ("guard",)


def test_open_preserves_negative_monitor_coordinates():
    port, factory, _, _ = _port()

    port.open(_item(x=-340, y=-120, width=320, height=100))

    assert factory.windows[0].operations[-1] == (
        "geometry",
        "320x100-340-120",
    )


def test_update_reuses_window_and_applies_new_position_and_content():
    port, factory, renderer, _ = _port()
    first = _item()
    port.open(first)
    window = factory.windows[0]
    renderer.calls.clear()
    changed = replace(
        first,
        card=replace(first.card, current_progress="已恢復登入"),
        placement=replace(first.placement, x=1500, y=760),
    )

    port.update(changed)

    assert len(factory.windows) == 1
    assert window.operations[-1] == ("geometry", "360x120+1500+760")
    assert renderer.calls == [(window, changed.card)]


def test_close_destroys_window_once_and_missing_close_is_safe():
    port, factory, _, _ = _port()
    port.open(_item())
    window = factory.windows[0]

    port.close("guard")
    port.close("guard")

    assert window.operations.count(("destroy",)) == 1
    assert port.open_card_ids == ()


def test_duplicate_open_and_missing_update_are_rejected():
    port, factory, _, _ = _port()
    item = _item()
    port.open(item)

    with pytest.raises(ValueError, match="already open"):
        port.open(item)
    with pytest.raises(KeyError, match="not open"):
        port.update(_item("missing"))

    assert len(factory.windows) == 1


def test_renderer_failure_destroys_partial_window_without_tracking_it():
    def failing_renderer(window, card):
        raise RuntimeError("renderer unavailable")

    port, factory, _, _ = _port(failing_renderer)

    with pytest.raises(RuntimeError, match="renderer unavailable"):
        port.open(_item())

    assert factory.windows[0].destroyed is True
    assert port.open_card_ids == ()
