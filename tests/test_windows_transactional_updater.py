import hashlib
import shutil
import subprocess
import time
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
UPDATER_SOURCE = PROJECT_ROOT / "tools" / "輔系統" / "輔更新核心.ps1"
VERIFIER_SOURCE = PROJECT_ROOT / "tools" / "verify_windows_release.ps1"
INSTALLER_LAUNCHER_SOURCE = PROJECT_ROOT / "tools" / "安裝輔.cmd"
LAUNCHER_SOURCE = PROJECT_ROOT / "tools" / "更新輔.cmd"
INSTALLER_SOURCE = PROJECT_ROOT / "tools" / "輔系統" / "安裝輔.ps1"
STATUS_CMD_SOURCE = PROJECT_ROOT / "tools" / "檢查輔同步狀態.cmd"
STATUS_PS1_SOURCE = PROJECT_ROOT / "tools" / "檢查輔同步狀態.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
SOURCE_COMMIT = "a" * 40
RELEASE_COMMIT = "b" * 40
MANIFEST_PATH = "輔系統/SHA256SUMS.txt"
PAYLOAD_PATHS = (
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
)
FIXED_IDENTITY_PATHS = ("更新輔.cmd", "輔系統/UPDATE_CHANNEL.txt")
MUTABLE_PATHS = tuple(
    path for path in PAYLOAD_PATHS if path not in FIXED_IDENTITY_PATHS
) + (MANIFEST_PATH,)

pytestmark = pytest.mark.skipif(
    POWERSHELL is None,
    reason="Windows PowerShell is required to exercise the transactional updater.",
)


def _path(root: Path, relative_path: str) -> Path:
    return root.joinpath(*relative_path.split("/"))


def _copy_payload_sources(release_root: Path) -> None:
    shutil.copy2(INSTALLER_LAUNCHER_SOURCE, _path(release_root, "安裝輔.cmd"))
    shutil.copy2(LAUNCHER_SOURCE, _path(release_root, "更新輔.cmd"))
    shutil.copy2(
        INSTALLER_SOURCE,
        _path(release_root, "輔系統/安裝輔.ps1"),
    )
    shutil.copy2(
        VERIFIER_SOURCE,
        _path(release_root, "輔系統/verify_windows_release.ps1"),
    )
    shutil.copy2(
        UPDATER_SOURCE,
        _path(release_root, "輔系統/輔更新核心.ps1"),
    )
    shutil.copy2(
        STATUS_CMD_SOURCE,
        _path(release_root, "輔系統/檢查輔同步狀態.cmd"),
    )
    shutil.copy2(
        STATUS_PS1_SOURCE,
        _path(release_root, "輔系統/檢查輔同步狀態.ps1"),
    )


