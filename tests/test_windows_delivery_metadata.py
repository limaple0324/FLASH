from pathlib import Path


WORKFLOW_PATH = Path(".github/workflows/build-windows.yml")


def _workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _step(workflow: str, name: str, next_name: str) -> str:
    return workflow.split(f"- name: {name}", 1)[1].split(f"- name: {next_name}", 1)[0]


def test_windows_build_info_separates_engine_and_delivery_identity():
    create_bundle = _step(
        _workflow(),
        "Create release bundle",
        "Verify release bundle layout",
    )

    for metadata_line in (
        '"product=輔"',
        '"technical_name=FLASH"',
        '"version=$env:FLASH_VERSION"',
        '"milestone=$env:FLASH_MILESTONE"',
        '"engine_version=$env:FLASH_VERSION"',
        '"engine_milestone=$env:FLASH_MILESTONE"',
        '"delivery_scope=$env:FLASH_DELIVERY_SCOPE"',
        '"build_kind=$env:FLASH_BUILD_KIND"',
        '"validation_state=$env:FLASH_VALIDATION_STATE"',
        '"source_branch=$env:FLASH_SOURCE_BRANCH"',
        '"publish_target=$env:FLASH_PUBLISH_TARGET"',
    ):
        assert metadata_line in create_bundle
    assert (
        "Set-Content 'release/輔系統/BUILD_INFO.txt' -Encoding utf8BOM"
        in create_bundle
    )


def test_manual_build_is_named_as_an_integration_engineering_snapshot():
    workflow = _workflow()
    metadata_step = _step(
        workflow,
        "Read application and delivery metadata",
        "Build windowed executable",
    )

    assert "$buildKind = 'integration_snapshot'" in metadata_step
    assert "$buildKind = 'main_snapshot'" in metadata_step
    assert "$publishTarget = 'none'" in metadata_step
    assert "$artifactKind = $metadata.delivery_label" in metadata_step
    assert "$scopeForName = $metadata.delivery_scope.Replace('+', '-')" in metadata_step
    assert (
        '$artifactName = "輔-$artifactKind-$scopeForName-'
        '$($metadata.version)-$shortCommit-windows-x64"'
    ) in metadata_step
    assert "name: ${{ env.FLASH_ARTIFACT_NAME }}" in workflow
    assert "name: FLASH-Windows-${{ env.FLASH_MILESTONE }}" not in workflow


def test_delivery_metadata_uses_ascii_safe_json_across_windows_code_pages():
    metadata_step = _step(
        _workflow(),
        "Read application and delivery metadata",
        "Build windowed executable",
    )

    assert "json.dumps(" in metadata_step
    assert "$metadataJson | ConvertFrom-Json" in metadata_step
    assert "print('|'.join(" not in metadata_step


def test_integration_snapshot_does_not_include_the_live_updater():
    workflow = _workflow()
    create_bundle = _step(
        workflow,
        "Create release bundle",
        "Verify release bundle layout",
    )
    release_only = create_bundle.split(
        "if ($env:FLASH_BUILD_KIND -eq 'main_release') {",
        1,
    )[1]

    assert "Copy-Item 'tools/更新輔.cmd' 'release/更新輔.cmd'" in release_only
    assert "Copy-Item 'tools/輔系統/輔更新核心.ps1'" in release_only
    assert "整合快照說明.txt" in create_bundle
    assert "不會連接 release/latest 更新通道" in create_bundle

    verify_layout = _step(
        workflow,
        "Verify release bundle layout",
        "Publish latest desktop updater files",
    )
    assert "Engineering snapshot must not contain a live updater" in verify_layout
    assert "'release/更新輔.cmd'" in verify_layout
    assert "'release/輔系統/輔更新核心.ps1'" in verify_layout


def test_workflow_runs_the_bundle_verifier_without_launching_the_app():
    workflow = _workflow()
    verify_step = _step(
        workflow,
        "Verify release bundle metadata and hash",
        "Publish latest desktop updater files",
    )

    assert "verify_windows_release.ps1' -NoLaunch" in verify_step
