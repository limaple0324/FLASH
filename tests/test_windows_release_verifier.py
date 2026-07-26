import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERIFIER_SOURCE = PROJECT_ROOT / "tools" / "verify_windows_release.ps1"
ICON_SOURCE = PROJECT_ROOT / "assets" / "flash_icon.ico"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")

pytestmark = pytest.mark.skipif(
    POWERSHELL is None,
    reason="PowerShell is required to exercise the Windows artifact verifier.",
)


def _create_bundle(
    tmp_path: Path,
    *,
    build_kind: str = "sp1_snapshot",
    source_branch: str = "sp1/completion-2026-07-25",
    milestone: str = "SP1",
    version: str = "0.1.3",
    publish_target: str = "none",
    event_name: str | None = None,
    source_ref: str | None = None,
    include_live_updater: bool | None = None,
) -> Path:
    release_dir = tmp_path / "輔"
    system_dir = release_dir / "輔系統"
    system_dir.mkdir(parents=True)

    verifier_path = system_dir / "verify_windows_release.ps1"
    shutil.copy2(VERIFIER_SOURCE, verifier_path)
    shutil.copy2(ICON_SOURCE, release_dir / "sync_plus_icon.ico")

    executable_path = release_dir / "FLASH.exe"
    executable_path.write_bytes(
        f"FLASH {milestone} Windows verifier fixture".encode()
    )
    digest = hashlib.sha256(executable_path.read_bytes()).hexdigest()

    if event_name is None:
        event_name = "push" if build_kind == "main_release" else "workflow_dispatch"
    if source_ref is None:
        if build_kind in {"main_release", "validation_build"}:
            source_ref = "refs/heads/main"
        elif build_kind == "sp2_snapshot":
            source_ref = "refs/heads/sp2/completion-2026-07-26"
        elif build_kind == "sp3_snapshot":
            source_ref = "refs/heads/sp3/completion-2026-07-26"
        else:
            source_ref = "refs/heads/sp1/completion-2026-07-25"

    build_info = {
        "product": "FLASH",
        "version": version,
        "milestone": milestone,
        "build_kind": build_kind,
        "event_name": event_name,
        "source_ref": source_ref,
        "source_branch": source_branch,
        "publish_target": publish_target,
        "commit": "a" * 40,
        "run_id": "123456789",
        "built_utc": "2026-07-25T00:00:00Z",
        "python": "Python 3.12",
        "sha256": digest,
    }
    (system_dir / "BUILD_INFO.txt").write_text(
        "".join(f"{key}={value}\n" for key, value in build_info.items()),
        encoding="utf-8",
    )
    if include_live_updater is None:
        include_live_updater = build_kind in {"main_release", "sp1_release"}
    if include_live_updater:
        (release_dir / "安裝輔.cmd").write_bytes(b"@echo off\r\n")
        (release_dir / "更新輔.cmd").write_bytes(b"@echo off\r\n")
        (system_dir / "安裝輔.ps1").write_text(
            "# installer fixture\n",
            encoding="utf-8",
        )
        (system_dir / "輔更新核心.ps1").write_text(
            "# updater fixture\n",
            encoding="utf-8",
        )
        (system_dir / "檢查輔同步狀態.cmd").write_bytes(b"@echo off\r\n")
        (system_dir / "檢查輔同步狀態.ps1").write_text(
            "# status fixture\n",
            encoding="utf-8",
        )
        (system_dir / "UPDATE_CHANNEL.txt").write_text(
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
    if build_kind == "sp1_snapshot":
        (release_dir / "SP1快照說明.txt").write_text(
            "SP1 snapshot fixture\n",
            encoding="utf-8",
        )
    elif build_kind == "sp2_snapshot":
        (release_dir / "SP1+SP2累積快照說明.txt").write_text(
            "SP1+SP2 cumulative snapshot fixture\n",
            encoding="utf-8",
        )
    elif build_kind == "sp3_snapshot":
        (release_dir / "SP1+SP2+SP3完整累積快照說明.txt").write_text(
            "SP1+SP2+SP3 cumulative snapshot fixture\n",
            encoding="utf-8",
        )
    elif build_kind == "validation_build":
        (release_dir / "分支驗證說明.txt").write_text(
            "validation fixture\n",
            encoding="utf-8",
        )
    if build_kind in {"main_release", "sp1_release"}:
        (release_dir / "LATEST.txt").write_text(
            "\n".join(
                (
                    f"branch={source_branch}",
                    f"commit={build_info['commit']}",
                    f"run_id={build_info['run_id']}",
                    "updated_utc=2026-07-25T00:00:00Z",
                    "",
                )
            ),
            encoding="utf-8",
        )

    manifest_paths = [
        "FLASH.exe",
        "輔系統/BUILD_INFO.txt",
        "sync_plus_icon.ico",
        "輔系統/verify_windows_release.ps1",
    ]
    if build_kind in {"main_release", "sp1_release"}:
        manifest_paths = [
            "FLASH.exe",
            "LATEST.txt",
            "安裝輔.cmd",
            "更新輔.cmd",
            "輔系統/BUILD_INFO.txt",
            "sync_plus_icon.ico",
            "輔系統/verify_windows_release.ps1",
            "輔系統/安裝輔.ps1",
            "輔系統/輔更新核心.ps1",
            "輔系統/UPDATE_CHANNEL.txt",
            "輔系統/檢查輔同步狀態.cmd",
            "輔系統/檢查輔同步狀態.ps1",
        ]
    elif build_kind == "sp1_snapshot":
        manifest_paths.append("SP1快照說明.txt")
    elif build_kind == "sp2_snapshot":
        manifest_paths.append("SP1+SP2累積快照說明.txt")
    elif build_kind == "sp3_snapshot":
        manifest_paths.append("SP1+SP2+SP3完整累積快照說明.txt")
    else:
        manifest_paths.append("分支驗證說明.txt")

    manifest_lines = []
    for relative_path in manifest_paths:
        payload_path = release_dir.joinpath(*relative_path.split("/"))
        payload_digest = (
            hashlib.sha256(payload_path.read_bytes()).hexdigest()
            if payload_path.is_file()
            else "0" * 64
        )
        manifest_lines.append(f"{payload_digest}  {relative_path}")
    (system_dir / "SHA256SUMS.txt").write_text(
        "\n".join((*manifest_lines, "")),
        encoding="utf-8",
    )
    return verifier_path


def _run_verifier(verifier_path: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(verifier_path),
            "-NoLaunch",
        ],
        capture_output=True,
        check=False,
    )


