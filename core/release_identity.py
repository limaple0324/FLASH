"""Build identity and formal release gate rules."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Mapping


MAIN_REF = "refs/heads/main"
SP1_BRANCH = "sp1/completion-2026-07-25"
SP1_REF = f"refs/heads/{SP1_BRANCH}"
SP2_BRANCH = "sp2/completion-2026-07-26"
SP2_REF = f"refs/heads/{SP2_BRANCH}"
SP3_BRANCH = "sp3/completion-2026-07-26"
SP3_REF = f"refs/heads/{SP3_BRANCH}"

RELEASE_BUILD_KINDS = frozenset({"main_release", "sp1_release"})
VALID_BUILD_KINDS = frozenset(
    {
        "sp1_snapshot",
        "sp2_snapshot",
        "sp3_snapshot",
        "validation_build",
        *RELEASE_BUILD_KINDS,
    }
)
ARTIFACT_PREFIX_BY_MILESTONE = {
    "SP1": "FLASH-SP1-Windows",
    "SP2": "FLASH-SP1+SP2-Windows",
    "SP3": "FLASH-SP1+SP2+SP3-Windows",
}
RELEASE_HISTORY_FIELDS = frozenset(
    {
        "version",
        "commit",
        "sha256",
        "artifact_name",
        "verification_run_id",
        "verification_artifact_sha256",
        "released_utc",
    }
)


class ReleaseIdentityError(ValueError):
    """Raised when an artifact identity or release gate is invalid."""


def classify_build(
    *,
    event_name: str,
    ref: str,
    source_branch: str,
    approve_latest: bool = False,
    approve_sp1: bool = False,
) -> dict[str, str]:
    """Classify a non-publishing validation workflow run."""
    if (
        event_name in {"push", "workflow_dispatch"}
        and ref == SP1_REF
        and source_branch == SP1_BRANCH
    ):
        return {
            "build_kind": "sp1_snapshot",
            "publish_target": "none",
            "artifact_kind": "validation",
            "approval_status": "not_approved",
            "approval_method": "none",
        }
    if (
        event_name in {"push", "workflow_dispatch"}
        and ref == SP2_REF
        and source_branch == SP2_BRANCH
    ):
        return {
            "build_kind": "sp2_snapshot",
            "publish_target": "none",
            "artifact_kind": "validation",
            "approval_status": "not_approved",
            "approval_method": "none",
        }
    if (
        event_name in {"push", "workflow_dispatch"}
        and ref == SP3_REF
        and source_branch == SP3_BRANCH
    ):
        return {
            "build_kind": "sp3_snapshot",
            "publish_target": "none",
            "artifact_kind": "validation",
            "approval_status": "not_approved",
            "approval_method": "none",
        }
    return {
        "build_kind": "validation_build",
        "publish_target": "none",
        "artifact_kind": "validation",
        "approval_status": "not_approved",
        "approval_method": "none",
    }


def read_key_value_file(path: Path) -> dict[str, str]:
    """Read the simple key=value metadata format used by release artifacts."""
    values: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    return values


def expected_artifact_name(
    *,
    version: str,
    milestone: str,
    short_commit: str,
    artifact_kind: str,
) -> str:
    """Return the canonical traceable artifact name."""
    try:
        prefix = ARTIFACT_PREFIX_BY_MILESTONE[milestone]
    except KeyError as exc:
        raise ReleaseIdentityError(f"Unsupported artifact milestone: {milestone}") from exc
    if not re.fullmatch(r"[0-9a-fA-F]{7}", short_commit):
        raise ReleaseIdentityError("Artifact short commit is invalid.")
    if artifact_kind not in {"validation", "release"}:
        raise ReleaseIdentityError("Artifact kind is invalid.")
    return f"{prefix}-{version}-{short_commit.lower()}-{artifact_kind}"


def validate_artifact_name(info: Mapping[str, str]) -> None:
    """Require the artifact name to encode version, commit, and artifact kind."""
    expected = expected_artifact_name(
        version=info["version"],
        milestone=info["milestone"],
        short_commit=info["short_commit"],
        artifact_kind=info["artifact_kind"],
    )
    if info["artifact_name"] != expected:
        raise ReleaseIdentityError(
            "Artifact name does not match version, short commit, and artifact kind."
        )


def validate_packaged_identity(
    info: Mapping[str, str],
    *,
    expected_version: str,
    expected_milestone: str,
) -> None:
    """Validate identity fields embedded in a packaged artifact."""
    required = {
        "version",
        "milestone",
        "build_kind",
        "artifact_kind",
        "approval_status",
        "approval_method",
        "approval_actor",
        "approval_run_id",
        "approval_event",
        "publish_target",
        "commit",
        "short_commit",
        "run_id",
        "artifact_name",
    }
    missing = sorted(key for key in required if not str(info.get(key, "")).strip())
    if missing:
        raise ReleaseIdentityError(
            "Missing packaged identity fields: " + ", ".join(missing)
        )
    if info["version"] != expected_version:
        raise ReleaseIdentityError("Packaged version does not match source version.")
    if info["milestone"] != expected_milestone:
        raise ReleaseIdentityError(
            "Packaged milestone does not match source milestone."
        )
    if info["build_kind"] not in VALID_BUILD_KINDS:
        raise ReleaseIdentityError("Packaged build kind is invalid.")
    if info["artifact_kind"] not in {"validation", "release"}:
        raise ReleaseIdentityError("Packaged artifact kind is invalid.")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", info["commit"]):
        raise ReleaseIdentityError("Packaged source commit is invalid.")
    if info["short_commit"].lower() != info["commit"][:7].lower():
        raise ReleaseIdentityError("Packaged short commit does not match source commit.")
    validate_artifact_name(info)
    if info["build_kind"] in RELEASE_BUILD_KINDS:
        if info["artifact_kind"] != "release":
            raise ReleaseIdentityError("A release build must be a release artifact.")
        if info["approval_status"] != "approved":
            raise ReleaseIdentityError("A release build requires approval.")
        if info["approval_method"] != "workflow_dispatch_input":
            raise ReleaseIdentityError("A release build requires explicit approval.")
        if info["approval_event"] != "workflow_dispatch":
            raise ReleaseIdentityError("A release build requires a manual workflow run.")
        if info["approval_actor"] == "none":
            raise ReleaseIdentityError("A release build requires an approval actor.")
        if info["approval_run_id"] != info.get("run_id"):
            raise ReleaseIdentityError("Approval run does not match build run.")
    else:
        if info["artifact_kind"] != "validation":
            raise ReleaseIdentityError("A non-release build must be validation-only.")
        if info["approval_status"] != "not_approved":
            raise ReleaseIdentityError("A validation build must not be approved.")
        if info["approval_method"] != "none":
            raise ReleaseIdentityError("A validation build must not have approval.")
        if info["approval_actor"] != "none" or info["approval_run_id"] != "none":
            raise ReleaseIdentityError("A validation build must not have approval identity.")
        if info["approval_event"] != "none":
            raise ReleaseIdentityError("A validation build must not have approval event.")
        if info["publish_target"] != "none":
            raise ReleaseIdentityError("A validation build must not publish.")


def reject_reused_release_version(
    *,
    current_version: str,
    current_commit: str,
    previous: Mapping[str, str] | None,
) -> None:
    """Block a changed source commit from reusing a formal version."""
    if previous is None:
        return
    previous_version = str(previous.get("version", "")).strip()
    previous_commit = str(previous.get("commit", "")).strip()
    if not previous_version or not previous_commit:
        raise ReleaseIdentityError(
            "Existing formal release has no traceable version or source commit."
        )
    if previous_version != current_version:
        return
    if previous_commit == current_commit:
        raise ReleaseIdentityError("A formal version and source commit are already recorded.")
    raise ReleaseIdentityError(
        "A changed source commit cannot reuse an existing formal version."
    )


def read_release_history(path: Path) -> list[dict[str, str]]:
    """Read and validate append-only formal release history records."""
    history_path = Path(path)
    if not history_path.exists():
        return []
    records: list[dict[str, str]] = []
    for line_number, line in enumerate(
        history_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            raw_record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReleaseIdentityError(
                f"Release history line {line_number} is not valid JSON."
            ) from exc
        if not isinstance(raw_record, dict):
            raise ReleaseIdentityError(
                f"Release history line {line_number} must be an object."
            )
        record = {str(key): str(value) for key, value in raw_record.items()}
        missing = sorted(
            key for key in RELEASE_HISTORY_FIELDS if not record.get(key, "").strip()
        )
        if missing:
            raise ReleaseIdentityError(
                "Release history record is missing: " + ", ".join(missing)
            )
        if not re.fullmatch(r"[0-9a-fA-F]{40}", record["commit"]):
            raise ReleaseIdentityError("Release history commit is invalid.")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", record["sha256"]):
            raise ReleaseIdentityError("Release history SHA-256 is invalid.")
        if not re.fullmatch(
            r"[0-9a-fA-F]{64}", record["verification_artifact_sha256"]
        ):
            raise ReleaseIdentityError("Release history verification SHA-256 is invalid.")
        try:
            datetime.strptime(record["released_utc"], "%Y-%m-%dT%H:%M:%SZ")
        except ValueError as exc:
            raise ReleaseIdentityError("Release history UTC timestamp is invalid.") from exc
        records.append(record)
    return records


def reject_reused_release_history_version(
    *,
    current_version: str,
    current_commit: str,
    history: list[Mapping[str, str]],
) -> None:
    """Reject every repeated formal version in the complete retained history."""
    for record in history:
        if record.get("version") != current_version:
            continue
        if record.get("commit") == current_commit:
            raise ReleaseIdentityError(
                "This formal version and source commit are already in release history."
            )
        raise ReleaseIdentityError(
            "A changed source commit cannot reuse a historical formal version."
        )


def validate_formal_release_inputs(
    *,
    ref: str,
    actor: str,
    confirmation: str,
    verification_run_id: str,
    source_commit: str,
    artifact_sha256: str,
    version: str,
) -> None:
    """Validate explicit inputs before a formal release can start."""
    if ref != MAIN_REF:
        raise ReleaseIdentityError("Formal release must start from main.")
    if actor != "limaple0324":
        raise ReleaseIdentityError("Formal release actor is not authorized.")
    if confirmation.lower() != "true":
        raise ReleaseIdentityError("Formal release requires explicit confirmation.")
    if not verification_run_id.strip():
        raise ReleaseIdentityError("Formal release requires a verification run id.")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", source_commit):
        raise ReleaseIdentityError("Formal release source commit is invalid.")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", artifact_sha256):
        raise ReleaseIdentityError("Formal release artifact SHA-256 is invalid.")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ReleaseIdentityError("Formal release version is invalid.")


def validate_verification_candidate_identity(
    info: Mapping[str, str],
    *,
    expected_version: str,
    expected_commit: str,
) -> None:
    """Require the downloaded candidate to be the requested successful validation artifact."""
    validate_packaged_identity(
        info,
        expected_version=expected_version,
        expected_milestone="SP3",
    )
    if info["build_kind"] != "validation_build":
        raise ReleaseIdentityError("Formal release requires a main validation artifact.")
    if info["artifact_kind"] != "validation" or info["publish_target"] != "none":
        raise ReleaseIdentityError("Formal release candidate has an invalid artifact identity.")
    if info["commit"].lower() != expected_commit.lower():
        raise ReleaseIdentityError("Verification artifact source commit does not match the request.")
