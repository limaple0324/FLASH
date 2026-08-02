from pathlib import Path


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build-windows.yml"


def _workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _step(workflow: str, name: str, next_name: str) -> str:
    return workflow.split(f"- name: {name}", 1)[1].split(
        f"- name: {next_name}", 1
    )[0]


def test_sp3_branch_uses_the_complete_validation_identity():
    workflow = _workflow()
    metadata_step = _step(
        workflow,
        "Read delivery metadata",
        "Build windowed executable",
    )

    assert "      - sp3/completion-2026-07-26" in workflow
    assert "from core.release_identity import classify_build" in metadata_step
    assert "'SP3' { 'FLASH-SP1+SP2+SP3-Windows' }" in metadata_step
    assert "$artifactKind -ne 'validation'" in metadata_step
    assert "$publishTarget -ne 'none'" in metadata_step
    assert "main_release" not in workflow
    assert "release/latest" not in workflow


def test_sp3_validation_branch_requires_the_sp3_milestone():
    metadata_step = _step(
        _workflow(),
        "Read delivery metadata",
        "Build windowed executable",
    )

    assert "$buildKind -eq 'sp3_snapshot'" in metadata_step
    assert "$metadata.milestone -ne 'SP3'" in metadata_step
    assert "SP3 validation branch must use milestone=SP3." in metadata_step


def test_validation_builds_never_create_formal_install_or_update_payloads():
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
    assert "release/安裝輔.cmd" in verify_layout
    assert "release/更新輔.cmd" in verify_layout
    assert "release/輔系統/安裝輔.ps1" in verify_layout
    assert "release/輔系統/輔更新核心.ps1" in verify_layout
    assert "release/輔系統/UPDATE_CHANNEL.txt" in verify_layout
    assert "A validation bundle must not contain a formal delivery file" in verify_layout


def test_validation_build_verifies_and_uploads_a_windows_zip_without_publication():
    workflow = _workflow()

    assert "- name: Create and verify Windows ZIP" in workflow
    assert "[IO.Compression.ZipFile]::CreateFromDirectory(" in workflow
    assert "[IO.Compression.CompressionLevel]::Optimal" in workflow
    assert "tar.exe -a -c -f" not in workflow
    assert "Expand-Archive -LiteralPath $zipPath" in workflow
    assert "$zipVerified = $?" in workflow
    assert "if (-not $zipVerified)" in workflow
    assert "- name: Upload Windows validation bundle" in workflow
    assert "dist/*.zip*" in workflow
    assert "Verify live install update rollback" not in workflow