def _output(result: subprocess.CompletedProcess[bytes]) -> str:
    return (result.stdout + result.stderr).decode(errors="replace")


def test_no_launch_accepts_an_independent_sp1_snapshot(tmp_path: Path):
    result = _run_verifier(_create_bundle(tmp_path))

    assert result.returncode == 0, _output(result)
    assert "NoLaunch was specified" in _output(result)


def test_no_launch_accepts_an_sp1_plus_sp2_cumulative_snapshot(tmp_path: Path):
    verifier_path = _create_bundle(
        tmp_path,
        build_kind="sp2_snapshot",
        source_branch="sp2/completion-2026-07-26",
        milestone="SP2",
        version="0.2.0",
    )

    result = _run_verifier(verifier_path)

    assert result.returncode == 0, _output(result)
    assert "NoLaunch was specified" in _output(result)


def test_sp2_snapshot_rejects_sp1_milestone_metadata(tmp_path: Path):
    verifier_path = _create_bundle(
        tmp_path,
        build_kind="sp2_snapshot",
        source_branch="sp2/completion-2026-07-26",
        milestone="SP1",
    )

    result = _run_verifier(verifier_path)

    assert result.returncode != 0
    assert "SP2 delivery must use milestone=SP2" in _output(result)


def test_sp2_snapshot_rejects_a_different_source_identity(tmp_path: Path):
    verifier_path = _create_bundle(
        tmp_path,
        build_kind="sp2_snapshot",
        source_branch="sp2/wrong",
        source_ref="refs/heads/sp2/wrong",
        milestone="SP2",
        version="0.2.0",
    )

    result = _run_verifier(verifier_path)

    assert result.returncode != 0
    assert "dedicated SP2 workflow source identity" in _output(result)


