import os
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


def test_complete_installer_verifies_copies_and_creates_one_shortcut(tmp_path: Path):
    release_root = _create_release(
        tmp_path,
        build_kind="sp1_release",
        event_name="push",
        source_ref="refs/heads/sp1/completion-2026-07-25",
        source_branch="sp1/completion-2026-07-25",
        publish_target="release/sp1",
    )
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
    assert shortcut_path.is_file()
    env = os.environ.copy()
    env["FLASH_TEST_SHORTCUT"] = str(shortcut_path)
    inspected = subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-Command",
            (
                "$shell=New-Object -ComObject WScript.Shell; "
                "$shortcut=$shell.CreateShortcut($env:FLASH_TEST_SHORTCUT); "
                "Write-Output $shortcut.TargetPath; "
                "Write-Output $shortcut.WorkingDirectory; "
                "Write-Output $shortcut.IconLocation"
            ),
        ],
        env=env,
        capture_output=True,
        check=True,
        timeout=15,
        text=True,
    )
    inspected_lines = tuple(
        line.strip() for line in inspected.stdout.splitlines() if line.strip()
    )
    assert str(install_root / "FLASH.exe") in inspected_lines
    assert str(install_root) in inspected_lines
    assert f"{install_root / 'FLASH.exe'},0" in inspected_lines
    assert tuple(desktop_root.glob("*.lnk")) == (shortcut_path,)


def test_install_failure_restores_existing_install_and_shortcut(tmp_path: Path):
    release_root = _create_release(
        tmp_path,
        build_kind="sp1_release",
        event_name="push",
        source_ref="refs/heads/sp1/completion-2026-07-25",
        source_branch="sp1/completion-2026-07-25",
        publish_target="release/sp1",
    )
    install_root = tmp_path / "installed" / "SP1"
    install_root.mkdir(parents=True)
    sentinel = install_root / "existing-player-install.txt"
    sentinel.write_bytes(b"preserve existing install")
    desktop_root = tmp_path / "desktop"
    desktop_root.mkdir()
    shortcut_path = desktop_root / "輔.lnk"
    shortcut_path.write_bytes(b"preserve existing shortcut")

    result = _run_installer(
        release_root,
        install_root,
        desktop_root,
        "-TestFailAfterSwap",
        "1",
    )

    assert result.returncode != 0
    assert "測試指定在安裝內容交換後中斷" in _output(result)
    assert sentinel.read_bytes() == b"preserve existing install"
    assert shortcut_path.read_bytes() == b"preserve existing shortcut"
    assert not tuple((tmp_path / "installed").glob(".輔-SP1-*"))
