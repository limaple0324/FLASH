import hashlib
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


WINDOWS_COMMAND_FILES = (
    Path("tools/安裝輔.cmd"),
    Path("tools/更新輔.cmd"),
    Path("tools/檢查輔同步狀態.cmd"),
)


def _workflow_step(workflow: str, name: str, next_name: str) -> str:
    step = workflow.split(f"- name: {name}", 1)[1]
    return step.split(f"- name: {next_name}", 1)[0]


def _tree_snapshot(root: Path) -> dict[str, tuple[int, str]]:
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _write_key_values(path: Path, values: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in values),
        encoding="utf-8-sig",
    )


def _write_sha256_manifest(root: Path, relative_paths: tuple[str, ...]) -> None:
    lines = []
    for relative_path in relative_paths:
        payload = root / Path(relative_path)
        lines.append(f"{hashlib.sha256(payload.read_bytes()).hexdigest()}  {relative_path}")
    (root / "輔系統" / "SHA256SUMS.txt").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8-sig",
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
    assert publish_step.count("Get-ReleaseLatestRef") == 4
    assert publish_step.count(
        "if ((Get-ReleaseLatestRef) -ne $oldReleaseLatest) {"
    ) == 2
    assert "parents = @($oldReleaseLatest)" in publish_step
    assert "base_tree" not in publish_step
    assert "force = $false" in publish_step
    assert "$updatedReleaseLatest = [string]$indexCommit.sha" in publish_step
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
    assert "$updatedReleaseLatest = Get-ReleaseLatestRef" not in publish_step
    assert publish_step.index(
        "$updatedReleaseLatest = [string]$indexCommit.sha"
    ) < publish_step.index("$updatedCommit = Invoke-GitHubJson")
    assert publish_step.index("$updatedCommit = Invoke-GitHubJson") < (
        publish_step.index("$updatedTree = Invoke-GitHubJson")
    )
    index_section = publish_step.split("$oldReleaseLatest = Get-ReleaseLatestRef", 1)[1]
    assert "FLASH.exe" not in index_section
    assert "git fetch" not in index_section
    assert "git push" not in index_section


def test_main_release_latest_patch_and_rest_readback_are_strict_and_bounded():
    workflow = Path(".github/workflows/build-windows.yml").read_text(encoding="utf-8")
    publish_step = workflow.split("- name: Publish latest desktop updater files", 1)[1]
    publish_step = publish_step.split(
        "- name: Publish SP1-only desktop updater files",
        1,
    )[0]

    assert (
        "$updatedRefResponse = Invoke-GitHubJson -Method 'Patch' "
        '-Uri "$apiRoot/git/refs/heads/release/latest" -Body @{'
    ) in publish_step
    assert "sha = $expectedReleaseLatest" in publish_step
    assert "force = $false" in publish_step
    assert (
        "[string]$updatedRefResponse.ref -cne "
        "'refs/heads/release/latest'"
    ) in publish_step
    assert "[string]$updatedRefResponse.object.type -cne 'commit'" in publish_step
    assert (
        "[string]$updatedRefResponse.object.sha -cne $expectedReleaseLatest"
    ) in publish_step

    assert "$releaseLatestRestAttempts = 3" in publish_step
    assert (
        "for ($readbackAttempt = 1; "
        "$readbackAttempt -le $releaseLatestRestAttempts; "
        "$readbackAttempt++) {"
    ) in publish_step
    assert "$readbackHeaders = $SharedHeaders.Clone()" in publish_step
    assert "$readbackHeaders['Cache-Control'] = 'no-cache'" in publish_step
    assert "[Guid]::NewGuid().ToString('N')" in publish_step
    assert (
        '"$ApiRoot/git/ref/heads/release/latest?readback=$readbackNonce"'
    ) in publish_step
    assert "function Confirm-ReleaseLatestRefUpdate" in publish_step
    assert publish_step.count("Confirm-ReleaseLatestRefUpdate") == 2
    assert (
        "$null = Confirm-ReleaseLatestRefUpdate `\n"
        "            -SharedHeaders $headers `\n"
        "            -ApiRoot $apiRoot `\n"
        "            -Repository $repo `\n"
        "            -OldSha $oldReleaseLatest `\n"
        "            -ExpectedSha $expectedReleaseLatest"
    ) in publish_step

    disposition_helper = publish_step.split(
        "function Get-ReleaseLatestReadbackDisposition",
        1,
    )[1].split("function Confirm-AnonymousReleaseLatestReadback", 1)[0]
    assert "$observed -eq $ExpectedSha.ToLowerInvariant()" in disposition_helper
    assert "return 'confirmed'" in disposition_helper
    assert "$observed -eq $OldSha.ToLowerInvariant()" in disposition_helper
    assert "return 'retry'" in disposition_helper
    assert disposition_helper.count("throw") == 2
    assert "if (-not $releaseLatestRestConfirmed) {" in publish_step


