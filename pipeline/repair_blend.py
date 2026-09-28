"""Repair make_blend.py with small, exact replacements; never regenerate it wholesale."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import ask_model
from local_repair_utils import EmptyRepairPlan, apply_replacements, parse_repair_plan

ROOT = Path(__file__).resolve().parent.parent


def known_runtime_replacements(report_data: dict, script: str) -> list[dict[str, str]]:
    """Return deterministic, narrowly-scoped fixes for known Blender API variants."""
    output = str(report_data.get("output", ""))
    old = "ids = [vertex_ids[tuple(v)] for v in tri]"
    if ("TypeError: 'int' object is not iterable" in output
            and old in script
            and "tessellate_polygon" in script):
        return [{
            "old": old,
            "new": ("ids = (list(tri) if tri and isinstance(tri[0], int) else "
                    "[vertex_ids[tuple(v)] for v in tri])"),
            "reason": "兼容 Blender 三角剖分返回顶点索引或顶点向量两种形式",
        }]
    return []

PROMPT = """请局部修复下面的 make_blend.py。只输出一个JSON对象，不要Markdown、完整脚本或解释。

这是局部修复，不是重新设计：
1. 只修改诊断报告直接涉及的代码；保留其他造型、尺寸、材料、相机、灯光、渲染和报价映射。
2. 禁止返回完整脚本。每个old必须从当前脚本逐字复制、包含足够上下文并且只出现一次。
3. 可用一次替换修改函数、参数块或插入少量代码；最多20处。
4. 禁止放宽容差、删除必备构件、降低数量、修改锁定标准或用隐藏/排除分类绕过验证。
5. 禁止启动pip、Blender、Excel或其他脚本，也不得增加新依赖。
6. 修复优先级固定为：用户文字逐条合规 > 参考图主要外观 > 明显结构错误 > 尺寸数量。
7. 视觉问题必须修改模型本体的几何、位置、比例、开口、材质或颜色；禁止只调整灯光、相机、背景
   或验证视图来伪装修复。报告中的 validation_views 是当前模型证据，原始图片是目标参考。
8. 保留 PIPELINE_SKIP_OPTIONAL_RENDERS 对可选高质量预览的保护；该开关不得影响模型构建和.blend保存。

严格输出结构：
{
  "replacements": [
    {"old": "当前脚本中唯一存在的完整原文", "new": "替换后的代码", "reason": "对应的错误"}
  ]
}

