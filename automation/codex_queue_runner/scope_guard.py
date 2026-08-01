from __future__ import annotations

import fnmatch

from .models import QueueRunError


def _normalize(path: str) -> str:
    path = path.replace("\\", "/").strip()
    while path.startswith("./"):
        path = path[2:]
    return path


def _match(patterns: list[str], target: str) -> bool:
    normalized_target = _normalize(target)
    for pattern in patterns:
        normalized_pattern = _normalize(pattern)
        if normalized_pattern.endswith("/"):
            normalized_pattern = normalized_pattern.rstrip("/") + "/*"

        if "*" in normalized_pattern:
            if fnmatch.fnmatch(normalized_target, normalized_pattern):
                return True
            continue

        if normalized_target == normalized_pattern:
            return True
        if normalized_target.startswith(normalized_pattern.rstrip("/") + "/"):
            return True
    return False


def validate_modified_paths(
    modified: list[str],
    owned_files: list[str],
    forbidden: list[str],
) -> tuple[bool, list[str]]:
    if not owned_files:
        return False, ["OWNED_FILES 為空，無法判斷範圍"]

    errors: list[str] = []
    for path in modified:
        normalized = _normalize(path)

        if ".." in normalized.split("/"):
            errors.append(f"不安全路徑: {normalized}")
            continue

        if _match(forbidden, normalized):
            errors.append(f"修改到禁用路徑: {normalized}")
            continue

        if not _match(owned_files, normalized):
            errors.append(f"修改超出 OWNED_FILES 範圍: {normalized}")

    return (len(errors) == 0, errors)


def parse_diff_paths(diff_output: str) -> list[str]:
    paths: list[str] = []
    for line in diff_output.splitlines():
        entry = line.strip()
        if not entry:
            continue
        if entry[:1] not in {"A", "M", "D", "R", "C"}:
            continue
        # 常見輸入："A  path" 或 "A\tpath"
        for token in [entry[1:].strip(), *entry[1:].split(None, 1)]:
            if token:
                break
        parts = entry[1:].strip().split(None, 1)
        if not parts:
            continue
        paths.append(parts[-1])
    return [p for p in paths if p]


def ensure_scope(
    modified_paths: list[str],
    owned_files: list[str],
    forbidden: list[str],
) -> tuple[bool, list[str]]:
    return validate_modified_paths(modified_paths, owned_files, forbidden)
