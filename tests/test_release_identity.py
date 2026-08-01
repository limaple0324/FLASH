import pytest

from core.release_identity import (
    ReleaseIdentityError,
    classify_build,
    reject_reused_release_version,
    validate_packaged_identity,
)


def _info(**overrides: str) -> dict[str, str]:
    info = {
        "version": "0.3.0",
        "milestone": "SP3",
        "build_kind": "validation_build",
        "artifact_kind": "validation",
        "approval_status": "not_approved",
        "approval_method": "none",
        "approval_actor": "none",
        "approval_run_id": "none",
        "approval_event": "none",
        "publish_target": "none",
        "commit": "a" * 40,
        "short_commit": "aaaaaaa",
        "run_id": "123",
        "artifact_name": "FLASH-SP1+SP2+SP3-Windows-0.3.0-aaaaaaa-validation",
    }
    info.update(overrides)
    return info


def test_main_push_is_validation_only():
    result = classify_build(
        event_name="push",
        ref="refs/heads/main",
        source_branch="main",
    )

    assert result["build_kind"] == "validation_build"
    assert result["publish_target"] == "none"
    assert result["approval_status"] == "not_approved"


def test_validation_workflow_cannot_classify_a_manual_request_as_formal_release():
    result = classify_build(
        event_name="workflow_dispatch",
        ref="refs/heads/main",
        source_branch="main",
        approve_latest=True,
    )

    assert result["build_kind"] == "validation_build"
    assert result["publish_target"] == "none"
    assert result["approval_status"] == "not_approved"


def test_sp1_push_is_snapshot_even_when_the_branch_is_a_delivery_branch():
    result = classify_build(
        event_name="push",
        ref="refs/heads/sp1/completion-2026-07-25",
        source_branch="sp1/completion-2026-07-25",
    )

    assert result["build_kind"] == "sp1_snapshot"
    assert result["publish_target"] == "none"


def test_packaged_identity_rejects_a_mismatched_short_commit():
    with pytest.raises(ReleaseIdentityError, match="short commit"):
        validate_packaged_identity(
            _info(short_commit="bbbbbbb"),
            expected_version="0.3.0",
            expected_milestone="SP3",
        )


def test_packaged_identity_accepts_an_approved_release():
    validate_packaged_identity(
        _info(
            build_kind="main_release",
            artifact_kind="release",
            approval_status="approved",
            approval_method="workflow_dispatch_input",
            approval_actor="fixture-user",
            approval_run_id="123",
            approval_event="workflow_dispatch",
            publish_target="release/latest",
            artifact_name="FLASH-SP1+SP2+SP3-Windows-0.3.0-aaaaaaa-release",
        ),
        expected_version="0.3.0",
        expected_milestone="SP3",
    )


def test_changed_source_cannot_reuse_a_formal_version():
    with pytest.raises(ReleaseIdentityError, match="cannot reuse"):
        reject_reused_release_version(
            current_version="0.3.0",
            current_commit="b" * 40,
            previous={"version": "0.3.0", "commit": "a" * 40},
        )


def test_same_source_cannot_create_an_unconditional_duplicate_formal_version():
    with pytest.raises(ReleaseIdentityError, match="already recorded"):
        reject_reused_release_version(
            current_version="0.3.0",
            current_commit="a" * 40,
            previous={"version": "0.3.0", "commit": "a" * 40},
        )
