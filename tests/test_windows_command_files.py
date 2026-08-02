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
    assert updater.count("-UseBasicParsing") == 2
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


def test_windows_build_workflow_is_validation_only_and_never_publishes():
    workflow = Path(".github/workflows/build-windows.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "contents: read" in workflow
    assert "persist-credentials: false" in workflow
    assert "The Windows build workflow must produce a validation-only artifact." in workflow
    assert "- name: Upload Windows validation bundle" in workflow
    assert "git push" not in workflow
    assert "release/latest" not in workflow
    assert "release/sp1" not in workflow
    assert "main_release" not in workflow
    assert "sp1_release" not in workflow
