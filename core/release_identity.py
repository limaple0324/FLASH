"""Build identity and formal release gate rules."""

from __future__ import annotations

import re
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
    """Classify one workflow run without allowing implicit publication."""
    if (
        event_name == "workflow_dispatch"
        and ref == MAIN_REF
        and source_branch == "main"
        and approve_latest
    ):
        return {
            "build_kind": "main_release",
            "publish_target": "release/latest",
            "artifact_kind": "release",
            "approval_status": "approved",
            "approval_method": "workflow_dispatch_input",
        }
    if (
        event_name == "workflow_dispatch"
        and ref == SP1_REF
        and source_branch == SP1_BRANCH
        and approve_sp1
    ):
        return {
            "build_kind": "sp1_release",
            "publish_target": "release/sp1",
            "artifact_kind": "release",
            "approval_status": "approved",
            "approval_method": "workflow_dispatch_input",
        }
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
    if previous_version == current_version and previous_commit != current_commit:
        raise ReleaseIdentityError(
            "A changed source commit cannot reuse an existing formal version."
        )
