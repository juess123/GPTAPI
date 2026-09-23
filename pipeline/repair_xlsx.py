"""Ask the configured model to repair make_xlsx.py from a runtime report."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import ask_model


ROOT = Path(__file__).resolve().parent.parent

REPAIR_PROMPT = """修复下面的 Excel 生成脚本。只输出完整 make_xlsx.py，不要解释。

这是自动修复，不是重新设计：
1. 修复诊断报告中的 Python异常、库调用、公式、共享数据读取或输出文件问题。
2. 不得修改用户明确确认的尺寸、数量、材料、报价规则和文字要求。
3. 如果提供了 model_manifest.json，Blender实际构件、尺寸和数量优先于脚本猜测。
4. 不得启动 pip、blender.exe、make_blend.py 或其他交付脚本；不得增加新的第三方依赖。
5. 只能在 OUT_DIR 写文件，必须生成 final_quote.xlsx 并打印其绝对路径。
6. 禁止使用不存在的占位模块、伪代码、TODO或“稍后替换”的未完成实现。

════════════════ 诊断报告 ════════════════
{report}
════════════════ Blender共享数据 ════════════════
{manifest}
════════════════ 当前 make_xlsx.py ════════════════
{script}
════════════════ 原始文本材料 ════════════════
{materials}

严格输出：
<<<FILE:make_xlsx.py>>>
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
    script_path = gen_dir / "make_xlsx.py"
    if not script_path.is_file() or not report_path.is_file():
        print("[错误] Excel自动修复缺少 make_xlsx.py 或诊断报告", file=sys.stderr)
        return 1

    materials, _, _ = ask_model.collect_inputs(input_dir)
    report = report_path.read_text(encoding="utf-8", errors="replace")
    script = script_path.read_text(encoding="utf-8", errors="replace")
    try:
        report_data = json.loads(report)
    except json.JSONDecodeError:
        report_data = {}
    manifest_path = Path(report_data.get("model_manifest", "")) if report_data.get("model_manifest") else None
    manifest = (manifest_path.read_text(encoding="utf-8", errors="replace")
                if manifest_path and manifest_path.is_file() else "（本次Blender未生成共享清单）")
    prompt = REPAIR_PROMPT.format(
        report=report, manifest=manifest, script=script,
        materials=materials or "（无文本材料）")
    content = [{"type": "text", "text": prompt}]

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
        repaired = ask_model.extract_files(text).get("make_xlsx.py")
        if repaired:
            break
        print(f"Excel修复响应不完整（内容尝试 {content_attempt}/2）", flush=True)
        content[0]["text"] += "\n\n上一次回答为空或格式不完整。请从头输出完整 make_xlsx.py。"

    (gen_dir / f"raw_xlsx_repair_response_{args.attempt}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if not repaired:
        print("[错误] 模型连续两次没有返回完整 make_xlsx.py", file=sys.stderr)
        return 1
    archive = gen_dir / f"make_xlsx.attempt-{args.attempt}.py"
    archive.write_text(script, encoding="utf-8")
    script_path.write_text(repaired, encoding="utf-8")
    print(f"已保存修复前Excel脚本：{archive}")
    print(f"已写入Excel修复脚本：{script_path}（{len(repaired.splitlines())} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