==================== 诊断报告 ====================
{report}
==================== 锁定标准 ====================
{spec}
==================== 当前脚本 ====================
{script}
==================== 原始文字材料 ====================
{materials}
"""


def compact_repair_context(report_data: dict, spec_data: dict, gen_dir: Path) -> tuple[str, str, str, bool]:
    """Keep only fatal errors and the locked rules needed to repair them."""
    errors = [item for item in report_data.get("errors", []) if isinstance(item, dict)]
    requirement_ids = {str(item.get("requirement_id")) for item in errors
                       if item.get("requirement_id")}
    criterion_ids = {str(item.get("criterion_id")) for item in errors
                     if item.get("criterion_id")}
    feature_ids = {str(item.get("feature_id")) for item in errors if item.get("feature_id")}
    component_types = {str(item.get("component_type")) for item in errors
                       if item.get("component_type")}

    requirements = []
    source_ids: set[str] = set()
    for item in spec_data.get("source_requirements", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("id")) in requirement_ids or str(item.get("target")) in component_types:
            requirements.append(item)
            source_ids.update(map(str, item.get("src", [])))
    criteria = [item for item in spec_data.get("visual_criteria", [])
                if isinstance(item, dict) and str(item.get("id")) in criterion_ids]
    features = [item for item in spec_data.get("required_features", [])
                if isinstance(item, dict) and (str(item.get("id")) in feature_ids
                                                or str(item.get("component_type")) in component_types)]
    counts = {key: value for key, value in spec_data.get("expected_counts", {}).items()
              if str(key) in component_types}
    compact_spec = {
        "schema": spec_data.get("schema"),
        "project_type": spec_data.get("project_type"),
        "overall": spec_data.get("overall", {}),
        "expected_counts": counts,
        "required_features": features,
        "spatial_rules": spec_data.get("spatial_rules", []),
        "source_requirements": requirements,
        "visual_criteria": criteria,
    }
    compact_report = {
        "schema": report_data.get("schema"),
        "status": report_data.get("status"),
        "failure_type": report_data.get("failure_type"),
        "message": report_data.get("message"),
        "summary": {"fatal_errors": len(errors)},
        "errors": errors,
        "validation_views": report_data.get("validation_views", []),
    }
    # Runtime failures do not have validation `errors`; their actionable evidence
    # is the captured subprocess output.  Keep the tail where Python puts the
    # exception and traceback so the repair model is never asked to guess.
    runtime_output = report_data.get("output")
    if isinstance(runtime_output, str) and runtime_output.strip():
        compact_report["output"] = runtime_output[-16000:]

    source_excerpt = []
    inventory_path = gen_dir / "source_inventory.json"
    if source_ids and inventory_path.is_file():
        try:
            inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
            source_excerpt = [item for item in inventory
                              if isinstance(item, dict) and str(item.get("id")) in source_ids]
        except (OSError, json.JSONDecodeError):
            source_excerpt = []
    needs_images = any(item.get("phase") == "requirements_and_visual"
                       or item.get("criterion_id")
                       or item.get("code") == "VISUALLY_OBVIOUS_STRUCTURE_ERROR"
                       for item in errors)
    return (
        json.dumps(compact_report, ensure_ascii=False, separators=(",", ":")),
        json.dumps(compact_spec, ensure_ascii=False, separators=(",", ":")),
        json.dumps(source_excerpt, ensure_ascii=False, separators=(",", ":"))
        if source_excerpt else "（致命错误对应的锁定规则已包含所需上下文）",
        needs_images,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--gen-dir", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--attempt", type=int, required=True)
    args = parser.parse_args()
    input_dir = Path(args.input_dir).resolve()
    gen_dir = Path(args.gen_dir).resolve()
    report_path = Path(args.report).resolve()
    script_path = gen_dir / "make_blend.py"
    spec_path = gen_dir / "model_spec.json"
    if not all(path.is_file() for path in (report_path, script_path, spec_path)):
        print("[错误] 局部修复缺少报告、make_blend.py或model_spec.json", file=sys.stderr)
        return 1

    images = []
    for image_path in sorted(path for path in input_dir.rglob("*")
                             if path.is_file() and path.suffix.lower() in ask_model.IMAGE_EXTS):
        url, note = ask_model.image_data_url(image_path, max_dimension=1600)
        if url is None:
            print(f"[警告] 无法附加修复参考图 {image_path.name}: {note}", file=sys.stderr)
            continue
        images.append({"type": "image_url", "image_url": {"url": url}})
    report = report_path.read_text(encoding="utf-8", errors="replace")
    script = script_path.read_text(encoding="utf-8", errors="replace")
    spec = spec_path.read_text(encoding="utf-8", errors="strict")
    try:
        report_data = json.loads(report)
        spec_data = json.loads(spec)
    except json.JSONDecodeError as exc:
        print(f"[错误] 局部修复上下文JSON无效：{exc}", file=sys.stderr)
        return 1

    # Cheap and reliable fixes run before any model request.  They are exact
    # replacements and pass the same uniqueness/syntax checks as GPT patches.
    replacements = known_runtime_replacements(report_data, script)
    if replacements:
        try:
            updated = apply_replacements(script, replacements)
        except (ValueError, SyntaxError) as exc:
            print(f"[警告] 已知本地补丁不适用，将交给GPT：{exc}", flush=True)
        else:
            archive = gen_dir / f"make_blend.attempt-{args.attempt}.py"
            archive.write_text(script, encoding="utf-8")
            script_path.write_text(updated, encoding="utf-8")
            plan_path = gen_dir / f"blend-local-repair-{args.attempt}.json"
            plan_path.write_text(json.dumps(
                {"source": "deterministic-local-rule", "replacements": replacements},
                ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"已应用Blender本地兼容补丁：{len(replacements)}处；原脚本：{archive}")
            return 0
    compact_report, compact_spec, source_excerpt, needs_images = compact_repair_context(
        report_data, spec_data, gen_dir)
    prompt = PROMPT.replace("{report}", compact_report).replace("{spec}", compact_spec) \
                   .replace("{script}", script).replace("{materials}", source_excerpt)
    prompt += """

