#!/usr/bin/env python3
"""Fast path: one model call, installed libraries only, then build both files."""
from __future__ import annotations
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent

def run(cmd):
    print("$ " + " ".join(f'\"{part}\"' if " " in part else part for part in cmd), flush=True)
    return subprocess.run(cmd, cwd=ROOT).returncode

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--gen-dir", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--task-packages", required=True)
    args = parser.parse_args()
    gen_dir = Path(args.gen_dir).resolve()
    out_root = Path(args.out_dir).resolve()
    task_packages = Path(args.task_packages).resolve()
    gen_dir.mkdir(parents=True, exist_ok=True)
    model_spec = gen_dir / "model_spec.json"
    if run([sys.executable, str(HERE / "extract_model_spec.py"),
            "--input-dir", args.input_dir, "--gen-dir", str(gen_dir)]):
        return 1
    if run([sys.executable, str(HERE / "validate_model_spec.py"),
            "--spec", str(model_spec), "--lock"]):
        return 1
    if run([sys.executable, str(HERE / "ask_model.py"), "--input-dir", args.input_dir,
            "--gen-dir", str(gen_dir), "--model-spec", str(model_spec)]):
        return 1
    dependencies = gen_dir / "dependencies.json"
    if not dependencies.is_file():
        dependencies.write_text(json.dumps({"python": [], "blender": []}), encoding="utf-8")
    json.loads(dependencies.read_text(encoding="utf-8"))
    if run([sys.executable, str(HERE / "dependency_manager.py"), "--ensure-task",
            "--dependencies", str(dependencies), "--task-packages", str(task_packages)]):
        return 1
    build_cmd = [sys.executable, str(HERE / "build.py"), "--gen-dir", str(gen_dir),
                 "--out-dir", str(out_root), "--task-packages-dir", str(task_packages)]
    blend_repairs = 0
    xlsx_repairs = 0
    while True:
        if run([sys.executable, str(HERE / "validate_model_spec.py"),
                "--spec", str(model_spec)]):
            print("[错误] model_spec.json 锁校验失败，拒绝继续生成或修复", file=sys.stderr)
            return 1
        current_build_cmd = list(build_cmd)
        if blend_repairs >= 2:
            current_build_cmd.append("--allow-validation-failure")
        if run(current_build_cmd) == 0:
            break
        runs = sorted((path for path in out_root.iterdir() if path.is_dir()),
                      key=lambda path: path.stat().st_mtime, reverse=True)
        report = None
        if runs:
            validation = runs[0] / "validation_report.json"
            runtime_failure = runs[0] / "build_failure.json"
            xlsx_failure = runs[0] / "xlsx_failure.json"
            report = (runtime_failure if runtime_failure.is_file() else
                      xlsx_failure if xlsx_failure.is_file() else
                      validation if validation.is_file() else None)
        if report is None:
            print("[错误] 构建失败，且没有可继续处理的验证修复机会", file=sys.stderr)
            return 1
        if report.name == "xlsx_failure.json":
            if xlsx_repairs >= 2:
                print("[错误] Excel自动修复已达到2次上限", file=sys.stderr)
                return 1
            xlsx_repairs += 1
            numbered_report = gen_dir / f"xlsx-failure-attempt-{xlsx_repairs}.json"
            shutil.copy2(report, numbered_report)
            print(f"Excel运行错误，开始自动修复 {xlsx_repairs}/2", flush=True)
            if run([sys.executable, str(HERE / "repair_xlsx.py"),
                    "--input-dir", args.input_dir, "--gen-dir", str(gen_dir),
                    "--report", str(numbered_report), "--attempt", str(xlsx_repairs)]):
                return 1
            continue

        if blend_repairs >= 2:
            print("[错误] Blender自动修复已达到2次上限", file=sys.stderr)
            return 1
        blend_repairs += 1
        report_kind = "validation" if report.name == "validation_report.json" else "runtime-failure"
        numbered_report = gen_dir / f"{report_kind}-attempt-{blend_repairs}.json"
        shutil.copy2(report, numbered_report)
        reason = "空间/结构验证失败" if report_kind == "validation" else "Blender运行错误"
        print(f"{reason}，开始自动修复 {blend_repairs}/2", flush=True)
        if run([sys.executable, str(HERE / "repair_blend.py"),
                "--input-dir", args.input_dir, "--gen-dir", str(gen_dir),
                "--report", str(numbered_report), "--attempt", str(blend_repairs)]):
            return 1
    runs = sorted((path for path in out_root.iterdir() if path.is_dir()),
                  key=lambda path: path.stat().st_mtime, reverse=True)
    if not runs:
        print("[错误] build.py 未生成输出目录", file=sys.stderr)
        return 1
    (out_root / "LATEST_RUN.txt").write_text(str(runs[0]), encoding="utf-8")
    print(f"FAST_WORKFLOW_OK {runs[0]}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
