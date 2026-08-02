import json
from pathlib import Path

import pytest

from core.release_identity import (
    ReleaseIdentityError,
    classify_build,
    expected_artifact_name,
    read_release_history,
    reject_reused_release_history_version,
    validate_artifact_name,
    validate_formal_release_inputs,
    validate_verification_candidate_identity,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VALIDATION_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "build-windows.yml"
FORMAL_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "publish-windows-release.yml"


def _candidate_identity() -> dict[str, str]:
    commit = "a" * 40
    return {
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
        "commit": commit,
        "short_commit": commit[:7],
        "run_id": "123456789",
        "artifact_name": expected_artifact_name(
            version="0.3.0",
            milestone="SP3",
            short_commit=commit[:7],
            artifact_kind="validation",
        ),
    }


def test_validation_workflow_is_read_only_and_has_no_release_push():
    workflow = VALIDATION_WORKFLOW.read_text(encoding="utf-8")

    assert "contents: read" in workflow
    assert "persist-credentials: false" in workflow
    assert "contents: write" not in workflow
    assert "git push origin release/" not in workflow
    assert "--force" not in workflow


def test_formal_workflow_is_the_only_write_workflow_and_never_rebuilds():
    workflow = FORMAL_WORKFLOW.read_text(encoding="utf-8")
    permissions = workflow.split("permissions:", 1)[1].split("concurrency:", 1)[0]

    assert "workflow_dispatch:" in workflow
    assert "actions: read" in permissions
    assert "contents: write" in workflow
    assert permissions.split() == ["actions:", "read", "contents:", "write"]
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "github.actor == 'limaple0324'" in workflow
    assert "inputs.confirm_release" in workflow
    assert "actions/download-artifact@v4" in workflow
    assert "build_coordinator.py" not in workflow
    assert "python -m pytest" not in workflow


@pytest.mark.parametrize(
    ("ref", "actor", "confirmation"),
    [
        ("refs/heads/feature", "limaple0324", "true"),
        ("refs/heads/main", "other-user", "true"),
        ("refs/heads/main", "limaple0324", "false"),
    ],
)
def test_formal_release_rejects_untrusted_start_conditions(
    ref: str,
    actor: str,
    confirmation: str,
):
    with pytest.raises(ReleaseIdentityError):
        validate_formal_release_inputs(
            ref=ref,
            actor=actor,
            confirmation=confirmation,
            verification_run_id="123456789",
            source_commit="a" * 40,
            artifact_sha256="b" * 64,
            version="0.3.0",
        )


def test_validation_candidate_must_match_run_commit_version_and_hash_identity():
    candidate = _candidate_identity()
    validate_verification_candidate_identity(
        candidate,
        expected_version="0.3.0",
        expected_commit="a" * 40,
    )

    candidate["commit"] = "c" * 40
    with pytest.raises(ReleaseIdentityError, match="commit"):
        validate_verification_candidate_identity(
            candidate,
            expected_version="0.3.0",
            expected_commit="a" * 40,
        )


def test_artifact_name_mismatch_is_rejected():
    candidate = _candidate_identity()
    candidate["artifact_name"] = "FLASH-0.3.0-wrong-validation"

    with pytest.raises(ReleaseIdentityError, match="Artifact name"):
        validate_artifact_name(candidate)


def test_complete_release_history_rejects_changed_and_duplicate_version(tmp_path: Path):
    history_path = tmp_path / "RELEASE_HISTORY.jsonl"
    records = [
        {
            "version": "0.3.0",
            "commit": "a" * 40,
            "sha256": "b" * 64,
            "artifact_name": "FLASH-SP1+SP2+SP3-Windows-0.3.0-aaaaaaa-release",
            "verification_run_id": "101",
            "verification_artifact_sha256": "c" * 64,
            "released_utc": "2026-08-01T00:00:00Z",
        },
        {
            "version": "0.3.1",
            "commit": "d" * 40,
            "sha256": "e" * 64,
            "artifact_name": "FLASH-SP1+SP2+SP3-Windows-0.3.1-ddddddd-release",
            "verification_run_id": "102",
            "verification_artifact_sha256": "f" * 64,
            "released_utc": "2026-08-01T01:00:00Z",
        },
    ]
    history_path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    history = read_release_history(history_path)

    with pytest.raises(ReleaseIdentityError, match="historical formal version"):
        reject_reused_release_history_version(
            current_version="0.3.0",
            current_commit="c" * 40,
            history=history,
        )
    with pytest.raises(ReleaseIdentityError, match="already"):
        reject_reused_release_history_version(
            current_version="0.3.0",
            current_commit="a" * 40,
            history=history,
        )


def test_formal_workflow_fails_on_branch_read_errors_and_never_force_pushes():
    workflow = FORMAL_WORKFLOW.read_text(encoding="utf-8")

    assert "git ls-remote --exit-code --heads origin release/latest" in workflow
    assert "$remoteCheckExitCode -eq 2" in workflow
    assert "Could not determine whether release/latest exists" in workflow
    assert "git push origin HEAD:release/latest" in workflow
    assert "--force" not in workflow


def test_formal_workflow_uses_one_explicit_utc_timestamp_without_runner_start_time():
    workflow = FORMAL_WORKFLOW.read_text(encoding="utf-8")

    assert "$builtUtc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')" in workflow
    assert "FORMAL_RELEASED_UTC=$builtUtc" in workflow
    assert '"released_utc": os.environ["FORMAL_RELEASED_UTC"]' in workflow
    assert "GITHUB_RUN_STARTED_AT" not in workflow


@pytest.mark.parametrize("released_utc", ("", "2026-08-01", "2026-13-01T00:00:00Z"))
def test_release_history_rejects_blank_or_invalid_utc_timestamp(
    tmp_path: Path,
    released_utc: str,
):
    history_path = tmp_path / "RELEASE_HISTORY.jsonl"
    history_path.write_text(
        json.dumps(
            {
                "version": "0.3.0",
                "commit": "a" * 40,
                "sha256": "b" * 64,
                "artifact_name": "FLASH-SP1+SP2+SP3-Windows-0.3.0-aaaaaaa-release",
                "verification_run_id": "101",
                "verification_artifact_sha256": "c" * 64,
                "released_utc": released_utc,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseIdentityError, match="timestamp|missing"):
        read_release_history(history_path)