def test_main_release_latest_anonymous_fallback_is_read_only_and_exact():
    workflow = Path(".github/workflows/build-windows.yml").read_text(encoding="utf-8")
    publish_step = workflow.split("- name: Publish latest desktop updater files", 1)[1]
    publish_step = publish_step.split(
        "- name: Publish SP1-only desktop updater files",
        1,
    )[0]
    index_section = publish_step.split("$oldReleaseLatest = Get-ReleaseLatestRef", 1)[1]

    assert "$env:GIT_TERMINAL_PROMPT = '0'" in publish_step
    command_and_exit = (
        '& git ls-remote --refs --exit-code "https://github.com/$Repository.git" '
        "refs/heads/release/latest\n"
        "              )\n"
        "              $anonymousReadbackExit = $LASTEXITCODE"
    )
    assert command_and_exit in publish_step
    assert "-Lines $anonymousReadbackLines" in publish_step
    assert "-ExitCode $anonymousReadbackExit" in publish_step
    assert "-ExpectedSha $ExpectedSha" in publish_step
    assert "Confirm-ReleaseLatestRefUpdate" in index_section
    assert "git fetch" not in index_section
    assert "git push" not in index_section

    anonymous_helper = publish_step.split(
        "function Confirm-AnonymousReleaseLatestReadback",
        1,
    )[1].split("$artifactName =", 1)[0]
    assert "if ($ExitCode -ne 0)" in anonymous_helper
    assert "if ($ExpectedSha -cnotmatch '^[0-9a-f]{40}$')" in anonymous_helper
    assert "$readbackLines = @($Lines)" in anonymous_helper
    assert "if ($readbackLines.Count -ne 1)" in anonymous_helper
    assert (
        "$readbackLine -cnotmatch "
        "'^(?<sha>[0-9a-f]{40})\\s+refs/heads/release/latest$'"
    ) in anonymous_helper
    assert "$Matches.sha -cne $ExpectedSha" in anonymous_helper


