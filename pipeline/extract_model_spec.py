#!/usr/bin/env python3
"""Extract a compact, evidence-backed model specification before generation."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import ask_model


ROOT = Path(__file__).resolve().parent.parent

PROMPT = """根据任务材料生成一份精简、可执行的 Blender 验收标准。只输出一个JSON对象，不要Markdown、代码或解释。

核心目标只有三个：
1. 模型结构和尺寸正确。
2. 用户文字中直接影响 Blender 模型的要求得到落实。
3. 模型主要外观与参考图一致，不出现肉眼明显的空间或结构错误。

不要把采购流程、报价计算细节、工作簿操作方法、证据管理流程、交付说明展开成 Blender 验收规则。
灯光、阴影、摄影棚、背景和相机角度只作辅助，不能成为硬性验收标准。

==================== 文字清单分类 ====================
source_classification 只需要列出 B 类和 X 类；没有列出的编号由程序自动归为 N 类：
- "B"：直接影响并且可以从 .blend 的几何、尺寸、位置、数量、材质、颜色、层级或动画验证。
- "X"：只影响 Excel、报价、采购数据或工作簿。
- "N"：标题、解释、工作流程、证据处理、交付说明，或无法从模型本体直接验收的内容。

所有引用必须逐个使用清单中的标准编号，例如 "SRC-0005"；数字部分至少四位并保留前导零。
禁止写成 SRC-5、SRC-005 或 SRC-0001..SRC-0020。不要输出 N 类编号，省略项由程序自动归为 N。

只有 B 类才能展开到 source_requirements。每个 B 类 SRC 必须至少被一条 source_requirements.src 引用。
同类要求应合并，一条规则可以引用多个SRC；不要复制原文、文件名、证据和长篇解释。
source_requirements 最多80条，每条 check 尽量不超过60个汉字。

==================== 证据状态 ====================
confirmed=文字或明确标注直接给出；derived=由明确数据可靠计算；
assumption=合理补全，只能告警；conflict=来源冲突，不能强制判错。
文字明确要求高于参考图。图片看不清或不可见的内容不得标成confirmed。

==================== 视觉标准 ====================
visual_criteria 最多30条，只保留整体轮廓、主要构件、数量、布局、左右上下前后关系、比例、开口、
曲直形态、主要材质和颜色分区。多个视图应作为同一物体互相补充；不要把透视、灯光和阴影差异当错误。

==================== 严格输出结构 ====================
{
  "schema": 3,
  "project_type": "简短项目类型",
  "source_classification": [["SRC-0001","B"],["SRC-0002","X"],["SRC-0003","N"]],
  "source_requirements": [
    {
      "id": "R001",
      "src": ["SRC-0001"],
      "target": "稳定英文component_type或overall",
      "category": "dimension|count|position|spacing|elevation|thickness|opening|component|shape|material|color|function|animation|forbidden|other",
      "check": "短而明确的通过条件",
      "verify": "visual|geometry|metadata",
      "level": "hard|advisory",
      "status": "confirmed|derived|assumption|conflict"
    }
  ],
  "visual_criteria": [
    {
      "id": "V001",
      "source": "参考图文件名",
      "category": "silhouette|layout|proportion|component|opening|shape|material|color",
      "check": "短而明确的视觉特征",
      "level": "hard|advisory",
      "status": "confirmed|derived|assumption|conflict",
      "view": "front|back|left|right|top|perspective|any"
    }
  ],
  "overall": {
    "width_mm": {"value": 数字或null, "status": "confirmed|derived|assumption|conflict", "tolerance_mm": 数字},
    "depth_mm": 同上,
    "height_mm": 同上,
    "scope": {
      "height_reference": "ground|bounds",
      "ground_z_mm": 数字或null,
      "component_types": [],
      "exclude_component_types": []
    }
  },
  "expected_counts": {
    "稳定英文component_type": {"value": 整数或null, "status": "confirmed|derived|assumption|conflict"}
  },
  "required_features": [
    {"id":"F001","component_type":"稳定英文类型","required":true,"status":"confirmed|derived|assumption|conflict"}
  ],
  "spatial_rules": [],
  "quote_requirements": [
    {"id":"Q001","required":true,"status":"confirmed|derived|assumption|conflict","keywords":[]}
  ],
  "conflicts": [],
  "assumptions": []
}

