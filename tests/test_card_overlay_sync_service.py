from datetime import datetime, timedelta, timezone

from cards.models import GroupCard
from cards.service import CardService
from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.group import CharacterGroup
from services.card_overlay_layout_service import CardOverlayLayout
from services.card_overlay_sync_service import CardOverlaySyncService


def _card(card_id: str, progress: str = "守紀中斷") -> GroupCard:
    return GroupCard(
        card_id=card_id,
        group=CharacterGroup(group_id="14-windows", name="14支"),
        activity=ActivityDefinition(
            activity_id="guard",
            name="守紀",
            activity_type=ActivityType.DAILY,
            reset_rule=ResetRule.DAILY_MIDNIGHT,
        ),
        current_progress=progress,
    )


class MutableLayoutSource:
    def __init__(self):
        self.layout = CardOverlayLayout()
        self.error = None
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.layout


class RecordingLifecycle:
    def __init__(self):
        self.layouts = []
        self.close_all_calls = 0

    def sync(self, layout):
        self.layouts.append(layout)

    def close_all(self):
        self.close_all_calls += 1


def test_start_and_card_add_update_complete_trigger_overlay_refresh():
    cards = CardService()
    layouts = MutableLayoutSource()
    lifecycle = RecordingLifecycle()
    service = CardOverlaySyncService(cards, layouts, lifecycle)
    added = CardOverlayLayout()
    updated = CardOverlayLayout()

    service.start()
    lifecycle.layouts.clear()
    layouts.layout = added
    cards.upsert(_card("guard"))
    layouts.layout = updated
    cards.upsert(_card("guard", "已恢復登入"))
    layouts.layout = CardOverlayLayout()
    cards.complete("guard")

    assert lifecycle.layouts == [added, updated, CardOverlayLayout()]
    assert layouts.calls == 4
    assert service.last_error is None


def test_expiry_removal_triggers_overlay_refresh():
    cards = CardService()
    shown_at = datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc)
    cards.upsert(_card("guard"), shown_at=shown_at)
    layouts = MutableLayoutSource()
    lifecycle = RecordingLifecycle()
    service = CardOverlaySyncService(cards, layouts, lifecycle)
    service.start()
    lifecycle.layouts.clear()

    cards.remove_expired(shown_at + timedelta(seconds=29))
    cards.remove_expired(shown_at + timedelta(seconds=30))

    assert lifecycle.layouts == [CardOverlayLayout()]


def test_stop_unsubscribes_and_closes_all_windows_once():
    cards = CardService()
    layouts = MutableLayoutSource()
    lifecycle = RecordingLifecycle()
    service = CardOverlaySyncService(cards, layouts, lifecycle)
    service.start()
    calls_before_stop = layouts.calls

    service.stop()
    cards.upsert(_card("guard"))
    service.stop()

    assert service.running is False
    assert layouts.calls == calls_before_stop
    assert lifecycle.close_all_calls == 1


def test_repeated_start_does_not_subscribe_twice():
    cards = CardService()
    layouts = MutableLayoutSource()
    lifecycle = RecordingLifecycle()
    service = CardOverlaySyncService(cards, layouts, lifecycle)

    service.start()
    service.start()
    layouts.calls = 0
    cards.upsert(_card("guard"))

    assert layouts.calls == 1


def test_overlay_failure_does_not_interrupt_card_state_changes_and_can_recover():
    cards = CardService()
    layouts = MutableLayoutSource()
    layouts.error = RuntimeError("Windows 浮層暫時不可用")
    lifecycle = RecordingLifecycle()
    service = CardOverlaySyncService(cards, layouts, lifecycle)
    service.start()

    result = cards.upsert(_card("guard"))

    assert result.card_id == "guard"
    assert cards.cards == (result,)
    assert isinstance(service.last_error, RuntimeError)
    layouts.error = None
    assert service.refresh() is True
    assert service.last_error is None
