import json

from adapters.windows_window import WindowInfo
from core.reconnect_policy import ReconnectScreenState
from core.target_window_contract import (
    TargetWindowContract,
    TargetWindowPhase,
    TargetWindowSnapshot,
)
from services.group_launch_service import GroupLaunchService
from services.group_role_status_service import (
    GroupRoleStatusService,
    ROLE_STATUS_CLOSED,
    ROLE_STATUS_DISCONNECTED,
    ROLE_STATUS_FAILED,
    ROLE_STATUS_OPEN,
    ROLE_STATUS_RECONNECTING,
)
from services.reconnect_failure_status_service import (
    ReconnectFailureStatusService,
)


class Resolver:
    def resolve(self, paths):
        return {
            path: f"{index:064x}"
            for index, path in enumerate(paths, start=1)
        }


class Windows:
    def __init__(self, values=()):
        self.values = list(values)

    def list_windows(self):
        return list(self.values)

    def foreground_handle(self):
        return None

    def top_window_at(self, _x, _y):
        return None


class FailingWindows(Windows):
    def list_windows(self):
        raise AssertionError("central snapshot must be the only enumeration")


class Activator:
    def __init__(self, result=True):
        self.result = result
        self.handles = []

    def activate(self, handle):
        self.handles.append(handle)
        return self.result


class Opener:
    def __init__(self, result=True):
        self.result = result
        self.targets = []

    def open_shortcut(self, target):
        self.targets.append(target)
        return self.result


def configuration(tmp_path):
    shortcuts = []
    for name in ("甲", "乙"):
        path = tmp_path / f"{name}.lnk"
        path.touch()
        shortcuts.append(path)
    config = tmp_path / "groups.json"
    config.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "name": "兩支",
                        "launch_entries": [
                            {"path": str(path)} for path in shortcuts
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return GroupLaunchService(config, Resolver())


def window(handle, fingerprint):
    return WindowInfo(
        handle,
        "Adobe Flash Player 11",
        True,
        False,
        (0, 0, 100, 100),
        process_id=handle + 100,
        launch_fingerprint=fingerprint,
    )


def test_ordered_rows_show_open_closed_disconnect_and_reconnect(tmp_path):
    launch = configuration(tmp_path)
    plan = launch.plan("兩支")
    windows = Windows((window(1, plan.targets[0].fingerprint),))
    states = {plan.targets[0].fingerprint: ReconnectScreenState.DISCONNECTED}
    reconnecting = set()
    service = GroupRoleStatusService(
        launch,
        windows,
        ReconnectFailureStatusService(),
        screen_states_provider=lambda: states,
        reconnecting_provider=lambda: reconnecting,
    )

    rows = service.refresh("兩支")
    assert [row.display_name for row in rows] == ["甲", "乙"]
    assert [row.status for row in rows] == [
        ROLE_STATUS_DISCONNECTED,
        ROLE_STATUS_CLOSED,
    ]

    states.clear()
    reconnecting.add(plan.targets[0].fingerprint)
    assert service.refresh("兩支")[0].status == ROLE_STATUS_RECONNECTING
    reconnecting.clear()
    assert service.refresh("兩支")[0].status == ROLE_STATUS_OPEN


def test_failure_status_wins_and_duplicate_window_fails_closed(tmp_path):
    launch = configuration(tmp_path)
    plan = launch.plan("兩支")
    failure = ReconnectFailureStatusService()
    failure.report(f"role:{plan.targets[0].fingerprint}", "甲")
    windows = Windows(
        (
            window(1, plan.targets[0].fingerprint),
            window(2, plan.targets[1].fingerprint),
            window(3, plan.targets[1].fingerprint),
        )
    )
    service = GroupRoleStatusService(launch, windows, failure)

    rows = service.refresh("兩支")
    assert [row.status for row in rows] == [
        ROLE_STATUS_FAILED,
        ROLE_STATUS_FAILED,
    ]


def test_click_open_activates_only_exact_role(tmp_path):
    launch = configuration(tmp_path)
    plan = launch.plan("兩支")
    activator = Activator()
    opener = Opener()
    service = GroupRoleStatusService(
        launch,
        Windows((window(7, plan.targets[0].fingerprint),)),
        ReconnectFailureStatusService(),
        activation_backend=activator,
        shortcut_open_backend=opener,
    )

    result = service.activate_or_launch(
        "兩支",
        plan.targets[0].fingerprint,
    )
    assert result.success is True
    assert result.action == "activated"
    assert activator.handles == [7]
    assert opener.targets == []


def test_click_closed_launches_only_exact_shortcut(tmp_path):
    launch = configuration(tmp_path)
    plan = launch.plan("兩支")
    opener = Opener()
    service = GroupRoleStatusService(
        launch,
        Windows(),
        ReconnectFailureStatusService(),
        activation_backend=Activator(),
        shortcut_open_backend=opener,
        monotonic_clock=lambda: 5.0,
    )

    result = service.activate_or_launch(
        "兩支",
        plan.targets[1].fingerprint,
    )
    assert result.success is True
    assert result.action == "launched"
    assert opener.targets == [plan.targets[1]]
    assert service.refresh("兩支")[1].status == ROLE_STATUS_RECONNECTING


def test_unknown_existing_window_prevents_duplicate_launch(tmp_path):
    launch = configuration(tmp_path)
    plan = launch.plan("兩支")
    opener = Opener()
    failure = ReconnectFailureStatusService()
    service = GroupRoleStatusService(
        launch,
        Windows((window(9, None),)),
        failure,
        activation_backend=Activator(),
        shortcut_open_backend=opener,
    )

    result = service.activate_or_launch(
        "兩支",
        plan.targets[1].fingerprint,
    )
    assert result.success is False
    assert result.failure_code == "role_existing_window_unknown"
    assert opener.targets == []
    assert failure.has(f"role:{plan.targets[1].fingerprint}")


def test_status_and_single_role_action_use_only_central_snapshot(tmp_path):
    launch = configuration(tmp_path)
    plan = launch.plan("兩支")
    contracts = (
        TargetWindowContract(
            1,
            "兩支",
            "兩支:甲",
            "甲",
            "主窗口",
            None,
            "甲",
            101,
            plan.targets[0].fingerprint,
            TargetWindowPhase.FOREGROUND,
            True,
            handle=7,
            rect=(0, 0, 100, 100),
            visible=True,
        ),
        TargetWindowContract(
            1,
            "兩支",
            "兩支:乙",
            "乙",
            "同步窗口",
            None,
            "乙",
            None,
            plan.targets[1].fingerprint,
            TargetWindowPhase.OFFLINE,
            False,
            failure_codes=("window_offline",),
        ),
    )
    snapshot = TargetWindowSnapshot(1, "兩支", contracts)
    activator = Activator()
    service = GroupRoleStatusService(
        launch,
        FailingWindows(),
        ReconnectFailureStatusService(),
        activation_backend=activator,
        target_snapshot_provider=lambda _name: snapshot,
    )

    rows = service.refresh("兩支")
    activated = service.activate_or_launch(
        "兩支",
        plan.targets[0].fingerprint,
    )

    assert [row.status for row in rows] == [
        ROLE_STATUS_OPEN,
        ROLE_STATUS_CLOSED,
    ]
    assert activated.success is True
    assert activator.handles == [7]