本次补丁只能修复报告中的最终模型实体缺陷，不得为了让报告变好看而改动模型：
禁止为了补齐集合树、隐藏节点证明、验证清单或其他报告证据而修改模型。
但有一条例外：如果某条致命错误是构件数量或必备构件缺失，而该构件确实已在场景中
建成（网格/曲线/字体或其子件存在），只是 component_type / component_id 写在
非几何父级总成（EMPTY）上、或仅写在子件上，那么修正标签归属属于合法修复，允许生成补丁。
如果某条问题确实不需要改变实际几何、位置、材质或可见内容，就不要为它单独生成补丁，
但仍必须为上面列出的致命错误给出补丁，不得返回空数组。
"""
    content = [{"type": "text", "text": prompt}]
    print(f"Blender修复文本长度: {len(prompt)} 字符（仅含致命错误）", flush=True)
    if images and needs_images:
        content.append({"type": "text", "text": "以下是用户原始参考图片，是外观与结构目标："})
        content.extend(images)
    validation_views = report_data.get("validation_views", []) if isinstance(report_data, dict) else []
    for raw_path in validation_views if needs_images else []:
        view_path = Path(str(raw_path))
        if not view_path.is_file():
            continue
        url, note = ask_model.image_data_url(view_path)
        if url is None:
            print(f"[警告] 无法附加当前模型验证视图 {view_path.name}: {note}", file=sys.stderr)
            continue
        content.append({"type": "text", "text": f"当前Blender模型验证视图：{view_path.name}"})
        content.append({"type": "image_url", "image_url": {"url": url}})

    env = ask_model.load_env(ROOT / ".env")

    class Settings:
        timeout = None
        attempts = None
        max_tokens = None

    timeout, attempts, _ = ask_model.resolve_settings(Settings, env)
    result = None
    updated = None
    replacements = None
    last_error = ""
    empty_plan = False
    fatal_errors = [item for item in report_data.get("errors", []) if isinstance(item, dict)]
    fatal_brief = "；".join(
        f"{item.get('code')}({item.get('component_type') or item.get('feature_id') or '-'})"
        for item in fatal_errors[:8])
    # Invalid JSON is a response-format failure, not a failed Blender repair attempt,
    # so it is corrected inside this same attempt.  An explicit empty array is a
    # different outcome (the model judged nothing needs changing) and repeating the
    # same question at it only wastes requests, so it gets a single, different nudge.
    content_attempts = 4
    empty_attempts = 2
    empty_tries = 0
    for content_attempt in range(1, content_attempts + 1):
        result = ask_model.post_json(
            env["CNXMAI_CHAT_URL"],
            {"model": env["CNXMAI_MODEL"], "messages": [{"role": "user", "content": content}],
             "max_tokens": 16000, "temperature": 0},
            env["CNXMAI_API_KEY"], timeout=timeout, max_attempts=attempts,
        )
        response = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        # Persist every response as it arrives: a run that exhausts its retries used to
        # leave only the last one, so a systematic refusal was undiagnosable afterwards.
        (gen_dir / f"raw_repair_response_{args.attempt}_{content_attempt}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            replacements = parse_repair_plan(response)
            updated = apply_replacements(script, replacements)
            break
        except EmptyRepairPlan as exc:
            empty_tries += 1
            last_error = str(exc)
            empty_plan = True
            print(f"局部补丁为空（模型认为无需修改 {empty_tries}/{empty_attempts}）：{last_error}",
                  flush=True)
            if empty_tries >= empty_attempts:
                break
            content[0]["text"] += (
                f"\n上一响应是空补丁：{last_error}。但本轮致命错误共 {len(fatal_errors)} 项："
                f"{fatal_brief}。它们是外部验证器的硬性要求，空数组会被直接判为失败："
                "必须针对上述错误给出至少一处 replacement。"
                "若某条错误只是 component_type/component_id 的归属问题（构件确实已建成，"
                "标签却写在了非几何父级总成上，或只写在子件上），修正标签归属属于合法修复。"
                "请重新输出最小且 old 唯一匹配的JSON补丁。"
            )
        except (ValueError, json.JSONDecodeError, SyntaxError) as exc:
            last_error = str(exc)
            print(f"局部补丁无效（格式纠正 {content_attempt}/{content_attempts}）：{last_error}", flush=True)
            content[0]["text"] += (
                f"\n上一响应被拒绝：{last_error}。这只是响应格式纠正，不是新的模型修复轮次。"
                "必须返回至少一个直接修复Traceback根因的replacement；"
                "请重新输出更小且old唯一匹配的JSON补丁。"
            )

    (gen_dir / f"raw_repair_response_{args.attempt}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if updated is None or replacements is None:
        if empty_plan:
            detail = (f"模型连续 {empty_tries} 次返回空补丁，认为无需修改；"
                      f"但本轮 {len(fatal_errors)} 项致命错误仍需处理：{fatal_brief}")
        else:
            detail = last_error
        print(f"[错误] 无法生成可安全应用的局部补丁：{detail}", file=sys.stderr)
        return 1
    archive = gen_dir / f"make_blend.attempt-{args.attempt}.py"
    archive.write_text(script, encoding="utf-8")
    script_path.write_text(updated, encoding="utf-8")
    plan_path = gen_dir / f"blend-local-repair-{args.attempt}.json"
    plan_path.write_text(json.dumps({"replacements": replacements}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已安全应用Blender局部补丁：{len(replacements)}处；原脚本：{archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
