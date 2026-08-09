from pathlib import Path


WINDOWS_COMMAND_FILES = (
    Path("tools/安裝輔.cmd"),
    Path("tools/更新輔.cmd"),
    Path("tools/檢查輔同步狀態.cmd"),
)


def test_windows_command_files_use_crlf_only():
    for path in WINDOWS_COMMAND_FILES:
        content = path.read_bytes()
        assert b"\r\n" in content, f"{path} must use CRLF line endings"
        assert b"\n" not in content.replace(b"\r\n", b""), f"{path} contains bare LF"


def test_git_preserves_windows_command_file_bytes():
    attributes = Path(".gitattributes").read_text(encoding="ascii")

    assert "*.cmd -text whitespace=cr-at-eol" in attributes
    assert "*.bat -text whitespace=cr-at-eol" in attributes


def test_updater_preserves_the_existing_desktop_shortcut():
    updater = Path("tools/輔系統/輔更新核心.ps1").read_text(encoding="utf-8")

    assert "CreateShortcut" not in updater
    assert "IconLocation" not in updater
    assert 'Join-Path $Desktop "輔.lnk"' not in updater
    assert "已保留原本桌面捷徑的名稱與圖示" in updater
    assert "可以直接使用桌面的「輔」或「更新輔」" in updater
    assert "輔 V0.2" not in updater


def test_updater_runs_a_temporary_core_with_one_fixed_cmd_bootstrap():
    launcher = Path("tools/更新輔.cmd").read_text(encoding="utf-8")
    updater = Path("tools/輔系統/輔更新核心.ps1").read_text(encoding="utf-8")

    assert 'copy /y "%SCRIPT%" "%TEMPUPDATER%"' in launcher
    assert '-File "%TEMPUPDATER%" -InstallDirectory "%~dp0."' in launcher
    assert 'del /f /q "%TEMPUPDATER%"' in launcher
    assert "ReadKey(" not in updater
    assert updater.count("更新成功；全部檔案已套用並通過再次驗證") == 1


def test_installer_uses_safe_paths_and_two_confirmed_shortcuts():
    launcher = Path("tools/安裝輔.cmd").read_text(encoding="utf-8")
    installer = Path("tools/輔系統/安裝輔.ps1").read_text(encoding="utf-8")

    assert '-SourceDirectory "%~dp0."' in launcher
    assert 'Join-Path $env:LOCALAPPDATA "Programs\\輔\\$installFlavor"' in installer
    assert 'Join-Path $DesktopDir "輔.lnk"' in installer
    assert 'Join-Path $DesktopDir "更新輔.lnk"' in installer
    assert "啟動輔.lnk" not in installer
    assert "[FlashNativeShortcutWriter]::Create(" in installer
    assert "shellLink.SetPath(executablePath);" in installer
    assert 'Join-Path $WorkingDirectory "sync_plus_icon.ico"' in installer
    assert "shellLink.SetIconLocation(iconPath, 0);" in installer
    assert "shellLink.SetDescription(description);" in installer
    assert '-Description "輔"' in installer
    assert '-Description "更新輔"' in installer
    assert '$shortcut.Description = "輔 SP1"' not in installer


def test_windows_powershell_download_does_not_require_the_ie_engine():
    updater = Path("tools/輔系統/輔更新核心.ps1").read_text(encoding="utf-8")

    assert "Invoke-WebRequest `" in updater
    assert "-OutFile $TargetPath `" in updater
    assert "Invoke-RestMethod `" in updater
    assert updater.count("-UseBasicParsing") == 3
    assert "-TimeoutSec $ConnectionTimeoutSeconds" in updater
    assert "-TimeoutSec $DownloadTimeoutSeconds" in updater
    assert "Invoke-WithNetworkRetry" in updater
    assert "TestTransientFailuresBeforeSuccess" in updater


def test_hash_verification_does_not_require_powershell_module_autoload():
    scripts = (
        Path("tools/輔系統/輔更新核心.ps1").read_text(encoding="utf-8"),
        Path("tools/verify_windows_release.ps1").read_text(encoding="utf-8"),
    )

    for script in scripts:
        assert "Get-FileHash" not in script
        assert "[System.Security.Cryptography.SHA256]::Create()" in script
        assert "$sha256.ComputeHash($stream)" in script


