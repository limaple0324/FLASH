from datetime import datetime

from cards.history_store import CardHistoryStore
from cards.service import CardService
from domain.activity_schedule import build_confirmed_activity_catalog
from domain.group import CharacterGroup
from domain.progress import TAIPEI_TIMEZONE
from services.activity_reminder_service import ActivityReminderService
from services.card_coordinator import CardCoordinator
from services.card_history_service import CardHistoryService
from workspace.models import WorkspaceState


def _service(tmp_path, *, group=True):
    cards = CardService()
    coordinator = CardCoordinator(
        cards,
        CardHistoryService(CardHistoryStore(tmp_path / "history.json")),
    )
    current_group = (
        CharacterGroup(group_id="group-14", name="14支")
        if group
        else None
    )
    service = ActivityReminderService(
        build_confirmed_activity_catalog(),
        coordinator,
        lambda: WorkspaceState(current_group=current_group),
    )
    return service, cards


def test_hall_of_demons_card_appears_exactly_five_minutes_early(tmp_path):
    service, cards = _service(tmp_path)

    before = service.poll(
        datetime(2026, 7, 27, 12, 54, 59, tzinfo=TAIPEI_TIMEZONE)
    )
    due = service.poll(
        datetime(2026, 7, 27, 12, 55, 0, tzinfo=TAIPEI_TIMEZONE)
    )
    repeated = service.poll(
        datetime(2026, 7, 27, 12, 59, 0, tzinfo=TAIPEI_TIMEZONE)
    )

    assert before == ()
    assert len(due) == 1
    assert due[0].activity.name == "諸魔殿"
    assert due[0].current_progress == "諸魔殿"
    assert due[0].daily_summary is None
    assert due[0].next_step is None
    assert due[0].name_only is True
    assert repeated == ()
    assert cards.cards == due
    assert cards.entries[0].expires_at == datetime(
        2026,
        7,
        27,
        13,
        0,
        tzinfo=TAIPEI_TIMEZONE,
    )


def test_midnight_activities_are_reminded_at_previous_day_2355(tmp_path):
    service, _cards = _service(tmp_path)

    due = service.poll(
        datetime(2026, 7, 27, 23, 55, 0, tzinfo=TAIPEI_TIMEZONE)
    )

    assert {card.activity.name for card in due} == {"迷陣", "魔兵降臨"}
    assert all(card.current_progress == card.activity.name for card in due)
    assert all(card.name_only for card in due)


def test_manual_close_does_not_allow_same_occurrence_to_reappear(tmp_path):
    service, cards = _service(tmp_path)
    now = datetime(2026, 7, 27, 12, 55, tzinfo=TAIPEI_TIMEZONE)
    shown = service.poll(now)

    cards.remove(shown[0].card_id)
    repeated = service.poll(
        datetime(2026, 7, 27, 12, 57, tzinfo=TAIPEI_TIMEZONE)
    )

    assert cards.cards == ()
    assert repeated == ()


def test_simultaneous_activities_create_separate_cards(tmp_path):
    service, cards = _service(tmp_path)

    shown = service.poll(
        datetime(2026, 8, 2, 13, 55, tzinfo=TAIPEI_TIMEZONE)
    )

    assert {card.activity.name for card in shown} == {
        "釣魚大賽",
        "奇石廣場",
    }
    assert len({card.card_id for card in shown}) == 2
    assert len(cards.cards) == 2


def test_no_group_means_no_card_and_no_occurrence_is_consumed(tmp_path):
    service, cards = _service(tmp_path, group=False)

    shown = service.poll(
        datetime(2026, 7, 27, 12, 55, 0, tzinfo=TAIPEI_TIMEZONE)
    )

    assert shown == ()
    assert cards.cards == ()


def test_selected_weekdays_use_photo_schedule(tmp_path):
    service, _cards = _service(tmp_path)

    thursday_world_boss = service.poll(
        datetime(2026, 7, 30, 14, 25, 0, tzinfo=TAIPEI_TIMEZONE)
    )

    assert tuple(card.activity.name for card in thursday_world_boss) == (
        "世界BOSS",
    )
