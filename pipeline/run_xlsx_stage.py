#!/usr/bin/env python3
"""Rebuild and validate only Excel inside an existing successful Blender run."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from build import XLSX_NAME, run, write_build_failure


HERE = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gen-dir", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--task-packages-dir")
    args = parser.parse_args()

    gen_dir = Path(args.gen_dir).resolve()
    run_dir = Path(args.run_dir).resolve()
    task_packages_dir = (Path(args.task_packages_dir).resolve()
                         if args.task_packages_dir else None)
    xlsx_script = gen_dir / "make_xlsx.py"
    model_spec = gen_dir / "model_spec.json"
    blend = run_dir / "final_model.blend"
    if not run_dir.is_dir() or not xlsx_script.is_file():
        print("[错误] Excel单独重试缺少输出目录或make_xlsx.py", file=sys.stderr)
        return 1
    if not blend.is_file():
        print("[错误] Excel单独重试找不到可复用的final_model.blend", file=sys.stderr)
        return 1

    child_env = os.environ.copy()
    child_env["OUT_DIR"] = str(run_dir)
    child_env["PYTHONIOENCODING"] = "utf-8"
    if model_spec.is_file():
        child_env["MODEL_SPEC_PATH"] = str(model_spec)
    if task_packages_dir:
        python_packages = task_packages_dir / "python"
        child_env["PYTHONPATH"] = str(python_packages) + (
            os.pathsep + child_env["PYTHONPATH"] if child_env.get("PYTHONPATH") else ""
        )

    print(f"复用Blender输出目录: {run_dir}")
    print("本轮只重新生成并验证Excel，不启动Blender。")
    ok, xlsx_out = run([sys.executable, str(xlsx_script)], "xlsx-only", env=child_env)
    failure_path = run_dir / "xlsx_failure.json"
    if not ok:
        write_build_failure(
            run_dir, "xlsx_runtime_error", "make_xlsx.py 执行期间异常退出",
            xlsx_out, filename=failure_path.name, script=str(xlsx_script),
            model_manifest=str(run_dir / "model_manifest.json"),
        )
        return 1

    xlsx = run_dir / XLSX_NAME
    if not xlsx.is_file():
        write_build_failure(
            run_dir, "xlsx_missing_output", f"make_xlsx.py 没有生成 {XLSX_NAME}",
            xlsx_out, filename=failure_path.name, script=str(xlsx_script), expected=str(xlsx),
            model_manifest=str(run_dir / "model_manifest.json"),
        )
        return 1

    if model_spec.is_file():
        validation_report = run_dir / "xlsx_validation_report.json"
        ok, validation_out = run(
            [sys.executable, str(HERE / "validate_xlsx.py"),
             "--xlsx", str(xlsx), "--spec", str(model_spec),
             "--report", str(validation_report)],
            "validate-xlsx-only", env=child_env,
        )
        if not ok:
            write_build_failure(
                run_dir, "xlsx_validation_error",
                "Excel与锁定的 model_spec.json 不一致",
                validation_out, filename=failure_path.name, script=str(xlsx_script),
                model_spec=str(model_spec), validation_report=str(validation_report),
                model_manifest=str(run_dir / "model_manifest.json"),
            )
            return 1

    if failure_path.is_file():
        failure_path.unlink()
    print(f"XLSX_STAGE_OK {xlsx} ({xlsx.stat().st_size} 字节)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
