from pathlib import Path

from adapters.windows_window import WindowInfo
from services.group_launch_service import GroupLaunchPlan, GroupLaunchTarget
from services.window_size_adjustment_service import (
    WindowSizeAdjustmentService,
)


FP_MAIN = "1" * 64
FP_FOLLOWER = "2" * 64
FP_OTHER = "3" * 64


class LaunchPlans:
    def __init__(self, plan):
        self.plan_value = plan

    def plan(self, _group_name):
        return self.plan_value


class Windows:
    def __init__(self, values):
        self.values = values

    def list_windows(self):
        return tuple(self.values)

    def foreground_handle(self):
        return None

    def top_window_at(self, _x, _y):
        return None


class Sizes:
    def __init__(self, values):
        self.values = dict(values)
        self.resize_calls = []

    def read(self, handle):
        return self.values.get(handle)

    def resize(self, handle, width, height):
        self.resize_calls.append((handle, width, height))
        self.values[handle] = (width, height)
        return True


def target(order, name, path, fingerprint):
    return GroupLaunchTarget(
        order,
        name,
        Path(path),
        fingerprint,
    )


def window(handle, fingerprint):
    return WindowInfo(
        handle,
        "Adobe Flash Player 11",
        True,
        False,
        (0, 0, 1000, 700),
        process_id=handle,
        window_class="ShockwaveFlash",
        launch_fingerprint=fingerprint,
    )


def service(plan, windows, sizes):
    return WindowSizeAdjustmentService(
        LaunchPlans(plan),
        Windows(windows),
        sizes,
    )


def test_reads_configured_main_window_instead_of_first_visible_window():
    main_path = Path("main.lnk")
    plan = GroupLaunchPlan(
        "14支",
        (
            target(1, "同步窗口", "follower.lnk", FP_FOLLOWER),
            target(2, "主窗口", main_path, FP_MAIN),
        ),
    )
    sizes = Sizes({10: (900, 600), 20: (1000, 700)})
    result = service(
        plan,
        (window(10, FP_FOLLOWER), window(20, FP_MAIN)),
        sizes,
    ).read_main("14支", main_path)

    assert result.success is True
    assert (result.width, result.height) == (1000, 700)
    assert result.player_message == "已讀取主窗口尺寸：1000×700。"


def test_current_group_resizes_only_open_members_and_skips_matching_size():
    plan = GroupLaunchPlan(
        "14支",
        (
            target(1, "主窗口", "main.lnk", FP_MAIN),
            target(2, "同步窗口", "follower.lnk", FP_FOLLOWER),
        ),
    )
    sizes = Sizes({10: (800, 600), 20: (1000, 700), 30: (700, 500)})
    result = service(
        plan,
        (
            window(10, FP_MAIN),
            window(20, FP_FOLLOWER),
            window(30, FP_OTHER),
        ),
        sizes,
    ).apply_current_group("14支", 1000, 700)

    assert result.success is True
    assert result.matched_count == 2
    assert result.changed_count == 1
    assert sizes.resize_calls == [(10, 1000, 700)]


def test_all_flash_resizes_each_uniquely_identified_window():
    plan = GroupLaunchPlan(
        "14支",
        (target(1, "主窗口", "main.lnk", FP_MAIN),),
    )
    sizes = Sizes({10: (800, 600), 20: (900, 650)})
    result = service(
        plan,
        (window(10, FP_MAIN), window(20, FP_OTHER)),
        sizes,
    ).apply_all(1000, 700)

    assert result.success is True
    assert result.matched_count == 2
    assert result.changed_count == 2
    assert sizes.resize_calls == [
        (10, 1000, 700),
        (20, 1000, 700),
    ]


def test_unknown_or_duplicate_identity_fails_before_any_resize():
    plan = GroupLaunchPlan(
        "14支",
        (target(1, "主窗口", "main.lnk", FP_MAIN),),
    )
    sizes = Sizes({10: (800, 600), 20: (900, 650)})
    unknown = window(10, None)
    duplicate_windows = (window(10, FP_MAIN), window(20, FP_MAIN))

    unknown_result = service(
        plan,
        (unknown,),
        sizes,
    ).apply_all(1000, 700)
    duplicate_result = service(
        plan,
        duplicate_windows,
        sizes,
    ).apply_all(1000, 700)

    assert unknown_result.failure_code == "window_identity_unknown"
    assert duplicate_result.failure_code == "window_identity_duplicate"
    assert sizes.resize_calls == []


def test_invalid_size_is_rejected_before_reading_or_resizing():
    plan = GroupLaunchPlan(
        "14支",
        (target(1, "主窗口", "main.lnk", FP_MAIN),),
    )
    sizes = Sizes({10: (800, 600)})
    result = service(
        plan,
        (window(10, FP_MAIN),),
        sizes,
    ).apply_current_group("14支", 199, 700)

    assert result.failure_code == "window_size_invalid"
    assert sizes.resize_calls == []
