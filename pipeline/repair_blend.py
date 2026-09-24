"""Repair make_blend.py with small, exact replacements; never regenerate it wholesale."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import ask_model
from local_repair_utils import apply_replacements, parse_repair_plan

ROOT = Path(__file__).resolve().parent.parent

PROMPT = """请局部修复下面的 make_blend.py。只输出一个JSON对象，不要Markdown、完整脚本或解释。

这是局部修复，不是重新设计：
1. 只修改诊断报告直接涉及的代码；保留其他造型、尺寸、材料、相机、灯光、渲染和报价映射。
2. 禁止返回完整脚本。每个old必须从当前脚本逐字复制、包含足够上下文并且只出现一次。
3. 可用一次替换修改函数、参数块或插入少量代码；最多20处。
4. 禁止放宽容差、删除必备构件、降低数量、修改锁定标准或用隐藏/排除分类绕过验证。
5. 禁止启动pip、Blender、Excel或其他脚本，也不得增加新依赖。

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

    materials, images, _ = ask_model.collect_inputs(input_dir)
    report = report_path.read_text(encoding="utf-8", errors="replace")
    script = script_path.read_text(encoding="utf-8", errors="replace")
    spec = spec_path.read_text(encoding="utf-8", errors="strict")
    prompt = PROMPT.replace("{report}", report).replace("{spec}", spec) \
                   .replace("{script}", script).replace("{materials}", materials or "（仅有图片）")
    content = [{"type": "text", "text": prompt}]
    if images:
        content.append({"type": "text", "text": "参考图片仅用于核对诊断涉及的结构，不得借机重新设计："})
        content.extend(images)

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
    for content_attempt in range(1, 3):
        result = ask_model.post_json(
            env["CNXMAI_CHAT_URL"],
            {"model": env["CNXMAI_MODEL"], "messages": [{"role": "user", "content": content}],
             "max_tokens": 16000, "temperature": 0},
            env["CNXMAI_API_KEY"], timeout=timeout, max_attempts=attempts,
        )
        response = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        try:
            replacements = parse_repair_plan(response)
            updated = apply_replacements(script, replacements)
            break
        except (ValueError, json.JSONDecodeError, SyntaxError) as exc:
            last_error = str(exc)
            print(f"局部补丁无效（内容尝试 {content_attempt}/2）：{last_error}", flush=True)
            content[0]["text"] += f"\n上一补丁被拒绝：{last_error}。请重新输出更小且old唯一匹配的JSON补丁。"

    (gen_dir / f"raw_repair_response_{args.attempt}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if updated is None or replacements is None:
        print(f"[错误] 无法生成可安全应用的局部补丁：{last_error}", file=sys.stderr)
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
