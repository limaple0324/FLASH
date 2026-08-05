from datetime import datetime
from pathlib import Path

from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.progress import ActivityInterruptionReason, TAIPEI_TIMEZONE
from domain.progress_store import ActivityProgressStore
from main import (
    handle_group_role_status_change,
    resolve_group_role_progress_subject_id,
    route_group_role_status_to_activity_progress,
)
from services.activity_progress_service import (
    ACTIVITY_PROGRESS_CHANGED_EVENT,
    ActivityProgressService,
)
from services.event_bus import EventBus
from services.group_launch_service import GroupLaunchPlan, GroupLaunchTarget
from services.group_role_status_service import (
    GroupRoleStatus,
    GroupRoleStatusChange,
    ROLE_STATUS_CHECK_DISABLED,
    ROLE_STATUS_CLOSED,
    ROLE_STATUS_DISCONNECTED,
    ROLE_STATUS_FAILED,
    ROLE_STATUS_OPEN,
    ROLE_STATUS_RECONNECTING,
)
from services.group_selection_service import (
    PlayerGroupChoice,
    PlayerGroupMember,
)


_FINGERPRINT_A = "a" * 64
_FINGERPRINT_B = "b" * 64


class _LaunchPlans:
    def __init__(self, plan: GroupLaunchPlan):
        self.plan_value = plan

    def plan(self, group_name: str) -> GroupLaunchPlan:
        assert group_name == "group-a"
        return self.plan_value


class _Choices:
    def __init__(self, values):
        self.values = tuple(values)

    def choices(self):
        return self.values


def _target(
    fingerprint: str = _FINGERPRINT_A,
    entry_id: str = "entry-a",
) -> GroupLaunchTarget:
    return GroupLaunchTarget(
        order=1,
        display_name="role-a",
        shortcut_path=Path("role-a.lnk"),
        fingerprint=fingerprint,
        entry_id=entry_id,
    )


def _choice(
    *,
    entry_id: str = "entry-a",
    character_id: str | None = "character-a",
) -> PlayerGroupChoice:
    return PlayerGroupChoice(
        group_id="group-id-a",
        name="group-a",
        character_count=1,
        members=(
            PlayerGroupMember(
                entry_id=entry_id,
                display_name="role-a",
                role="main",
                character_id=character_id,
            ),
        ),
    )


def _change(status: str) -> GroupRoleStatusChange:
    return GroupRoleStatusChange(
        group_name="group-a",
        previous_status=None,
        current=GroupRoleStatus(
            action_id=_FINGERPRINT_A,
            display_name="role-a",
            status=status,
            order=1,
        ),
    )


def _service(tmp_path) -> ActivityProgressService:
    service = ActivityProgressService(
        ActivityProgressStore(tmp_path / "activity_progress.json")
    )
    service.register_definition(
        ActivityDefinition(
            activity_id="farm",
            name="farm",
            activity_type=ActivityType.DAILY,
            reset_rule=ResetRule.DAILY_MIDNIGHT,
            max_completions=2,
        )
    )
    service.start(
        "farm",
        "character-a",
        datetime(2026, 7, 11, 20, 0, tzinfo=TAIPEI_TIMEZONE),
    )
    return service


def test_group_role_subject_resolution_requires_unique_nonempty_registered_identity():
    change = _change(ROLE_STATUS_DISCONNECTED)
    assert resolve_group_role_progress_subject_id(
        change,
        group_launch_service=_LaunchPlans(
            GroupLaunchPlan("group-a", targets=(_target(),))
        ),
        group_selection_service=_Choices((_choice(),)),
    ) == "character-a"
    assert resolve_group_role_progress_subject_id(
        change,
        group_launch_service=_LaunchPlans(
            GroupLaunchPlan("group-a", targets=(_target(),))
        ),
        group_selection_service=_Choices(()),
    ) is None
    assert resolve_group_role_progress_subject_id(
        change,
        group_launch_service=_LaunchPlans(
            GroupLaunchPlan("group-a", targets=(_target(),))
        ),
        group_selection_service=_Choices((_choice(character_id=""),)),
    ) is None
    assert resolve_group_role_progress_subject_id(
        change,
        group_launch_service=_LaunchPlans(
            GroupLaunchPlan(
                "group-a",
                targets=(
                    _target(),
                    GroupLaunchTarget(
                        order=2,
                        display_name="role-b",
                        shortcut_path=Path("role-b.lnk"),
                        fingerprint=_FINGERPRINT_B,
                        entry_id="entry-a",
                    ),
                ),
            )
        ),
        group_selection_service=_Choices((_choice(),)),
    ) is None
    assert resolve_group_role_progress_subject_id(
        change,
        group_launch_service=_LaunchPlans(
            GroupLaunchPlan("group-a", targets=(_target(),))
        ),
        group_selection_service=_Choices((_choice(), _choice())),
    ) is None


