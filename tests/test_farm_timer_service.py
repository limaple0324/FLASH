from datetime import datetime, timedelta, timezone

import pytest

from cards.history_store import CardHistoryStore
from cards.priority import CardPriorityReason, CardPriorityTier
from cards.service import CardService
from domain.character import Character
from domain.group import CharacterGroup
from domain.progress import TAIPEI_TIMEZONE
from services.card_coordinator import CardCoordinator
from services.card_history_service import CardHistoryService
from services.farm_timer_service import (
    COPY_CODE_ACTION_ID,
    FarmCompleted,
    FarmPlantingConfirmed,
    FarmTimerService,
)
from services.group_role_status_service import (
    ROLE_STATUS_CLOSED,
    ROLE_STATUS_DISCONNECTED,
    ROLE_STATUS_OPEN,
)


def _context(tmp_path):
    characters = (
        Character("role-a", "120古", 120),
        Character("role-b", "120靈", 120),
    )
    group = CharacterGroup("group-14", "14支", characters)
    cards = CardService()
    copied = []
    records = []
    service = FarmTimerService(
        CardCoordinator(
            cards,
            CardHistoryService(CardHistoryStore(tmp_path / "history.json")),
        ),
        state_path=tmp_path / "farm.json",
        clipboard_writer=lambda value: copied.append(value),
        record_callback=lambda *values: records.append(values),
    )
    return service, cards, group, copied, records


def _start(service, group, role, planted_at, code):
    return service.start(
        FarmPlantingConfirmed(
            timer_id=f"timer-{role}",
            group=group,
            character_id=role,
            planted_at=planted_at,
            copy_code=code,
        )
    )


def test_each_character_matures_independently_at_exactly_forty_minutes(tmp_path):
    service, cards, group, _copied, _records = _context(tmp_path)
    planted_at = datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc)
    _start(service, group, "role-a", planted_at, "正確代碼甲")
    _start(
        service,
        group,
        "role-b",
        planted_at + timedelta(minutes=1),
        "正確代碼乙",
    )

    assert service.poll(planted_at + timedelta(minutes=39, seconds=59)) == ()
    first = service.poll(planted_at + timedelta(minutes=40))

    assert len(first) == 1
    assert first[0].affected_character_ids == ("role-a",)
    assert first[0].priority_reason is CardPriorityReason.TIME_LIMIT
    assert cards.cards == first

    second = service.poll(planted_at + timedelta(minutes=41))
    assert len(second) == 1
    assert second[0].affected_character_ids == ("role-b",)
    assert len(cards.cards) == 2

    overnight_service, _overnight_cards, overnight_group, _copied, _records = (
        _context(tmp_path / "overnight")
    )
    overnight_planted_at = datetime(
        2026,
        7,
        29,
        23,
        30,
        tzinfo=TAIPEI_TIMEZONE,
    )
    overnight_timer = _start(
        overnight_service,
        overnight_group,
        "role-a",
        overnight_planted_at,
        "跨日代碼",
    )
    overnight_mature = overnight_service.poll(
        datetime(2026, 7, 30, 0, 10, tzinfo=TAIPEI_TIMEZONE)
    )
    assert len(overnight_mature) == 1
    assert overnight_mature[0].card_id == overnight_timer.card_id


def test_maturity_priority_and_loss_stages_update_the_same_card(tmp_path):
    service, cards, group, _copied, _records = _context(tmp_path)
    planted_at = datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc)
    timer = _start(service, group, "role-a", planted_at, "正確代碼")
    mature = service.poll(planted_at + timedelta(minutes=40))[0]
    elevated = service.poll(planted_at + timedelta(minutes=50))[0]
    highest_risk = service.poll(planted_at + timedelta(minutes=55))[0]
    disappeared = service.poll(planted_at + timedelta(minutes=60))[0]

    assert mature.card_id == timer.card_id == elevated.card_id
    assert elevated.card_id == highest_risk.card_id == disappeared.card_id
    assert elevated.priority_reason is CardPriorityReason.TIME_LIMIT
    assert highest_risk.priority_reason is CardPriorityReason.LOSS_RISK
    assert highest_risk.priority_tier is CardPriorityTier.HIGHEST
    assert disappeared.priority_reason is CardPriorityReason.LOSS_RISK
    assert cards.cards == (disappeared,)
    assert service.poll(planted_at + timedelta(minutes=61)) == ()