def test_release_latest_readback_helpers_execute_and_publish_script_parses(tmp_path):
    powershell = shutil.which("pwsh") or shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("PowerShell is unavailable")

    workflow = Path(".github/workflows/build-windows.yml").read_text(encoding="utf-8")
    publish_step = workflow.split("- name: Publish latest desktop updater files", 1)[1]
    publish_step = publish_step.split(
        "- name: Publish SP1-only desktop updater files",
        1,
    )[0]
    helper_start = publish_step.index(
        "          function Get-ReleaseLatestReadbackDisposition"
    )
    helper_end = publish_step.index("          $artifactName =", helper_start)
    helpers = textwrap.dedent(publish_step[helper_start:helper_end])

    old_sha = "a" * 40
    expected_sha = "b" * 40
    third_sha = "c" * 40
    probe = f"""
$ErrorActionPreference = 'Stop'
{helpers}
$oldSha = '{old_sha}'
$expectedSha = '{expected_sha}'
$thirdSha = '{third_sha}'
if ((Get-ReleaseLatestReadbackDisposition -ObservedSha $expectedSha -OldSha $oldSha -ExpectedSha $expectedSha) -ne 'confirmed') {{ throw 'expected SHA was not confirmed' }}
if ((Get-ReleaseLatestReadbackDisposition -ObservedSha $oldSha -OldSha $oldSha -ExpectedSha $expectedSha) -ne 'retry') {{ throw 'old SHA was not retried' }}
$thirdRejected = $false
try {{ $null = Get-ReleaseLatestReadbackDisposition -ObservedSha $thirdSha -OldSha $oldSha -ExpectedSha $expectedSha }} catch {{ $thirdRejected = $true }}
if (-not $thirdRejected) {{ throw 'third SHA was accepted' }}
$correctLine = "$expectedSha`trefs/heads/release/latest"
if (-not (Confirm-AnonymousReleaseLatestReadback -Lines @($correctLine) -ExitCode 0 -ExpectedSha $expectedSha)) {{ throw 'anonymous readback was rejected' }}
$twoLinesRejected = $false
try {{ $null = Confirm-AnonymousReleaseLatestReadback -Lines @($correctLine, $correctLine) -ExitCode 0 -ExpectedSha $expectedSha }} catch {{ $twoLinesRejected = $true }}
if (-not $twoLinesRejected) {{ throw 'two anonymous lines were accepted' }}
$wrongShaRejected = $false
try {{ $null = Confirm-AnonymousReleaseLatestReadback -Lines @("$oldSha`trefs/heads/release/latest") -ExitCode 0 -ExpectedSha $expectedSha }} catch {{ $wrongShaRejected = $true }}
if (-not $wrongShaRejected) {{ throw 'old anonymous SHA was accepted' }}
$exitRejected = $false
try {{ $null = Confirm-AnonymousReleaseLatestReadback -Lines @($correctLine) -ExitCode 2 -ExpectedSha $expectedSha }} catch {{ $exitRejected = $true }}
if (-not $exitRejected) {{ throw 'failed anonymous command was accepted' }}

function Assert-ProbeCondition {{
  param([bool]$Condition, [string]$Message)
  if (-not $Condition) {{ throw $Message }}
}}

$script:mockShas = @()
$script:throwRest = $false
$script:restCalls = 0
$script:sleepCalls = 0
$script:gitCalls = 0
$script:requestUris = @()
$script:cacheValues = @()
$script:headerIdentities = @()
$script:gitArguments = @()
$script:gitLines = @()
$script:gitExitCode = 0

function Reset-ReadbackMocks {{
  param([string[]]$Shas, [bool]$ThrowRest = $false)
  $script:mockShas = @($Shas)
  $script:throwRest = $ThrowRest
  $script:restCalls = 0
  $script:sleepCalls = 0
  $script:gitCalls = 0
  $script:requestUris = @()
  $script:cacheValues = @()
  $script:headerIdentities = @()
  $script:gitArguments = @()
  $script:gitLines = @("$expectedSha`trefs/heads/release/latest")
  $script:gitExitCode = 0
}}

function Invoke-RestMethod {{
  [CmdletBinding()]
  param([string]$Method, [string]$Uri, [hashtable]$Headers)
  $script:restCalls += 1
  $script:requestUris += $Uri
  $script:cacheValues += [string]$Headers['Cache-Control']
  $script:headerIdentities += [Runtime.CompilerServices.RuntimeHelpers]::GetHashCode($Headers)
  if ($script:throwRest) {{ throw 'mock REST failure' }}
  $sha = [string]$script:mockShas[$script:restCalls - 1]
  return [pscustomobject]@{{ object = [pscustomobject]@{{ sha = $sha }} }}
}}

function Start-Sleep {{
  [CmdletBinding()]
  param([int]$Seconds)
  $script:sleepCalls += 1
}}

function git {{
  $script:gitCalls += 1
  $script:gitArguments = @($args)
  $global:LASTEXITCODE = $script:gitExitCode
  foreach ($line in $script:gitLines) {{ Write-Output $line }}
}}

$env:GITHUB_RUN_ID = '12345'
$env:GITHUB_RUN_ATTEMPT = '1'

Reset-ReadbackMocks -Shas @($expectedSha)
$sharedHeaders = @{{ Accept = 'application/json'; Authorization = 'secret' }}
$sharedHeaderIdentity = [Runtime.CompilerServices.RuntimeHelpers]::GetHashCode($sharedHeaders)
$result = Confirm-ReleaseLatestRefUpdate -SharedHeaders $sharedHeaders -ApiRoot 'https://api.example.test' -Repository 'example/repo' -OldSha $oldSha -ExpectedSha $expectedSha
Assert-ProbeCondition -Condition ($result -eq $true) -Message 'first-new did not succeed'
Assert-ProbeCondition -Condition ($script:restCalls -eq 1) -Message 'first-new REST count was not one'
Assert-ProbeCondition -Condition ($script:sleepCalls -eq 0) -Message 'first-new slept'
Assert-ProbeCondition -Condition ($script:gitCalls -eq 0) -Message 'first-new used anonymous fallback'
Assert-ProbeCondition -Condition ($script:cacheValues.Count -eq 1) -Message 'first-new cache header count was not one'
Assert-ProbeCondition -Condition ($script:cacheValues[0] -ceq 'no-cache') -Message 'first-new REST read lacked no-cache'
Assert-ProbeCondition -Condition ($script:requestUris.Count -eq 1) -Message 'first-new URI count was not one'
$firstNewUriPattern = '^https://api\\.example\\.test/git/ref/heads/release/latest\\?readback=12345-1-1-[0-9a-f]{{32}}$'
Assert-ProbeCondition -Condition ($script:requestUris[0] -cmatch $firstNewUriPattern) -Message 'first-new URI nonce was invalid'
Assert-ProbeCondition -Condition ($script:headerIdentities.Count -eq 1) -Message 'first-new header count was not one'
Assert-ProbeCondition -Condition ($script:headerIdentities[0] -ne $sharedHeaderIdentity) -Message 'first-new reused shared headers'
Assert-ProbeCondition -Condition (-not $sharedHeaders.ContainsKey('Cache-Control')) -Message 'first-new polluted shared headers'

Reset-ReadbackMocks -Shas @($oldSha, $oldSha, $expectedSha)
$sharedHeaders = @{{ Accept = 'application/json'; Authorization = 'secret' }}
$result = Confirm-ReleaseLatestRefUpdate -SharedHeaders $sharedHeaders -ApiRoot 'https://api.example.test' -Repository 'example/repo' -OldSha $oldSha -ExpectedSha $expectedSha
Assert-ProbeCondition -Condition ($result -eq $true) -Message 'old-old-new did not succeed'
Assert-ProbeCondition -Condition ($script:restCalls -eq 3) -Message 'old-old-new REST count was not three'
Assert-ProbeCondition -Condition ($script:sleepCalls -eq 2) -Message 'old-old-new sleep count was not two'
Assert-ProbeCondition -Condition ($script:gitCalls -eq 0) -Message 'old-old-new used anonymous fallback'
$badCacheValues = @($script:cacheValues | Where-Object {{ $_ -cne 'no-cache' }})
Assert-ProbeCondition -Condition ($badCacheValues.Count -eq 0) -Message 'a REST read lacked no-cache'
$uniqueUris = @($script:requestUris | Select-Object -Unique)
Assert-ProbeCondition -Condition ($uniqueUris.Count -eq 3) -Message 'REST readback URIs were not unique'
$uniqueHeaders = @($script:headerIdentities | Select-Object -Unique)
Assert-ProbeCondition -Condition ($uniqueHeaders.Count -eq 3) -Message 'REST readback headers were not cloned'
Assert-ProbeCondition -Condition (-not $sharedHeaders.ContainsKey('Cache-Control')) -Message 'shared headers were polluted'

Reset-ReadbackMocks -Shas @($oldSha, $oldSha, $oldSha)
$sharedHeaders = @{{ Accept = 'application/json'; Authorization = 'secret' }}
$result = Confirm-ReleaseLatestRefUpdate -SharedHeaders $sharedHeaders -ApiRoot 'https://api.example.test' -Repository 'example/repo' -OldSha $oldSha -ExpectedSha $expectedSha
Assert-ProbeCondition -Condition ($result -eq $true) -Message 'old-old-old fallback did not succeed'
Assert-ProbeCondition -Condition ($script:restCalls -eq 3) -Message 'old-old-old REST count was not three'
Assert-ProbeCondition -Condition ($script:sleepCalls -eq 2) -Message 'old-old-old sleep count was not two'
Assert-ProbeCondition -Condition ($script:gitCalls -eq 1) -Message 'old-old-old fallback count was not one'
Assert-ProbeCondition -Condition ($env:GIT_TERMINAL_PROMPT -ceq '0') -Message 'anonymous fallback allowed prompts'
$expectedGitArguments = 'ls-remote|--refs|--exit-code|https://github.com/example/repo.git|refs/heads/release/latest'
Assert-ProbeCondition -Condition (($script:gitArguments -join '|') -ceq $expectedGitArguments) -Message 'anonymous fallback arguments changed'

Reset-ReadbackMocks -Shas @($thirdSha)
$thirdDispatchRejected = $false
try {{ $null = Confirm-ReleaseLatestRefUpdate -SharedHeaders @{{ Accept = 'application/json' }} -ApiRoot 'https://api.example.test' -Repository 'example/repo' -OldSha $oldSha -ExpectedSha $expectedSha }} catch {{ $thirdDispatchRejected = $true }}
Assert-ProbeCondition -Condition $thirdDispatchRejected -Message 'third SHA dispatch was accepted'
Assert-ProbeCondition -Condition ($script:restCalls -eq 1) -Message 'third SHA did not stop after one REST read'
Assert-ProbeCondition -Condition ($script:sleepCalls -eq 0) -Message 'third SHA slept before failing'
Assert-ProbeCondition -Condition ($script:gitCalls -eq 0) -Message 'third SHA used anonymous fallback'

Reset-ReadbackMocks -Shas @($expectedSha) -ThrowRest $true
$restFailureRejected = $false
try {{ $null = Confirm-ReleaseLatestRefUpdate -SharedHeaders @{{ Accept = 'application/json' }} -ApiRoot 'https://api.example.test' -Repository 'example/repo' -OldSha $oldSha -ExpectedSha $expectedSha }} catch {{ $restFailureRejected = $true }}
Assert-ProbeCondition -Condition $restFailureRejected -Message 'REST exception was accepted'
Assert-ProbeCondition -Condition ($script:restCalls -eq 1) -Message 'REST exception did not stop after one call'
Assert-ProbeCondition -Condition ($script:sleepCalls -eq 0) -Message 'REST exception slept before failing'
Assert-ProbeCondition -Condition ($script:gitCalls -eq 0) -Message 'REST exception used anonymous fallback'
"""
    probe_path = tmp_path / "release_latest_readback_probe.ps1"
    probe_path.write_text(probe, encoding="utf-8-sig")
    probe_result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe_path),
        ],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=30,
        check=False,
    )
    assert probe_result.returncode == 0, probe_result.stdout + probe_result.stderr

    publish_body = textwrap.dedent(publish_step.split("        run: |\n", 1)[1])
    publish_script_path = tmp_path / "publish_latest.ps1"
    publish_script_path.write_text(publish_body, encoding="utf-8-sig")
    environment = os.environ.copy()
    environment["FLASH_PUBLISH_SCRIPT_PATH"] = str(publish_script_path)
    parse_result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$tokens=$null;$parseErrors=$null;"
                "[System.Management.Automation.Language.Parser]::ParseFile("
                "$env:FLASH_PUBLISH_SCRIPT_PATH,[ref]$tokens,[ref]$parseErrors)"
                "|Out-Null;"
                "if(@($parseErrors).Count -ne 0){"
                "$parseErrors|ForEach-Object{Write-Error $_.Message};exit 1}"
            ),
        ],
        env=environment,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=30,
        check=False,
    )
    assert parse_result.returncode == 0, parse_result.stdout + parse_result.stderr


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
    installer_step = _workflow_step(
        workflow,
        "Verify live install update rollback and desktop entries",
        "Upload Windows release bundle",
    )
    shortcut_check = installer_step.split("$beforeHash", 1)[0]

    assert "$shellApp = New-Object -ComObject Shell.Application" in shortcut_check
    assert "$desktopFolder = $shellApp.NameSpace($Desktop)" in shortcut_check
    assert "$mainShortcut = $desktopFolder.ParseName('輔.lnk').GetLink" in shortcut_check
    assert "$updateShortcut = $desktopFolder.ParseName('更新輔.lnk').GetLink" in shortcut_check
    assert "$mainShortcut.Path -ne (Join-Path $Install 'FLASH.exe')" in shortcut_check
    assert "$updateShortcut.Path -ne (Join-Path $Install '更新輔.cmd')" in shortcut_check
    assert "[string]$mainShortcut.WorkingDirectory -ne $Install" in shortcut_check
    assert "[string]$updateShortcut.WorkingDirectory -ne $Install" in shortcut_check
    assert "[string]$mainShortcut.Arguments" in shortcut_check
    assert "[string]$updateShortcut.Arguments" in shortcut_check
    assert "WScript.Shell" not in shortcut_check
    assert "TargetPath" not in shortcut_check


