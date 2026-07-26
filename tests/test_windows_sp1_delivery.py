import shutil
import subprocess
from pathlib import Path

import pytest


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
        "Read delivery metadata",
        "Build windowed executable",
    )

    assert "$buildKind = 'sp1_snapshot'" in metadata_step
    assert "$buildKind = 'validation_build'" in metadata_step
    assert "$sp1DeliveryBranch = 'sp1/completion-2026-07-25'" in metadata_step
    assert "$env:BUILD_EVENT_NAME -eq 'workflow_dispatch'" in metadata_step
    assert "$env:BUILD_REF -eq $sp1DeliveryRef" in metadata_step
    assert "$env:BUILD_SOURCE_BRANCH -eq $sp1DeliveryBranch" in metadata_step
    assert "$env:BUILD_PUBLISH_SP1 -ne 'true'" in metadata_step
    assert "elseif ($isSp1Snapshot)" in metadata_step
    assert "$publishTarget = 'none'" in metadata_step
    assert "$artifactPrefix = 'FLASH-SP1-Windows'" in metadata_step
    assert (
        '$artifactName = "$artifactPrefix-$($parts[0])-'
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

    assert "if ($env:FLASH_BUILD_KIND -in @('main_release', 'sp1_release'))" in (
        create_bundle
    )
    assert "SP1快照說明.txt" in create_bundle
    assert "本快照不包含「更新輔」，不會追蹤 release/latest。" in create_bundle

    verify_layout = _step(
        workflow,
        "Verify release bundle layout",
        "Verify release bundle metadata and hash",
    )
    assert "A snapshot must not contain a live updater" in verify_layout
    assert "'release/更新輔.cmd'" in verify_layout
    assert "'release/輔系統/輔更新核心.ps1'" in verify_layout
    assert "$manifestPaths += 'SP1快照說明.txt'" in create_bundle
    assert "$manifestPaths += '分支驗證說明.txt'" in create_bundle


def test_main_release_keeps_the_single_live_updater():
    create_bundle = _step(
        _workflow(),
        "Create release bundle",
        "Verify release bundle layout",
    )
    live_release_block = create_bundle.split(
        "if ($env:FLASH_BUILD_KIND -in @('main_release', 'sp1_release')) {",
        1,
    )[1].split("elseif ($env:FLASH_BUILD_KIND -eq 'sp1_snapshot')", 1)[0]

    assert "Copy-Item 'tools/更新輔.cmd' 'release/更新輔.cmd'" in live_release_block
    assert "Copy-Item 'tools/安裝輔.cmd' 'release/安裝輔.cmd'" in live_release_block
    assert "Copy-Item 'tools/輔系統/安裝輔.ps1'" in live_release_block
    assert "Copy-Item 'tools/輔系統/輔更新核心.ps1'" in live_release_block
    assert "release/輔系統/UPDATE_CHANNEL.txt" in live_release_block


def test_sp1_release_uses_one_verified_push_and_its_own_channel():
    workflow = _workflow()
    metadata_step = _step(
        workflow,
        "Read delivery metadata",
        "Build windowed executable",
    )

    assert "$env:BUILD_PUBLISH_SP1 -eq 'true'" in metadata_step
    assert "$env:BUILD_EVENT_NAME -eq 'push' -or" in metadata_step
    assert "$buildKind = 'sp1_release'" in metadata_step
    assert "$publishTarget = 'release/sp1'" in metadata_step
    publish_step = workflow.split(
        "- name: Publish SP1-only desktop updater files",
        1,
    )[1]
    assert "github.event_name == 'push'" in publish_step
    assert "inputs.publish_sp1" in publish_step
    assert "git push origin release/sp1 --force" in publish_step


def test_main_release_builds_latest_then_a_complete_payload_manifest():
    create_bundle = _step(
        _workflow(),
        "Create release bundle",
        "Verify release bundle layout",
    )

    assert create_bundle.index("Set-Content 'release/LATEST.txt'") < create_bundle.index(
        "Set-Content 'release/輔系統/SHA256SUMS.txt'"
    )
    for relative_path in (
        "FLASH.exe",
        "LATEST.txt",
        "安裝輔.cmd",
        "更新輔.cmd",
        "輔系統/BUILD_INFO.txt",
        "輔系統/verify_windows_release.ps1",
        "輔系統/安裝輔.ps1",
        "輔系統/輔更新核心.ps1",
        "輔系統/UPDATE_CHANNEL.txt",
        "輔系統/檢查輔同步狀態.cmd",
        "輔系統/檢查輔同步狀態.ps1",
    ):
        assert f"'{relative_path}'" in create_bundle

    publish_step = _workflow().split(
        "- name: Publish latest desktop updater files",
        1,
    )[1]
    assert 'release\\LATEST.txt" ".\\LATEST.txt"' in publish_step
    assert "Set-Content \".\\LATEST.txt\"" not in publish_step


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


def test_first_transactional_updater_migration_requires_a_full_installer():
    delivery_readme = Path("deliverables/sp1/README.md").read_text(encoding="utf-8")
    compact_readme = "".join(delivery_readme.split())

    assert "舊版不能安全地把自己直接升級成交易式更新器" in compact_readme
    assert "必須使用完整的正式SP1安裝包替換舊安裝" in compact_readme
    assert "若固定啟動器的內容日後需要改變，也必須再次使用完整安裝包" in (
        compact_readme
    )


def test_release_payload_bytes_survive_git_round_trip_with_autocrlf(tmp_path: Path):
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is required for release byte round-trip verification.")

    workflow = _workflow()
    create_bundle = _step(
        workflow,
        "Create release bundle",
        "Verify release bundle layout",
    )
    assert "'* -text'" in create_bundle

    repository = tmp_path / "release-repository"
    system_dir = repository / "輔系統"
    system_dir.mkdir(parents=True)
    (repository / ".gitattributes").write_bytes(
        b"* -text\n*.cmd -text\n*.bat -text\n*.ps1 -text\n*.txt -text\n"
    )
    payloads = {
        "安裝輔.cmd": b"@echo off\r\necho installer\r\n",
        "更新輔.cmd": b"@echo off\r\necho updater\r\n",
        "LATEST.txt": b"\xef\xbb\xbfbranch=main\r\ncommit=abc\r\n",
        "輔系統/BUILD_INFO.txt": b"\xef\xbb\xbfproduct=FLASH\r\nmilestone=SP1\r\n",
        "輔系統/SHA256SUMS.txt": b"0" * 64 + b"  FLASH.exe\r\n",
        "輔系統/verify_windows_release.ps1": (
            b"\xef\xbb\xbfWrite-Host 'verify'\r\n"
        ),
        "輔系統/安裝輔.ps1": b"\xef\xbb\xbfWrite-Host 'install'\r\n",
        "輔系統/輔更新核心.ps1": b"\xef\xbb\xbfWrite-Host 'update'\r\n",
        "FLASH.exe": b"MZ\x00\r\n\x1a\nbinary\xff",
    }
    for relative_path, content in payloads.items():
        target = repository.joinpath(*relative_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    subprocess.run(
        [git, "init", "-q"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [git, "-c", "core.autocrlf=true", "add", "-A"],
        cwd=repository,
        check=True,
        capture_output=True,
    )

    for relative_path, expected in payloads.items():
        result = subprocess.run(
            [git, "show", f":{relative_path}"],
            cwd=repository,
            check=True,
            capture_output=True,
        )
        assert result.stdout == expected, relative_path
