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
    assert "可以直接使用原本桌面的輔啟動捷徑" in updater
    assert "輔 V0.2" not in updater


def test_updater_runs_a_temporary_core_with_one_fixed_cmd_bootstrap():
    launcher = Path("tools/更新輔.cmd").read_text(encoding="utf-8")
    updater = Path("tools/輔系統/輔更新核心.ps1").read_text(encoding="utf-8")

    assert 'copy /y "%SCRIPT%" "%TEMPUPDATER%"' in launcher
    assert '-File "%TEMPUPDATER%" -InstallDirectory "%~dp0."' in launcher
    assert 'del /f /q "%TEMPUPDATER%"' in launcher
    assert "ReadKey(" not in updater
    assert updater.count("更新成功；全部檔案已套用並通過再次驗證") == 1


def test_installer_uses_a_safe_source_path_and_one_confirmed_shortcut():
    launcher = Path("tools/安裝輔.cmd").read_text(encoding="utf-8")
    installer = Path("tools/輔系統/安裝輔.ps1").read_text(encoding="utf-8")

    assert '-SourceDirectory "%~dp0."' in launcher
    assert 'Join-Path $env:LOCALAPPDATA "Programs\\輔\\SP1"' in installer
    assert 'Join-Path $DesktopDirectory "輔.lnk"' in installer
    assert 'Join-Path $DesktopDirectory "啟動輔.lnk"' in installer
    assert 'Join-Path $DesktopDirectory "輔"' in installer
    assert '$shortcut.TargetPath = $ExecutablePath' in installer
    assert 'Join-Path $WorkingDirectory "輔系統\\sync_plus_icon.ico"' in installer
    assert '$shortcut.IconLocation = "$iconPath,0"' in installer


def test_windows_powershell_download_does_not_require_the_ie_engine():
    updater = Path("tools/輔系統/輔更新核心.ps1").read_text(encoding="utf-8")

    assert "Invoke-WebRequest -Uri $url -OutFile $TargetPath -UseBasicParsing" in updater
    assert "Invoke-RestMethod `" in updater
    assert updater.count("-UseBasicParsing") == 2


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
    publish_step = publish_step.split("- name: Upload Windows release bundle", 1)[0]
    condition = next(
        line.strip()
        for line in publish_step.splitlines()
        if line.strip().startswith("if:")
    )

    assert condition == (
        "if: github.event_name == 'push' && github.ref == 'refs/heads/main'"
    )
    assert "git add -A" in publish_step


def test_windows_workflow_keeps_manual_artifact_build():
    workflow = Path(".github/workflows/build-windows.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "publish_sp1:" in workflow
    assert "- name: Upload Windows release bundle" in workflow


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
