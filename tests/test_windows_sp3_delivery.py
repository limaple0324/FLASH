from pathlib import Path


WORKFLOW_PATH = Path(".github/workflows/build-windows.yml")


def _workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _step(workflow: str, name: str, next_name: str) -> str:
    return workflow.split(f"- name: {name}", 1)[1].split(
        f"- name: {next_name}", 1
    )[0]


def test_sp3_branch_builds_the_complete_cumulative_snapshot():
    workflow = _workflow()
    metadata_step = _step(
        workflow,
        "Read delivery metadata",
        "Build windowed executable",
    )

    assert "      - sp3/completion-2026-07-26" in workflow
    assert "$sp3DeliveryBranch = 'sp3/completion-2026-07-26'" in metadata_step
    assert "$buildKind = 'sp3_snapshot'" in metadata_step
    assert "$publishTarget = 'none'" in metadata_step
    assert "$artifactPrefix = 'FLASH-SP1+SP2+SP3-Windows'" in metadata_step
    assert "$metadata.milestone -ne 'SP3'" in metadata_step


def test_sp3_snapshot_preserves_every_independent_delivery():
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

    assert "SP1+SP2+SP3完整累積快照說明.txt" in create_bundle
    assert "SP1、SP2 與 SP3 各自的交付檔案仍保留" in create_bundle
    assert "deliverables/sp3" in create_bundle
    assert "不會追蹤任何正式發布頻道" in create_bundle
    assert (
        "$manifestPaths += 'SP1+SP2+SP3完整累積快照說明.txt'"
        in create_bundle
    )
    assert "A snapshot must not contain a live updater" in verify_layout
    assert "SP1+SP2+SP3 cumulative snapshot notice is missing." in verify_layout


def test_main_release_is_the_complete_cumulative_identity():
    workflow = _workflow()
    metadata_step = _step(
        workflow,
        "Read delivery metadata",
        "Build windowed executable",
    )

    assert "$buildKind = 'main_release'" in metadata_step
    assert "$metadata.milestone -ne 'SP3'" in metadata_step
    assert (
        "$buildKind -in @('main_release', 'sp3_snapshot')"
        in metadata_step
    )
    assert (
        "$buildKind -eq 'validation_build' -and "
        "$metadata.milestone -eq 'SP3'"
    ) in metadata_step
    assert "$artifactPrefix = 'FLASH-SP1+SP2+SP3-Windows'" in metadata_step
    assert "$publishTarget = 'release/latest'" in metadata_step


def test_complete_release_gate_verifies_zip_install_update_and_rollback():
    workflow = _workflow()

    assert "- name: Create and verify Windows ZIP" in workflow
    assert "[IO.Compression.ZipFile]::CreateFromDirectory(" in workflow
    assert "[IO.Compression.CompressionLevel]::Optimal" in workflow
    assert "tar.exe -a -c -f" not in workflow
    assert "Expand-Archive -LiteralPath $zipPath" in workflow
    assert "$zipVerified = $?" in workflow
    assert "if (-not $zipVerified)" in workflow
    assert "- name: Verify live install update rollback and desktop entries" in workflow
    assert "$shortcutNames -notcontains '輔.lnk'" in workflow
    assert "$shortcutNames -notcontains '更新輔.lnk'" in workflow
    assert "-TestFailAfterReplacement 1" in workflow
    assert "回復完成；正式安裝內容已還原" in workflow
    assert '"$zipPath.sha256.txt"' in workflow
    assert "dist/*.zip*" in workflow
