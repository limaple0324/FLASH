from dataclasses import fields

import pytest

from automation.codex_queue_runner.models import AgentResult, QueueRunError, ROLE_TRANSITIONS, Role, TaskStatus
from automation.codex_queue_runner.parser import parse_task_comment
from automation.codex_queue_runner.selector import collect_candidates, select_task


_BATCH_FIELDS = (
    "PLAN_ID",
    "ITEM_ID",
    "ITEM_TITLE",
    "ITEM_INDEX",
    "GROUP_INDEX",
    "GROUP_SIZE",
    "TOTAL_ITEMS",
    "TOTAL_GROUPS",
)


def _task_body(*, status: str = "READY", queue_id: str = "BCP-1", batch: dict[str, str] | None = None) -> str:
    values = {
        "QUEUE_ID": queue_id,
        "STATUS": status,
        "ROLE": "WORKER_A",
        "SOURCE_ISSUE": "#14",
        "SOURCE_PR": "NONE",
        "BASE_COMMIT": "a" * 40,
        "TARGET_BRANCH": "automation/batch-control",
        "SCOPE": "batch contract fixture",
        "OWNED_FILES": "automation/codex_queue_runner/models.py",
        "FORBIDDEN": "main",
        "ACCEPTANCE": "pass",
        "MINIMUM_TESTS": "tests/test_batch_control_contract.py",
        "BLOCKER_INBOX": "#18",
    }
    if batch:
        values.update(batch)
    return "\n".join(f"{key}: {value}" for key, value in values.items())


def _batch(**overrides: str) -> dict[str, str]:
    values = {
        "PLAN_ID": "FLASH-BATCH-CONTROL-20260801-A",
        "ITEM_ID": "BCP-001",
        "ITEM_TITLE": "Batch contract",
        "ITEM_INDEX": "1",
        "GROUP_INDEX": "1",
        "GROUP_SIZE": "3",
        "TOTAL_ITEMS": "3",
        "TOTAL_GROUPS": "1",
    }
    values.update(overrides)
    return values


def _comment(*, queue_id: str, status: str = "READY", batch: dict[str, str] | None = None, number: int = 1) -> dict:
    return {
        "id": number,
        "created_at": f"2026-08-01T00:00:0{number}Z",
        "user": {"login": "limaple0324"},
        "body": _task_body(status=status, queue_id=queue_id, batch=batch),
    }


def test_legacy_task_without_batch_fields_remains_compatible():
    task = parse_task_comment(_task_body())

    assert not task.is_batch_item
    assert task.plan_id == task.item_id == task.item_title == ""


def test_complete_batch_task_parses_all_contract_fields():
    task = parse_task_comment(_task_body(batch=_batch()))

    assert task.is_batch_item
    assert (task.plan_id, task.item_id, task.item_title) == (
        "FLASH-BATCH-CONTROL-20260801-A",
        "BCP-001",
        "Batch contract",
    )
    assert (task.item_index, task.group_index, task.group_size, task.total_items, task.total_groups) == (1, 1, 3, 3, 1)


@pytest.mark.parametrize("missing", _BATCH_FIELDS)
def test_every_missing_batch_field_fails_closed(missing: str):
    values = _batch()
    values.pop(missing)

    with pytest.raises(QueueRunError, match="batch fields"):
        parse_task_comment(_task_body(batch=values))


@pytest.mark.parametrize("field", ("ITEM_INDEX", "GROUP_INDEX", "GROUP_SIZE", "TOTAL_ITEMS", "TOTAL_GROUPS"))
@pytest.mark.parametrize("value", ("one", "0", "-1"))
def test_batch_numeric_fields_require_positive_integers(field: str, value: str):
    with pytest.raises(QueueRunError, match="positive integer"):
        parse_task_comment(_task_body(batch=_batch(**{field: value})))


def test_item_index_cannot_exceed_total_items():
    with pytest.raises(QueueRunError, match="ITEM_INDEX"):
        parse_task_comment(_task_body(batch=_batch(ITEM_INDEX="4")))


def test_group_index_cannot_exceed_total_groups():
    with pytest.raises(QueueRunError, match="GROUP_INDEX"):
        parse_task_comment(_task_body(batch=_batch(GROUP_INDEX="2")))


def test_total_groups_must_match_the_fixed_group_size():
    with pytest.raises(QueueRunError, match="TOTAL_GROUPS"):
        parse_task_comment(_task_body(batch=_batch(TOTAL_ITEMS="4", TOTAL_GROUPS="1")))


def test_group_size_is_fixed_to_three():
    with pytest.raises(QueueRunError, match="GROUP_SIZE"):
        parse_task_comment(_task_body(batch=_batch(GROUP_SIZE="2")))


def test_item_index_must_belong_to_the_declared_group():
    with pytest.raises(QueueRunError, match="GROUP_INDEX"):
        parse_task_comment(
            _task_body(
                batch=_batch(
                    ITEM_INDEX="2",
                    GROUP_INDEX="2",
                    TOTAL_ITEMS="4",
                    TOTAL_GROUPS="2",
                )
            )
        )


def test_only_ready_and_needs_fix_tasks_are_claimable():
    for status in ("READY", "NEEDS_FIX"):
        selected = select_task([type("Candidate", (), {"task": parse_task_comment(_task_body(status=status))})()])
        assert selected.task.status.value == status
    for status in ("PENDING", "VERIFIED", "CLOSED"):
        with pytest.raises(QueueRunError):
            select_task([type("Candidate", (), {"task": parse_task_comment(_task_body(status=status))})()])


def test_selector_rejects_duplicate_item_id_within_one_plan():
    comments = [
        _comment(queue_id="BCP-1", batch=_batch(ITEM_ID="BCP-001"), number=1),
        _comment(queue_id="BCP-2", batch=_batch(ITEM_ID="BCP-001", ITEM_INDEX="2"), number=2),
    ]

    with pytest.raises(QueueRunError, match="ITEM_ID"):
        collect_candidates(comments)


def test_selector_rejects_duplicate_item_index_within_one_plan():
    comments = [
        _comment(queue_id="BCP-1", batch=_batch(ITEM_ID="BCP-001"), number=1),
        _comment(queue_id="BCP-2", batch=_batch(ITEM_ID="BCP-002"), number=2),
    ]

    with pytest.raises(QueueRunError, match="ITEM_INDEX"):
        collect_candidates(comments)


def test_selector_allows_item_id_and_index_reuse_in_different_plans():
    comments = [
        _comment(queue_id="BCP-1", batch=_batch(), number=1),
        _comment(queue_id="BCP-2", batch=_batch(PLAN_ID="OTHER-PLAN"), number=2),
    ]

    assert len(collect_candidates(comments)) == 2


def test_models_keep_main_role_routing_sandbox_and_manual_gates():
    assert {status.value for status in TaskStatus} == {
        "PENDING", "READY", "CLAIMED", "WAITING_REVIEW", "NEEDS_FIX", "VERIFIED", "CLOSED", "BLOCKED"
    }
    assert not Role.INTEGRATION.requires_codex()
    assert Role.INTEGRATION.sandbox() == "read-only"
    assert Role.INTEGRATION.is_manual_gate()
    assert Role.WORKER_A.sandbox() == "workspace-write"
    assert ROLE_TRANSITIONS == {
        Role.WORKER_A: Role.REQUIREMENTS_AUDIT,
        Role.REQUIREMENTS_AUDIT: Role.CODE_REVIEW,
        Role.CODE_REVIEW: Role.TEST_VALIDATION,
        Role.TEST_VALIDATION: None,
    }
    assert "route" not in {field.name for field in fields(AgentResult)}
