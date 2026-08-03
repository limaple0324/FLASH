from datetime import timedelta

from cards.priority import CardPriorityReason
from decision.models import (
    DecisionCandidate,
    DecisionCategory,
    DecisionOutput,
)
from decision.service import DecisionService
from domain.character import CharacterImportance
from main import build_services
from services.app_context import AppContext


def _candidate(candidate_id: str, **changes) -> DecisionCandidate:
    values = {
        "candidate_id": candidate_id,
        "priority_reason": CardPriorityReason.ACTIVITY,
    }
    values.update(changes)
    return DecisionCandidate(**values)


def test_unknown_evidence_and_player_cancellation_fail_closed():
    service = DecisionService()

    unknown, cancelled = service.decide(
        (
            _candidate(
                "unknown",
                evidence_confirmed=False,
                requires_player_action=True,
                is_important_today=True,
            ),
            _candidate(
                "cancelled",
                player_cancelled=True,
                requires_player_action=True,
                is_important_today=True,
            ),
        )
    )

    assert {unknown.output, cancelled.output} == {DecisionOutput.QUIET}
    assert {unknown.reason_code, cancelled.reason_code} == {
        "evidence.unknown",
        "player.cancelled",
    }


def test_safety_loss_risk_and_time_remind_in_fixed_common_order():
    service = DecisionService()
    results = service.decide(
        (
            _candidate(
                "disconnect",
                priority_reason=CardPriorityReason.DISCONNECTION,
                context_permits_notification=False,
            ),
            _candidate(
                "time",
                priority_reason=CardPriorityReason.TIME_LIMIT,
                context_permits_notification=False,
            ),
            _candidate(
                "loss",
                priority_reason=CardPriorityReason.LOSS_RISK,
                context_permits_notification=False,
            ),
        )
    )

    assert [item.candidate_id for item in results] == ["disconnect", "loss", "time"]
    assert all(item.output is DecisionOutput.REMIND for item in results)


def test_current_focus_stays_quiet_while_player_is_proceeding():
    result = DecisionService().decide(
        (_candidate("focus", is_current_focus=True),)
    )[0]

    assert result.output is DecisionOutput.QUIET
    assert result.reason_code == "focus.proceeding"


def test_recovery_group_and_today_activity_require_action_before_reminding():
    service = DecisionService()
    results = service.decide(
        (
            _candidate(
                "recovery",
                interrupted_recoverable=True,
                requires_player_action=True,
            ),
            _candidate(
                "group",
                is_current_group_progress=True,
                requires_player_action=True,
            ),
            _candidate(
                "today",
                is_important_today=True,
                requires_player_action=False,
            ),
        )
    )
    by_id = {item.candidate_id: item for item in results}

    assert by_id["recovery"].output is DecisionOutput.REMIND
    assert by_id["recovery"].category is DecisionCategory.INTERRUPTED_RECOVERY
    assert by_id["group"].output is DecisionOutput.REMIND
    assert by_id["today"].output is DecisionOutput.QUIET


def test_recovery_without_required_player_action_stays_quiet():
    result = DecisionService().decide(
        (
            _candidate(
                "recovered",
                priority_reason=CardPriorityReason.RECOVERY,
                requires_player_action=False,
            ),
        )
    )[0]

    assert result.output is DecisionOutput.QUIET
    assert result.category is DecisionCategory.QUIET
    assert result.reason_code == "action.not_required"


def test_deferrable_and_optional_items_are_suggestions_not_commands():
    results = DecisionService().decide(
        (
            _candidate("later", is_deferrable=True),
            _candidate("optional", suggestion_only=True),
        )
    )

    assert all(item.output is DecisionOutput.SUGGEST for item in results)


def test_general_information_is_a_visible_notification_not_a_suggestion():
    result = DecisionService().decide(
        (
            _candidate(
                "general",
                priority_reason=CardPriorityReason.GENERAL,
            ),
        )
    )[0]

    assert result.output is DecisionOutput.REMIND
    assert result.category is DecisionCategory.GENERAL_INFORMATION


def test_unclassified_activity_fails_closed_to_quiet():
    result = DecisionService().decide((_candidate("activity"),))[0]

    assert result.output is DecisionOutput.QUIET
    assert result.category is DecisionCategory.QUIET


def test_unchanged_reminder_does_not_repeat():
    result = DecisionService().decide(
        (
            _candidate(
                "same",
                is_important_today=True,
                requires_player_action=True,
                has_new_information=False,
                already_reminded_without_change=True,
            ),
        )
    )[0]

    assert result.output is DecisionOutput.QUIET
    assert result.reason_code == "reminder.unchanged"


def test_same_layer_sort_is_time_then_character_importance_then_stable_id():
    results = DecisionService().decide(
        (
            _candidate(
                "secondary-later",
                priority_reason=CardPriorityReason.TIME_LIMIT,
                remaining_time=timedelta(minutes=10),
            ),
            _candidate(
                "reserve-soon",
                priority_reason=CardPriorityReason.TIME_LIMIT,
                character_importance=CharacterImportance.RESERVE,
                remaining_time=timedelta(minutes=2),
            ),
            _candidate(
                "primary-soon-b",
                priority_reason=CardPriorityReason.TIME_LIMIT,
                character_importance=CharacterImportance.PRIMARY,
                remaining_time=timedelta(minutes=2),
            ),
            _candidate(
                "primary-soon-a",
                priority_reason=CardPriorityReason.TIME_LIMIT,
                character_importance=CharacterImportance.PRIMARY,
                remaining_time=timedelta(minutes=2),
            ),
        )
    )

    assert [item.candidate_id for item in results] == [
        "primary-soon-a",
        "primary-soon-b",
        "reserve-soon",
        "secondary-later",
    ]


def test_build_services_registers_decision_service(tmp_path):
    build_services(root=tmp_path)

    assert isinstance(AppContext.get(DecisionService), DecisionService)
