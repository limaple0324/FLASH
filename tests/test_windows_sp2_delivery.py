from pathlib import Path


WORKFLOW_PATH = Path(".github/workflows/build-windows.yml")


def _workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _step(workflow: str, name: str, next_name: str) -> str:
    return workflow.split(f"- name: {name}", 1)[1].split(
        f"- name: {next_name}", 1
    )[0]


def test_sp2_branch_builds_a_separate_cumulative_snapshot():
    workflow = _workflow()
    metadata_step = _step(
        workflow,
        "Read delivery metadata",
        "Build windowed executable",
    )

    assert "      - sp2/completion-2026-07-26" in workflow
    assert "$sp2DeliveryBranch = 'sp2/completion-2026-07-26'" in metadata_step
    assert "$buildKind = 'sp2_snapshot'" in metadata_step
    assert "$publishTarget = 'none'" in metadata_step
    assert "$artifactPrefix = 'FLASH-SP1+SP2-Windows'" in metadata_step
    assert (
        "$buildKind -eq 'validation_build' -and "
        "$metadata.milestone -eq 'SP2'"
    ) in metadata_step
    assert "$metadata.milestone -ne 'SP2'" in metadata_step


def test_sp2_snapshot_is_explicitly_separate_from_sp1_and_live_updaters():
    workflow = _workflow()
    create_bundle = _step(
        workflow,
        "Create release bundle",
        "Verify release bundle layout",
    )
    verify_layout = _step(
        workflow,
        "Verify release bundle layout",
        "Verify release bundle metadata and hash",
    )

    assert "SP1+SP2累積快照說明.txt" in create_bundle
    assert "SP1 獨立成品仍保留，不會被本快照覆蓋。" in create_bundle
    assert "不會追蹤 release/latest 或 release/sp1" in create_bundle
    assert "$manifestPaths += 'SP1+SP2累積快照說明.txt'" in create_bundle
    assert "A snapshot must not contain a live updater" in verify_layout
    assert "SP1+SP2 cumulative snapshot notice is missing." in verify_layout
