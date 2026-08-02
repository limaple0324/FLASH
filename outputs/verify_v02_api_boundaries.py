# -*- coding: utf-8 -*-
"""
Validate the V0.2 API boundary index without importing the synchronizer.

This script is intentionally read-only. It checks that future targeted changes
can rely on the V0.2 API index before editing a specific module boundary.
"""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent
V01_SOURCE = ROOT / "flash_sync.py"
V02_SOURCE = ROOT / "flash_sync_v02.py"

REQUIRED_API_IDS = (
    "GroupAPI",
    "LaunchAPI",
    "MainWindowAPI",
    "SyncAPI",
    "HotkeyAPI",
    "CharacterAPI",
    "MonitorAPI",
    "TrayAPI",
    "StatusWindowAPI",
    "SettingsAPI",
    "DpiWindowAPI",
)


def parse_source(path: Path) -> ast.Module:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except FileNotFoundError as exc:
        raise SystemExit(f"Missing source file: {path}") from exc


def literal_assignment(tree: ast.Module, name: str):
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise SystemExit(f"Missing assignment: {name}")


def string_assignment(tree: ast.Module, name: str) -> str:
    value = literal_assignment(tree, name)
    if not isinstance(value, str):
        raise SystemExit(f"{name} is not a string")
    return value


def collect_defined_functions(tree: ast.Module) -> set[str]:
    names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            names.update(child.name for child in node.body if isinstance(child, ast.FunctionDef))
    return names


def main() -> int:
    v02_tree = parse_source(V02_SOURCE)
    defined_functions = collect_defined_functions(v02_tree)

    api_index = literal_assignment(v02_tree, "MODULE_API_METHOD_INDEX_V02")
    protected = literal_assignment(v02_tree, "PROTECTED_API_BOUNDARIES_V02")

    errors: list[str] = []
    warnings: list[str] = []

    if tuple(api_index.keys()) != REQUIRED_API_IDS:
        errors.append("MODULE_API_METHOD_INDEX_V02 keys do not match REQUIRED_API_IDS.")

    if tuple(protected) != REQUIRED_API_IDS:
        errors.append("PROTECTED_API_BOUNDARIES_V02 does not match REQUIRED_API_IDS.")

    seen_methods: dict[str, str] = {}
    duplicate_methods: list[tuple[str, str, str]] = []
    for api_id, method_names in api_index.items():
        if not method_names:
            errors.append(f"{api_id} has no indexed methods.")
        for method_name in method_names:
            if method_name not in defined_functions:
                errors.append(f"{api_id}: method not found: {method_name}")
            previous_api = seen_methods.get(method_name)
            if previous_api and previous_api != api_id:
                duplicate_methods.append((method_name, previous_api, api_id))
            else:
                seen_methods[method_name] = api_id

    if duplicate_methods:
        for method_name, first_api, second_api in duplicate_methods:
            warnings.append(f"{method_name} is indexed by both {first_api} and {second_api}.")

    if string_assignment(v02_tree, "APP_DISPLAY_NAME") != "輔V0.2":
        errors.append("V0.2 APP_DISPLAY_NAME is not 輔V0.2.")
    if string_assignment(v02_tree, "APP_VERSION_CODE") != "v0.2":
        errors.append("V0.2 APP_VERSION_CODE is not v0.2.")
    if string_assignment(v02_tree, "APP_DATA_DIR_NAME") != "輔V0.2":
        errors.append("V0.2 APP_DATA_DIR_NAME is not 輔V0.2.")
    if string_assignment(v02_tree, "APP_OUTPUT_CONFIG_BACKUP_FILENAME") != "sync_launch_config_v02.json":
        errors.append("V0.2 backup filename is not sync_launch_config_v02.json.")

    if V01_SOURCE.exists():
        v01_tree = parse_source(V01_SOURCE)
        if string_assignment(v01_tree, "APP_DISPLAY_NAME") != "輔V0.1":
            errors.append("V0.1 APP_DISPLAY_NAME is not 輔V0.1; check that V0.1 was not modified unexpectedly.")

    for warning in warnings:
        print(f"WARNING: {warning}")

    if errors:
        print("V0.2 API boundary validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print("V0.2 API boundary validation passed.")
    print(f"API count: {len(api_index)}")
    print(f"Method references: {sum(len(methods) for methods in api_index.values())}")
    print("V0.1/V0.2 identity checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
