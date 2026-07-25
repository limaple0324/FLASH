from pathlib import Path


WORKFLOW_PATH = Path(".github/workflows/build-windows.yml")


def _workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _step(workflow: str, name: str, next_name: str) -> str:
    return workflow.split(f"- name: {name}", 1)[1].split(f"- name: {next_name}", 1)[0]


def test_sp1_build_info_records_delivery_identity():
    create_bundle = _step(
        _workflow(),
        "Create release bundle",
        "Verify release bundle layout",
    )

    for metadata_line in (
        '"product=FLASH"',
        '"version=$env:FLASH_VERSION"',
        '"milestone=$env:FLASH_MILESTONE"',
        '"build_kind=$env:FLASH_BUILD_KIND"',
        '"event_name=${{ github.event_name }}"',
        '"source_ref=${{ github.ref }}"',
        '"source_branch=$env:FLASH_SOURCE_BRANCH"',
        '"publish_target=$env:FLASH_PUBLISH_TARGET"',
        '"commit=$env:GITHUB_SHA"',
        '"run_id=$env:GITHUB_RUN_ID"',
    ):
        assert metadata_line in create_bundle


def test_manual_build_is_an_independent_sp1_snapshot():
    workflow = _workflow()
    metadata_step = _step(
        workflow,
        "Read SP1 delivery metadata",
        "Build windowed executable",
    )

    assert "$buildKind = 'sp1_snapshot'" in metadata_step
    assert "$buildKind = 'validation_build'" in metadata_step
    assert "$sp1DeliveryBranch = 'sp1/completion-2026-07-25'" in metadata_step
    assert "$env:BUILD_EVENT_NAME -in @('push', 'workflow_dispatch')" in metadata_step
    assert "$env:BUILD_REF -eq $sp1DeliveryRef" in metadata_step
    assert "$env:BUILD_SOURCE_BRANCH -eq $sp1DeliveryBranch" in metadata_step
    assert "elseif ($isSp1Snapshot)" in metadata_step
    assert "$publishTarget = 'none'" in metadata_step
    assert (
        '$artifactName = "FLASH-SP1-Windows-$($parts[0])-'
        '$shortCommit-$artifactKind"'
    ) in metadata_step
    assert "name: ${{ env.FLASH_ARTIFACT_NAME }}" in workflow
    assert "      - sp1/completion-2026-07-25" in workflow


def test_sp1_snapshot_does_not_include_the_live_updater():
    workflow = _workflow()
    create_bundle = _step(
        workflow,
        "Create release bundle",
        "Verify release bundle layout",
    )

    assert "if ($env:FLASH_BUILD_KIND -eq 'main_release')" in create_bundle
    assert "SP1快照說明.txt" in create_bundle
    assert "本快照不包含「更新輔」，不會追蹤 release/latest。" in create_bundle

    verify_layout = _step(
        workflow,
        "Verify release bundle layout",
        "Verify release bundle metadata and hash",
    )
    assert "SP1 snapshot must not contain a live updater" in verify_layout
    assert "'release/更新輔.cmd'" in verify_layout
    assert "'release/輔系統/輔更新核心.ps1'" in verify_layout


def test_main_release_keeps_the_single_live_updater():
    create_bundle = _step(
        _workflow(),
        "Create release bundle",
        "Verify release bundle layout",
    )
    main_release_block = create_bundle.split(
        "if ($env:FLASH_BUILD_KIND -eq 'main_release') {",
        1,
    )[1].split("else {", 1)[0]

    assert "Copy-Item 'tools/更新輔.cmd' 'release/更新輔.cmd'" in main_release_block
    assert "Copy-Item 'tools/輔系統/輔更新核心.ps1'" in main_release_block


def test_workflow_verifies_and_uploads_snapshot_before_any_publication():
    workflow = _workflow()
    verify_step = _step(
        workflow,
        "Verify release bundle metadata and hash",
        "Upload Windows release bundle",
    )

    assert "verify_windows_release.ps1' -NoLaunch" in verify_step
    assert "include-hidden-files: true" in workflow
    assert workflow.index("- name: Upload Windows release bundle") < workflow.index(
        "- name: Publish latest desktop updater files"
    )
