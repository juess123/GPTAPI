#!/usr/bin/env python3
"""Extract a task-level, evidence-backed model specification before code generation."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import ask_model


ROOT = Path(__file__).resolve().parent.parent

PROMPT = """根据下面全部任务材料提取一份独立的工程验证标准。只输出一个JSON对象，不要代码、Markdown或解释。

这份JSON会在Blender和Excel生成前锁定，后续生成模型无权修改。必须以材料证据为准：
1. 用户补充文字 > 标为最终版/修订版的文字 > 表格 > 图片明确标注 > 可计算结果 > 专业假设。
2. confirmed=材料明确给出；derived=由明确数据计算；assumption=合理补全；conflict=来源冲突。
3. 不得因为参考图只画关闭状态，就把文字明确要求的活动窗、PC、合页或液压杆删除。
4. component_type使用稳定的英文标识；数量未知用null，不要用0代替未知。
5. 每个confirmed/derived事实必须包含evidence，指出文件和依据摘要。

严格结构：
{
  "schema": 1,
  "project_type": "字符串",
  "overall": {
    "width_mm": {"value": 数字或null, "status": "confirmed|derived|assumption|conflict", "evidence": []},
    "depth_mm": 同上,
    "height_mm": 同上
  },
  "expected_counts": {
    "稳定英文component_type": {"value": 整数或null, "status": "confirmed|derived|assumption|conflict", "evidence": []}
  },
  "required_features": [
    {"id": "稳定英文ID", "component_type": "对应构件类型或null", "required": true或false, "status": "confirmed|derived|assumption|conflict", "evidence": []}
  ],
  "spatial_rules": [],
  "quote_requirements": [
    {"id": "稳定英文ID", "required": true或false, "status": "confirmed|derived|assumption|conflict", "keywords": [], "evidence": []}
  ],
  "conflicts": [],
  "assumptions": []
}

════════════════ 任务材料 ════════════════
{materials}
══════════════════════════════════════════
"""


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--gen-dir", required=True)
    args = parser.parse_args()
    input_dir = Path(args.input_dir).resolve()
    gen_dir = Path(args.gen_dir).resolve()
    gen_dir.mkdir(parents=True, exist_ok=True)

    materials, images, _ = ask_model.collect_inputs(input_dir)
    prompt = PROMPT.replace("{materials}", materials or "（只有图片材料）")
    content = [{"type": "text", "text": prompt}, *images]
    env = ask_model.load_env(ROOT / ".env")

    class Settings:
        timeout = None
        attempts = None
        max_tokens = None

    timeout, attempts, _ = ask_model.resolve_settings(Settings, env)
    result = None
    spec = None
    for attempt in range(1, 3):
        result = ask_model.post_json(
            env["CNXMAI_CHAT_URL"],
            {"model": env["CNXMAI_MODEL"], "messages": [{"role": "user", "content": content}],
             "max_tokens": 24000, "temperature": 0},
            env["CNXMAI_API_KEY"], timeout=timeout, max_attempts=attempts,
        )
        response = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        try:
            spec = extract_json(response)
            break
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"规格提取响应无效（内容尝试 {attempt}/2）：{exc}", flush=True)
            content[0]["text"] += "\n上一回答不是有效JSON。请重新输出完整且唯一的JSON对象。"

    (gen_dir / "raw_model_spec_response.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if spec is None:
        print("[错误] 无法从模型响应解析 model_spec.json", file=sys.stderr)
        return 1
    spec["schema"] = 1
    path = gen_dir / "model_spec.json"
    path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已提取锁定前标准：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