def test_live_install_validation_condition_is_exact_and_publish_remains_forbidden():
    workflow = Path(".github/workflows/build-windows.yml").read_text(encoding="utf-8")
    installer_step = _workflow_step(
        workflow,
        "Verify live install update rollback and desktop entries",
        "Upload Windows release bundle",
    )
    condition = next(
        line.strip()
        for line in installer_step.splitlines()
        if line.strip().startswith("if:")
    )
    assert condition == (
        "if: env.FLASH_BUILD_KIND == 'main_release' || "
        "env.FLASH_BUILD_KIND == 'sp1_release' || "
        "env.FLASH_BUILD_KIND == 'validation_build'"
    )
    assert "sp1_snapshot" not in condition
    assert "sp2_snapshot" not in condition
    assert "sp3_snapshot" not in condition

    metadata_step = _workflow_step(
        workflow,
        "Read delivery metadata",
        "Build windowed executable",
    )
    validation_branch = metadata_step.split("else {", 1)[1]
    assert "$buildKind = 'validation_build'" in validation_branch
    assert "$publishTarget = 'none'" in validation_branch

    publish_header = workflow.split("\n  publish:\n", 1)[1].split("    permissions:", 1)[0]
    assert publish_header == (
        "    name: Publish verified Windows updater files\n"
        "    needs: test-and-build\n"
        "    if: >-\n"
        "      github.event_name == 'push' && github.ref == 'refs/heads/main' ||\n"
        "      github.ref == 'refs/heads/sp1/completion-2026-07-25' &&\n"
        "      (github.event_name == 'push' ||\n"
        "      (github.event_name == 'workflow_dispatch' && inputs.publish_sp1))\n"
    )
    assert "validation_build" not in publish_header


