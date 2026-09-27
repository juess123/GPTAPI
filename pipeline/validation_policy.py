"""Shared severity policy for generated-deliverable validation.

Only defects that require changing the actual deliverable should trigger a model
repair. Missing evidence, metadata, labels, or validator-only bookkeeping remain
visible in reports but must not cause an expensive Blender rebuild.
"""
from __future__ import annotations

from typing import Any


NON_BLOCKING_STRUCTURE_CODES = {
    "UNCLASSIFIED_OBJECT",
    "CONFLICTING_CLASSIFICATION",
    "MISSING_COMPONENT_METADATA",
    "NO_QUOTE_GEOMETRY",
    "EMPTY_OVERALL_SCOPE",
    "INVALID_EXPECTED_COUNTS",
    "INVALID_EXPECTED_COUNT",
    "MISSING_REQUIRED_FEATURE",
    "MISSING_RULE_SUBJECT",
    "MISSING_RULE_HOST",
    "VALIDATOR_INTERNAL_ERROR",
}

EVIDENCE_GAP_MARKERS = (
    "证据不足", "缺少证据", "没有证据", "未有专项证据", "未提供", "未输出",
    "未展示", "未被完整展示", "未列出", "未给", "未验证", "待核", "尚无",
    "未有", "未包含", "不足以确认", "没有足够细节", "节点验证",
    "无法全面核实", "不能据此证明", "缺少实际壁厚测量",
    "无法确认", "不能确认", "无法核实", "不能核实", "无法验证", "不能验证",
    "不能证明", "不可验证", "not verifiable", "insufficient evidence", "not provided",
)

MODEL_MUTATION_MARKERS = (
    "修正", "调整", "移动", "补建", "重建", "删除", "替换", "恢复", "补回",
    "缩放", "生成实体", "修改几何", "修改材质", "fix ", "move ", "rebuild",
    "replace", "restore",
)

EVIDENCE_ONLY_SUBJECTS = (
    "集合树", "集合归属", "语义标签", "构件类型标记", "metadata", "元数据",
    "工艺路径颜色", "工艺曲线", "隐藏节点", "管腔证据",
    "求值证据", "验证证据",
)


def _joined_text(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (list, tuple, set)):
            parts.extend(str(item) for item in value)
    return "\n".join(parts).casefold()


def is_evidence_only_failure(definition: dict | None, evaluation: dict | None) -> bool:
    """Return True when a complaint is about proof/bookkeeping, not the model."""
    definition = definition or {}
    evaluation = evaluation or {}
    observed = _joined_text(
        evaluation.get("observed"), evaluation.get("description"), evaluation.get("message")
    )
    repair = _joined_text(evaluation.get("repair_instruction"))
    check = _joined_text(
        definition.get("check"), definition.get("category"), definition.get("target")
    )

    # Evidence gaps never justify mutating a model, even if a reviewer suggests it.
    if any(marker in observed for marker in EVIDENCE_GAP_MARKERS):
        return True
    if any(subject in observed or subject in check for subject in EVIDENCE_ONLY_SUBJECTS):
        return True
    # A requested mutation of real geometry/material/content is actionable only
    # when the reviewer first established a concrete defect.
    if any(marker in repair for marker in MODEL_MUTATION_MARKERS):
        return False
    if repair.startswith("补充") and any(word in repair for word in (
            "证据", "报告", "清单", "数据", "记录", "检查", "检测", "视图", "近景",
            "剖视", "剖面", "截图")):
        return True
    return False


def is_non_blocking_structure_issue(issue: dict) -> bool:
    return str(issue.get("code", "")) in NON_BLOCKING_STRUCTURE_CODES


def as_non_blocking(issue: dict, reason: str) -> dict:
    result = dict(issue)
    result["non_blocking"] = True
    result["non_blocking_reason"] = reason
    return result