def _create_release(
    root: Path,
    *,
    milestone: str = "SP1",
    missing_path: str | None = None,
    corrupt_path: str | None = None,
    latest_commit: str = SOURCE_COMMIT,
    build_kind: str = "main_release",
    event_name: str = "push",
    source_ref: str = "refs/heads/main",
    source_branch: str = "main",
    publish_target: str = "release/latest",
) -> Path:
    release_root = root / "release"
    (release_root / "輔系統").mkdir(parents=True)
    (release_root / "FLASH.exe").write_bytes(b"new FLASH SP1 executable")
    _copy_payload_sources(release_root)

    executable_hash = hashlib.sha256(
        (release_root / "FLASH.exe").read_bytes()
    ).hexdigest()
    _path(release_root, "輔系統/UPDATE_CHANNEL.txt").write_text(
        "\n".join(
            (
                f"release_branch={publish_target}",
                f"source_branch={source_branch}",
                f"build_kind={build_kind}",
                f"publish_target={publish_target}",
                "",
            )
        ),
        encoding="utf-8",
    )
    build_info = {
        "product": "FLASH",
        "version": "0.1.2",
        "milestone": milestone,
        "build_kind": build_kind,
        "event_name": event_name,
        "source_ref": source_ref,
        "source_branch": source_branch,
        "publish_target": publish_target,
        "commit": SOURCE_COMMIT,
        "run_id": "123456789",
        "built_utc": "2026-07-25T00:00:00Z",
        "python": "Python 3.12",
        "sha256": executable_hash,
    }
    _path(release_root, "輔系統/BUILD_INFO.txt").write_text(
        "".join(f"{key}={value}\n" for key, value in build_info.items()),
        encoding="utf-8",
    )
    (release_root / "LATEST.txt").write_text(
        "\n".join(
            (
                f"branch={source_branch}",
                f"commit={latest_commit}",
                "run_id=123456789",
                "updated_utc=2026-07-25T00:00:00Z",
                "",
            )
        ),
        encoding="utf-8",
    )

    manifest_lines = []
    for relative_path in PAYLOAD_PATHS:
        payload = _path(release_root, relative_path)
        digest = hashlib.sha256(payload.read_bytes()).hexdigest()
        manifest_lines.append(f"{digest}  {relative_path}")
    _path(release_root, MANIFEST_PATH).write_text(
        "\n".join((*manifest_lines, "")),
        encoding="utf-8",
    )

    if missing_path:
        _path(release_root, missing_path).unlink()
    if corrupt_path:
        with _path(release_root, corrupt_path).open("ab") as stream:
            stream.write(b"\ncorrupted after manifest\n")
    return release_root


def _create_existing_install(
    root: Path,
    *,
    build_kind: str = "main_release",
    source_branch: str = "main",
    publish_target: str = "release/latest",
) -> Path:
    install_root = root / "安裝"
    (install_root / "輔系統").mkdir(parents=True)
    shutil.copy2(LAUNCHER_SOURCE, install_root / "更新輔.cmd")
    _path(install_root, "輔系統/UPDATE_CHANNEL.txt").write_text(
        "\n".join(
            (
                f"release_branch={publish_target}",
                f"source_branch={source_branch}",
                f"build_kind={build_kind}",
                f"publish_target={publish_target}",
                "",
            )
        ),
        encoding="utf-8",
    )
    for relative_path in MUTABLE_PATHS:
        target = _path(install_root, relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"old::{relative_path}".encode("utf-8"))
    return install_root


def _installed_payload_snapshot(install_root: Path) -> dict[str, bytes | None]:
    return {
        relative_path: (
            _path(install_root, relative_path).read_bytes()
            if _path(install_root, relative_path).is_file()
            else None
        )
        for relative_path in (*PAYLOAD_PATHS, MANIFEST_PATH)
    }


def _updater_command(
    install_root: Path,
    release_root: Path,
    *extra_arguments: str,
) -> list[str]:
    return [
        str(POWERSHELL),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(UPDATER_SOURCE),
        "-InstallDirectory",
        str(install_root),
        "-ReleaseSourceDirectory",
        str(release_root),
        "-ResolvedReleaseCommit",
        RELEASE_COMMIT,
        *extra_arguments,
    ]


def _run_updater(
    install_root: Path,
    release_root: Path,
    *extra_arguments: str,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        _updater_command(install_root, release_root, *extra_arguments),
        capture_output=True,
        check=False,
        timeout=30,
    )


def _output(result: subprocess.CompletedProcess[bytes]) -> str:
    return (result.stdout + result.stderr).decode("utf-8", errors="replace")


def _log(install_root: Path) -> str:
    log_path = install_root / "更新紀錄.txt"
    for attempt in range(20):
        try:
            return log_path.read_text(
                encoding="utf-8-sig",
                errors="replace",
            )
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)
    raise AssertionError("unreachable")


