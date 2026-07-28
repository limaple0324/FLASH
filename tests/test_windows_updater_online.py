"""Small real-network regression for the slash-containing SP1 release branch."""

from __future__ import annotations

import json
import os
import urllib.request

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("FLASH_RUN_ONLINE_TESTS") != "1",
    reason="真正線上更新測試只在明確要求時執行",
)


def test_release_sp1_branch_resolves_online_with_its_real_slash() -> None:
    request = urllib.request.Request(
        "https://api.github.com/repos/limaple0324/FLASH/commits/release/sp1",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "FLASH-Windows-Updater-Regression",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.load(response)
    assert isinstance(payload.get("sha"), str)
    assert len(payload["sha"]) == 40
