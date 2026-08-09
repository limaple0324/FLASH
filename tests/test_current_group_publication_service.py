from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

import services.identity_data_transaction_coordinator as transaction_module

from config.config_manager import ConfigManager
from domain.group import CharacterGroup
from services.current_group_publication_service import (
    CurrentGroupPublicationPlan,
    CurrentGroupPublicationService,
)
from services.identity_data_transaction_coordinator import (
    IdentityDataResource,
    IdentityDataTransactionCoordinator,
    IdentityTransactionClosedError,
)
from workspace.models import WorkspaceState
from workspace.service import WorkspaceService


CURRENT_GROUP_NAME_KEY = "current_group_name"


def _services(tmp_path, initial_state: WorkspaceState | None = None):
    config = ConfigManager(tmp_path / "settings.json")
    coordinator = IdentityDataTransactionCoordinator()
    workspace = WorkspaceService(coordinator, initial_state)
    publication = CurrentGroupPublicationService(
        config,
        workspace,
        coordinator,
        current_group_name_key=CURRENT_GROUP_NAME_KEY,
    )
    return config, coordinator, workspace, publication


def _group(name: str = "甲組") -> CharacterGroup:
    return CharacterGroup(group_id=f"id-{name}", name=name)


def test_publication_updates_file_config_memory_and_workspace_once(tmp_path) -> None:
    initial = WorkspaceState(next_step="保留步驟")
    config, _coordinator, workspace, publication = _services(tmp_path, initial)
    config.set("general", {"value": 1})
    group = _group()
    notifications = []
    workspace.subscribe(lambda: notifications.append(workspace.snapshot()))

    result = publication.execute(
        lambda _transaction: CurrentGroupPublicationPlan(
            group_name=group.name,
            workspace_group=group,
            result="完成",
        )
    )

    assert result.result == "完成"
    assert result.config_changed is True
    assert result.current_group_changed is True
    assert config.get(CURRENT_GROUP_NAME_KEY) == "甲組"
    assert json.loads(config.config_path.read_text(encoding="utf-8")) == {
        "general": {"value": 1},
        CURRENT_GROUP_NAME_KEY: "甲組",
    }
    assert workspace.state.current_group is group
    assert workspace.state.next_step == "保留步驟"
    assert notifications == [WorkspaceState(group, None, "保留步驟")]


def test_unchanged_publication_does_not_notify_or_rewrite_current_group(
    tmp_path,
) -> None:
    group = _group()
    config, _coordinator, workspace, publication = _services(
        tmp_path,
        WorkspaceState(current_group=group),
    )
    config.set(CURRENT_GROUP_NAME_KEY, group.name)
    notifications = []
    workspace.subscribe(lambda: notifications.append(workspace.snapshot()))

    result = publication.execute(
        lambda _transaction: CurrentGroupPublicationPlan(
            group.name,
            group,
            None,
        )
    )

    assert result.config_changed is False
    assert result.current_group_changed is False
    assert notifications == []


def test_closed_coordinator_rejects_publication_without_any_change(tmp_path) -> None:
    config, coordinator, workspace, publication = _services(tmp_path)
    before_config = config.snapshot_with_revision()
    before_file = config.config_path.read_bytes()
    called = False
    assert coordinator.close_and_wait()

    def prepare(_transaction):
        nonlocal called
        called = True
        return CurrentGroupPublicationPlan("甲組", _group(), None)

    with pytest.raises(IdentityTransactionClosedError):
        publication.execute(prepare)

    assert called is False
    assert config.snapshot_with_revision() == before_config
    assert config.config_path.read_bytes() == before_file
    with pytest.raises(IdentityTransactionClosedError):
        workspace.snapshot()


def test_composite_memory_failure_restores_all_earlier_files_and_memories(
    tmp_path,
    monkeypatch,
) -> None:
    config, _coordinator, workspace, publication = _services(tmp_path)
    config.set("general", "old")
    other_path = tmp_path / "group.json"
    other_path.write_bytes(b"old-group")
    other_memory = {"value": "old"}
    before_config = config.snapshot_with_revision()
    before_config_file = config.config_path.read_bytes()
    original_install = config.install_candidate_locked

    def fail_after_config_install(candidate, *, expected_revision):
        original_install(candidate, expected_revision=expected_revision)
        raise OSError("config memory publish interrupted")

    monkeypatch.setattr(config, "install_candidate_locked", fail_after_config_install)

    def prepare(transaction):
        transaction.stage_file(
            IdentityDataResource.GROUP_SETTINGS,
            other_path,
            b"new-group",
            lambda content: content == b"new-group",
        )
        transaction.stage_memory(
            IdentityDataResource.GROUP_SETTINGS,
            lambda: dict(other_memory),
            lambda: other_memory.update(value="new"),
            lambda snapshot: other_memory.update(snapshot),
        )
        group = _group()
        return CurrentGroupPublicationPlan(group.name, group, None)

    with pytest.raises(OSError, match="config memory publish interrupted"):
        publication.execute(prepare)

    assert config.snapshot_with_revision() == before_config
    assert config.config_path.read_bytes() == before_config_file
    assert other_path.read_bytes() == b"old-group"
    assert other_memory == {"value": "old"}
    assert workspace.state == WorkspaceState()


