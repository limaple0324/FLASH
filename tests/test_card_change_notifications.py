from datetime import datetime, timedelta, timezone
import threading

from cards.models import GroupCard
from cards.service import CardService
from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.group import CharacterGroup


def _card(card_id: str, progress: str | None = None) -> GroupCard:
    return GroupCard(
        card_id=card_id,
        group=CharacterGroup(group_id="14-windows", name="14支"),
        activity=ActivityDefinition(
            activity_id="guard",
            name="守紀",
            activity_type=ActivityType.DAILY,
            reset_rule=ResetRule.DAILY_MIDNIGHT,
        ),
        current_progress=progress or card_id,
    )


def test_card_changes_notify_home_refresh_listener():
    service = CardService()
    changes: list[tuple[str, ...]] = []
    service.subscribe(
        lambda: changes.append(tuple(card.card_id for card in service.cards))
    )

    service.upsert(_card("guard"))
    service.upsert(_card("guard", "守紀進度更新"))
    service.complete("guard")

    assert changes == [("guard",), ("guard",), ()]


def test_missing_remove_is_quiet_and_queued_fourth_card_triggers_refresh():
    service = CardService()
    notifications = 0

    def record_change() -> None:
        nonlocal notifications
        notifications += 1

    service.subscribe(record_change)
    for card_id in ("first", "second", "third"):
        service.upsert(_card(card_id))
    baseline = notifications

    assert service.remove("missing") is None
    service.upsert(_card("fourth"))

    assert notifications == baseline + 1
    assert service.pending_entries[0].card.card_id == "fourth"


def test_expiry_notifies_once_only_when_cards_are_removed():
    service = CardService()
    shown_at = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)
    notifications = []
    service.upsert(_card("guard"), shown_at=shown_at)
    service.subscribe(lambda: notifications.append(service.cards))

    service.remove_expired(shown_at + timedelta(seconds=29))
    service.remove_expired(shown_at + timedelta(seconds=30))

    assert notifications == [()]


def test_unsubscribe_stops_future_refresh_notifications():
    service = CardService()
    notifications = []

    def record_change() -> None:
        notifications.append(True)

    service.subscribe(record_change)
    service.upsert(_card("first"))
    service.unsubscribe(record_change)
    service.remove("first")

    assert notifications == [True]


def test_parallel_updates_converge_to_one_consistent_snapshot():
    service = CardService()
    barrier = threading.Barrier(9)

    def add_card(index):
        barrier.wait()
        service.upsert(_card(f"parallel-{index}"))

    threads = [
        threading.Thread(target=add_card, args=(index,))
        for index in range(8)
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    snapshot = service.snapshot()
    all_ids = tuple(
        card.card_id
        for card in snapshot.visible_cards + snapshot.pending_cards
    )
    assert len(all_ids) == 8
    assert len(set(all_ids)) == 8
    assert len(snapshot.visible_cards) == 3
    assert snapshot.revision == 8
