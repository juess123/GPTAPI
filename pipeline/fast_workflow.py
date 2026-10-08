#!/usr/bin/env python3
"""Fast path: one model call, installed libraries only, then build both files."""
from __future__ import annotations
import argparse
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent

def run(cmd):
    print("$ " + " ".join(f'\"{part}\"' if " " in part else part for part in cmd), flush=True)
    return subprocess.run(cmd, cwd=ROOT).returncode


def newest_buildable_run(out_root: Path) -> Path | None:
    """Return the newest run whose Blender stage completed successfully."""
    candidates = [
        path for path in out_root.iterdir()
        if path.is_dir()
        and (path / "final_model.blend").is_file()
        and (path / "model_quantities.json").is_file()
    ]
    return max(candidates, key=lambda path: path.stat().st_mtime, default=None)


def is_usable_xlsx(path: Path) -> bool:
    """Reject missing, empty, or truncated Excel files before fallback delivery."""
    try:
        return path.is_file() and path.stat().st_size > 0 and zipfile.is_zipfile(path)
    except OSError:
        return False


def newest_usable_delivery(out_root: Path, preferred: Path | None = None) -> Path | None:
    """Return the newest directory containing both usable delivery files."""
    candidates = []
    if preferred is not None:
        candidates.append(preferred)
    candidates.extend(
        sorted(
            (path for path in out_root.iterdir() if path.is_dir() and path != preferred),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    )
    for path in candidates:
        blend = path / "final_model.blend"
        try:
            blend_ok = blend.is_file() and blend.stat().st_size > 0
        except OSError:
            blend_ok = False
        if blend_ok and is_usable_xlsx(path / "final_quote.xlsx"):
            return path
    return None


def write_degraded_delivery_marker(run_dir: Path, reason: str) -> None:
    (run_dir / "delivery_warning.json").write_text(
        json.dumps(
            {
                "status": "delivered_with_warnings",
                "reason": reason,
                "final_model": str(run_dir / "final_model.blend"),
                "final_quote": str(run_dir / "final_quote.xlsx"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

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
                 "--out-dir", str(out_root), "--task-packages-dir", str(task_packages),
                 "--input-dir", str(Path(args.input_dir).resolve())]
    blend_repairs = 0
    xlsx_repairs = 0
    xlsx_only_run: Path | None = None
    selected_run: Path | None = None
    while True:
        if run([sys.executable, str(HERE / "validate_model_spec.py"),
                "--spec", str(model_spec)]):
            print("[错误] model_spec.json 锁校验失败，拒绝继续生成或修复", file=sys.stderr)
            return 1
        if xlsx_only_run is not None:
            current_build_cmd = [
                sys.executable, str(HERE / "run_xlsx_stage.py"),
                "--gen-dir", str(gen_dir), "--run-dir", str(xlsx_only_run),
                "--task-packages-dir", str(task_packages),
            ]
        else:
            current_build_cmd = list(build_cmd)
            if blend_repairs:
                current_build_cmd.append("--skip-optional-renders")
            if blend_repairs >= 2:
                current_build_cmd.append("--allow-validation-failure")
        if run(current_build_cmd) == 0:
            break
        runs = sorted((path for path in out_root.iterdir() if path.is_dir()),
                      key=lambda path: path.stat().st_mtime, reverse=True)
        report = None
        report_run = xlsx_only_run if xlsx_only_run is not None else (runs[0] if runs else None)
        if report_run is not None:
            validation = report_run / "validation_report.json"
            runtime_failure = report_run / "build_failure.json"
            xlsx_failure = report_run / "xlsx_failure.json"
            report = (runtime_failure if runtime_failure.is_file() else
                      xlsx_failure if xlsx_failure.is_file() else
                      validation if validation.is_file() else None)
        if report is None:
            print("[错误] 构建失败，且没有可继续处理的验证修复机会", file=sys.stderr)
            return 1
        if report.name == "xlsx_failure.json":
            if xlsx_repairs >= 2:
                fallback_run = newest_usable_delivery(out_root, report_run)
                if fallback_run is None:
                    print("[错误] Excel自动修复已达到2次上限，且没有可用的Excel历史版本", file=sys.stderr)
                    return 1
                reason = "Excel自动修复达到2次上限；交付最近一次可正常打开的Excel与对应Blender"
                write_degraded_delivery_marker(fallback_run, reason)
                print(f"[警告] {reason}: {fallback_run}", flush=True)
                selected_run = fallback_run
                break
            xlsx_repairs += 1
            numbered_report = gen_dir / f"xlsx-failure-attempt-{xlsx_repairs}.json"
            shutil.copy2(report, numbered_report)
            print(f"Excel运行错误，开始自动修复 {xlsx_repairs}/2", flush=True)
            if run([sys.executable, str(HERE / "repair_xlsx.py"),
                    "--input-dir", args.input_dir, "--gen-dir", str(gen_dir),
                    "--report", str(numbered_report), "--attempt", str(xlsx_repairs)]):
                if xlsx_repairs >= 2:
                    fallback_run = newest_usable_delivery(out_root, report_run)
                    if fallback_run is not None:
                        reason = "第2次Excel修复请求失败；交付最近一次可正常打开的Excel与对应Blender"
                        write_degraded_delivery_marker(fallback_run, reason)
                        print(f"[警告] {reason}: {fallback_run}", flush=True)
                        selected_run = fallback_run
                        break
                return 1
            xlsx_only_run = report_run
            continue

        if blend_repairs >= 2:
            fallback_run = newest_buildable_run(out_root)
            if report.name == "build_failure.json" and fallback_run is not None:
                print(
                    "[警告] Blender自动修复已达到2次上限，且最后一次修复导致运行错误；"
                    f"回退到最近可运行版本：{fallback_run}",
                    flush=True,
                )
                print("[继续] 使用该版本的模型实体与数量数据生成Excel", flush=True)
                xlsx_only_run = fallback_run
                continue
            print("[错误] Blender自动修复已达到2次上限，且没有可回退的成功模型", file=sys.stderr)
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
            if blend_repairs >= 2:
                fallback_run = newest_buildable_run(out_root)
                if fallback_run is not None:
                    print(
                        "[警告] 第2次Blender修复请求失败；回退到最近一次可运行模型，继续生成Excel: "
                        f"{fallback_run}",
                        flush=True,
                    )
                    xlsx_only_run = fallback_run
                    continue
            return 1
        xlsx_only_run = None
    runs = ([selected_run] if selected_run is not None else sorted(
        (path for path in out_root.iterdir()
         if path.is_dir()
         and (path / "final_model.blend").is_file()
         and is_usable_xlsx(path / "final_quote.xlsx")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    ))
    if not runs:
        print("[错误] 流程结束但没有同时包含Blend与Excel的完整输出目录", file=sys.stderr)
        return 1
    (out_root / "LATEST_RUN.txt").write_text(str(runs[0]), encoding="utf-8")
    print(f"FAST_WORKFLOW_OK {runs[0]}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