def test_card_action_copies_exact_event_code_and_completion_removes_card(tmp_path):
    service, cards, group, copied, records = _context(tmp_path)
    planted_at = datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc)
    timer = _start(service, group, "role-a", planted_at, "/farm exact")
    card = service.poll(planted_at + timedelta(minutes=40))[0]

    result = service.handle_action(card.card_id, COPY_CODE_ACTION_ID)
    completed = service.complete(
        FarmCompleted(timer.timer_id, planted_at + timedelta(minutes=41))
    )

    assert result is True
    assert copied == ["/farm exact"]
    assert completed is True
    assert cards.cards == ()
    assert [item[2] for item in records] == [
        "已開始獨立計時",
        "已成熟",
        "已複製正確代碼",
        "已完成並移除提醒",
    ]


def test_timer_state_survives_restart_without_repeating_same_stage(tmp_path):
    service, _cards, group, _copied, _records = _context(tmp_path)
    planted_at = datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc)
    _start(service, group, "role-a", planted_at, "正確代碼")
    service.poll(planted_at + timedelta(minutes=40))

    reloaded_cards = CardService()
    copied = []
    reloaded = FarmTimerService(
        CardCoordinator(
            reloaded_cards,
            CardHistoryService(CardHistoryStore(tmp_path / "other-history.json")),
        ),
        state_path=tmp_path / "farm.json",
        clipboard_writer=copied.append,
    )

    assert reloaded.poll(planted_at + timedelta(minutes=49)) == ()
    elevated = reloaded.poll(planted_at + timedelta(minutes=50))
    assert len(elevated) == 1
    assert elevated[0].priority_reason is CardPriorityReason.TIME_LIMIT


def test_unconfirmed_or_empty_code_is_rejected_before_timer_exists(tmp_path):
    service, _cards, group, _copied, _records = _context(tmp_path)
    planted_at = datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc)

    try:
        FarmPlantingConfirmed(
            "timer-a",
            group,
            "role-a",
            planted_at,
            " ",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("empty code must be rejected")

    assert not (tmp_path / "farm.json").exists()


def test_game_close_pauses_farm_but_disconnect_does_not_and_reopen_reemits_stage(
    tmp_path,
):
    service, cards, group, _copied, _records = _context(tmp_path)
    planted_at = datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc)
    timer = _start(service, group, "role-a", planted_at, "正確代碼")
    assert service.poll(planted_at + timedelta(minutes=40))
    assert service.handle_role_status(
        "role-a",
        ROLE_STATUS_CLOSED,
        planted_at + timedelta(minutes=41),
    ) is True
    assert cards.cards == ()
    assert service.poll(planted_at + timedelta(minutes=70)) == ()
    assert service.handle_role_status(
        "role-a",
        ROLE_STATUS_OPEN,
        planted_at + timedelta(minutes=70),
    ) is True
    resumed = service.poll(planted_at + timedelta(minutes=70))
    assert len(resumed) == 1
    assert resumed[0].card_id == timer.card_id
    assert service.handle_role_status(
        "role-a",
        ROLE_STATUS_DISCONNECTED,
        planted_at + timedelta(minutes=70),
    ) is False
    elevated = service.poll(planted_at + timedelta(minutes=79))
    assert len(elevated) == 1
    assert elevated[0].priority_reason is CardPriorityReason.TIME_LIMIT


def test_poll_keeps_all_timers_and_cards_unchanged_when_one_atomic_save_fails(
    tmp_path,
    monkeypatch,
):
    service, cards, group, _copied, records = _context(tmp_path)
    planted_at = datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc)
    _start(service, group, "role-a", planted_at, "代碼甲")
    _start(service, group, "role-b", planted_at, "代碼乙")

    def fail_save(_timers):
        raise OSError("save failed")

    original_save = service._save
    monkeypatch.setattr(service, "_save", fail_save)
    with pytest.raises(OSError):
        service.poll(planted_at + timedelta(minutes=40))

    assert cards.cards == ()
    assert [entry[2] for entry in records] == ["已開始獨立計時", "已開始獨立計時"]
    monkeypatch.setattr(service, "_save", original_save)
    retried = service.poll(planted_at + timedelta(minutes=40))
    assert {card.affected_character_ids for card in retried} == {
        ("role-a",),
        ("role-b",),
    }
