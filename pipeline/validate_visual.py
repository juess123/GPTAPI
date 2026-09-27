#!/usr/bin/env python3
"""Compare neutral Blender views with references and locked source requirements."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import ask_model
from validation_policy import as_non_blocking, is_evidence_only_failure


ROOT = Path(__file__).resolve().parent.parent
REFERENCE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

PROMPT = """你是严格但保守的工程模型验收员。请比较用户参考图、Blender固定验证视图、锁定标准和程序化结构报告。
只输出一个JSON对象，不要Markdown或解释。

验收优先级：
1. 用户文字 source_requirements 中的精简模型规则逐条合规，明确文字高于图片。
2. visual_criteria 中的整体轮廓、主要构件、数量、布局、相对位置、比例、开口和曲直形态与参考图一致。
3. 发现验证视图中肉眼明确可见的结构错误：缺件、重复、悬空、脱离、明显穿插、左右/前后反向、明显比例失真、洞口错误、构件超出主体。
4. 尺寸、数量和空间规则以程序化结构报告为准，不要凭透视图猜毫米尺寸。

重要边界：
- 灯光、阴影、背景、摄影棚、渲染噪点、相机角度和透视差异不属于硬性错误。
- 不要因为参考图没有展示隐藏面，就臆测隐藏面错误。
- 无法从现有证据确认时必须返回 not_verifiable，不能猜 pass，也不能猜 fail。
- 只有清楚、具体、能定位到构件和视图的差异才能判 fail。
- not_verifiable 只表示证据不足，将被记录为警告；它不会触发模型修复。
- 每个结果都要给出 evidence_views、observed 和 repair_instruction；通过项 repair_instruction 为空字符串。
- requirement_results 必须覆盖每一个 source_requirements ID。
- visual_results 必须覆盖每一个 visual_criteria ID。

严格输出结构：
{
  "requirement_results": [
    {"id":"R001","verdict":"pass|fail|not_verifiable","evidence_views":[],"observed":"实际观察","repair_instruction":"局部修复指令"}
  ],
  "visual_results": [
    {"id":"V001","verdict":"pass|fail|not_verifiable","evidence_views":[],"observed":"实际观察","repair_instruction":"局部修复指令"}
  ],
  "obvious_structure_issues": [
    {"id":"OBS-001","severity":"error|warning","category":"missing|duplicate|floating|detached|intersection|orientation|proportion|opening|bounds|other","evidence_views":[],"description":"明确可见的问题","repair_instruction":"局部修复指令"}
  ],
  "notes": []
}

