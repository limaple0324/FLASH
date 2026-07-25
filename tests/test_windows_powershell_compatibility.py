import os
import shutil
import subprocess
from pathlib import Path

import pytest


PLAYER_POWERSHELL_SCRIPTS = (
    Path("tools/verify_windows_release.ps1"),
    Path("tools/檢查輔同步狀態.ps1"),
    Path("tools/輔系統/安裝輔.ps1"),
    Path("tools/輔系統/輔更新核心.ps1"),
)
WINDOWS_POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")

pytestmark = pytest.mark.skipif(
    WINDOWS_POWERSHELL is None,
    reason="Windows PowerShell 5.1 is required for player-script compatibility.",
)


def test_player_powershell_scripts_use_utf8_bom():
    for path in PLAYER_POWERSHELL_SCRIPTS:
        content = path.read_bytes()
        assert content.startswith(b"\xef\xbb\xbf"), f"{path} must use UTF-8 BOM"
        content.decode("utf-8-sig")


@pytest.mark.parametrize("path", PLAYER_POWERSHELL_SCRIPTS)
def test_player_powershell_scripts_parse_in_windows_powershell_51(path: Path):
    parser_command = (
        "$tokens=$null; $errors=$null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        "$env:FLASH_PS_SCRIPT_PATH, [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors.Count -ne 0) { "
        "$errors | ForEach-Object { "
        "Write-Error ($_.Extent.StartLineNumber.ToString() + ': ' + $_.Message) "
        "}; exit 1 }"
    )
    environment = os.environ.copy()
    environment["FLASH_PS_SCRIPT_PATH"] = str(path.resolve())
    result = subprocess.run(
        [
            str(WINDOWS_POWERSHELL),
            "-NoProfile",
            "-Command",
            parser_command,
        ],
        capture_output=True,
        check=False,
        env=environment,
    )

    output = (result.stdout + result.stderr).decode(errors="replace")
    assert result.returncode == 0, output
