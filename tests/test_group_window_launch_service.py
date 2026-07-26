import json

from adapters.windows_window import WindowInfo
from services.group_launch_service import GroupLaunchService
from services.group_window_launch_service import GroupWindowLaunchService


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


class Opener:
    def __init__(self, windows):
        self.windows = windows
        self.targets = []

    def open_shortcut(self, target):
        self.targets.append(target)
        self.windows.values.append(
            WindowInfo(
                handle=100 + target.order,
                title="Adobe Flash Player 11",
                visible=True,
                minimized=False,
                rect=(0, 0, 100, 100),
                process_id=200 + target.order,
                launch_fingerprint=target.fingerprint,
            )
        )
        return True


class Placer:
    def __init__(self):
        self.calls = []

    def place(self, handle, placement):
        self.calls.append((handle, placement))
        return True


def launch_service(tmp_path, *, with_layout=True):
    entries = []
    for index, name in enumerate(("甲", "乙"), start=1):
        shortcut = tmp_path / f"{name}.lnk"
        shortcut.touch()
        entry = {"path": str(shortcut)}
        if with_layout:
            entry.update(
                {
                    "x": -1000 + index,
                    "y": index * 10,
                    "width": 916,
                    "height": 629,
                    "delay_ms": 0,
                }
            )
        entries.append(entry)
    path = tmp_path / "groups.json"
    path.write_text(
        json.dumps(
            {
                "groups": [
                    {"name": "兩支", "launch_entries": entries}
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return GroupLaunchService(path, Resolver())


def open_window(handle, fingerprint):
    return WindowInfo(
        handle=handle,
        title="Adobe Flash Player 11",
        visible=True,
        minimized=False,
        rect=(0, 0, 100, 100),
        process_id=handle + 100,
        launch_fingerprint=fingerprint,
    )


def test_launches_only_missing_roles_and_restores_every_saved_position(
    tmp_path,
):
    launch = launch_service(tmp_path)
    plan = launch.plan("兩支")
    windows = Windows((open_window(1, plan.targets[0].fingerprint),))
    opener = Opener(windows)
    placer = Placer()
    service = GroupWindowLaunchService(
        launch,
        windows,
        shortcut_open_backend=opener,
        placement_backend=placer,
        sleeper=lambda _seconds: None,
    )

    result = service._run("兩支")

    assert result.success is True
    assert result.launched_count == 1
    assert result.restored_count == 2
    assert opener.targets == [plan.targets[1]]
    assert [call[0] for call in placer.calls] == [1, 102]
    assert placer.calls[0][1].x == -999
    assert placer.calls[1][1].x == -998


def test_unknown_existing_game_window_stops_before_launch(tmp_path):
    launch = launch_service(tmp_path)
    windows = Windows((open_window(9, None),))
    opener = Opener(windows)
    placer = Placer()
    service = GroupWindowLaunchService(
        launch,
        windows,
        shortcut_open_backend=opener,
        placement_backend=placer,
    )

    result = service._run("兩支")

    assert result.success is False
    assert result.failure_code == "group_existing_window_unknown"
    assert opener.targets == []
    assert placer.calls == []


def test_missing_saved_layout_is_reported_without_guessing(tmp_path):
    launch = launch_service(tmp_path, with_layout=False)
    plan = launch.plan("兩支")
    windows = Windows(
        (
            open_window(1, plan.targets[0].fingerprint),
            open_window(2, plan.targets[1].fingerprint),
        )
    )
    placer = Placer()
    service = GroupWindowLaunchService(
        launch,
        windows,
        placement_backend=placer,
    )

    result = service._run("兩支")

    assert result.success is False
    assert result.failure_code == "group_layout_unavailable"
    assert result.launched_count == 0
    assert result.restored_count == 0
    assert placer.calls == []


def test_restore_only_never_launches_missing_window(tmp_path):
    launch = launch_service(tmp_path)
    plan = launch.plan("兩支")
    windows = Windows((open_window(1, plan.targets[0].fingerprint),))
    opener = Opener(windows)
    service = GroupWindowLaunchService(
        launch,
        windows,
        shortcut_open_backend=opener,
        placement_backend=Placer(),
    )

    result = service._run_restore("兩支")

    assert result.success is False
    assert result.action == "restore"
    assert result.failure_code == "group_window_missing"
    assert opener.targets == []


def test_records_all_current_exact_window_positions_in_one_save(tmp_path):
    launch = launch_service(tmp_path)
    plan = launch.plan("兩支")
    windows = Windows(
        (
            WindowInfo(
                1,
                "Adobe Flash Player 11",
                True,
                False,
                (-2000, 20, -1084, 649),
                process_id=101,
                launch_fingerprint=plan.targets[0].fingerprint,
            ),
            WindowInfo(
                2,
                "Adobe Flash Player 11",
                True,
                False,
                (100, 200, 1016, 829),
                process_id=102,
                launch_fingerprint=plan.targets[1].fingerprint,
            ),
        )
    )
    saved = []
    service = GroupWindowLaunchService(
        launch,
        windows,
        placement_update_callback=lambda group, values: (
            saved.append((group, values)) or True
        ),
    )

    result = service._run_record("兩支")

    assert result.success is True
    assert result.action == "record"
    assert len(saved) == 1
    assert saved[0][0] == "兩支"
    assert saved[0][1][plan.targets[0].shortcut_path].x == -2000
    assert saved[0][1][plan.targets[0].shortcut_path].width == 916
    assert saved[0][1][plan.targets[1].shortcut_path].y == 200
