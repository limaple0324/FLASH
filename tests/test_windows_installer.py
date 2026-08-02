import base64
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_windows_transactional_updater import (
    MANIFEST_PATH,
    PAYLOAD_PATHS,
    POWERSHELL,
    _create_release,
    _path,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_SOURCE = PROJECT_ROOT / "tools" / "輔系統" / "安裝輔.ps1"

pytestmark = pytest.mark.skipif(
    POWERSHELL is None,
    reason="Windows PowerShell is required to exercise the complete installer.",
)


def _run_installer(
    release_root: Path,
    install_root: Path,
    desktop_root: Path,
    *extra_arguments: str,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(INSTALLER_SOURCE),
            "-SourceDirectory",
            str(release_root),
            "-InstallDirectory",
            str(install_root),
            "-DesktopDirectory",
            str(desktop_root),
            *extra_arguments,
        ],
        capture_output=True,
        check=False,
        timeout=45,
    )


def _output(result: subprocess.CompletedProcess[bytes]) -> str:
    return (result.stdout + result.stderr).decode("utf-8", errors="replace")


def _inspect_shortcut(shortcut_path: Path, inspected_path: Path) -> tuple[str, ...]:
    shutil.copy2(shortcut_path, inspected_path)
    env = os.environ.copy()
    env["FLASH_TEST_SHORTCUT"] = str(inspected_path)
    inspected = subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-Command",
            (
                "$shortcutPath=$env:FLASH_TEST_SHORTCUT; "
                "$shellApp=New-Object -ComObject Shell.Application; "
                "$folder=$shellApp.NameSpace((Split-Path -Parent $shortcutPath)); "
                "$item=$folder.ParseName((Split-Path -Leaf $shortcutPath)); "
                "$unicodeLink=$item.GetLink; "
                "$wscript=New-Object -ComObject WScript.Shell; "
                "$shortcut=$wscript.CreateShortcut($shortcutPath); "
                "$values=@($unicodeLink.Path,$shortcut.WorkingDirectory,"
                "$shortcut.IconLocation); "
                "foreach($value in $values){ "
                "[Convert]::ToBase64String("
                "[Text.Encoding]::UTF8.GetBytes([string]$value)) }"
            ),
        ],
        env=env,
        capture_output=True,
        check=True,
        timeout=15,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return tuple(
        base64.b64decode(line.strip()).decode("utf-8")
        for line in inspected.stdout.splitlines()
        if line.strip()
    )


def _assert_shortcut_targets(
    inspected_lines: tuple[str, ...],
    expected_target: Path,
) -> None:
    assert inspected_lines
    actual_target = Path(inspected_lines[0])
    assert actual_target.is_file()
    assert os.path.samefile(actual_target, expected_target)


def test_complete_installer_creates_only_main_and_update_shortcuts(tmp_path: Path):
    release_root = _create_release(tmp_path)
    install_root = tmp_path / "installed" / "SP1"
    desktop_root = tmp_path / "desktop"
    desktop_root.mkdir()

    result = _run_installer(release_root, install_root, desktop_root)

    assert result.returncode == 0, _output(result)
    for relative_path in (*PAYLOAD_PATHS, MANIFEST_PATH):
        assert _path(install_root, relative_path).read_bytes() == _path(
            release_root,
            relative_path,
        ).read_bytes()
    assert (install_root / "安裝紀錄.txt").is_file()

    shortcut_path = desktop_root / "輔.lnk"
    update_shortcut_path = desktop_root / "更新輔.lnk"
    assert set(desktop_root.glob("*.lnk")) == {
        shortcut_path,
        update_shortcut_path,
    }
    inspected_lines = _inspect_shortcut(
        shortcut_path,
        tmp_path / "main-shortcut-under-test.lnk",
    )
    _assert_shortcut_targets(inspected_lines, install_root / "FLASH.exe")
    assert str(install_root) in inspected_lines
    assert (
        f"{install_root / 'sync_plus_icon.ico'},0"
        in inspected_lines
    )
    update_lines = _inspect_shortcut(
        update_shortcut_path,
        tmp_path / "update-shortcut-under-test.lnk",
    )
    _assert_shortcut_targets(update_lines, install_root / "更新輔.cmd")
    assert str(install_root) in update_lines
    assert f"{install_root / 'sync_plus_icon.ico'},0" in update_lines


