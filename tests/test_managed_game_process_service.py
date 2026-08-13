import json
from pathlib import Path

from adapters.windows_window import WindowInfo
from services.group_launch_service import GroupLaunchTarget
from services.managed_game_process_service import (
    ManagedGameProcessService,
)


class Windows:
    def __init__(self, values=()):
        self.values = list(values)

    def list_windows(self):
        return list(self.values)


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


def target(name, fingerprint, order=1):
    return GroupLaunchTarget(
        order=order,
        display_name=name,
        shortcut_path=Path(f"{name}.lnk"),
        fingerprint=fingerprint,
    )


def window(handle, process_id, fingerprint, title="Adobe Flash Player 11"):
    return WindowInfo(
        handle=handle,
        title=title,
        visible=True,
        minimized=False,
        rect=(0, 0, 100, 100),
        process_id=process_id,
        launch_fingerprint=fingerprint,
    )


def test_records_are_atomic_persistent_and_stop_only_exact_managed_windows(
    tmp_path,
):
    first_fingerprint = "1" * 64
    second_fingerprint = "2" * 64
    unknown_fingerprint = "9" * 64
    first = window(11, 101, first_fingerprint)
    second = window(22, 202, second_fingerprint)
    unknown = window(99, 909, unknown_fingerprint)
    windows = Windows((first, second, unknown))
    closer = Closer((11, 22, 99))
    path = tmp_path / "managed.json"
    service = ManagedGameProcessService(
        path,
        windows,
        close_backend=closer,
    )

    assert service.remember_group_windows(
        "兩支",
        (
            (target("甲", first_fingerprint), first),
            (target("乙", second_fingerprint, 2), second),
        ),
    ) is True
    restored = ManagedGameProcessService(
        path,
        windows,
        close_backend=closer,
    )

    assert len(json.loads(path.read_text(encoding="utf-8"))["windows"]) == 2
    result = restored.stop_all()

    assert result.success is True
    assert result.stopped_count == 2
    assert set(closer.closed) == {11, 22}
    assert 99 in closer.handles
    assert json.loads(path.read_text(encoding="utf-8"))["windows"] == []


def test_identity_drift_is_never_closed_and_invalid_record_is_removed(
    tmp_path,
):
    fingerprint = "3" * 64
    original = window(33, 303, fingerprint)
    windows = Windows((original,))
    path = tmp_path / "managed.json"
    service = ManagedGameProcessService(
        path,
        windows,
        close_backend=Closer((33,)),
    )
    assert service.remember_group_windows(
        "單支",
        ((target("甲", fingerprint), original),),
    ) is True

    changed = window(33, 404, "4" * 64)
    closer = Closer((33,))
    restored = ManagedGameProcessService(
        path,
        Windows((changed,)),
        close_backend=closer,
    )
    result = restored.stop_all()

    assert result.success is False
    assert result.failure_code == "managed_game_stop_partial"
    assert closer.closed == []
    assert closer.handles == {33}
    assert json.loads(path.read_text(encoding="utf-8"))["windows"] == []


def test_corrupt_state_fails_closed_without_overwriting_or_closing(tmp_path):
    path = tmp_path / "managed.json"
    original = b"{not-json"
    path.write_bytes(original)
    closer = Closer((55,))
    service = ManagedGameProcessService(
        path,
        Windows((window(55, 505, "5" * 64),)),
        close_backend=closer,
    )

    result = service.stop_all()

    assert result.success is False
    assert result.failure_code == "managed_game_state_unavailable"
    assert closer.closed == []
    assert path.read_bytes() == original


def test_invalid_or_partial_identity_is_not_saved(tmp_path):
    fingerprint = "6" * 64
    invalid = window(66, 606, None)
    path = tmp_path / "managed.json"
    service = ManagedGameProcessService(path, Windows((invalid,)))

    assert service.remember_group_windows(
        "單支",
        ((target("甲", fingerprint), invalid),),
    ) is False
    assert path.exists() is False


def test_state_file_uses_only_identity_fields(tmp_path):
    fingerprint = "7" * 64
    current = window(77, 707, fingerprint)
    path = tmp_path / "managed.json"
    service = ManagedGameProcessService(path, Windows((current,)))

    assert service.remember_group_windows(
        "單支",
        ((target("甲", fingerprint), current),),
    ) is True
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["version"] == 1
    assert set(payload["windows"][0]) == {
        "group_name",
        "role_name",
        "process_id",
        "window_handle",
        "launch_fingerprint",
    }