==================== 锁定标准 ====================
{spec}
==================== 程序化结构报告 ====================
{structure_report}
"""


def compact_value(value, *, list_limit: int = 20, text_limit: int = 240):
    """Bound verbose validator evidence while preserving actionable facts."""
    if isinstance(value, dict):
        return {str(key): compact_value(item, list_limit=list_limit, text_limit=text_limit)
                for key, item in value.items()}
    if isinstance(value, list):
        compacted = [compact_value(item, list_limit=list_limit, text_limit=text_limit)
                     for item in value[:list_limit]]
        if len(value) > list_limit:
            compacted.append({"omitted_items": len(value) - list_limit})
        return compacted
    if isinstance(value, str) and len(value) > text_limit:
        return value[:text_limit] + "…"
    return value


def compact_spec(spec: dict) -> dict:
    """Only send fields the visual reviewer can use for a verdict."""
    requirement_fields = ("id", "target", "category", "check", "verify", "level", "status")
    criterion_fields = ("id", "source", "category", "check", "level", "status", "view")
    return {
        "schema": spec.get("schema"),
        "project_type": spec.get("project_type"),
        "source_requirements": [
            {key: item.get(key) for key in requirement_fields if key in item}
            for item in spec.get("source_requirements", []) if isinstance(item, dict)
        ],
        "visual_criteria": [
            {key: item.get(key) for key in criterion_fields if key in item}
            for item in spec.get("visual_criteria", []) if isinstance(item, dict)
        ],
        "overall": compact_value(spec.get("overall", {}), list_limit=40),
        "expected_counts": compact_value(spec.get("expected_counts", {}), list_limit=80),
        "required_features": compact_value(spec.get("required_features", []), list_limit=80),
        "spatial_rules": compact_value(spec.get("spatial_rules", []), list_limit=80),
    }


def compact_structure_report(structure: dict) -> dict:
    """Remove bulky duplicate object evidence before the GPT visual review."""
    compact_info = []
    for item in structure.get("info", []):
        if not isinstance(item, dict):
            continue
        if item.get("code") != "COMPONENT_EVIDENCE":
            compact_info.append(compact_value(item, list_limit=60))
            continue
        components = []
        for component in item.get("components", []):
            if not isinstance(component, dict):
                continue
            components.append({
                "id": component.get("component_id"),
                "types": component.get("component_types"),
                "count": component.get("object_count"),
                "dimensions_mm": component.get("dimensions_mm"),
                "center_mm": component.get("center_mm"),
                "materials": compact_value(component.get("materials", []), list_limit=8),
                "properties": compact_value(component.get("properties", {}), list_limit=12,
                                              text_limit=120),
            })
        compact_info.append({"code": "COMPONENT_EVIDENCE", "components": components})
    return {
        "status": structure.get("status"),
        "summary": structure.get("summary"),
        "errors": compact_value(structure.get("errors", []), list_limit=80),
        "warnings": compact_value(structure.get("warnings", []), list_limit=80),
        "info": compact_info,
        "checks": compact_value(structure.get("checks", []), list_limit=30),
    }


def extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("响应中没有JSON对象")
    value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("视觉验收结果顶层必须是对象")
    return value


def image_part(path: Path) -> dict | None:
    url, note = ask_model.image_data_url(path, max_dimension=1600)
    if url is None:
        print(f"[警告] 无法附加验证图片 {path.name}: {note}", file=sys.stderr)
        return None
    return {"type": "image_url", "image_url": {"url": url}}


def normalized_results(items) -> dict[str, dict]:
    result = {}
    if not isinstance(items, list):
        return result
    for item in items:
        if isinstance(item, dict) and str(item.get("id", "")).strip():
            result[str(item["id"]).strip()] = item
    return result


def append_verdict(*, record: dict, definition: dict, evaluation: dict,
                   hard: bool, errors: list[dict], warnings: list[dict]) -> None:
    """Only a concrete deliverable defect may trigger a repair."""
    verdict = evaluation.get("verdict")
    if verdict == "fail" and hard and not is_evidence_only_failure(definition, evaluation):
        errors.append(record)
        return
    if verdict == "fail" and hard:
        record = as_non_blocking(record, "insufficient_evidence_or_metadata")
    warnings.append(record)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--views-dir", required=True)
    parser.add_argument("--structure-report", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    input_dir = Path(args.input_dir).resolve()
    spec_path = Path(args.spec).resolve()
    views_dir = Path(args.views_dir).resolve()
    structure_path = Path(args.structure_report).resolve()
    report_path = Path(args.report).resolve()

    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    structure = json.loads(structure_path.read_text(encoding="utf-8"))
    review_spec = compact_spec(spec)
    review_structure = compact_structure_report(structure)
    prompt = PROMPT.replace(
        "{spec}", json.dumps(review_spec, ensure_ascii=False, separators=(",", ":"))
    ).replace(
        "{structure_report}",
        json.dumps(review_structure, ensure_ascii=False, separators=(",", ":")),
    )
    prompt += """