def test_only_main_push_can_publish_over_the_live_release():
    workflow = Path(".github/workflows/build-windows.yml").read_text(encoding="utf-8")

    publish_step = workflow.split("- name: Publish latest desktop updater files", 1)[1]
    publish_step = publish_step.split(
        "- name: Publish SP1-only desktop updater files",
        1,
    )[0]
    condition = next(
        line.strip()
        for line in publish_step.splitlines()
        if line.strip().startswith("if:")
    )

    assert condition == (
        "if: github.event_name == 'push' && github.ref == 'refs/heads/main'"
    )
    assert "windows-release-$env:GITHUB_SHA" in publish_step
    assert "FLASH-Windows-release.zip" in publish_step
    assert "git push origin release/latest --force" not in publish_step
    assert "force = $false" in publish_step
    assert "release-index.json" in publish_step


def test_main_publish_maps_and_validates_cross_job_artifact_name():
    workflow = Path(".github/workflows/build-windows.yml").read_text(encoding="utf-8")
    publish_step = workflow.split("- name: Publish latest desktop updater files", 1)[1]
    publish_step = publish_step.split(
        "- name: Publish SP1-only desktop updater files",
        1,
    )[0]

    output_mapping = (
        "FLASH_ARTIFACT_NAME: "
        "${{ needs.test-and-build.outputs.artifact_name }}"
    )
    read_name = "$artifactName = [string]$env:FLASH_ARTIFACT_NAME"
    reject_empty = "[string]::IsNullOrWhiteSpace($artifactName)"
    reject_unsafe = (
        "$artifactName -notmatch "
        "'^[A-Za-z0-9][A-Za-z0-9+._-]{0,127}$'"
    )
    resolve_zip = (
        "$zipPath = Resolve-Path "
        "(Join-Path 'dist' \"$artifactName.zip\")"
    )

    assert output_mapping in publish_step
    assert read_name in publish_step
    assert reject_empty in publish_step
    assert reject_unsafe in publish_step
    assert resolve_zip in publish_step
    assert (
        "Resolve-Path (Join-Path 'dist' \"$env:FLASH_ARTIFACT_NAME.zip\")"
        not in publish_step
    )
    assert publish_step.index(output_mapping) < publish_step.index(read_name)
    assert publish_step.index(read_name) < publish_step.index(reject_empty)
    assert publish_step.index(reject_empty) < publish_step.index(reject_unsafe)
    assert publish_step.index(reject_unsafe) < publish_step.index(resolve_zip)


def test_main_release_uses_draft_asset_verification_before_public_index():
    workflow = Path(".github/workflows/build-windows.yml").read_text(encoding="utf-8")
    publish_step = workflow.split("- name: Publish latest desktop updater files", 1)[1]
    publish_step = publish_step.split(
        "- name: Publish SP1-only desktop updater files",
        1,
    )[0]

    assert "draft = $true" in publish_step
    assert "make_latest = 'false'" in publish_step
    assert "Invoke-RestMethod -Method 'Post' -Uri \"${uploadUrl}?name=$assetName\"" in publish_step
    assert "& gh release upload $releaseTag $uploadAssetPath --repo $repo" in publish_step
    assert "$assetHeaders['Accept'] = 'application/octet-stream'" in publish_step
    assert (
        "Remove-Item -LiteralPath $remoteAssetPath -Force "
        "-ErrorAction SilentlyContinue"
    ) in publish_step
    assert (
        "& gh release download $releaseTag --repo $repo --pattern $assetName "
        "--dir $remoteDownloadRoot --clobber"
    ) in publish_step
    assert "$remoteAssets.Count -ne 1" in publish_step
    assert "[string]$remoteAsset.digest" in publish_step
    assert "Expand-Archive -LiteralPath $remoteAssetPath" in publish_step
    assert "verify_windows_release.ps1') -NoLaunch" in publish_step
    assert "-TestFailAfterReplacement 1" in publish_step
    assert "make_latest = 'true'" in publish_step
    assert publish_step.index("-TestFailAfterReplacement 1") < publish_step.index(
        "draft = $false"
    )
    assert publish_step.index("draft = $false") < publish_step.index(
        "$oldReleaseLatest = Get-ReleaseLatestRef"
    )