source_requirements、visual_criteria、required_features、quote_requirements、conflicts和assumptions没有内容时输出空数组。
quote_requirements最多15条，只保留最终Excel必须具备的栏目或汇总大类，不要逐条展开X类文字。
禁止增加上述结构之外的长篇说明字段。

==================== 表格等辅助材料 ====================
{supporting_materials}

==================== 文字材料逐项清单 ====================
每项格式为 [SRC编号, 文件名, 行号, 原文]：
{source_inventory}
"""


def build_source_inventory(input_dir: Path) -> list[dict]:
    """Create a deterministic line inventory so no text input is silently ignored."""
    inventory: list[dict] = []
    for path in sorted(item for item in input_dir.rglob("*") if item.is_file()):
        if path.suffix.lower() not in ask_model.TEXT_EXTS:
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            text = path.read_text(encoding="gbk", errors="replace")
        for line_number, raw in enumerate(text.splitlines(), 1):
            content = raw.strip()
            if not content or content in {"```", "```text", "```markdown"}:
                continue
            inventory.append({
                "id": f"SRC-{len(inventory) + 1:04d}",
                "source_file": path.relative_to(input_dir).as_posix(),
                "line": line_number,
                "text": content,
            })
    return inventory


def supporting_materials(input_dir: Path) -> str:
    """Include tabular inputs once; text inputs are already represented by the inventory."""
    blocks = []
    for path in sorted(item for item in input_dir.rglob("*") if item.is_file()):
        if path.suffix.lower() in ask_model.XLSX_EXTS:
            rel = path.relative_to(input_dir).as_posix()
            blocks.append(f"────────── 表格：{rel} ──────────\n{ask_model.xlsx_to_text(path)}")
    return "\n\n".join(blocks) or "（没有表格材料）"


def compact_inventory(inventory: list[dict]) -> str:
    rows = [[item["id"], item["source_file"], item["line"], item["text"]] for item in inventory]
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


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
        raise ValueError("规格顶层必须是JSON对象")
    return value


SOURCE_ID_RE = re.compile(r"^SRC-(\d{4,})$")
RULE_CATEGORIES = {
    "dimension", "count", "position", "spacing", "elevation", "thickness", "opening",
    "component", "shape", "material", "color", "function", "animation", "forbidden", "other",
}
RULE_CATEGORY_ALIASES = {
    "process": "other",
    "manufacturing": "other",
    "fabrication": "other",
    "workmanship": "other",
}
VISUAL_CATEGORIES = {
    "silhouette", "layout", "proportion", "component", "opening", "shape", "material", "color",
}
VISUAL_CATEGORY_ALIASES = {
    "dimension": "proportion",
    "size": "proportion",
    "position": "layout",
    "spacing": "layout",
}


def expand_source_id(value: object) -> list[str]:
    """Accept only the canonical inventory form, such as `SRC-0005`."""
    text = str(value).strip()
    exact = SOURCE_ID_RE.fullmatch(text)
    if not exact:
        raise ValueError(f"SRC编号必须使用标准格式（如SRC-0005）：{text}")
    return [text]


def normalize_source_references(spec: dict, inventory: list[dict]) -> None:
    """Expand compact ranges and classify omitted inventory rows as background (N)."""
    expected_order = [str(item["id"]) for item in inventory]
    expected = set(expected_order)
    rows = spec.get("source_classification")
    if not isinstance(rows, list):
        raise ValueError("缺少source_classification数组")
    classified: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != 2:
            raise ValueError("source_classification必须使用[标准SRC编号,分类]格式")
        source_expr, category = row
        category = str(category)
        if category not in {"B", "X", "N"}:
            raise ValueError(f"SRC分类无效：{source_expr}={category}")
        for source_id in expand_source_id(source_expr):
            if source_id not in expected:
                raise ValueError(f"引用了不存在的SRC编号：{source_id}")
            if source_id in classified:
                raise ValueError(f"SRC重复分类：{source_id}")
            classified[source_id] = category
    requirements = spec.get("source_requirements")
    if not isinstance(requirements, list):
        raise ValueError("缺少source_requirements数组")
    covered_by_rule: set[str] = set()
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        source_values = requirement.get("src")
        if not isinstance(source_values, list):
            continue
        expanded: list[str] = []
        for source_value in source_values:
            expanded.extend(expand_source_id(source_value))
        # Preserve order while avoiding duplicate evidence references.
        requirement["src"] = list(dict.fromkeys(expanded))
        for source_id in requirement["src"]:
            if source_id not in expected:
                raise ValueError(f"规则引用了不存在的SRC编号：{source_id}")
            # A source used as direct evidence for a Blender rule is B by
            # definition.  The concrete rule reference wins if the model's
            # compact classification omitted it or contradicted itself.
            classified[source_id] = "B"
            covered_by_rule.add(source_id)

    # A bare B label has no executable meaning without a requirement. Treat
    # such coarse/accidental classifications as background rather than making
    # a thousand-line source inventory fatal.
    for source_id, category in list(classified.items()):
        if category == "B" and source_id not in covered_by_rule:
            classified[source_id] = "N"

    for source_id in expected_order:
        classified.setdefault(source_id, "N")
    spec["source_classification"] = [[source_id, classified[source_id]]
                                     for source_id in expected_order]


def normalize_rule_categories(spec: dict) -> None:
    """Normalize common model-produced aliases to the locked category enum."""
    requirements = spec.get("source_requirements")
    if not isinstance(requirements, list):
        raise ValueError("缺少source_requirements数组")
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        category = str(requirement.get("category", "")).strip().lower()
        category = RULE_CATEGORY_ALIASES.get(category, category)
        requirement["category"] = category
        if category not in RULE_CATEGORIES:
            raise ValueError(
                f"规则{requirement.get('id')}类别无效：{category}；"
                f"允许值：{sorted(RULE_CATEGORIES)}"
            )


def normalize_visual_categories(spec: dict) -> None:
    """Normalize visual-only aliases before the independent lock validator."""
    criteria = spec.get("visual_criteria")
    if not isinstance(criteria, list):
        raise ValueError("缺少visual_criteria数组")
    for criterion in criteria:
        if not isinstance(criterion, dict):
            continue
        category = str(criterion.get("category", "")).strip().lower()
        category = VISUAL_CATEGORY_ALIASES.get(category, category)
        criterion["category"] = category
        if category not in VISUAL_CATEGORIES:
            raise ValueError(
                f"视觉规则{criterion.get('id')}类别无效：{category}；"
                f"允许值：{sorted(VISUAL_CATEGORIES)}"
            )


def classification_map(spec: dict) -> dict[str, str]:
    result: dict[str, str] = {}
    rows = spec.get("source_classification")
    if not isinstance(rows, list):
        raise ValueError("缺少source_classification数组")
    for row in rows:
        if not isinstance(row, list) or len(row) != 2:
            raise ValueError("source_classification必须使用[SRC编号,分类]格式")
        source_id, category = map(str, row)
        if source_id in result:
            raise ValueError(f"SRC重复分类：{source_id}")
        if category not in {"B", "X", "N"}:
            raise ValueError(f"SRC分类无效：{source_id}={category}")
        result[source_id] = category
    return result


def validate_compact_spec(spec: dict, inventory: list[dict]) -> None:
    expected = {str(item["id"]) for item in inventory}
    classified = classification_map(spec)
    unknown = set(classified) - expected
    missing = expected - set(classified)
    if unknown:
        raise ValueError(f"引用了不存在的SRC编号：{sorted(unknown)}")
    if missing:
        raise ValueError(f"未分类的SRC编号：{sorted(missing)}")

    requirements = spec.get("source_requirements")
    if not isinstance(requirements, list):
        raise ValueError("缺少source_requirements数组")
    if len(requirements) > 80:
        raise ValueError(f"source_requirements过多：{len(requirements)}，上限80")
    covered: set[str] = set()
    for requirement in requirements:
        if not isinstance(requirement, dict):
            raise ValueError("source_requirements包含非对象")
        if requirement.get("category") not in RULE_CATEGORIES:
            raise ValueError(f"规则{requirement.get('id')}类别无效：{requirement.get('category')}")
        source_ids = requirement.get("src")
        if not isinstance(source_ids, list) or not source_ids:
            raise ValueError(f"规则{requirement.get('id')}没有src")
        for source_id in map(str, source_ids):
            if classified.get(source_id) != "B":
                raise ValueError(f"规则{requirement.get('id')}引用非B类编号：{source_id}")
            covered.add(source_id)
    missing_blender = {source_id for source_id, category in classified.items()
                       if category == "B" and source_id not in covered}
    if missing_blender:
        raise ValueError(f"B类文字没有对应模型规则：{sorted(missing_blender)}")
    visual = spec.get("visual_criteria")
    if not isinstance(visual, list):
        raise ValueError("缺少visual_criteria数组")
    if len(visual) > 30:
        raise ValueError(f"visual_criteria过多：{len(visual)}，上限30")
    quote = spec.get("quote_requirements")
    if not isinstance(quote, list):
        raise ValueError("缺少quote_requirements数组")
    if len(quote) > 15:
        raise ValueError(f"quote_requirements过多：{len(quote)}，上限15")


def inventory_digest(inventory_text: str) -> str:
    return hashlib.sha256(inventory_text.encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--gen-dir", required=True)
    args = parser.parse_args()
    input_dir = Path(args.input_dir).resolve()
    gen_dir = Path(args.gen_dir).resolve()
    gen_dir.mkdir(parents=True, exist_ok=True)

    _, images, _ = ask_model.collect_inputs(input_dir)
    inventory = build_source_inventory(input_dir)
    inventory_text = json.dumps(inventory, ensure_ascii=False, indent=2)
    inventory_path = gen_dir / "source_inventory.json"
    inventory_path.write_text(inventory_text, encoding="utf-8")

    prompt = PROMPT.replace("{supporting_materials}", supporting_materials(input_dir)) \
                   .replace("{source_inventory}", compact_inventory(inventory))
    content = [{"type": "text", "text": prompt}, *images]
    env = ask_model.load_env(ROOT / ".env")

    class Settings:
        timeout = None
        attempts = None
        max_tokens = None

    timeout, attempts, _ = ask_model.resolve_settings(Settings, env)
    spec = None
    last_result = None
    for attempt in range(1, 3):
        result = ask_model.post_json(
            env["CNXMAI_CHAT_URL"],
            {"model": env["CNXMAI_MODEL"], "messages": [{"role": "user", "content": content}],
             "max_tokens": 16000, "temperature": 0},
            env["CNXMAI_API_KEY"], timeout=timeout, max_attempts=attempts,
        )
        last_result = result
        (gen_dir / f"raw_model_spec_response_attempt_{attempt}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        response = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        try:
            candidate = extract_json(response)
            normalize_source_references(candidate, inventory)
            normalize_rule_categories(candidate)
            normalize_visual_categories(candidate)
            validate_compact_spec(candidate, inventory)
            spec = candidate
            break
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"规格提取响应无效（内容尝试 {attempt}/2）：{exc}", flush=True)
            content[0]["text"] += (f"\n上一回答未通过校验：{exc}。"
                                   "请保持精简，重新输出完整JSON；SRC必须逐个使用至少四位标准编号，省略项自动归N类，"
                                   "每个B类必须被短规则引用。")

    (gen_dir / "raw_model_spec_response.json").write_text(
        json.dumps(last_result, ensure_ascii=False, indent=2), encoding="utf-8")
    if spec is None:
        print("[错误] 无法从模型响应解析精简model_spec.json", file=sys.stderr)
        return 1

    spec["schema"] = 3
    spec["source_inventory_count"] = len(inventory)
    spec["source_inventory_sha256"] = inventory_digest(inventory_text)
    path = gen_dir / "model_spec.json"
    path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已提取精简锁定标准：{path}")
    print(f"  文字清单: {len(inventory)} 条")
    print(f"  模型规则: {len(spec.get('source_requirements', []))} 条")
    print(f"  视觉规则: {len(spec.get('visual_criteria', []))} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
