import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERIFIER_SOURCE = PROJECT_ROOT / "tools" / "verify_windows_release.ps1"
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

    executable_path = release_dir / "FLASH.exe"
    executable_path.write_bytes(b"FLASH SP1 Windows verifier fixture")
    digest = hashlib.sha256(executable_path.read_bytes()).hexdigest()

    (system_dir / "SHA256SUMS.txt").write_text(
        f"{digest}  FLASH.exe\n",
        encoding="ascii",
    )
    if event_name is None:
        event_name = "push" if build_kind == "main_release" else "workflow_dispatch"
    if source_ref is None:
        source_ref = (
            "refs/heads/main"
            if build_kind in {"main_release", "validation_build"}
            else "refs/heads/sp1/completion-2026-07-25"
        )

    build_info = {
        "product": "FLASH",
        "version": "0.1.2",
        "milestone": "SP1",
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
        include_live_updater = build_kind == "main_release"
    if include_live_updater:
        (release_dir / "更新輔.cmd").write_bytes(b"@echo off\r\n")
        (system_dir / "輔更新核心.ps1").write_text(
            "# updater fixture\n",
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


def test_no_launch_accepts_a_non_release_validation_build(tmp_path: Path):
    verifier_path = _create_bundle(
        tmp_path,
        build_kind="validation_build",
        source_branch="main",
        publish_target="none",
    )

    result = _run_verifier(verifier_path)

    assert result.returncode == 0, _output(result)


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