def test_live_install_validation_is_runner_temp_local_and_keeps_original_artifact():
    workflow = Path(".github/workflows/build-windows.yml").read_text(encoding="utf-8")
    installer_step = _workflow_step(
        workflow,
        "Verify live install update rollback and desktop entries",
        "Upload Windows release bundle",
    )
    upload_step = _workflow_step(
        workflow,
        "Upload Windows release bundle",
        "Publish verified Windows updater files",
    )

    assert "$runnerRoot = Get-NormalizedRoot $env:RUNNER_TEMP" in installer_step
    assert installer_step.count("Assert-PathUnderRoot `") == 5
    assert "-Path (Join-Path $validationRoot 'source')" in installer_step
    assert "-Path (Join-Path $validationRoot 'install')" in installer_step
    assert "-Path (Join-Path $validationRoot 'desktop')" in installer_step
    assert "-Path (Join-Path $validationRoot 'temp')" in installer_step
    assert "$candidateSnapshotBefore = Get-DirectorySnapshot $candidateRoot" in installer_step
    assert "$candidateSnapshotAfter = Get-DirectorySnapshot $candidateRoot" in installer_step
    assert "-Description 'Original release candidate'" in installer_step
    assert "'build_kind=main_release'" in installer_step
    assert "'publish_target=release/latest'" in installer_step
    assert "Copy-ValidationFile" in installer_step
    assert "Write-LiveManifest -Root $sourceRoot" in installer_step
    assert "-ReleaseSourceDirectory $sourceRoot" in installer_step
    assert "TestFailAfterReplacement 1" in installer_step
    assert "$rollbackSucceeded = $?" in installer_step
    assert "$rollbackExitCode = $LASTEXITCODE" in installer_step
    assert "$updateSucceeded = $?" in installer_step
    assert "$updateExitCode = $LASTEXITCODE" in installer_step
    assert "-RelativePaths $installedPayloadPaths" in installer_step
    assert "12 payload files plus manifest" in installer_step
    assert "Get-ValidationProcesses" in installer_step
    assert "Assert-NoValidationResidue" in installer_step
    assert "Stop-Process" not in installer_step
    assert "Invoke-WebRequest" not in installer_step
    assert "Invoke-RestMethod" not in installer_step
    assert "git " not in installer_step
    assert "gh " not in installer_step
    assert "release/*" in upload_step
    assert "dist/*.zip*" in upload_step
    assert "$sourceRoot" not in upload_step
    assert "$validationRoot" not in upload_step


