from pathlib import Path


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build-windows.yml"


def _workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _step(workflow: str, name: str, next_name: str) -> str:
    return workflow.split(f"- name: {name}", 1)[1].split(
        f"- name: {next_name}", 1
    )[0]


def test_sp2_branch_is_built_as_a_validation_artifact():
    workflow = _workflow()
    metadata_step = _step(
        workflow,
        "Read delivery metadata",
        "Build windowed executable",
    )

    assert "      - sp2/completion-2026-07-26" in workflow
    assert "from core.release_identity import classify_build" in metadata_step
    assert "$artifactKind -ne 'validation'" in metadata_step
    assert "$publishTarget -ne 'none'" in metadata_step
    assert "$approvalStatus -ne 'not_approved'" in metadata_step
    assert "$approvalMethod -ne 'none'" in metadata_step
    assert "'SP2' { 'FLASH-SP1+SP2-Windows' }" in metadata_step


def test_sp2_validation_bundle_is_separate_from_all_formal_delivery_files():
    workflow = _workflow()
    create_bundle = _step(
        workflow,
        "Create validation bundle",
        "Verify validation bundle layout",
    )
    verify_layout = _step(
        workflow,
        "Verify validation bundle layout",
        "Verify validation bundle metadata and hash",
    )

    assert "'分支驗證說明.txt'" in create_bundle
    assert "A validation bundle must not contain a formal delivery file" in verify_layout
    assert "release/輔系統/UPDATE_CHANNEL.txt" in verify_layout
