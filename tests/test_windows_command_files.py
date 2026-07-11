from pathlib import Path


WINDOWS_COMMAND_FILES = (
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


def test_pull_request_build_does_not_publish_over_the_live_release():
    workflow = Path(".github/workflows/build-windows.yml").read_text(encoding="utf-8")

    publish_step = workflow.split("- name: Publish latest desktop updater files", 1)[1]
    publish_step = publish_step.split("- name: Upload Windows release bundle", 1)[0]
    assert "if: github.event_name != 'pull_request'" in publish_step
    assert "git add -A" in publish_step