@pytest.mark.parametrize(
    ("fixture_change", "relative_path", "expected_log"),
    [
        ("corrupt_path", "FLASH.exe", "檔案雜湊核對失敗"),
        ("corrupt_path", "輔系統/輔更新核心.ps1", "檔案雜湊核對失敗"),
        ("missing_path", "輔系統/檢查輔同步狀態.ps1", "缺少必要檔案"),
    ],
)
def test_bad_or_missing_payload_never_changes_the_install(
    tmp_path: Path,
    fixture_change: str,
    relative_path: str,
    expected_log: str,
):
    release_root = _create_release(
        tmp_path,
        **{fixture_change: relative_path},
    )
    install_root = _create_existing_install(tmp_path)
    before = _installed_payload_snapshot(install_root)

    result = _run_updater(install_root, release_root)

    assert result.returncode != 0, _output(result)
    assert _installed_payload_snapshot(install_root) == before
    log = _log(install_root)
    assert expected_log in log
    assert "尚未修改正式安裝內容" in log
    assert "更新成功" not in log


def test_failure_after_nth_file_rolls_back_every_file(tmp_path: Path):
    release_root = _create_release(tmp_path)
    install_root = _create_existing_install(tmp_path)
    before = _installed_payload_snapshot(install_root)

    result = _run_updater(
        install_root,
        release_root,
        "-TestFailAfterReplacement",
        "3",
    )

    assert result.returncode != 0, _output(result)
    assert _installed_payload_snapshot(install_root) == before, _output(result)
    log = _log(install_root)
    assert "更新失敗" in log
    assert "開始回復原本安裝內容" in log
    assert "回復完成；正式安裝內容已還原" in log
    assert "更新成功" not in log


def test_rollback_failure_preserves_recovery_backups(tmp_path: Path):
    release_root = _create_release(tmp_path)
    install_root = _create_existing_install(tmp_path)

    result = _run_updater(
        install_root,
        release_root,
        "-TestFailAfterReplacement",
        "3",
        "-TestFailDuringRollbackAt",
        "1",
    )

    assert result.returncode == 2, _output(result)
    transaction_base = install_root / "輔系統" / "更新交易"
    transaction_roots = tuple(transaction_base.iterdir())
    assert len(transaction_roots) == 1
    backup_root = transaction_roots[0] / "backup"
    assert backup_root.is_dir()
    assert any(path.is_file() for path in backup_root.rglob("*"))

    log = _log(install_root)
    assert "回復失敗" in log
    assert f"已保留救援資料：{transaction_roots[0]}" in log
    assert "更新成功" not in log


@pytest.mark.parametrize("milestone", ["SP2", "SP3"])
def test_updater_rejects_sp2_and_sp3_metadata_without_installing(
    tmp_path: Path,
    milestone: str,
):
    release_root = _create_release(tmp_path, milestone=milestone)
    install_root = _create_existing_install(tmp_path)
    before = _installed_payload_snapshot(install_root)

    result = _run_updater(install_root, release_root)

    assert result.returncode != 0, _output(result)
    assert _installed_payload_snapshot(install_root) == before
    assert "milestone 必須是 SP1" in _log(install_root)


def test_updater_rejects_latest_and_build_info_commit_mismatch(tmp_path: Path):
    release_root = _create_release(tmp_path, latest_commit="c" * 40)
    install_root = _create_existing_install(tmp_path)
    before = _installed_payload_snapshot(install_root)

    result = _run_updater(install_root, release_root)

    assert result.returncode != 0, _output(result)
    assert _installed_payload_snapshot(install_root) == before
    assert "來源 commit 不一致" in _log(install_root)


def test_changed_fixed_bootstrap_requires_a_full_installer(tmp_path: Path):
    release_root = _create_release(tmp_path)
    install_root = _create_existing_install(tmp_path)
    (install_root / "更新輔.cmd").write_bytes(b"@echo off\r\nrem old bootstrap\r\n")
    before = _installed_payload_snapshot(install_root)

    result = _run_updater(install_root, release_root)

    assert result.returncode != 0, _output(result)
    assert _installed_payload_snapshot(install_root) == before
    log = _log(install_root)
    assert "固定更新身分檔案版本不相容：更新輔.cmd" in log
    assert "請改用完整安裝包更新" in log