def test_first_install_keeps_fixed_shortcut_names_when_desktop_has_fu_directory(
    tmp_path: Path,
):
    release_root = _create_release(tmp_path)
    install_root = tmp_path / "installed" / "SP1"
    desktop_root = tmp_path / "desktop"
    visible_name_conflict = desktop_root / "輔"
    visible_name_conflict.mkdir(parents=True)
    sentinel = visible_name_conflict / "preserve-project-entry.txt"
    sentinel.write_bytes(b"preserve desktop directory")

    result = _run_installer(release_root, install_root, desktop_root)

    assert result.returncode == 0, _output(result)
    shortcut_path = desktop_root / "輔.lnk"
    update_shortcut_path = desktop_root / "更新輔.lnk"
    assert set(desktop_root.glob("*.lnk")) == {
        shortcut_path,
        update_shortcut_path,
    }
    assert not (desktop_root / "啟動輔.lnk").exists()
    assert sentinel.read_bytes() == b"preserve desktop directory"
    install_record = (install_root / "安裝紀錄.txt").read_text(encoding="utf-8-sig")
    assert f"shortcut_path={shortcut_path}" in install_record
    assert f"update_shortcut_path={update_shortcut_path}" in install_record


def test_reinstall_preserves_existing_shortcut_name_despite_visible_name_conflict(
    tmp_path: Path,
):
    release_root = _create_release(tmp_path)
    install_root = tmp_path / "installed" / "SP1"
    desktop_root = tmp_path / "desktop"
    (desktop_root / "輔").mkdir(parents=True)
    existing_shortcut = desktop_root / "輔.lnk"
    existing_shortcut.write_bytes(b"replace this prior shortcut")

    result = _run_installer(release_root, install_root, desktop_root)

    assert result.returncode == 0, _output(result)
    assert existing_shortcut.is_file()
    assert existing_shortcut.read_bytes() != b"replace this prior shortcut"
    assert not (desktop_root / "啟動輔.lnk").exists()
    assert set(desktop_root.glob("*.lnk")) == {
        existing_shortcut,
        desktop_root / "更新輔.lnk",
    }


def test_install_failure_restores_existing_install_and_shortcut(tmp_path: Path):
    release_root = _create_release(tmp_path)
    install_root = tmp_path / "installed" / "SP1"
    install_root.mkdir(parents=True)
    sentinel = install_root / "existing-player-install.txt"
    sentinel.write_bytes(b"preserve existing install")
    desktop_root = tmp_path / "desktop"
    desktop_root.mkdir()
    shortcut_path = desktop_root / "輔.lnk"
    shortcut_path.write_bytes(b"preserve existing shortcut")
    update_shortcut_path = desktop_root / "更新輔.lnk"
    update_shortcut_path.write_bytes(b"preserve existing update shortcut")

    result = _run_installer(
        release_root,
        install_root,
        desktop_root,
        "-TestFailAfterShortcut",
        "1",
    )

    assert result.returncode != 0
    assert "測試指定在建立第一個桌面捷徑後中斷" in _output(result)
    assert sentinel.read_bytes() == b"preserve existing install"
    assert shortcut_path.read_bytes() == b"preserve existing shortcut"
    assert (
        update_shortcut_path.read_bytes()
        == b"preserve existing update shortcut"
    )
    assert not tuple((tmp_path / "installed").glob(".輔-*"))
    persistent_log = tmp_path / "installed" / "輔-安裝紀錄.txt"
    assert "安裝失敗" in persistent_log.read_text(
        encoding="utf-8-sig",
        errors="replace",
    )


def test_complete_cumulative_release_installs_without_sp1_identity_rejection(
    tmp_path: Path,
):
    release_root = _create_release(
        tmp_path,
        milestone="SP3",
        build_kind="main_release",
        source_ref="refs/heads/main",
        source_branch="main",
        publish_target="release/latest",
    )
    install_root = tmp_path / "installed" / "完整累積版"
    desktop_root = tmp_path / "desktop"
    desktop_root.mkdir()

    result = _run_installer(release_root, install_root, desktop_root)

    assert result.returncode == 0, _output(result)
    assert (install_root / "FLASH.exe").read_bytes() == (
        release_root / "FLASH.exe"
    ).read_bytes()
    assert set(desktop_root.glob("*.lnk")) == {
        desktop_root / "輔.lnk",
        desktop_root / "更新輔.lnk",
    }
