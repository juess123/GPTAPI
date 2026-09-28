"""Parse and safely apply model-proposed exact replacements."""
from __future__ import annotations

import json


class EmptyRepairPlan(ValueError):
    """The response was well-formed but asked for no change at all.

    This is a semantic answer from the model ("there is nothing here worth patching"),
    not a broken response shape.  Callers must not report it as a format error and
    re-ask the same question, or a deliberate no-op is retried until the job dies.
    """


def parse_repair_plan(text: str) -> list[dict[str, str]]:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("响应中没有JSON补丁对象")
    payload = json.loads(text[start:end + 1])
    replacements = payload.get("replacements") if isinstance(payload, dict) else None
    if not isinstance(replacements, list):
        raise ValueError("replacements必须是非空数组")
    if not replacements:
        raise EmptyRepairPlan("replacements是空数组（模型认为无需修改）")
    if len(replacements) > 20:
        raise ValueError("单次局部修复最多20处替换")
    result = []
    for index, item in enumerate(replacements, 1):
        if not isinstance(item, dict):
            raise ValueError(f"第{index}项不是对象")
        old, new = item.get("old"), item.get("new")
        if not isinstance(old, str) or not old:
            raise ValueError(f"第{index}项缺少非空old")
        if not isinstance(new, str):
            raise ValueError(f"第{index}项缺少字符串new")
        if old == new:
            raise ValueError(f"第{index}项没有产生变化")
        result.append({"old": old, "new": new, "reason": str(item.get("reason", ""))})
    return result


def apply_replacements(source: str, replacements: list[dict[str, str]]) -> str:
    updated = source
    for index, item in enumerate(replacements, 1):
        count = updated.count(item["old"])
        if count != 1:
            raise ValueError(f"第{index}项old必须唯一匹配，实际匹配{count}次")
        updated = updated.replace(item["old"], item["new"], 1)
    compile(updated, "<locally-repaired-script>", "exec")
    return updated