def test_listener_exception_does_not_rollback_committed_publication(tmp_path) -> None:
    config, _coordinator, workspace, publication = _services(tmp_path)
    group = _group()

    def fail_listener() -> None:
        raise RuntimeError("listener failed")

    workspace.subscribe(fail_listener)

    with pytest.raises(RuntimeError, match="listener failed"):
        publication.execute(
            lambda _transaction: CurrentGroupPublicationPlan(
                group.name,
                group,
                None,
            )
        )

    assert config.get(CURRENT_GROUP_NAME_KEY) == group.name
    assert workspace.state.current_group is group


@pytest.mark.parametrize("first_writer", ["publication", "general"])
def test_current_group_and_general_setting_both_survive_in_either_order(
    tmp_path,
    first_writer,
) -> None:
    config, _coordinator, workspace, publication = _services(tmp_path)
    group = _group()
    entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def run(action) -> None:
        try:
            action()
        except BaseException as error:
            errors.append(error)

    if first_writer == "publication":
        def publish() -> None:
            def prepare(_transaction):
                entered.set()
                assert release.wait(5)
                return CurrentGroupPublicationPlan(group.name, group, None)

            publication.execute(prepare)

        first_action = publish
        second_action = lambda: config.set("general", "new")
    else:
        def write_general() -> None:
            with config.resource_guard():
                config.set("general", "new")
                entered.set()
                assert release.wait(5)

        first_action = write_general
        second_action = lambda: publication.execute(
            lambda _transaction: CurrentGroupPublicationPlan(
                group.name,
                group,
                None,
            )
        )

    first_thread = threading.Thread(target=run, args=(first_action,))
    second_thread = threading.Thread(target=run, args=(second_action,))
    first_thread.start()
    assert entered.wait(5)
    second_thread.start()
    release.set()
    first_thread.join(5)
    second_thread.join(5)

    assert errors == []
    assert first_thread.is_alive() is False
    assert second_thread.is_alive() is False
    assert config.get("general") == "new"
    assert config.get(CURRENT_GROUP_NAME_KEY) == group.name
    assert workspace.state.current_group is group


def test_mismatched_group_plan_fails_before_any_publication(tmp_path) -> None:
    config, _coordinator, workspace, publication = _services(tmp_path)
    before = config.snapshot_with_revision()

    with pytest.raises(ValueError, match="does not match"):
        publication.execute(
            lambda _transaction: CurrentGroupPublicationPlan(
                "甲組",
                _group("乙組"),
                None,
            )
        )

    assert config.snapshot_with_revision() == before
    assert workspace.state == WorkspaceState()


def test_workspace_reader_cannot_observe_file_replace_before_memory_publish(
    tmp_path,
    monkeypatch,
) -> None:
    config, _coordinator, workspace, publication = _services(tmp_path)
    group = _group()
    file_replaced = threading.Event()
    release_publish = threading.Event()
    reader_finished = threading.Event()
    observed = []
    errors = []
    real_replace = transaction_module.os.replace

    def replace_then_pause(source, destination) -> None:
        real_replace(source, destination)
        if Path(destination) == config.config_path:
            file_replaced.set()
            assert release_publish.wait(2)

    monkeypatch.setattr(transaction_module.os, "replace", replace_then_pause)

    def publish() -> None:
        try:
            publication.execute(
                lambda _transaction: CurrentGroupPublicationPlan(
                    group.name,
                    group,
                    None,
                )
            )
        except BaseException as error:
            errors.append(error)

    def read_workspace() -> None:
        observed.append(workspace.snapshot())
        reader_finished.set()

    publish_thread = threading.Thread(target=publish)
    reader_thread = threading.Thread(target=read_workspace)
    publish_thread.start()
    assert file_replaced.wait(1)
    reader_thread.start()
    assert reader_finished.wait(0.05) is False
    release_publish.set()
    publish_thread.join(2)
    reader_thread.join(2)

    assert errors == []
    assert publish_thread.is_alive() is False
    assert reader_thread.is_alive() is False
    assert config.get(CURRENT_GROUP_NAME_KEY) == group.name
    assert observed == [WorkspaceState(current_group=group)]