@pytest.mark.skipif(os.name != "nt", reason="requires Windows installer contracts")
def test_live_install_validation_executes_formal_rollback_update_and_cleanup(tmp_path):
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("pwsh is unavailable")

    running_flash = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "if(Get-Process -Name FLASH -ErrorAction SilentlyContinue){exit 10}",
        ],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=10,
        check=False,
    )
    if running_flash.returncode == 10:
        pytest.skip("a real FLASH process is running")
    assert running_flash.returncode == 0, running_flash.stdout + running_flash.stderr

    repository = Path(__file__).resolve().parents[1]
    workspace = tmp_path / "workflow"
    candidate = workspace / "release"
    candidate_system = candidate / "輔系統"
    candidate_system.mkdir(parents=True)
    (candidate / "FLASH.exe").write_bytes(b"new-validation-candidate")
    (candidate / "sync_plus_icon.ico").write_bytes(b"validation-icon")
    shutil.copy2(
        repository / "tools" / "verify_windows_release.ps1",
        candidate_system / "verify_windows_release.ps1",
    )
    (candidate / "分支驗證說明.txt").write_text(
        "validation only\n",
        encoding="utf-8-sig",
    )
    commit = "a" * 40
    executable_hash = hashlib.sha256((candidate / "FLASH.exe").read_bytes()).hexdigest()
    _write_key_values(
        candidate_system / "BUILD_INFO.txt",
        [
            ("product", "輔"),
            ("technical_name", "FLASH"),
            ("version", "0.3.0"),
            ("milestone", "SP3"),
            ("build_kind", "validation_build"),
            ("event_name", "pull_request"),
            ("source_ref", "refs/pull/36/merge"),
            ("source_branch", "repair/test"),
            ("publish_target", "none"),
            ("commit", commit),
            ("run_id", "12345"),
            ("built_utc", "2026-08-10T00:00:00Z"),
            ("python", "Python 3.12.10"),
            ("sha256", executable_hash),
        ],
    )
    _write_sha256_manifest(
        candidate,
        (
            "FLASH.exe",
            "輔系統/BUILD_INFO.txt",
            "sync_plus_icon.ico",
            "輔系統/verify_windows_release.ps1",
            "分支驗證說明.txt",
        ),
    )

    required_tools = (
        Path("tools/安裝輔.cmd"),
        Path("tools/更新輔.cmd"),
        Path("tools/輔系統/安裝輔.ps1"),
        Path("tools/輔系統/輔更新核心.ps1"),
        Path("tools/檢查輔同步狀態.cmd"),
        Path("tools/檢查輔同步狀態.ps1"),
    )
    for relative_path in required_tools:
        destination = workspace / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repository / relative_path, destination)

    workflow = (repository / ".github/workflows/build-windows.yml").read_text(
        encoding="utf-8"
    )
    installer_step = _workflow_step(
        workflow,
        "Verify live install update rollback and desktop entries",
        "Upload Windows release bundle",
    )
    script_body = textwrap.dedent(installer_step.split("        run: |\n", 1)[1])
    assert script_body.count("& powershell.exe") == 2
    script_path = workspace / "validation_install.ps1"
    script_path.write_text(script_body, encoding="utf-8-sig")
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    candidate_before = _tree_snapshot(candidate)
    environment = os.environ.copy()
    environment.update(
        {
            "RUNNER_TEMP": str(runner_temp),
            "FLASH_BUILD_KIND": "validation_build",
            "FLASH_PUBLISH_TARGET": "none",
            "GITHUB_SHA": commit,
            "GITHUB_RUN_ID": "12345",
        }
    )
    result = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
        ],
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Validation rollback restored the deliberately different payload." in result.stdout
    assert "Validation normal update exactly matches" in result.stdout
    assert "Original release candidate remained byte-for-byte unchanged." in result.stdout
    assert _tree_snapshot(candidate) == candidate_before
    candidate_info = (candidate_system / "BUILD_INFO.txt").read_text(encoding="utf-8-sig")
    assert "build_kind=validation_build" in candidate_info
    assert "publish_target=none" in candidate_info
    assert "build_kind=main_release" not in candidate_info
    assert not (runner_temp / "flash-live-validation").exists()


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
