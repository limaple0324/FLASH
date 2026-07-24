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
    build_kind: str = "integration_snapshot",
    source_branch: str = "integration/sp2-sp3-sp35",
    publish_target: str = "none",
) -> Path:
    release_dir = tmp_path / "輔"
    system_dir = release_dir / "輔系統"
    system_dir.mkdir(parents=True)

    verifier_path = system_dir / "verify_windows_release.ps1"
    shutil.copy2(VERIFIER_SOURCE, verifier_path)

    executable_path = release_dir / "FLASH.exe"
    executable_path.write_bytes(b"FLASH Windows verifier fixture")
    digest = hashlib.sha256(executable_path.read_bytes()).hexdigest()

    (system_dir / "SHA256SUMS.txt").write_text(
        f"{digest}  FLASH.exe\n",
        encoding="ascii",
    )
    build_info = {
        "product": "輔",
        "technical_name": "FLASH",
        "version": "0.2.0-dev.1",
        "milestone": "SP1+SP2+SP3",
        "delivery_scope": "integrated",
        "build_kind": build_kind,
        "validation_state": "automated",
        "source_branch": source_branch,
        "publish_target": publish_target,
        "commit": "a" * 40,
        "run_id": "123456789",
        "built_utc": "2026-07-25T00:00:00Z",
        "sha256": digest,
    }
    (system_dir / "BUILD_INFO.txt").write_text(
        "".join(f"{key}={value}\n" for key, value in build_info.items()),
        encoding="utf-8-sig",
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


def test_no_launch_accepts_an_integration_snapshot(tmp_path: Path) -> None:
    result = _run_verifier(_create_bundle(tmp_path))

    assert result.returncode == 0, _output(result)
    assert "NoLaunch was specified" in _output(result)


def test_no_launch_accepts_a_main_release(tmp_path: Path) -> None:
    verifier_path = _create_bundle(
        tmp_path,
        build_kind="main_release",
        source_branch="main",
        publish_target="release/latest",
    )

    result = _run_verifier(verifier_path)

    assert result.returncode == 0, _output(result)


def test_no_launch_accepts_a_main_snapshot(tmp_path: Path) -> None:
    verifier_path = _create_bundle(
        tmp_path,
        build_kind="main_snapshot",
        source_branch="main",
        publish_target="none",
    )

    result = _run_verifier(verifier_path)

    assert result.returncode == 0, _output(result)


@pytest.mark.parametrize(
    ("source_branch", "publish_target", "message"),
    [
        ("main", "none", "source_branch=main"),
        ("integration/sp2-sp3-sp35", "release/latest", "publish_target=none"),
    ],
)
def test_integration_snapshot_rejects_release_metadata(
    tmp_path: Path,
    source_branch: str,
    publish_target: str,
    message: str,
) -> None:
    verifier_path = _create_bundle(
        tmp_path,
        source_branch=source_branch,
        publish_target=publish_target,
    )

    result = _run_verifier(verifier_path)

    assert result.returncode != 0
    assert message in _output(result)
