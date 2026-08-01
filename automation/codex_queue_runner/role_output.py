from __future__ import annotations

import json

from .models import AgentResult, QueueRunError, Role, Task


def output_schema(role: Role) -> dict:
    properties = {"role": {"const": role.value}, "result": {"enum": ["pass", "fail"]}, "summary": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "string"}}}
    required = ["role", "result", "summary", "evidence"]
    if role is Role.WORKER_A:
        properties["patch"] = {"type": "string"}; required.append("patch")
    if role is Role.REQUIREMENTS_AUDIT:
        properties["reasons"] = {"type": "array", "items": {"type": "string"}}; required.append("reasons")
    if role is Role.CODE_REVIEW:
        properties["severity"] = {"enum": ["none", "low", "medium", "high", "critical"]}; properties["findings"] = {"type": "array", "items": {"type": "string"}}; required.extend(["severity", "findings"])
    return {"type": "object", "additionalProperties": False, "properties": properties, "required": required}


def parse_agent_result(text: str, task: Task) -> AgentResult:
    try: value = json.loads(text)
    except json.JSONDecodeError as exc: raise QueueRunError("Agent 未輸出合法 JSON") from exc
    if not isinstance(value, dict) or value.get("role") != task.role.value or value.get("result") not in {"pass", "fail"}: raise QueueRunError("Agent JSON role 或 result 不合法")
    evidence = value.get("evidence", []); reasons = value.get("reasons", []); findings = value.get("findings", [])
    if not all(isinstance(items, list) for items in (evidence, reasons, findings)) or not all(isinstance(item, str) for item in evidence + reasons + findings): raise QueueRunError("Agent JSON 陣列內容不合法")
    if task.role is Role.WORKER_A and not isinstance(value.get("patch"), str): raise QueueRunError("WORKER_A 缺少 patch")
    if task.role is Role.REQUIREMENTS_AUDIT and "reasons" not in value: raise QueueRunError("REQUIREMENTS_AUDIT 缺少 reasons")
    if task.role is Role.CODE_REVIEW and value.get("severity") not in {"none", "low", "medium", "high", "critical"}: raise QueueRunError("CODE_REVIEW 缺少 severity")
    return AgentResult(task.role, value["result"], str(value.get("summary", "")), str(value.get("patch", "")), tuple(reasons), tuple(evidence), str(value.get("severity", "none")), tuple(findings))


def dry_agent_result(task: Task) -> str:
    value = {"role": task.role.value, "result": "pass", "summary": "乾跑模擬通過", "evidence": ["fixture"]}
    if task.role is Role.WORKER_A: value["patch"] = ""
    if task.role is Role.REQUIREMENTS_AUDIT: value["reasons"] = []
    if task.role is Role.CODE_REVIEW: value.update({"severity": "none", "findings": []})
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