补充严重级别规则：只有需要修改最终模型实体的明确缺陷才可判定 fail。
以下情况一律返回 not_verifiable，不得触发模型重建：缺少管腔或隐藏节点证明、
没有集合树或集合归属清单、没有工艺路径颜色数据、缺少元数据或语义标签、
验证报告没有输出某项证据。若 repair_instruction 只是补充报告、清单、记录、
近景或检查数据，而不需要改变几何、位置、材质或可见内容，也必须返回 not_verifiable。
"""
    print(f"视觉验收文本长度: {len(prompt)} 字符（已使用精简证据）", flush=True)
    content: list[dict] = [{"type": "text", "text": prompt}]

    reference_paths = sorted(path for path in input_dir.rglob("*")
                             if path.is_file() and path.suffix.lower() in REFERENCE_EXTS)
    view_paths = sorted(views_dir.glob("validation_*.png"))
    for path in reference_paths:
        content.append({"type": "text", "text": f"用户参考图：{path.relative_to(input_dir).as_posix()}"})
        part = image_part(path)
        if part:
            content.append(part)
    for path in view_paths:
        content.append({"type": "text", "text": f"Blender固定验证视图：{path.name}"})
        part = image_part(path)
        if part:
            content.append(part)

    env = ask_model.load_env(ROOT / ".env")

    class Settings:
        timeout = None
        attempts = None
        max_tokens = None

    timeout, attempts, _ = ask_model.resolve_settings(Settings, env)
    result = None
    evaluation = None
    last_error = ""
    for content_attempt in range(1, 3):
        result = ask_model.post_json(
            env["CNXMAI_CHAT_URL"],
            {"model": env["CNXMAI_MODEL"], "messages": [{"role": "user", "content": content}],
             "max_tokens": 16000, "temperature": 0},
            env["CNXMAI_API_KEY"], timeout=timeout, max_attempts=attempts,
        )
        response = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        try:
            evaluation = extract_json(response)
            break
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            content[0]["text"] += "\n上一回答不是有效JSON，请重新输出严格结构的唯一JSON对象。"
            print(f"视觉验收响应无效（内容尝试 {content_attempt}/2）：{exc}", flush=True)

    raw_path = report_path.with_name("raw_visual_validation_response.json")
    raw_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if evaluation is None:
        print(f"[错误] 无法解析视觉验收结果：{last_error}", file=sys.stderr)
        return 2

    requirement_results = normalized_results(evaluation.get("requirement_results"))
    visual_results = normalized_results(evaluation.get("visual_results"))
    errors: list[dict] = []
    warnings: list[dict] = []
    info: list[dict] = []

    schema = spec.get("schema", 1)
    for requirement in spec.get("source_requirements", []):
        if not isinstance(requirement, dict):
            continue
        if schema < 3 and requirement.get("applies_to") not in {"blender", "both"}:
            continue
        verification = requirement.get("verify", requirement.get("verification"))
        if schema < 3 and verification in {"xlsx", "cross_file"}:
            info.append({"code": "SOURCE_REQUIREMENT_DEFERRED_TO_XLSX",
                         "requirement_id": requirement.get("id")})
            continue
        requirement_id = str(requirement.get("id", ""))
        result_item = requirement_results.get(requirement_id)
        hard = ((requirement.get("level") == "hard" if schema >= 3 else bool(requirement.get("required")))
                and requirement.get("status") in {"confirmed", "derived"})
        if result_item is None:
            issue = {"code": "MISSING_REQUIREMENT_RESULT", "requirement_id": requirement_id,
                     "requirement": requirement}
            warnings.append(issue)
            continue
        verdict = result_item.get("verdict")
        record = {"code": "SOURCE_REQUIREMENT_NOT_PASSED", "requirement_id": requirement_id,
                  "verdict": verdict, "requirement": requirement, "evaluation": result_item}
        if verdict == "pass":
            info.append({"code": "SOURCE_REQUIREMENT_PASSED", "requirement_id": requirement_id})
        elif verdict == "fail" and hard:
            append_verdict(record=record, definition=requirement, evaluation=result_item,
                           hard=hard, errors=errors, warnings=warnings)
        else:
            warnings.append(record)

    for criterion in spec.get("visual_criteria", []):
        if not isinstance(criterion, dict):
            continue
        criterion_id = str(criterion.get("id", ""))
        result_item = visual_results.get(criterion_id)
        hard = ((criterion.get("level") == "hard" if schema >= 3
                 else criterion.get("importance") in {"critical", "major"})
                and criterion.get("status") in {"confirmed", "derived"})
        if result_item is None:
            issue = {"code": "MISSING_VISUAL_RESULT", "criterion_id": criterion_id,
                     "criterion": criterion}
            warnings.append(issue)
            continue
        verdict = result_item.get("verdict")
        record = {"code": "VISUAL_CRITERION_NOT_PASSED", "criterion_id": criterion_id,
                  "verdict": verdict, "criterion": criterion, "evaluation": result_item}
        if verdict == "pass":
            info.append({"code": "VISUAL_CRITERION_PASSED", "criterion_id": criterion_id})
        elif verdict == "fail" and hard:
            append_verdict(record=record, definition=criterion, evaluation=result_item,
                           hard=hard, errors=errors, warnings=warnings)
        else:
            warnings.append(record)

    structure_issues = evaluation.get("obvious_structure_issues", [])
    if isinstance(structure_issues, list):
        for item in structure_issues:
            if not isinstance(item, dict):
                continue
            record = {"code": "VISUALLY_OBVIOUS_STRUCTURE_ERROR", **item}
            if (item.get("severity") == "error"
                    and not is_evidence_only_failure(item, item)):
                errors.append(record)
            else:
                if item.get("severity") == "error":
                    record = as_non_blocking(record, "insufficient_evidence_or_metadata")
                warnings.append(record)

    report = {
        "schema": 1,
        "status": "failed" if errors else "passed",
        "model_spec": str(spec_path),
        "reference_images": [str(path) for path in reference_paths],
        "validation_views": [str(path) for path in view_paths],
        "summary": {"errors": len(errors), "warnings": len(warnings), "info": len(info)},
        "errors": errors,
        "warnings": warnings,
        "info": info,
        "evaluation": evaluation,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"VISUAL_VALIDATION_REPORT={report_path}")
    print(f"VISUAL_VALIDATION_STATUS={report['status']} errors={len(errors)} warnings={len(warnings)}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
