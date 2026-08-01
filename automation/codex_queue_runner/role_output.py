from __future__ import annotations

import json

from .models import AgentResult, QueueRunError, Role, Task

MAX_AGENT_OUTPUT_BYTES = 96_000
MAX_PATCH_BYTES = 64_000
MAX_LIST_ITEMS = 16
MAX_TEXT_LENGTH = 2_000
MAX_ITEM_LENGTH = 500


def _text(maximum: int, minimum: int = 1) -> dict:
    return {"type": "string", "minLength": minimum, "maxLength": maximum}


def _list(minimum: int = 0) -> dict:
    return {"type": "array", "minItems": minimum, "maxItems": MAX_LIST_ITEMS, "items": _text(MAX_ITEM_LENGTH)}


def output_schema(role: Role) -> dict:
    properties = {"role": {"const": role.value}, "result": {"enum": ["pass", "fail"]}, "summary": _text(MAX_TEXT_LENGTH), "evidence": _list(1)}
    required = ["role", "result", "summary", "evidence"]
    if role is Role.WORKER_A:
        properties["patch"] = _text(MAX_PATCH_BYTES, 0)
        required.append("patch")
    elif role is Role.REQUIREMENTS_AUDIT:
        properties["reasons"] = _list()
        required.append("reasons")
    elif role is Role.CODE_REVIEW:
        properties.update({"severity": {"enum": ["none", "low", "medium", "high", "critical"]}, "findings": _list()})
        required.extend(["severity", "findings"])
    return {"type": "object", "additionalProperties": False, "properties": properties, "required": required}


def _strings(value: object, field: str, minimum: int = 0) -> tuple[str, ...]:
    if not isinstance(value, list) or not minimum <= len(value) <= MAX_LIST_ITEMS or any(not isinstance(item, str) or not item.strip() or len(item) > MAX_ITEM_LENGTH for item in value):
        raise QueueRunError(f"invalid {field}")
    return tuple(value)


def parse_agent_result(text: str, task: Task) -> AgentResult:
    if len(text.encode("utf-8")) > MAX_AGENT_OUTPUT_BYTES:
        raise QueueRunError("agent result exceeds output limit")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise QueueRunError("agent result is not JSON") from exc
    schema = output_schema(task.role)
    if not isinstance(value, dict) or set(value) != set(schema["required"]):
        raise QueueRunError("agent result fields do not match schema")
    if value.get("role") != task.role.value or value.get("result") not in {"pass", "fail"}:
        raise QueueRunError("agent role or result is invalid")
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > MAX_TEXT_LENGTH:
        raise QueueRunError("agent summary is invalid")
    evidence = _strings(value.get("evidence"), "evidence", 1)
    result, patch, reasons, severity, findings = value["result"], "", (), "none", ()
    if task.role is Role.WORKER_A:
        patch = value["patch"]
        if not isinstance(patch, str) or len(patch.encode("utf-8")) > MAX_PATCH_BYTES or (result == "pass" and (not patch or not patch.startswith("diff --git "))) or (result == "fail" and patch):
            raise QueueRunError("worker patch violates result policy")
    elif task.role is Role.REQUIREMENTS_AUDIT:
        reasons = _strings(value["reasons"], "reasons")
        if (result == "pass" and reasons) or (result == "fail" and not reasons):
            raise QueueRunError("audit reasons violate result policy")
    elif task.role is Role.CODE_REVIEW:
        severity, findings = value["severity"], _strings(value["findings"], "findings")
        if severity not in {"none", "low", "medium", "high", "critical"} or (result == "pass" and (severity != "none" or findings)):
            raise QueueRunError("review findings violate result policy")
    return AgentResult(task.role, result, summary, patch, reasons, evidence, severity, findings)


def result_mapping(result: AgentResult) -> dict:
    return {"role": result.role.value, "result": result.result, "summary": result.summary, "patch": result.patch, "reasons": list(result.reasons), "evidence": list(result.evidence), "severity": result.severity, "findings": list(result.findings)}


def dry_agent_result(task: Task) -> str:
    value = {"role": task.role.value, "result": "pass", "summary": "dry-run structured result", "evidence": ["fixture output"]}
    if task.role is Role.WORKER_A:
        value.update({"result": "fail", "summary": "dry-run worker has no patch", "patch": ""})
    elif task.role is Role.REQUIREMENTS_AUDIT:
        value["reasons"] = []
    elif task.role is Role.CODE_REVIEW:
        value.update({"severity": "none", "findings": []})
    return json.dumps(value, separators=(",", ":"))
