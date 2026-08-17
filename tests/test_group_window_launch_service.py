import json

import pytest

from adapters.windows_window import WindowInfo
from core.window_registry import WindowRegistry
from core.window_registry_store import WindowRegistryStore
from services.group_launch_service import GroupLaunchService
from services.group_window_launch_service import GroupWindowLaunchService
from services.managed_game_process_service import (
    ManagedGameProcessService,
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


class Opener:
    def __init__(self, windows):
        self.windows = windows
        self.targets = []

    def open_shortcut(self, target):
        self.targets.append(target)
        handle = 100 + target.order
        self.windows.values.append(
            WindowInfo(
                handle=handle,
                title="Adobe Flash Player 11",
                visible=True,
                minimized=False,
                rect=(0, 0, 100, 100),
                process_id=200 + target.order,
                window_class="ShockwaveFlash",
                launch_fingerprint=target.fingerprint,
                thread_id=300 + target.order,
                process_lifecycle_token=400 + target.order,
            )
        )
        return True


class Placer:
    def __init__(self):
        self.calls = []

    def place(self, handle, placement):
        self.calls.append((handle, placement))
        return True


class Closer:
    def __init__(self, handles):
        self.handles = set(handles)
        self.closed = []

    def is_window(self, handle):
        return handle in self.handles

    def close_window(self, handle):
        if handle not in self.handles:
            return False
        self.closed.append(handle)
        self.handles.remove(handle)
        return True


class FailingManaged:
    def __init__(self):
        self.calls = []

    def remember_group_windows(self, group_name, values):
        self.calls.append((group_name, tuple(values)))
        return False


def launch_service(
    tmp_path,
    *,
    with_layout=True,
    delays=(0, 0),
):
    entries = []
    for index, name in enumerate(("甲", "乙"), start=1):
        shortcut = tmp_path / f"{name}.lnk"
        shortcut.touch()
        entry = {
            "entry_id": f"entry-{index}",
            "path": str(shortcut),
            "role_id": f"role-{index}",
        }
        if with_layout:
            entry.update(
                {
                    "x": -1000 + index,
                    "y": index * 10,
                    "width": 916,
                    "height": 629,
                    "delay_ms": delays[index - 1],
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


def open_window(
    handle,
    fingerprint,
    *,
    rect=(0, 0, 100, 100),
    process_id=None,
    window_class="ShockwaveFlash",
    thread_id=None,
    process_lifecycle_token=None,
):
    return WindowInfo(
        handle=handle,
        title="Adobe Flash Player 11",
        visible=True,
        minimized=False,
        rect=rect,
        process_id=process_id if process_id is not None else handle + 100,
        window_class=window_class,
        launch_fingerprint=fingerprint,
        thread_id=thread_id if thread_id is not None else handle + 200,
        process_lifecycle_token=(
            process_lifecycle_token
            if process_lifecycle_token is not None
            else handle + 300
        ),
    )


def placement_rect(target):
    placement = target.placement
    assert placement is not None
    return (
        placement.x,
        placement.y,
        placement.x + placement.width,
        placement.y + placement.height,
    )


def registry_store_for(tmp_path, entry_id="entry-1"):
    registry = WindowRegistry()
    registry.register_character(entry_id, "甲")
    store = WindowRegistryStore(tmp_path / "registry.json")
    store.save(registry)
    return registry, store


def stored_record(store, entry_id):
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    matches = [
        item
        for item in payload["characters"]
        if item["character_id"] == entry_id
    ]
    assert len(matches) == 1
    return matches[0]


def test_claim_existing_unique_window_persists_managed_and_registry(
    tmp_path,
):
    launch = launch_service(tmp_path)
    plan = launch.plan("兩支")
    target = plan.targets[0]
    window = open_window(
        501,
        target.fingerprint,
        rect=placement_rect(target),
    )
    windows = Windows((window, open_window(2, plan.targets[1].fingerprint)))
    registry, store = registry_store_for(tmp_path, target.entry_id)
    managed_path = tmp_path / "managed.json"
    managed = ManagedGameProcessService(managed_path, windows)
    opener = Opener(windows)
    placer = Placer()
    service = GroupWindowLaunchService(
        launch,
        windows,
        shortcut_open_backend=opener,
        placement_backend=placer,
        managed_process_service=managed,
        window_registry=registry,
        window_registry_store=store,
    )

    result = service.claim_existing_window("兩支", target.entry_id)

    assert result.success is True
    assert result.window == window
    managed_payload = json.loads(managed_path.read_text(encoding="utf-8"))
    assert managed_payload["windows"] == [
        {
            "group_name": "兩支",
            "role_name": "甲",
            "process_id": window.process_id,
            "window_handle": window.handle,
            "launch_fingerprint": target.fingerprint,
        }
    ]
    stored = stored_record(store, target.entry_id)
    live = registry.get(target.entry_id)
    assert stored["handle"] == window.handle
    assert stored["process_id"] == window.process_id
    assert stored["window_class"] == window.window_class
    assert stored["rect"] == list(window.rect)
    assert stored["confirmed"] is True
    assert live.handle == window.handle
    assert live.confirmed is True
    assert opener.targets == []
    assert placer.calls == []


def test_claim_existing_window_converges_one_target_from_many_same_fingerprint(
    tmp_path,
):
    launch = launch_service(tmp_path)
    plan = launch.plan("兩支")
    target = plan.targets[0]
    matching = open_window(
        501,
        target.fingerprint,
        rect=placement_rect(target),
    )
    windows = Windows(
        (matching,)
        + tuple(
            open_window(
                600 + index,
                target.fingerprint,
                rect=(index * 1000, 9000, index * 1000 + 916, 9629),
            )
            for index in range(10)
        )
        + (open_window(2, plan.targets[1].fingerprint),)
    )
    registry, store = registry_store_for(tmp_path, target.entry_id)
    managed_path = tmp_path / "managed.json"
    managed = ManagedGameProcessService(managed_path, windows)
    service = GroupWindowLaunchService(
        launch,
        windows,
        managed_process_service=managed,
        window_registry=registry,
        window_registry_store=store,
    )

    result = service.claim_existing_window("兩支", target.entry_id)

    assert result.success is True
    assert result.window == matching
    managed_payload = json.loads(managed_path.read_text(encoding="utf-8"))
    assert managed_payload["windows"][0]["window_handle"] == matching.handle
    assert stored_record(store, target.entry_id)["handle"] == matching.handle


def test_claim_existing_window_missing_match_writes_no_state(tmp_path):
    launch = launch_service(tmp_path)
    plan = launch.plan("兩支")
    target = plan.targets[0]
    windows = Windows((open_window(2, plan.targets[1].fingerprint),))
    registry, store = registry_store_for(tmp_path, target.entry_id)
    managed_path = tmp_path / "managed.json"
    managed = ManagedGameProcessService(managed_path, windows)
    service = GroupWindowLaunchService(
        launch,
        windows,
        managed_process_service=managed,
        window_registry=registry,
        window_registry_store=store,
    )

    result = service.claim_existing_window("兩支", target.entry_id)

    assert result.success is False
    assert result.failure_code == "group_window_missing"
    assert not managed_path.exists()
    assert stored_record(store, target.entry_id)["confirmed"] is False
    assert registry.get(target.entry_id).confirmed is False


def test_claim_existing_window_rejects_nonunique_target(tmp_path):
    launch = launch_service(tmp_path)
    plan = launch.plan("兩支")
    target = plan.targets[0]
    windows = Windows(
        (
            open_window(501, target.fingerprint, rect=placement_rect(target)),
            open_window(502, target.fingerprint, rect=placement_rect(target)),
            open_window(2, plan.targets[1].fingerprint),
        )
    )
    registry, store = registry_store_for(tmp_path, target.entry_id)
    managed_path = tmp_path / "managed.json"
    managed = ManagedGameProcessService(managed_path, windows)
    service = GroupWindowLaunchService(
        launch,
        windows,
        managed_process_service=managed,
        window_registry=registry,
        window_registry_store=store,
    )

    result = service.claim_existing_window("兩支", target.entry_id)

    assert result.success is False
    assert result.failure_code == "group_existing_window_duplicate"
    assert not managed_path.exists()
    assert stored_record(store, target.entry_id)["confirmed"] is False


def test_claim_existing_window_ignores_wrong_fingerprint(tmp_path):
    launch = launch_service(tmp_path)
    plan = launch.plan("兩支")
    target = plan.targets[0]
    windows = Windows(
        (
            open_window(501, "9" * 64, rect=placement_rect(target)),
            open_window(2, plan.targets[1].fingerprint),
        )
    )
    registry, store = registry_store_for(tmp_path, target.entry_id)
    managed_path = tmp_path / "managed.json"
    managed = ManagedGameProcessService(managed_path, windows)
    service = GroupWindowLaunchService(
        launch,
        windows,
        managed_process_service=managed,
        window_registry=registry,
        window_registry_store=store,
    )

    result = service.claim_existing_window("兩支", target.entry_id)

    assert result.success is False
    assert result.failure_code == "group_window_missing"
    assert not managed_path.exists()
    assert stored_record(store, target.entry_id)["confirmed"] is False


@pytest.mark.parametrize(
    "broken_identity",
    (
        {"process_id": 0},
        {"thread_id": 0},
        {"process_lifecycle_token": 0},
    ),
)
def test_claim_existing_window_rejects_incomplete_identity(
    tmp_path,
    broken_identity,
):
    launch = launch_service(tmp_path)
    plan = launch.plan("兩支")
    target = plan.targets[0]
    windows = Windows(
        (
            open_window(
                501,
                target.fingerprint,
                rect=placement_rect(target),
                **broken_identity,
            ),
            open_window(2, plan.targets[1].fingerprint),
        )
    )
    registry, store = registry_store_for(tmp_path, target.entry_id)
    managed_path = tmp_path / "managed.json"
    managed = ManagedGameProcessService(managed_path, windows)
    service = GroupWindowLaunchService(
        launch,
        windows,
        managed_process_service=managed,
        window_registry=registry,
        window_registry_store=store,
    )

    result = service.claim_existing_window("兩支", target.entry_id)

    assert result.success is False
    assert result.failure_code == "group_existing_window_unknown"
    assert not managed_path.exists()
    assert stored_record(store, target.entry_id)["confirmed"] is False


def test_claim_existing_window_rolls_back_registry_when_managed_save_fails(
    tmp_path,
):
    launch = launch_service(tmp_path)
    plan = launch.plan("兩支")
    target = plan.targets[0]
    window = open_window(
        501,
        target.fingerprint,
        rect=placement_rect(target),
    )
    windows = Windows((window, open_window(2, plan.targets[1].fingerprint)))
    registry, store = registry_store_for(tmp_path, target.entry_id)
    managed = FailingManaged()
    service = GroupWindowLaunchService(
        launch,
        windows,
        managed_process_service=managed,
        window_registry=registry,
        window_registry_store=store,
    )

    result = service.claim_existing_window("兩支", target.entry_id)

    assert result.success is False
    assert result.failure_code == "group_process_ownership_unavailable"
    assert len(managed.calls) == 1
    assert stored_record(store, target.entry_id)["confirmed"] is False
    assert registry.get(target.entry_id).confirmed is False


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


def test_duplicate_fingerprint_converges_by_complete_target_identity(
    tmp_path,
):
    launch = launch_service(tmp_path)
    plan = launch.plan("兩支")
    target = plan.targets[0]
    peer = plan.targets[1]
    matching = open_window(
        501,
        target.fingerprint,
        rect=placement_rect(target),
    )
    duplicates = [
        open_window(
            600 + index,
            target.fingerprint,
            rect=(index * 1000, 9000, index * 1000 + 916, 9629),
        )
        for index in range(10)
    ]
    windows = Windows((matching, *duplicates, open_window(2, peer.fingerprint)))
    placer = Placer()
    service = GroupWindowLaunchService(
        launch,
        windows,
        placement_backend=placer,
    )

    result = service._run_restore("兩支")

    assert result.success is True
    assert result.failure_code is None
    assert [call[0] for call in placer.calls] == [501, 2]


def test_duplicate_fingerprint_rejects_when_two_targets_remain(
    tmp_path,
):
    launch = launch_service(tmp_path)
    plan = launch.plan("兩支")
    target = plan.targets[0]
    windows = Windows(
        (
            open_window(501, target.fingerprint, rect=placement_rect(target)),
            open_window(502, target.fingerprint, rect=placement_rect(target)),
            open_window(2, plan.targets[1].fingerprint),
        )
    )
    placer = Placer()
    service = GroupWindowLaunchService(
        launch,
        windows,
        placement_backend=placer,
    )

    result = service._run_restore("兩支")

    assert result.success is False
    assert result.failure_code == "group_existing_window_duplicate"
    assert placer.calls == []


def test_duplicate_fingerprint_rejects_when_no_target_matches(
    tmp_path,
):
    launch = launch_service(tmp_path)
    plan = launch.plan("兩支")
    target = plan.targets[0]
    windows = Windows(
        tuple(
            open_window(
                600 + index,
                target.fingerprint,
                rect=(index * 1000, 9000, index * 1000 + 916, 9629),
            )
            for index in range(11)
        )
        + (open_window(2, plan.targets[1].fingerprint),)
    )
    placer = Placer()
    service = GroupWindowLaunchService(
        launch,
        windows,
        placement_backend=placer,
    )

    result = service._run_restore("兩支")

    assert result.success is False
    assert result.failure_code == "group_existing_window_duplicate"
    assert placer.calls == []


@pytest.mark.parametrize(
    "broken_identity",
    (
        {"process_id": 0},
        {"thread_id": 0},
        {"process_lifecycle_token": 0},
    ),
)
def test_single_fingerprint_rejects_incomplete_window_identity(
    tmp_path,
    broken_identity,
):
    launch = launch_service(tmp_path)
    plan = launch.plan("兩支")
    windows = Windows(
        (
            open_window(
                1,
                plan.targets[0].fingerprint,
                **broken_identity,
            ),
            open_window(2, plan.targets[1].fingerprint),
        )
    )
    placer = Placer()
    service = GroupWindowLaunchService(
        launch,
        windows,
        placement_backend=placer,
    )

    result = service._run_restore("兩支")

    assert result.success is False
    assert result.failure_code == "group_existing_window_unknown"
    assert placer.calls == []


def test_single_fingerprint_single_complete_window_still_restores(tmp_path):
    launch = launch_service(tmp_path)
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

    result = service._run_restore("兩支")

    assert result.success is True
    assert [call[0] for call in placer.calls] == [1, 2]


def test_unrelated_flash_window_does_not_claim_missing_target(tmp_path):
    launch = launch_service(tmp_path)
    plan = launch.plan("兩支")
    target = plan.targets[0]
    windows = Windows(
        (
            open_window(99, "9" * 64, rect=placement_rect(target)),
            open_window(2, plan.targets[1].fingerprint),
        )
    )
    placer = Placer()
    service = GroupWindowLaunchService(
        launch,
        windows,
        placement_backend=placer,
    )

    result = service._run_restore("兩支")

    assert result.success is False
    assert result.failure_code == "group_window_missing"
    assert placer.calls == []


def test_launch_uses_saved_order_and_each_role_delay(tmp_path):
    launch = launch_service(tmp_path, delays=(120, 340))
    windows = Windows()
    opener = Opener(windows)
    delays = []
    service = GroupWindowLaunchService(
        launch,
        windows,
        shortcut_open_backend=opener,
        placement_backend=Placer(),
        sleeper=delays.append,
    )

    result = service._run("兩支")

    assert result.success is True
    assert [target.display_name for target in opener.targets] == [
        "甲",
        "乙",
    ]
    assert delays == [0.12, 0.34]


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
                window_class="ShockwaveFlash",
                launch_fingerprint=plan.targets[0].fingerprint,
                thread_id=201,
                process_lifecycle_token=301,
            ),
            WindowInfo(
                2,
                "Adobe Flash Player 11",
                True,
                False,
                (100, 200, 1016, 829),
                process_id=102,
                window_class="ShockwaveFlash",
                launch_fingerprint=plan.targets[1].fingerprint,
                thread_id=202,
                process_lifecycle_token=302,
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


def test_successful_group_launch_records_exact_ownership_for_safe_stop(
    tmp_path,
):
    launch = launch_service(tmp_path)
    plan = launch.plan("兩支")
    windows = Windows(
        (
            open_window(1, plan.targets[0].fingerprint),
            open_window(2, plan.targets[1].fingerprint),
            open_window(99, "9" * 64),
        )
    )
    closer = Closer((1, 2, 99))
    managed_path = tmp_path / "managed.json"
    managed = ManagedGameProcessService(
        managed_path,
        windows,
        close_backend=closer,
    )
    service = GroupWindowLaunchService(
        launch,
        windows,
        placement_backend=Placer(),
        managed_process_service=managed,
    )

    launched = service._run("兩支")
    assert launched.success is True
    assert len(
        json.loads(managed_path.read_text(encoding="utf-8"))["windows"]
    ) == 2

    restored = ManagedGameProcessService(
        managed_path,
        windows,
        close_backend=closer,
    )
    service = GroupWindowLaunchService(
        launch,
        windows,
        placement_backend=Placer(),
        managed_process_service=restored,
    )

    stopped = service._run_stop_all("全部受管遊戲")

    assert json.loads(
        managed_path.read_text(encoding="utf-8")
    )["windows"] == []
    assert stopped.success is True
    assert stopped.stopped_count == 2
    assert set(closer.closed) == {1, 2}
    assert 99 in closer.handles