def test_no_launch_accepts_an_sp1_plus_sp2_plus_sp3_snapshot(tmp_path: Path):
    verifier_path = _create_bundle(
        tmp_path,
        build_kind="sp3_snapshot",
        source_branch="sp3/completion-2026-07-26",
        milestone="SP3",
        version="0.3.0",
    )

    result = _run_verifier(verifier_path)

    assert result.returncode == 0, _output(result)
    assert "NoLaunch was specified" in _output(result)


def test_sp3_snapshot_rejects_sp2_milestone_metadata(tmp_path: Path):
    verifier_path = _create_bundle(
        tmp_path,
        build_kind="sp3_snapshot",
        source_branch="sp3/completion-2026-07-26",
        milestone="SP2",
        version="0.3.0",
    )

    result = _run_verifier(verifier_path)

    assert result.returncode != 0
    assert "SP3 delivery must use milestone=SP3" in _output(result)


def test_sp3_snapshot_rejects_a_different_source_identity(tmp_path: Path):
    verifier_path = _create_bundle(
        tmp_path,
        build_kind="sp3_snapshot",
        source_branch="sp3/wrong",
        source_ref="refs/heads/sp3/wrong",
        milestone="SP3",
        version="0.3.0",
    )

    result = _run_verifier(verifier_path)

    assert result.returncode != 0
    assert "dedicated SP3 workflow source identity" in _output(result)


def test_no_launch_accepts_an_sp1_branch_push_snapshot(tmp_path: Path):
    verifier_path = _create_bundle(
        tmp_path,
        event_name="push",
    )

    result = _run_verifier(verifier_path)

    assert result.returncode == 0, _output(result)


def test_no_launch_accepts_a_main_release(tmp_path: Path):
    verifier_path = _create_bundle(
        tmp_path,
        build_kind="main_release",
        source_branch="main",
        publish_target="release/latest",
    )

    result = _run_verifier(verifier_path)

    assert result.returncode == 0, _output(result)


def test_no_launch_accepts_an_sp1_only_release(tmp_path: Path):
    verifier_path = _create_bundle(
        tmp_path,
        build_kind="sp1_release",
        source_branch="sp1/completion-2026-07-25",
        publish_target="release/sp1",
    )

    result = _run_verifier(verifier_path)

    assert result.returncode == 0, _output(result)


def test_no_launch_accepts_an_sp1_only_push_release(tmp_path: Path):
    verifier_path = _create_bundle(
        tmp_path,
        build_kind="sp1_release",
        source_branch="sp1/completion-2026-07-25",
        publish_target="release/sp1",
        event_name="push",
    )

    result = _run_verifier(verifier_path)

    assert result.returncode == 0, _output(result)


def test_no_launch_accepts_a_non_release_validation_build(tmp_path: Path):
    verifier_path = _create_bundle(
        tmp_path,
        build_kind="validation_build",
        source_branch="main",
        publish_target="none",
    )

    result = _run_verifier(verifier_path)

    assert result.returncode == 0, _output(result)


def test_snapshot_manifest_rejects_a_tampered_notice(tmp_path: Path):
    verifier_path = _create_bundle(tmp_path)
    with (verifier_path.parents[1] / "SP1快照說明.txt").open("ab") as stream:
        stream.write(b"\ntampered\n")

    result = _run_verifier(verifier_path)

    assert result.returncode != 0
    assert "Release file hash mismatch" in _output(result)


