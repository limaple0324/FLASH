from pathlib import Path


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build-windows.yml"


def _workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _step(workflow: str, name: str, next_name: str) -> str:
    return workflow.split(f"- name: {name}", 1)[1].split(
        f"- name: {next_name}", 1
    )[0]


def test_validation_build_info_records_non_publishable_identity():
    create_bundle = _step(
        _workflow(),
        "Create validation bundle",
        "Verify validation bundle layout",
    )

    for metadata_line in (
        '"product=$env:FLASH_PRODUCT_NAME"',
        '"technical_name=$env:FLASH_TECHNICAL_NAME"',
        '"version=$env:FLASH_VERSION"',
        '"milestone=$env:FLASH_MILESTONE"',
        '"build_kind=$env:FLASH_BUILD_KIND"',
        '"artifact_kind=$env:FLASH_ARTIFACT_KIND"',
        '"publish_target=$env:FLASH_PUBLISH_TARGET"',
        '"approval_status=$env:FLASH_APPROVAL_STATUS"',
        '"approval_method=$env:FLASH_APPROVAL_METHOD"',
        '"commit=$env:GITHUB_SHA"',
        '"run_id=$env:GITHUB_RUN_ID"',
    ):
        assert metadata_line in _workflow()
    assert "'分支驗證說明.txt'" in create_bundle


def test_sp1_validation_branch_requires_the_sp1_milestone():
    metadata_step = _step(
        _workflow(),
        "Read delivery metadata",
        "Build windowed executable",
    )

    assert "$buildKind -eq 'sp1_snapshot'" in metadata_step
    assert "$metadata.milestone -ne 'SP1'" in metadata_step
    assert "SP1 validation branch must use milestone=SP1." in metadata_step


def test_validation_bundle_excludes_the_live_updater_and_installers():
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

    assert "分支驗證說明.txt" in create_bundle
    assert "A validation bundle must not contain a formal delivery file" in verify_layout
    for forbidden_path in (
        "release/LATEST.txt",
        "release/安裝輔.cmd",
        "release/更新輔.cmd",
        "release/輔系統/安裝輔.ps1",
        "release/輔系統/輔更新核心.ps1",
        "release/輔系統/UPDATE_CHANNEL.txt",
    ):
        assert f"'{forbidden_path}'" in verify_layout


def test_validation_bundle_is_verified_and_uploaded_without_publication():
    workflow = _workflow()
    verify_step = _step(
        workflow,
        "Verify validation bundle metadata and hash",
        "Create and verify Windows ZIP",
    )

    assert "verify_windows_release.ps1' -NoLaunch" in verify_step
    assert "- name: Upload Windows validation bundle" in workflow
    assert "include-hidden-files: true" in workflow
    assert "git push" not in workflow
    assert "Publish " not in workflow
