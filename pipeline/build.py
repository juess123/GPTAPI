#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A' 流水线 · 第 2 步：在本地执行模型生成的脚本，产出真正的二进制文件。

流程:
    generated/make_blend.py  --(统一 Blender 启动器)-------> output/<本次运行>/final_model.blend
    generated/make_xlsx.py   --(python)-------------------> output/<本次运行>/final_quote.xlsx

最终交付：每次运行在 output/ 下新建一个独立子目录，里面必定包含
final_quote.xlsx 与 final_model.blend。
只交付 .blend 本体这件事由投料口的提示词保证（input/money.md 末尾那段硬性要求
写明「严禁用 zip 打包 .blend 代替本体，也不要写任何 zip / 打包相关代码」），
因此这里不再对 zip 做任何兜底处理。
任务原文要求模型额外渲染的附属件（如成品材料标注图）会一并保留，
默认只告警；加 --strict 则要求目录里严格只有那两个文件。

子目录命名:
  · 默认用时间戳，如 output/2026-09-22_162530/  → 历次结果都保留，互不覆盖
  · 也可用 --run-name 指定固定名字，便于反复调试同一次任务（同名目录会被先清空）

说明:
  · 不校验 .blend 的内部内容（信任模型生成的结果）。
  · 不本地改写生成的脚本。跨版本 API 兼容写法由投料口提示词约束
    （见 input/money.md 与 pipeline/ask_model.py 里的技术契约），
    build.py 只负责「执行 + 校验两个成品是否产出」。

用法:
    python pipeline/build.py
    python pipeline/build.py --run-name demo      # 固定输出到 output/demo/
    python pipeline/build.py --strict             # 只允许两个交付物，多一个就报错
    python pipeline/build.py --gen-dir D --out-dir E   # 供 Web 后端指定独立工作区
