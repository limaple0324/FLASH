# -*- coding: utf-8 -*-
"""
Validate the V0.2 API boundary index without importing the synchronizer.

This script is intentionally read-only. It checks that future targeted changes
can rely on the V0.2 API index before editing a specific module boundary.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
V01_SOURCE = ROOT / "flash_sync.py"
V02_SOURCE = ROOT / "flash_sync_v02.py"
API_SHAPE_BASELINE = ROOT / "V02_API_SHAPE_BASELINE.json"

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


def _annotation(node: ast.expr | None) -> str | None:
    return ast.unparse(node) if node is not None else None


def _api_entries(tree: ast.Module):
    entries = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            entries[node.name] = ("module", node)
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    entries.setdefault(child.name, (node.name, child))
    return entries


def _signature_shape(owner, node):
    positional = list(node.args.posonlyargs) + list(node.args.args)
    default_start = len(positional) - len(node.args.defaults)
    parameters = []

    def add(argument, kind, has_default):
        parameters.append({
            "name": argument.arg,
            "kind": kind,
            "annotation": _annotation(argument.annotation),
            "has_default": bool(has_default),
        })

    for index, argument in enumerate(node.args.posonlyargs):
        add(argument, "positional_only", index >= default_start)
    for index, argument in enumerate(node.args.args, start=len(node.args.posonlyargs)):
        add(argument, "positional_or_keyword", index >= default_start)
    if node.args.vararg is not None:
        add(node.args.vararg, "var_positional", False)
    for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
        add(argument, "keyword_only", default is not None)
    if node.args.kwarg is not None:
        add(node.args.kwarg, "var_keyword", False)
    decorators = [
        ast.unparse(item) for item in node.decorator_list
        if ast.unparse(item) in ("staticmethod", "classmethod", "property")
    ]
    return {
        "owner": owner,
        "async": isinstance(node, ast.AsyncFunctionDef),
        "decorators": decorators,
        "parameters": parameters,
        "returns": _annotation(node.returns),
    }


def api_shape_payload(tree: ast.Module, api_index) -> dict:
    entries = _api_entries(tree)
    signatures = {}
    for method_names in api_index.values():
        for name in method_names:
            if name not in entries:
                raise ValueError(f"indexed API is missing: {name}")
            signatures[name] = _signature_shape(*entries[name])
    return {
        "schema_version": 1,
        "api_index": {key: list(value) for key, value in api_index.items()},
        "signatures": signatures,
    }


def api_shape_sha256(payload: dict) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def validate_api_shape_baseline(tree: ast.Module, api_index,
                                baseline_path: Path = API_SHAPE_BASELINE) -> tuple[list[str], str]:
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return (["sanitized API-shape baseline is missing or invalid"], "")
    required = {"schema_version", "base_commit", "api_count", "method_references", "shape_sha256"}
    if set(baseline) != required:
        return (["sanitized API-shape baseline schema is invalid"], "")
    try:
        payload = api_shape_payload(tree, api_index)
    except (TypeError, ValueError) as exc:
        return ([str(exc)], "")
    digest = api_shape_sha256(payload)
    errors = []
    if baseline["schema_version"] != 1:
        errors.append("sanitized API-shape baseline version is invalid")
    if not isinstance(baseline["base_commit"], str) or len(baseline["base_commit"]) != 40:
        errors.append("sanitized API-shape provenance is invalid")
    if baseline["api_count"] != len(api_index):
        errors.append("sanitized API-shape API count changed")
    if baseline["method_references"] != sum(len(items) for items in api_index.values()):
        errors.append("sanitized API-shape method count changed")
    if baseline["shape_sha256"] != digest:
        errors.append("sanitized API shape changed")
    return errors, digest


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

    shape_errors, shape_digest = validate_api_shape_baseline(v02_tree, api_index)
    errors.extend(shape_errors)

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

    if string_assignment(v02_tree, "APP_DISPLAY_NAME") != "輔魔":
        errors.append("V0.2 APP_DISPLAY_NAME is not 輔魔.")
    if string_assignment(v02_tree, "APP_VERSION_CODE") != "v0.2":
        errors.append("V0.2 APP_VERSION_CODE is not v0.2.")
    if string_assignment(v02_tree, "APP_DATA_DIR_NAME") != "輔V0.2_自動重連獨立版":
        errors.append("V0.2 APP_DATA_DIR_NAME does not match the current standalone product identity.")
    if string_assignment(v02_tree, "APP_OUTPUT_CONFIG_BACKUP_FILENAME") != "sync_launch_config_reconnect_standalone_backup.json":
        errors.append("V0.2 backup filename does not match the current standalone product identity.")

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
    print(f"API shape SHA256: {shape_digest}")
    print("V0.1/V0.2 identity checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
