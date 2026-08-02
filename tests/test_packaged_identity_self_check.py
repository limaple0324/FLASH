from pathlib import Path

import pytest

from core.release_identity import ReleaseIdentityError
from core.self_check import SelfCheck
from core.version import MILESTONE, VERSION


def _packaged_build_info(*, version: str = VERSION) -> str:
    commit = "a" * 40
    return "\n".join(
        (
            f"version={version}",
            f"milestone={MILESTONE}",
            "build_kind=main_release",
            "artifact_kind=release",
            "approval_status=approved",
            "approval_method=workflow_dispatch_input",
            "approval_actor=test-approver",
            "approval_run_id=123456789",
            "approval_event=workflow_dispatch",
            "publish_target=release/latest",
            f"commit={commit}",
            f"short_commit={commit[:7]}",
            "run_id=123456789",
            "artifact_name=FLASH-SP1+SP2+SP3-Windows-test-release",
            "",
        )
    )


def test_source_self_check_allows_missing_packaged_metadata(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("core.self_check.sys.frozen", False, raising=False)

    result = SelfCheck._check_packaged_identity(object())

    assert "source run" in result


def test_packaged_self_check_requires_build_info(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr("core.self_check.sys.frozen", True, raising=False)
    monkeypatch.setattr("core.self_check.sys.executable", str(tmp_path / "FLASH.exe"))

    with pytest.raises(RuntimeError, match="missing BUILD_INFO"):
        SelfCheck._check_packaged_identity(object())


def test_packaged_self_check_rejects_mismatched_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    build_info_path = tmp_path / "輔系統" / "BUILD_INFO.txt"
    build_info_path.parent.mkdir()
    build_info_path.write_text(_packaged_build_info(version="0.0.0"), encoding="utf-8")
    monkeypatch.setattr("core.self_check.sys.frozen", True, raising=False)
    monkeypatch.setattr("core.self_check.sys.executable", str(tmp_path / "FLASH.exe"))

    with pytest.raises(ReleaseIdentityError, match="version does not match"):
        SelfCheck._check_packaged_identity(object())


def test_workflow_creates_identity_before_packaged_self_check():
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build-windows.yml"
    ).read_text(encoding="utf-8")

    assert workflow.index("Create packaged identity sidecar") < workflow.index(
        "Verify packaged executable self-check"
    )
    assert "dist/輔系統/BUILD_INFO.txt" in workflow
    assert "packaged_identity check" in workflow
