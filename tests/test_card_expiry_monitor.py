from datetime import datetime, timedelta, timezone

from cards.models import GroupCard
from cards.service import CardService
from cards.settings import CardDisplaySettings
from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.group import CharacterGroup
from services.card_expiry_monitor import CARD_EXPIRY_CHECK_MS, CardExpiryMonitor


def _card() -> GroupCard:
    return GroupCard(
        card_id="guard",
        group=CharacterGroup(group_id="14-windows", name="14支"),
        activity=ActivityDefinition(
            activity_id="guard",
            name="守紀",
            activity_type=ActivityType.DAILY,
            reset_rule=ResetRule.DAILY_MIDNIGHT,
        ),
        current_progress="守紀中斷",
    )


class _Schedule:
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, delay_ms, callback) -> None:
        self.calls.append((delay_ms, callback))

    def run_next(self) -> None:
        _delay_ms, callback = self.calls.pop(0)
        callback()


def test_monitor_removes_card_at_thirty_seconds_and_triggers_change_notice():
    shown_at = datetime(2026, 7, 14, 13, 0, tzinfo=timezone.utc)
    cards = CardService()
    cards.upsert(_card(), shown_at=shown_at)
    changes = []
    cards.subscribe(lambda: changes.append(cards.cards))
    schedule = _Schedule()
    monitor = CardExpiryMonitor(
        cards,
        schedule,
        now=lambda: shown_at + timedelta(seconds=30),
    )

    monitor.start()
    schedule.run_next()

    assert cards.cards == ()
    assert changes == [()]
    assert schedule.calls[0][0] == CARD_EXPIRY_CHECK_MS


def test_monitor_keeps_card_before_expiry_and_schedules_next_check():
    shown_at = datetime(2026, 7, 14, 13, 0, tzinfo=timezone.utc)
    cards = CardService()
    card = _card()
    cards.upsert(card, shown_at=shown_at)
    schedule = _Schedule()
    monitor = CardExpiryMonitor(
        cards,
        schedule,
        now=lambda: shown_at + timedelta(seconds=29),
    )

    monitor.start()
    schedule.run_next()

    assert cards.cards == (card,)
    assert len(schedule.calls) == 1


def test_monitor_uses_configured_card_lifetime():
    shown_at = datetime(2026, 7, 14, 13, 0, tzinfo=timezone.utc)
    cards = CardService(CardDisplaySettings(lifetime_seconds=45))
    card = _card()
    cards.upsert(card, shown_at=shown_at)
    schedule = _Schedule()
    current_time = shown_at + timedelta(seconds=44)
    monitor = CardExpiryMonitor(
        cards,
        schedule,
        now=lambda: current_time,
    )

    monitor.start()
    schedule.run_next()
    assert cards.cards == (card,)

    current_time = shown_at + timedelta(seconds=45)
    schedule.run_next()
    assert cards.cards == ()


def test_start_is_idempotent_and_stop_prevents_pending_check():
    cards = CardService()
    schedule = _Schedule()
    monitor = CardExpiryMonitor(cards, schedule)

    monitor.start()
    monitor.start()
    monitor.stop()
    schedule.run_next()

    assert len(schedule.calls) == 0
    assert monitor.running is False
