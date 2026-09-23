"""Expose project-local Blender packages, then run a generated script."""
from __future__ import annotations
import runpy
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ROOT / ".blender_packages"
if str(PACKAGES) not in sys.path:
    sys.path.insert(0, str(PACKAGES))
task_packages = os.environ.get("BLENDER_TASK_PACKAGES", "").strip()
if task_packages and task_packages not in sys.path:
    sys.path.insert(0, task_packages)
try:
    separator = sys.argv.index("--")
    target = Path(sys.argv[separator + 1]).resolve()
except (ValueError, IndexError):
    raise SystemExit("缺少生成脚本路径：-- <make_blend.py>")
sys.argv = [str(target)]
runpy.run_path(str(target), run_name="__main__")