"""

import os
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
GEN = HERE / "generated"
OUT = ROOT / "output"          # 历次运行的父目录；本次结果落在 OUT/<运行目录>/ 下

XLSX_NAME = "final_quote.xlsx"
BLEND_NAME = "final_model.blend"

BLENDER_CANDIDATES = [
    r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 5.0\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 4.5\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 4.2\blender.exe",
]


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------

def find_blender() -> str | None:
    """定位 blender.exe：先查 PATH，再扫常见安装目录。"""
    found = shutil.which("blender")
    if found:
        return found
    for c in BLENDER_CANDIDATES:
        if Path(c).exists():
            return c
    for root in (r"C:\Program Files\Blender Foundation",
                 r"C:\Program Files (x86)\Blender Foundation"):
        p = Path(root)
        if p.is_dir():
            hits = sorted(p.glob("Blender */blender.exe"), reverse=True)
            if hits:
                return str(hits[0])
    return None


def run(cmd: list[str], label: str, env: dict | None = None) -> tuple[bool, str]:
    """执行子进程并回显输出。"""
    print(f"\n$ {' '.join(f'\"{c}\"' if ' ' in c else c for c in cmd)}")
    proc = subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if out.strip():
        print(out.rstrip())
    ok = proc.returncode == 0
    print(f"[{label}] {'成功' if ok else f'失败 (exit={proc.returncode})'}")
    return ok, out


def fail(msg: str) -> int:
    print(f"\n[错误] {msg}")
    return 1


def write_build_failure(run_dir: Path, failure_type: str, message: str,
                        output: str = "", filename: str = "build_failure.json", **details) -> Path:
    """Persist generated-script failures so the fast workflow can request a repair."""
    path = run_dir / filename
    payload = {
        "schema": 1,
        "failure_type": failure_type,
        "message": message,
        "output": output[-50000:],
        **details,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  [诊断] 已写入 {path}")
    return path


def make_run_dir(name: str | None) -> Path:
    """
    为本次运行创建独立输出目录：output/<名字>/。

    默认名字是时间戳（如 2026-09-22_162530），因此每次运行互不覆盖，
    output/ 下会累积历次结果。可用 --run-name 指定固定名字，
    此时若同名目录已存在会先清空，便于反复调试同一次任务。
    """
    OUT.mkdir(parents=True, exist_ok=True)

    if name:
        # 只取最后一段，防止 --run-name 里带路径穿越
        safe = Path(name).name.strip() or "run"
        run_dir = OUT / safe
        if run_dir.exists():
            print(f"  [输出] 目录已存在，先清空: {run_dir}")
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True)
        return run_dir

    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    run_dir = OUT / stamp
    n = 2
    while run_dir.exists():          # 同一秒内连跑两次也不冲突
        run_dir = OUT / f"{stamp}-{n}"
        n += 1
    run_dir.mkdir(parents=True)
    return run_dir


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def main() -> int:
    strict = "--strict" in sys.argv
    allow_validation_failure = "--allow-validation-failure" in sys.argv
    run_name = None
    task_packages_dir = None
    if "--run-name" in sys.argv:
        i = sys.argv.index("--run-name")
        if i + 1 >= len(sys.argv):
            return fail("--run-name 后面要跟一个目录名")
        run_name = sys.argv[i + 1]

    # Web 后端可为每个任务指定独立的脚本目录与输出根目录
    global GEN, OUT
    if "--gen-dir" in sys.argv:
        i = sys.argv.index("--gen-dir")
        if i + 1 >= len(sys.argv):
            return fail("--gen-dir 后面要跟一个目录路径")
        GEN = Path(sys.argv[i + 1]).expanduser().resolve()
    if "--out-dir" in sys.argv:
        i = sys.argv.index("--out-dir")
        if i + 1 >= len(sys.argv):
            return fail("--out-dir 后面要跟一个目录路径")
        OUT = Path(sys.argv[i + 1]).expanduser().resolve()
    if "--task-packages-dir" in sys.argv:
        i = sys.argv.index("--task-packages-dir")
        if i + 1 >= len(sys.argv):
            return fail("--task-packages-dir 后面要跟一个目录路径")
        task_packages_dir = Path(sys.argv[i + 1]).expanduser().resolve()

    xlsx_script = GEN / "make_xlsx.py"
    blend_script = GEN / "make_blend.py"
    if not xlsx_script.exists() or not blend_script.exists():
        return fail("缺少 generated/ 下的脚本，请先运行：python pipeline/ask_model.py")

    blender = find_blender()
    if not blender:
        return fail("未找到 blender.exe，无法生成 .blend")

    # 本次运行的独立输出目录：output/<时间戳>/，历次结果互不覆盖
    run_dir = make_run_dir(run_name)
    print(f"本次输出目录: {run_dir}")

    child_env = os.environ.copy()
    child_env["OUT_DIR"] = str(run_dir)
    child_env["PYTHONIOENCODING"] = "utf-8"
    if task_packages_dir:
        python_packages = task_packages_dir / "python"
        blender_packages = task_packages_dir / "blender"
        child_env["PYTHONPATH"] = str(python_packages) + (
            os.pathsep + child_env["PYTHONPATH"] if child_env.get("PYTHONPATH") else ""
        )
        child_env["BLENDER_TASK_PACKAGES"] = str(blender_packages)

    # ---- 步骤 1: 先生成 blend ----
    # Blender 可同时在 OUT_DIR 写出清单/统计数据；随后 Excel 可以读取这些真实模型数据。
    # 所有 Blender 脚本必须经过统一启动器，以注入项目级和任务级第三方包。
    print("\n" + "=" * 60)
    print(f"步骤 1/2  生成 {BLEND_NAME} (Blender 无界面模式)")
    print("=" * 60)
    print(f"Blender: {blender}")

    # 执行生成的脚本：原样运行，不做任何本地改写。
    # 跨版本 API 兼容写法由投料口提示词约束（见 input/money.md 与 ask_model.py 的契约）。
    # --python-exit-code 1：Blender 默认不会把 --python 脚本里的异常反映到退出码，
    # 不加这个参数的话脚本崩了也返回 0，出错会被掩盖。
    blender_launcher = HERE / "run_blender_script.py"
    ok, out = run(
        [blender, "--background", "--factory-startup",
         "--python-exit-code", "1", "--python", str(blender_launcher),
         "--", str(blend_script)],
        "blend", env=child_env,
    )
    if not ok or "Traceback (most recent call last)" in out:
        write_build_failure(
            run_dir, "blender_runtime_error",
            "make_blend.py 执行期间异常退出",
            out, script=str(blend_script), blender=blender,
        )
        return fail("make_blend.py 执行失败（详见上方 Blender 输出的 Traceback）"
                    " —— 请对照报错行号检查生成的脚本")

    blend = run_dir / BLEND_NAME
    if not blend.exists():
        write_build_failure(
            run_dir, "blender_missing_output",
            f"make_blend.py 结束后没有生成 {BLEND_NAME}",
            out, script=str(blend_script), expected=str(blend),
        )
        return fail(f"make_blend.py 没有报错，但未产出 {BLEND_NAME}"
                    " —— 请检查脚本是否真的调用了保存逻辑")

    print(f"  -> {blend}  ({blend.stat().st_size} 字节)")

    # ---- 固定验证器：从已保存的真实 .blend 读取几何和空间关系 ----
    validation_report = run_dir / "validation_report.json"
    child_env["BLEND_IN"] = str(blend)
    child_env["VALIDATION_REPORT"] = str(validation_report)
    validator = HERE / "validate_blend.py"
    ok, out = run(
        [blender, "--background", "--factory-startup", str(blend),
         "--python-exit-code", "1", "--python", str(validator)],
        "validate-blend", env=child_env,
    )
    if not ok or "Traceback (most recent call last)" in out:
        if allow_validation_failure and validation_report.is_file():
            print("\n[警告] Blender 自动修复已达到 2 次上限；保留 validation_report.json，"
                  "不再修改 make_blend.py，继续生成 Excel。")
        else:
            return fail("Blender 几何/空间关系验证失败；已生成 validation_report.json")

    # ---- 步骤 2: 再生成 xlsx ----
    print("\n" + "=" * 60)
    print(f"步骤 2/2  生成 {XLSX_NAME}")
    print("=" * 60)
    ok, xlsx_out = run([sys.executable, str(xlsx_script)], "xlsx", env=child_env)
    if not ok:
        write_build_failure(
            run_dir, "xlsx_runtime_error",
            "make_xlsx.py 执行期间异常退出",
            xlsx_out, filename="xlsx_failure.json", script=str(xlsx_script),
            model_manifest=str(run_dir / "model_manifest.json"),
        )
        return fail("make_xlsx.py 执行失败")
    xlsx = run_dir / XLSX_NAME
    if not xlsx.exists():
        write_build_failure(
            run_dir, "xlsx_missing_output",
            f"make_xlsx.py 结束后没有生成 {XLSX_NAME}",
            xlsx_out, filename="xlsx_failure.json", script=str(xlsx_script), expected=str(xlsx),
            model_manifest=str(run_dir / "model_manifest.json"),
        )
        return fail(f"make_xlsx.py 没报错，但未产出 {XLSX_NAME}")
    print(f"  -> {xlsx}  ({xlsx.stat().st_size} 字节)")

    # ---- 汇总 ----
    print("\n" + "=" * 60)
    print(f"最终交付（{run_dir}）")
    print("=" * 60)
    final = sorted(p for p in run_dir.iterdir())
    for p in final:
        print(f"  ✔ {p.name}  ({p.stat().st_size} 字节)")

    allowed = {XLSX_NAME, BLEND_NAME}
    missing = [n for n in sorted(allowed) if not (run_dir / n).exists()]
    if missing:
        return fail(f"缺少交付物: {missing}")
    extras = [p.name for p in final if p.name not in allowed]
    if extras:
        # 任务原文（如 v7.26）常要求模型额外渲染成品材料标注图等预览件，
        # 这属于按规范产出的附属物，默认只告警不判失败；--strict 可恢复严格模式。
        if strict:
            return fail(f"存在多余文件: {extras}（应只有 {sorted(allowed)}）")
        print(f"\n  [提示] 除两个交付物外还有 {len(extras)} 个附属文件（按任务原文产出，予以保留）:")
        for n in extras:
            print(f"        · {n}")
        print("        如需严格只允许两个文件，请加 --strict")

    try:
        shown = run_dir.relative_to(ROOT)
    except ValueError:
        shown = run_dir
    if extras:
        print(f"\n完成：交付物已就位 —— {XLSX_NAME} + {BLEND_NAME}"
              f"（另有 {len(extras)} 个附属文件）。")
    else:
        print(f"\n完成：恰好两个文件 —— {XLSX_NAME} + {BLEND_NAME}。")
    print(f"位置：{shown}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
