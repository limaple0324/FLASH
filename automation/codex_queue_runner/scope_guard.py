from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

from .models import QueueRunError


@dataclass(frozen=True)
class GitChange:
    status: str
    paths: tuple[str, ...]
    old_mode: str
    new_mode: str


def normalize_path(path: str) -> str:
    value = path.replace("\\", "/").strip()
    if not value or "\x00" in value or value.startswith(("/", "//")) or re.match(r"^[A-Za-z]:/", value):
        raise QueueRunError(f"不安全路徑: {path}")
    while value.startswith("./"):
        value = value[2:]
    if not value or ".." in value.split("/"):
        raise QueueRunError(f"不安全路徑: {path}")
    return value


def _matches(patterns: list[str], path: str) -> bool:
    for raw in patterns:
        try:
            pattern = normalize_path(raw.rstrip("/"))
        except QueueRunError:
            continue
        if fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(path, f"{pattern}/*"):
            return True
    return False


def validate_modified_paths(paths: list[str], owned_files: list[str], forbidden: list[str]) -> tuple[bool, list[str]]:
    if not owned_files:
        return False, ["OWNED_FILES 為空"]
    errors: list[str] = []
    for raw in paths:
        try:
            path = normalize_path(raw)
        except QueueRunError as exc:
            errors.append(str(exc)); continue
        if _matches(forbidden, path): errors.append(f"修改到禁用路徑: {path}")
        elif not _matches(owned_files, path): errors.append(f"修改超出 OWNED_FILES 範圍: {path}")
    return not errors, errors


def parse_raw_changes(raw: bytes) -> list[GitChange]:
    tokens = raw.split(b"\0")
    if tokens and not tokens[-1]: tokens.pop()
    changes: list[GitChange] = []; index = 0
    while index < len(tokens):
        header = tokens[index].decode("utf-8", "surrogateescape"); index += 1
        if not header.startswith(":"): raise QueueRunError("Git raw diff 格式不正確")
        pieces = header[1:].split()
        if len(pieces) != 5: raise QueueRunError("Git raw diff 標頭不完整")
        old_mode, new_mode, _old, _new, status = pieces; count = 2 if status[:1] in {"R", "C"} else 1
        if index + count > len(tokens): raise QueueRunError("Git raw diff 路徑不完整")
        paths = tuple(item.decode("utf-8", "surrogateescape") for item in tokens[index:index + count]); index += count
        changes.append(GitChange(status[:1], paths, old_mode, new_mode))
    return changes


def validate_git_changes(changes: list[GitChange], owned_files: list[str], forbidden: list[str]) -> tuple[bool, list[str]]:
    paths_ok, errors = validate_modified_paths([path for change in changes for path in change.paths], owned_files, forbidden)
    for change in changes:
        modes = {change.old_mode, change.new_mode}
        if "120000" in modes: errors.append(f"拒絕符號連結: {','.join(change.paths)}")
        if "160000" in modes: errors.append(f"拒絕子模組: {','.join(change.paths)}")
        if "100755" in modes: errors.append(f"拒絕可執行檔模式: {','.join(change.paths)}")
    return paths_ok and not errors, errors


def ensure_scope(paths: list[str], owned_files: list[str], forbidden: list[str]) -> tuple[bool, list[str]]:
    return validate_modified_paths(paths, owned_files, forbidden)
