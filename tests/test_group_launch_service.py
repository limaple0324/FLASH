import json

from services.group_launch_service import GroupLaunchService


CONFIRMED_14 = (
    "100古",
    "100靈",
    "100福",
    "100獵",
    "120古",
    "120靈",
    "120射",
    "120福",
    "120獵",
    "亞洛",
    "餐廳",
    "大排",
    "160帥",
    "和尚",
)

OLD_14 = (
    "120古",
    "120靈",
    "120射",
    "120福",
    "120獵",
    "100古",
    "100靈",
    "100福",
    "100獵",
    "160福",
    "160帥",
    "大排",
    "和尚",
    "餐廳",
)


class _Resolver:
    def __init__(self, *, duplicate=False, missing=False):
        self.duplicate = duplicate
        self.missing = missing
        self.calls = []

    def resolve(self, paths):
        paths = tuple(paths)
        self.calls.append(paths)
        resolved = {}
        for index, path in enumerate(paths, start=1):
            if self.missing and index == len(paths):
                continue
            resolved[path] = (
                "f" * 64 if self.duplicate else f"{index:064x}"
            )
        return resolved


def _config(tmp_path, groups):
    path = tmp_path / "sync_launch_config_v02.json"
    payload = {"groups": []}
    for group_name, names in groups:
        entries = []
        for name in names:
            shortcut = tmp_path / f"{name}.lnk"
            shortcut.touch(exist_ok=True)
            entries.append({"path": str(shortcut)})
        payload["groups"].append(
            {"name": group_name, "launch_entries": entries}
        )
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _layout_config(tmp_path, group_name, names):
    path = tmp_path / "legacy-layout.json"
    payload = {
        "groups": [
            {
                "name": group_name,
                "launch_entries": [
                    {
                        "path": str(tmp_path / f"{name}.lnk"),
                        "x": -2000 + index,
                        "y": 100 + index,
                        "width": 916,
                        "height": 629,
                        "delay_ms": index * 100,
                    }
                    for index, name in enumerate(names)
                ],
            }
        ]
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def test_confirmed_14_group_uses_player_order_and_exact_alias(tmp_path):
    resolver = _Resolver()
    service = GroupLaunchService(
        _config(tmp_path, [("14支", OLD_14)]),
        resolver,
    )

    plan = service.plan("14支")

    assert plan.ready is True
    assert tuple(target.display_name for target in plan.targets) == CONFIRMED_14
    assert tuple(target.order for target in plan.targets) == tuple(range(1, 15))
    assert plan.targets[9].shortcut_path.name == "160福.lnk"
    assert plan.targets[9].display_name == "亞洛"
    assert len(plan.fingerprints) == 14


def test_confirmed_group_uses_saved_custom_order_after_player_reorders(
    tmp_path,
):
    custom_names = (OLD_14[1], OLD_14[0], *OLD_14[2:])
    path = _config(tmp_path, [("14支", custom_names)])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["groups"][0]["entry_order_customized"] = True
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    plan = GroupLaunchService(path, _Resolver()).plan("14支")

    assert plan.ready is True
    assert tuple(target.display_name for target in plan.targets) == tuple(
        "亞洛" if name == "160福" else name
        for name in custom_names
    )
    assert tuple(target.shortcut_path.stem for target in plan.targets) == (
        custom_names
    )


def test_other_groups_keep_their_registered_fixed_list_order(tmp_path):
    resolver = _Resolver()
    names = ("120古", "120靈", "120射", "120福", "120獵")
    service = GroupLaunchService(
        _config(tmp_path, [("120", names)]),
        resolver,
    )

    plan = service.plan("120")

    assert plan.ready is True
    assert tuple(target.display_name for target in plan.targets) == names


def test_plan_preserves_registered_entry_and_role_identity(tmp_path):
    path = _config(tmp_path, [("120", ("120古",))])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["groups"][0]["launch_entries"][0].update(
        {
            "entry_id": "entry-120-ancient",
            "role_id": "role-120-ancient",
        }
    )
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    plan = GroupLaunchService(path, _Resolver()).plan("120")

    assert plan.ready is True
    assert plan.targets[0].entry_id == "entry-120-ancient"
    assert plan.targets[0].role_id == "role-120-ancient"


def test_changed_14_registration_fails_closed_instead_of_guessing(tmp_path):
    service = GroupLaunchService(
        _config(tmp_path, [("14支", (*OLD_14[:-1], "未知角色"))]),
        _Resolver(),
    )

    plan = service.plan("14支")

    assert plan.ready is False
    assert plan.failure_codes == ("group_fixed_order_mismatch",)


def test_missing_or_duplicate_shortcut_identity_fails_closed(tmp_path):
    path = _config(tmp_path, [("120", ("120古", "120靈"))])

    missing = GroupLaunchService(
        path,
        _Resolver(missing=True),
    ).plan("120")
    duplicate = GroupLaunchService(
        path,
        _Resolver(duplicate=True),
    ).plan("120")

    assert missing.failure_codes == ("shortcut_identity_unresolved",)
    assert duplicate.failure_codes == ("shortcut_identity_duplicate",)


def test_plan_lookup_requires_one_complete_fingerprint(tmp_path):
    plan = GroupLaunchService(
        _config(tmp_path, [("120", ("120古", "120靈"))]),
        _Resolver(),
    ).plan("120")

    assert plan.target_for_fingerprint(f"{1:064x}") is plan.targets[0]
    assert plan.target_for_fingerprint("bad") is None


def test_plan_reads_saved_layout_from_legacy_without_modifying_it(tmp_path):
    names = ("120古", "120靈")
    current = _config(tmp_path, [("120", names)])
    legacy = _layout_config(tmp_path, "120", names)
    before = legacy.read_bytes()

    plan = GroupLaunchService(
        current,
        _Resolver(),
        legacy_layout_config_path=legacy,
    ).plan("120")

    assert plan.ready is True
    assert plan.targets[0].placement is not None
    assert (
        plan.targets[0].placement.x,
        plan.targets[0].placement.y,
        plan.targets[0].placement.width,
        plan.targets[0].placement.height,
        plan.targets[0].placement.delay_ms,
    ) == (-2000, 100, 916, 629, 0)
    assert plan.targets[1].placement.delay_ms == 100
    assert legacy.read_bytes() == before


def test_new_owned_layout_overrides_legacy_after_player_records_again(
    tmp_path,
):
    names = ("120古",)
    current = _config(tmp_path, [("120", names)])
    current_payload = json.loads(current.read_text(encoding="utf-8"))
    current_payload["groups"][0]["launch_entries"][0].update(
        {
            "x": 321,
            "y": 654,
            "width": 900,
            "height": 600,
            "delay_ms": 50,
        }
    )
    current.write_text(
        json.dumps(current_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    legacy = _layout_config(tmp_path, "120", names)

    plan = GroupLaunchService(
        current,
        _Resolver(),
        legacy_layout_config_path=legacy,
    ).plan("120")

    assert plan.targets[0].placement is not None
    assert (
        plan.targets[0].placement.x,
        plan.targets[0].placement.y,
        plan.targets[0].placement.width,
        plan.targets[0].placement.height,
        plan.targets[0].placement.delay_ms,
    ) == (321, 654, 900, 600, 50)
