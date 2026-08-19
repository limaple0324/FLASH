import json
import os
from types import SimpleNamespace

import pytest

from adapters.windows_launch_fingerprint import (
    PowerShellLaunchFingerprintResolver,
    PowerShellShortcutFingerprintResolver,
    normalize_launch_fingerprint,
)


def test_normalize_launch_fingerprint_is_strict_and_case_insensitive():
    assert normalize_launch_fingerprint(" " + "A1" * 32 + " ") == "a1" * 32
    assert normalize_launch_fingerprint("a" * 63) is None
    assert normalize_launch_fingerprint("z" * 64) is None
    assert normalize_launch_fingerprint(None) is None


def test_process_resolver_keeps_manual_launch_fallback_secret_safe():
    script = PowerShellLaunchFingerprintResolver._SCRIPT

    assert "function Get-DirectArgumentTail" in script
    assert "$identityArguments = Get-DirectArgumentTail $commandLine $executablePath" in script
    assert "Launch origin is not identity authority" in script
    assert "Write-Output $commandLine" not in script
    assert "Write-Output $identityArguments" not in script


@pytest.mark.skipif(os.name != "nt", reason="Resolver intentionally runs only on Windows")
def test_resolver_returns_only_requested_valid_fingerprints(monkeypatch):
    observed = {}

    def runner(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "42": "A" * 64,
                    "43": "invalid",
                    "999": "b" * 64,
                }
            ),
            stderr="",
        )

    resolver = PowerShellLaunchFingerprintResolver(runner=runner)
    result = resolver.resolve([43, 42, 42, -1, True])

    assert result == {42: "a" * 64}
    assert observed["kwargs"]["env"]["FLASH_WINDOW_PIDS"] == "42,43"
    assert observed["kwargs"]["capture_output"] is True
    assert observed["kwargs"]["text"] is False
    assert observed["kwargs"]["check"] is False
    assert "-NonInteractive" in observed["command"]
    assert "-EncodedCommand" in observed["command"]


@pytest.mark.skipif(os.name != "nt", reason="Resolver intentionally runs only on Windows")
def test_resolver_never_propagates_child_error_text():
    secret = "raw-auth-token-must-not-escape"

    def runner(_command, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr=secret)

    resolver = PowerShellLaunchFingerprintResolver(runner=runner)

    assert resolver.resolve([42]) == {}


@pytest.mark.skipif(os.name != "nt", reason="Resolver intentionally runs only on Windows")
def test_resolver_rejects_non_json_output_without_exposing_it():
    secret = "process command line with raw-auth-token"

    def runner(_command, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=secret, stderr="")

    resolver = PowerShellLaunchFingerprintResolver(runner=runner)

    assert resolver.resolve([42]) == {}


@pytest.mark.skipif(os.name != "nt", reason="Resolver intentionally runs only on Windows")
def test_resolver_decodes_utf8_bytes_without_decoding_stderr():
    def runner(_command, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"42": "a" * 64}).encode("utf-8"),
            stderr=b"\xa5\xff",
        )

    resolver = PowerShellLaunchFingerprintResolver(runner=runner)

    assert resolver.resolve([42]) == {42: "a" * 64}


@pytest.mark.skipif(os.name != "nt", reason="Resolver intentionally runs only on Windows")
def test_shortcut_resolver_returns_only_valid_indexed_fingerprints(tmp_path):
    first = tmp_path / "角色甲.lnk"
    second = tmp_path / "角色乙.lnk"
    first.touch()
    second.touch()

    def runner(_command, **kwargs):
        assert "FLASH_SHORTCUT_PATHS_B64" in kwargs["env"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "0": "a" * 64,
                    "1": "b" * 64,
                    "9": "c" * 64,
                }
            ).encode("utf-8"),
            stderr=b"",
        )

    resolver = PowerShellShortcutFingerprintResolver(runner=runner)

    assert resolver.resolve([first, second]) == {
        first: "a" * 64,
        second: "b" * 64,
    }


@pytest.mark.skipif(os.name != "nt", reason="Resolver intentionally runs only on Windows")
def test_shortcut_resolver_fails_closed_without_emitting_error_text(tmp_path):
    shortcut = tmp_path / "角色.lnk"
    shortcut.touch()

    def runner(_command, **_kwargs):
        raise OSError("secret shortcut data")

    resolver = PowerShellShortcutFingerprintResolver(runner=runner)

    assert resolver.resolve([shortcut]) == {}
