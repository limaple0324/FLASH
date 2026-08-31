"""Fail-closed verification for every packaged first-party binary asset."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath


MANIFEST_NAME = "RUNTIME_ASSET_MANIFEST.json"


def verify_runtime_assets(resource_root) -> dict:
    root = Path(resource_root)
    manifest_path = root / MANIFEST_NAME
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        assets = payload.get("assets")
        required_files = payload.get("required_files")
        if (payload.get("version") != 1 or payload.get("algorithm") != "SHA256"
                or not isinstance(assets, list) or payload.get("asset_count") != len(assets)
                or not assets or not isinstance(required_files, list) or not required_files):
            return {"ok": False, "asset_count": 0, "reason": "manifest_invalid"}
        seen = set()
        for item in assets + required_files:
            if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
                return {"ok": False, "asset_count": 0, "reason": "manifest_invalid"}
            relative = PurePosixPath(str(item["path"]))
            digest = str(item["sha256"])
            if (relative.is_absolute() or ".." in relative.parts or str(relative) in seen
                    or not isinstance(item["bytes"], int) or item["bytes"] < 1
                    or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
                return {"ok": False, "asset_count": 0, "reason": "manifest_invalid"}
            seen.add(str(relative))
            path = root.joinpath(*relative.parts)
            if not path.is_file() or path.stat().st_size != item["bytes"]:
                return {"ok": False, "asset_count": len(assets), "reason": "asset_missing_or_size"}
            hasher = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(block)
            if hasher.hexdigest() != digest:
                return {"ok": False, "asset_count": len(assets), "reason": "asset_hash"}
        return {"ok": True, "asset_count": len(assets), "reason": ""}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"ok": False, "asset_count": 0, "reason": "manifest_unreadable"}
