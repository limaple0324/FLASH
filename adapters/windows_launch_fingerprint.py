"""Secret-safe launcher identity for otherwise identical Windows processes.

Only SHA-256 fingerprints leave the PowerShell child process. Raw shortcut
arguments and process command lines are never returned to Python.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from collections.abc import Iterable
from typing import Protocol


_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def normalize_launch_fingerprint(value: object) -> str | None:
    """Return a canonical SHA-256 fingerprint without accepting partial values."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if _FINGERPRINT_PATTERN.fullmatch(normalized) else None


class LaunchFingerprintResolver(Protocol):
    def resolve(self, process_ids: Iterable[int]) -> dict[int, str]:
        """Map process IDs to anonymous launcher-argument fingerprints."""


class PowerShellLaunchFingerprintResolver:
    """Resolve fingerprints in one hidden PowerShell process.

    The script keeps shortcut arguments and command lines inside its own
    process. Its only output is a JSON mapping of numeric PIDs to SHA-256
    digests.
    """

    _SCRIPT = r"""
$ErrorActionPreference = 'Stop'
try {
    $requested = @(
        ([string]$env:FLASH_WINDOW_PIDS).Split(
            ',',
            [System.StringSplitOptions]::RemoveEmptyEntries
        ) | ForEach-Object {
            $parsed = 0
            if ([int]::TryParse($_, [ref]$parsed) -and $parsed -gt 0) {
                $parsed
            }
        } | Sort-Object -Unique
    )

    if ($requested.Count -eq 0) {
        Write-Output '{}'
        exit 0
    }

    $shortcutShell = New-Object -ComObject WScript.Shell
    $roots = @(
        [Environment]::GetFolderPath('Desktop'),
        [Environment]::GetFolderPath('CommonDesktopDirectory')
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Sort-Object -Unique

    $shortcuts = @()
    foreach ($root in $roots) {
        foreach ($item in Get-ChildItem -LiteralPath $root -Filter '*.lnk' -File -Recurse -ErrorAction SilentlyContinue) {
            try {
                $shortcut = $shortcutShell.CreateShortcut($item.FullName)
                $arguments = [string]$shortcut.Arguments
                if (-not [string]::IsNullOrWhiteSpace($arguments)) {
                    $shortcuts += [pscustomobject]@{
                        Arguments = $arguments
                        TargetPath = [string]$shortcut.TargetPath
                    }
                }
            } catch {
                # An unreadable shortcut is simply not an identity candidate.
            }
        }
    }

    $resolved = @{}
    foreach ($processId in $requested) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
        if ($null -eq $process -or [string]::IsNullOrEmpty([string]$process.CommandLine)) {
            continue
        }

        $commandLine = [string]$process.CommandLine
        $executablePath = [string]$process.ExecutablePath
        $candidateArguments = @(
            $shortcuts | Where-Object {
                $expectedArguments = ([string]$_.Arguments).Trim()
                $trimmedCommandLine = $commandLine.TrimEnd()
                $argumentTailMatches = (
                    -not [string]::IsNullOrEmpty($expectedArguments) -and
                    $trimmedCommandLine.EndsWith(
                        $expectedArguments,
                        [StringComparison]::Ordinal
                    )
                )
                $prefixLength = $trimmedCommandLine.Length - $expectedArguments.Length
                $hasArgumentBoundary = (
                    $prefixLength -gt 0 -and
                    [char]::IsWhiteSpace($trimmedCommandLine[$prefixLength - 1])
                )
                $targetMatches = (
                    [string]::IsNullOrWhiteSpace($_.TargetPath) -or
                    [string]::IsNullOrWhiteSpace($executablePath) -or
                    [string]::Equals(
                        [IO.Path]::GetFullPath($_.TargetPath),
                        [IO.Path]::GetFullPath($executablePath),
                        [StringComparison]::OrdinalIgnoreCase
                    )
                )
                $targetMatches -and $argumentTailMatches -and $hasArgumentBoundary
            } | Select-Object -ExpandProperty Arguments -Unique
        )

        if ($candidateArguments.Count -ne 1) {
            continue
        }

        $sha256 = [Security.Cryptography.SHA256]::Create()
        try {
            $bytes = [Text.Encoding]::UTF8.GetBytes([string]$candidateArguments[0])
            $digest = $sha256.ComputeHash($bytes)
            $resolved[[string]$processId] = (
                [BitConverter]::ToString($digest).Replace('-', '').ToLowerInvariant()
            )
        } finally {
            $sha256.Dispose()
        }
    }

    Write-Output ($resolved | ConvertTo-Json -Compress)
} catch {
    # Never allow exception text to expose command lines or shortcut arguments.
    Write-Output '{}'
}
"""

    def __init__(self, *, timeout_seconds: float = 12.0, runner=None):
        self._timeout_seconds = timeout_seconds
        self._runner = runner or subprocess.run

    @classmethod
    def _encoded_script(cls) -> str:
        return base64.b64encode(cls._SCRIPT.encode("utf-16-le")).decode("ascii")

    def resolve(self, process_ids: Iterable[int]) -> dict[int, str]:
        normalized_ids = sorted(
            {
                int(process_id)
                for process_id in process_ids
                if isinstance(process_id, int) and not isinstance(process_id, bool) and process_id > 0
            }
        )
        if os.name != "nt" or not normalized_ids:
            return {}

        environment = os.environ.copy()
        environment["FLASH_WINDOW_PIDS"] = ",".join(str(process_id) for process_id in normalized_ids)
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            completed = self._runner(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-EncodedCommand",
                    self._encoded_script(),
                ],
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                env=environment,
                creationflags=creation_flags,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return {}

        if completed.returncode != 0:
            return {}

        try:
            raw = json.loads(completed.stdout.strip() or "{}")
        except (AttributeError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}

        resolved: dict[int, str] = {}
        requested = set(normalized_ids)
        for process_id, fingerprint in raw.items():
            try:
                parsed_process_id = int(process_id)
            except (TypeError, ValueError):
                continue
            normalized_fingerprint = normalize_launch_fingerprint(fingerprint)
            if parsed_process_id in requested and normalized_fingerprint is not None:
                resolved[parsed_process_id] = normalized_fingerprint
        return resolved
