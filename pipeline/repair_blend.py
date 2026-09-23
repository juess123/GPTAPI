"""Ask the configured model to repair make_blend.py from a validation report."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import ask_model


ROOT = Path(__file__).resolve().parent.parent

REPAIR_PROMPT = """修复下面的 Blender 生成脚本。诊断材料可能是空间验证报告，也可能是运行时
Traceback报告。只输出完整 make_blend.py，不要解释。

这是自动修复，不是重新设计：
1. 只修复诊断报告中的错误以及导致错误的空间关系、模型结构、Python调用或 Blender API 问题。
2. 不得修改用户明确确认的尺寸、数量、材料和文字要求。
3. 不得增大 tolerance、把 confirmed 改成 assumption、删除 validation_spec_json，或把错误工程对象改成
   exclude_from_quote=True / quote_relevant=False 来绕过验证。
4. 保留并修正 component_id、instance_id、component_type、quote_relevant、exclude_from_quote。
5. 不得启动 pip、blender.exe、make_xlsx.py 或其他交付脚本；不得增加新的第三方依赖。
6. 必须继续从 OUT_DIR 写出 final_model.blend；保存后直接结束，不重新打开或自行验证。
7. Blender 版本兼容必须运行时探测，尤其处理 tessellate_polygon 返回索引或 Vector、集合成员按名称判断。

════════════════ 诊断报告 ════════════════
{report}
════════════════ 当前 make_blend.py ════════════════
{script}
════════════════ 原始文本材料 ════════════════
{materials}

严格输出：
<<<FILE:make_blend.py>>>
```python
完整修复代码
```
<<<ENDFILE>>>
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
    if not script_path.is_file() or not report_path.is_file():
        print("[错误] 自动修复缺少 make_blend.py 或诊断报告", file=sys.stderr)
        return 1

    materials, images, _ = ask_model.collect_inputs(input_dir)
    report = report_path.read_text(encoding="utf-8", errors="replace")
    script = script_path.read_text(encoding="utf-8", errors="replace")
    prompt = REPAIR_PROMPT.format(report=report, script=script, materials=materials or "（仅提供图片）")
    content = [{"type": "text", "text": prompt}]
    if images:
        content.append({"type": "text", "text": "原始参考图片如下，仅用于核对结构和空间关系："})
        content.extend(images)

    env = ask_model.load_env(ROOT / ".env")
    class Settings:
        timeout = None
        attempts = None
        max_tokens = None
    timeout, attempts, max_tokens = ask_model.resolve_settings(Settings, env)
    result = None
    repaired = None
    for content_attempt in range(1, 3):
        result = ask_model.post_json(
            env["CNXMAI_CHAT_URL"],
            {"model": env["CNXMAI_MODEL"], "messages": [{"role": "user", "content": content}],
             "max_tokens": max_tokens},
            env["CNXMAI_API_KEY"], timeout=timeout, max_attempts=attempts,
        )
        text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        repaired = ask_model.extract_files(text).get("make_blend.py")
        if repaired:
            break
        print(f"自动修复响应不完整（内容尝试 {content_attempt}/2）", flush=True)
        content[0]["text"] += "\n\n上一次回答为空或格式不完整。请从头输出完整 make_blend.py。"

    (gen_dir / f"raw_repair_response_{args.attempt}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if not repaired:
        print("[错误] 模型连续两次没有返回完整 make_blend.py", file=sys.stderr)
        return 1
    archive = gen_dir / f"make_blend.attempt-{args.attempt}.py"
    archive.write_text(script, encoding="utf-8")
    script_path.write_text(repaired, encoding="utf-8")
    print(f"已保存修复前脚本：{archive}")
    print(f"已写入修复脚本：{script_path}（{len(repaired.splitlines())} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