def test_group_status_routes_disconnect_reconnect_closed_and_open(tmp_path):
    service = _service(tmp_path)
    resolver = lambda _change: "character-a"
    disconnected_at = datetime(2026, 7, 11, 20, 5, tzinfo=TAIPEI_TIMEZONE)
    reopened_at = datetime(2026, 7, 11, 20, 6, tzinfo=TAIPEI_TIMEZONE)
    reconnecting_at = datetime(2026, 7, 11, 20, 7, tzinfo=TAIPEI_TIMEZONE)
    closed_at = datetime(2026, 7, 11, 20, 8, tzinfo=TAIPEI_TIMEZONE)

    route_group_role_status_to_activity_progress(
        _change(ROLE_STATUS_DISCONNECTED),
        activity_progress_service=service,
        subject_id_resolver=resolver,
        occurred_at=disconnected_at,
    )
    assert service.get("farm", "character-a").interruption.reason is (
        ActivityInterruptionReason.DISCONNECTED
    )
    assert (
        service.get("farm", "character-a").interruption.occurred_at
        == disconnected_at
    )
    route_group_role_status_to_activity_progress(
        _change(ROLE_STATUS_OPEN),
        activity_progress_service=service,
        subject_id_resolver=resolver,
        occurred_at=reopened_at,
    )
    assert service.get("farm", "character-a").interruption is None
    route_group_role_status_to_activity_progress(
        _change(ROLE_STATUS_RECONNECTING),
        activity_progress_service=service,
        subject_id_resolver=resolver,
        occurred_at=reconnecting_at,
    )
    assert service.get("farm", "character-a").interruption.reason is (
        ActivityInterruptionReason.DISCONNECTED
    )
    assert (
        service.get("farm", "character-a").interruption.occurred_at
        == reconnecting_at
    )
    route_group_role_status_to_activity_progress(
        _change(ROLE_STATUS_CLOSED),
        activity_progress_service=service,
        subject_id_resolver=resolver,
        occurred_at=closed_at,
    )
    assert service.get("farm", "character-a").interruption.reason is (
        ActivityInterruptionReason.GAME_CLOSED
    )
    assert (
        service.get("farm", "character-a").interruption.occurred_at
        == closed_at
    )
    route_group_role_status_to_activity_progress(
        _change(ROLE_STATUS_OPEN),
        activity_progress_service=service,
        subject_id_resolver=resolver,
        occurred_at=datetime(2026, 7, 11, 20, 9, tzinfo=TAIPEI_TIMEZONE),
    )
    assert service.get("farm", "character-a").interruption is None


def test_non_group_or_unresolved_status_does_not_change_activity_progress(tmp_path):
    """Only closing 輔 emits no game-role status, so progress stays unchanged."""
    service = _service(tmp_path)
    before = service.get("farm", "character-a")
    at = datetime(2026, 7, 11, 20, 5, tzinfo=TAIPEI_TIMEZONE)
    only_closing_fu_non_game_status_event = object()

    assert route_group_role_status_to_activity_progress(
        only_closing_fu_non_game_status_event,
        activity_progress_service=service,
        subject_id_resolver=lambda _change: "character-a",
        occurred_at=at,
    ) == ()
    assert route_group_role_status_to_activity_progress(
        _change(ROLE_STATUS_FAILED),
        activity_progress_service=service,
        subject_id_resolver=lambda _change: "character-a",
        occurred_at=at,
    ) == ()
    assert route_group_role_status_to_activity_progress(
        _change(ROLE_STATUS_CHECK_DISABLED),
        activity_progress_service=service,
        subject_id_resolver=lambda _change: "character-a",
        occurred_at=at,
    ) == ()
    assert route_group_role_status_to_activity_progress(
        _change(ROLE_STATUS_DISCONNECTED),
        activity_progress_service=service,
        subject_id_resolver=lambda _change: None,
        occurred_at=at,
    ) == ()
    assert service.get("farm", "character-a") == before

    class FailingStore(ActivityProgressStore):
        def __init__(self, path):
            super().__init__(path)
            self.fail_saves = False

        def save(self, progress):
            if self.fail_saves:
                raise OSError("progress save failed")
            super().save(progress)

    class Logger:
        def __init__(self):
            self.messages = []

        def error(self, message):
            self.messages.append(message)

    failing_store = FailingStore(tmp_path / "failed_activity_progress.json")
    event_bus = EventBus()
    progress_changes = []
    event_bus.subscribe(
        ACTIVITY_PROGRESS_CHANGED_EVENT,
        progress_changes.append,
    )
    failing_service = ActivityProgressService(failing_store, event_bus)
    failing_service.register_definition(
        ActivityDefinition(
            activity_id="farm",
            name="farm",
            activity_type=ActivityType.DAILY,
            reset_rule=ResetRule.DAILY_MIDNIGHT,
            max_completions=2,
        )
    )
    failing_service.start("farm", "character-a", at)
    progress_changes.clear()
    before_failed_route = failing_service.get("farm", "character-a")
    failing_store.fail_saves = True
    logger = Logger()
    card_changes = []
    change = _change(ROLE_STATUS_DISCONNECTED)

    assert handle_group_role_status_change(
        change,
        activity_progress_service=failing_service,
        subject_id_resolver=lambda _change: "character-a",
        occurred_at=at,
        logger=logger,
        on_role_status_card=card_changes.append,
    ) == ()
    assert failing_service.get("farm", "character-a") == before_failed_route
    assert progress_changes == []
    assert card_changes == [change]
    assert len(logger.messages) == 1


def test_new_activity_status_routes_are_isolated_from_existing_status_card(tmp_path):
    class Logger:
        def __init__(self):
            self.messages = []

        def error(self, message):
            self.messages.append(message)

    change = _change(ROLE_STATUS_DISCONNECTED)
    logger = Logger()
    cards = []

    def fail_farm(_change):
        raise OSError("farm route failed")

    def fail_confirmed(_change):
        raise OSError("confirmed route failed")

    assert handle_group_role_status_change(
        change,
        activity_progress_service=_service(tmp_path),
        subject_id_resolver=lambda _change: None,
        occurred_at=datetime(2026, 7, 11, 20, 5, tzinfo=TAIPEI_TIMEZONE),
        logger=logger,
        on_role_status_card=cards.append,
        on_farm_timer_status=fail_farm,
        on_confirmed_activity_status=fail_confirmed,
    ) == ()
    assert cards == [change]
    assert len(logger.messages) == 2