def test_changed_update_channel_requires_a_full_installer(tmp_path: Path):
    release_root = _create_release(tmp_path)
    install_root = _create_existing_install(tmp_path)
    _path(install_root, "輔系統/UPDATE_CHANNEL.txt").write_text(
        "\n".join(
            (
                "release_branch=release/sp1",
                "source_branch=sp1/completion-2026-07-25",
                "build_kind=sp1_release",
                "publish_target=release/sp1",
                "",
            )
        ),
        encoding="utf-8",
    )
    before = _installed_payload_snapshot(install_root)

    result = _run_updater(install_root, release_root)

    assert result.returncode != 0, _output(result)
    assert _installed_payload_snapshot(install_root) == before
    assert "BUILD_INFO.txt 的 source_branch 必須是 sp1/completion-2026-07-25" in (
        _log(install_root)
    )


def test_second_updater_instance_is_rejected_by_the_single_lock(tmp_path: Path):
    release_root = _create_release(tmp_path)
    install_root = _create_existing_install(tmp_path)
    first = subprocess.Popen(
        _updater_command(
            install_root,
            release_root,
            "-TestHoldLockMilliseconds",
            "2500",
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            log_path = install_root / "更新紀錄.txt"
            if log_path.is_file() and "已取得單一更新鎖" in _log(install_root):
                break
            time.sleep(0.05)
        else:
            pytest.fail("first updater did not acquire the lock in time")

        second = _run_updater(install_root, release_root)
        assert second.returncode != 0, _output(second)
        assert "另一個更新程序正在執行" in _output(second)
        assert "另一個更新程序正在執行" in _log(install_root)

        first_stdout, first_stderr = first.communicate(timeout=30)
        assert first.returncode == 0, (first_stdout + first_stderr).decode(
            "utf-8",
            errors="replace",
        )
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate()


def test_success_uses_one_fixed_release_commit_and_verifies_installed_files(
    tmp_path: Path,
):
    release_root = _create_release(tmp_path)
    install_root = _create_existing_install(tmp_path)

    result = _run_updater(install_root, release_root)

    assert result.returncode == 0, _output(result)
    for relative_path in (*PAYLOAD_PATHS, MANIFEST_PATH):
        assert _path(install_root, relative_path).read_bytes() == _path(
            release_root,
            relative_path,
        ).read_bytes()
    log = _log(install_root)
    assert f"固定發布版本：{RELEASE_COMMIT}" in log
    assert "安裝前驗證：執行 verify_windows_release.ps1 -NoLaunch" in log
    assert "安裝後驗證：執行 verify_windows_release.ps1 -NoLaunch" in log
    assert "更新成功；全部檔案已套用並通過再次驗證" in log

    updater = UPDATER_SOURCE.read_text(encoding="utf-8-sig")
    assert "commits/$ReleaseBranch" in updater
    assert "raw.githubusercontent.com/$Repo/$ReleaseCommit/$urlPath" in updater
    assert "?t=" not in updater


def test_sp1_only_channel_updates_only_from_the_sp1_release_identity(tmp_path: Path):
    release_root = _create_release(
        tmp_path,
        build_kind="sp1_release",
        event_name="push",
        source_ref="refs/heads/sp1/completion-2026-07-25",
        source_branch="sp1/completion-2026-07-25",
        publish_target="release/sp1",
    )
    install_root = _create_existing_install(
        tmp_path,
        build_kind="sp1_release",
        source_branch="sp1/completion-2026-07-25",
        publish_target="release/sp1",
    )

    result = _run_updater(install_root, release_root)

    assert result.returncode == 0, _output(result)
    assert "固定更新來源：release/sp1" in _log(install_root)
    assert _path(
        install_root,
        "輔系統/UPDATE_CHANNEL.txt",
    ).read_bytes() == _path(
        release_root,
        "輔系統/UPDATE_CHANNEL.txt",
    ).read_bytes()