def test_main_manifest_rejects_a_tampered_updater_core(tmp_path: Path):
    verifier_path = _create_bundle(
        tmp_path,
        build_kind="main_release",
        source_branch="main",
        publish_target="release/latest",
    )
    with (verifier_path.parent / "輔更新核心.ps1").open("ab") as stream:
        stream.write(b"\ntampered\n")

    result = _run_verifier(verifier_path)

    assert result.returncode != 0
    assert "Release file hash mismatch" in _output(result)


def test_sp1_release_rejects_a_channel_mismatch(tmp_path: Path):
    verifier_path = _create_bundle(
        tmp_path,
        build_kind="sp1_release",
        source_branch="sp1/completion-2026-07-25",
        publish_target="release/sp1",
    )
    channel_path = verifier_path.parent / "UPDATE_CHANNEL.txt"
    content = channel_path.read_text(encoding="utf-8")
    channel_path.write_text(
        content.replace("release_branch=release/sp1", "release_branch=release/latest"),
        encoding="utf-8",
    )
    manifest_path = verifier_path.parent / "SHA256SUMS.txt"
    manifest = manifest_path.read_text(encoding="utf-8")
    digest = hashlib.sha256(channel_path.read_bytes()).hexdigest()
    manifest_path.write_text(
        "\n".join(
            (
                *(
                    f"{digest}  輔系統/UPDATE_CHANNEL.txt"
                    if line.endswith("  輔系統/UPDATE_CHANNEL.txt")
                    else line
                    for line in manifest.splitlines()
                ),
                "",
            )
        ),
        encoding="utf-8",
    )

    result = _run_verifier(verifier_path)

    assert result.returncode != 0
    assert "release_branch=release/sp1" in _output(result)


def test_sp1_snapshot_rejects_a_live_publish_target(tmp_path: Path):
    verifier_path = _create_bundle(
        tmp_path,
        publish_target="release/latest",
    )

    result = _run_verifier(verifier_path)

    assert result.returncode != 0
    assert "publish_target=none" in _output(result)


def test_sp1_snapshot_rejects_live_updater_files(tmp_path: Path):
    verifier_path = _create_bundle(
        tmp_path,
        include_live_updater=True,
    )

    result = _run_verifier(verifier_path)

    assert result.returncode != 0
    assert "must not contain live updater files" in _output(result)


def test_sp1_snapshot_rejects_a_different_source_branch(tmp_path: Path):
    verifier_path = _create_bundle(
        tmp_path,
        source_branch="integration/sp2-sp3-sp35",
    )

    result = _run_verifier(verifier_path)

    assert result.returncode != 0
    assert "dedicated SP1 workflow source identity" in _output(result)


def test_sp1_snapshot_rejects_a_pull_request_merge_ref(tmp_path: Path):
    verifier_path = _create_bundle(
        tmp_path,
        event_name="pull_request",
        source_ref="refs/pull/123/merge",
    )

    result = _run_verifier(verifier_path)

    assert result.returncode != 0
    assert "dedicated SP1 workflow source identity" in _output(result)


def test_main_release_requires_live_updater_files(tmp_path: Path):
    verifier_path = _create_bundle(
        tmp_path,
        build_kind="main_release",
        source_branch="main",
        publish_target="release/latest",
        include_live_updater=False,
    )

    result = _run_verifier(verifier_path)

    assert result.returncode != 0
    assert "Required release file is missing" in _output(result)


@pytest.mark.parametrize(
    ("source_branch", "publish_target", "message"),
    [
        ("sp1/completion-2026-07-25", "release/latest", "source_branch=main"),
        ("main", "none", "publish_target=release/latest"),
    ],
)
def test_main_release_rejects_inconsistent_identity(
    tmp_path: Path,
    source_branch: str,
    publish_target: str,
    message: str,
):
    verifier_path = _create_bundle(
        tmp_path,
        build_kind="main_release",
        source_branch=source_branch,
        publish_target=publish_target,
    )

    result = _run_verifier(verifier_path)

    assert result.returncode != 0
    assert message in _output(result)