def test_main_release_index_is_an_immutable_single_file_compare_and_swap():
    workflow = Path(".github/workflows/build-windows.yml").read_text(encoding="utf-8")
    publish_step = workflow.split("- name: Publish latest desktop updater files", 1)[1]
    publish_step = publish_step.split(
        "- name: Publish SP1-only desktop updater files",
        1,
    )[0]

    for field in (
        "schema = 1",
        "source_commit = $env:GITHUB_SHA",
        "run_id = [Int64]$env:GITHUB_RUN_ID",
        "release_tag = $releaseTag",
        "asset_name = $assetName",
        "asset_size = $assetSize",
        "asset_sha256 = $assetHash",
        "published_utc = $publishedUtc",
    ):
        assert field in publish_step
    assert publish_step.count("Get-ReleaseLatestRef") >= 4
    assert "parents = @($oldReleaseLatest)" in publish_step
    assert "base_tree" not in publish_step
    assert "force = $false" in publish_step
    assert (
        'Invoke-GitHubJson -Method \'Get\' '
        '-Uri "$apiRoot/git/commits/${updatedReleaseLatest}"'
    ) in publish_step
    assert "[string]$updatedCommit.sha -ne [string]$indexCommit.sha" in publish_step
    assert (
        "[string]$updatedCommit.tree.sha -ne [string]$indexTree.sha"
    ) in publish_step
    assert "$updatedTreeSha = [string]$updatedCommit.tree.sha" in publish_step
    assert (
        'Invoke-GitHubJson -Method \'Get\' '
        '-Uri "$apiRoot/git/trees/${updatedTreeSha}?recursive=1"'
    ) in publish_step
    assert "updatedEntries.Count -ne 1" in publish_step
    assert "$updatedEntries[0].path -ne 'release-index.json'" in publish_step
    assert publish_step.index("$updatedReleaseLatest = Get-ReleaseLatestRef") < (
        publish_step.index("$updatedCommit = Invoke-GitHubJson")
    )
    assert publish_step.index("$updatedCommit = Invoke-GitHubJson") < (
        publish_step.index("$updatedTree = Invoke-GitHubJson")
    )
    index_section = publish_step.split("$oldReleaseLatest = Get-ReleaseLatestRef", 1)[1]
    assert "FLASH.exe" not in index_section


def test_windows_workflow_keeps_manual_artifact_build():
    workflow = Path(".github/workflows/build-windows.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "publish_sp1:" in workflow
    assert "- name: Upload Windows release bundle" in workflow


def test_live_installer_workflow_uses_its_immediate_success_status():
    workflow = Path(".github/workflows/build-windows.yml").read_text(encoding="utf-8")
    installer_step = workflow.split(
        "- name: Verify live install update rollback and desktop entries", 1
    )[1]
    installer_step = installer_step.split("$shortcutNames = @(", 1)[0]

    assert (
        "-DesktopDirectory $desktopRoot\n"
        "          $installerSucceeded = $?\n"
        "          if (-not $installerSucceeded) {"
    ) in installer_step
    assert "$LASTEXITCODE" not in installer_step


def test_live_installer_workflow_reads_unicode_shortcuts_with_shell_application():
    workflow = Path(".github/workflows/build-windows.yml").read_text(encoding="utf-8")
    installer_step = workflow.split(
        "- name: Verify live install update rollback and desktop entries", 1
    )[1]
    shortcut_check = installer_step.split("$beforeHash", 1)[0]

    assert "$shellApp = New-Object -ComObject Shell.Application" in shortcut_check
    assert "$desktopFolder = $shellApp.NameSpace($desktopRoot)" in shortcut_check
    assert "$mainShortcut = $desktopFolder.ParseName('輔.lnk').GetLink" in shortcut_check
    assert "$updateShortcut = $desktopFolder.ParseName('更新輔.lnk').GetLink" in shortcut_check
    assert "$mainShortcut.Path -ne (Join-Path $installRoot 'FLASH.exe')" in shortcut_check
    assert "$updateShortcut.Path -ne (Join-Path $installRoot '更新輔.cmd')" in shortcut_check
    assert "WScript.Shell" not in shortcut_check
    assert "TargetPath" not in shortcut_check


def test_sp1_only_publication_has_a_separate_branch_and_verified_push_gate():
    workflow = Path(".github/workflows/build-windows.yml").read_text(encoding="utf-8")
    publish_step = workflow.split("- name: Publish SP1-only desktop updater files", 1)[1]

    condition = next(
        line.strip()
        for line in publish_step.splitlines()
        if line.strip().startswith("if:")
    )
    assert condition == (
        "if: github.ref == 'refs/heads/sp1/completion-2026-07-25' && "
        "(github.event_name == 'push' || "
        "(github.event_name == 'workflow_dispatch' && inputs.publish_sp1))"
    )
    assert "git push origin release/sp1 --force" in publish_step
