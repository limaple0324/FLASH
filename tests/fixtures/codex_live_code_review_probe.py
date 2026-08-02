"""Intentional unsafe fixture for one controlled CODE_REVIEW probe.

This module must never be imported or executed. It exists only so the read-only
review role has deterministic security findings to report, after which the
controlled live test must stop.
"""

import subprocess


def run_untrusted(command: str) -> subprocess.CompletedProcess[str]:
    """Deliberately unsafe: shell injection probe for review only."""
    return subprocess.run(command, shell=True, check=False, text=True)


def evaluate_untrusted(expression: str):
    """Deliberately unsafe: arbitrary-code execution probe for review only."""
    return eval(expression)
