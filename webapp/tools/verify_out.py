#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立校验 Web 任务产出的两个成品 —— 不复用生成脚本的任何结论。"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import openpyxl

xlsx = Path(sys.argv[1])
blend = Path(sys.argv[2])

print("=" * 62)
print("成品独立校验")
print("=" * 62)

# ── xlsx ────────────────────────────────────────────────────
print(f"\n[XLSX] {xlsx.name}  {xlsx.stat().st_size} 字节")
with xlsx.open("rb") as fh:
    head = fh.read(4)
print(f"  文件头 : {head!r}   {'是合法 ZIP/OOXML' if head[:2] == b'PK' else '❗ 不是 ZIP'}")

with zipfile.ZipFile(xlsx) as z:
    names = z.namelist()
print(f"  内部条目 : {len(names)} 个  {[n for n in names if n.startswith('xl/worksheets')]}")

wb = openpyxl.load_workbook(xlsx, data_only=False)
print(f"  工作表   : {wb.sheetnames}")

formula_total = 0
for ws in wb.worksheets:
    formulas = [c for row in ws.iter_rows() for c in row
                if isinstance(c.value, str) and c.value.startswith("=")]
    formula_total += len(formulas)
    print(f"    · {ws.title!r}  {ws.max_row} 行 × {ws.max_column} 列  "
          f"合并 {len(ws.merged_cells.ranges)} 处  公式 {len(formulas)} 个"
          f"  冻结 {ws.freeze_panes}")
print(f"  公式单元格总数 : {formula_total}")

# ── blend ───────────────────────────────────────────────────
print(f"\n[BLEND] {blend.name}  {blend.stat().st_size} 字节")
with blend.open("rb") as fh:
    raw = fh.read(16)
if raw[:2] == b"\x28\xb5":
    enc = "zstd 压缩（Blender 4.0+ 默认）"
elif raw[:7] == b"BLENDER":
    enc = "未压缩"
else:
    enc = "❗ 未知格式"
print(f"  文件头 : {raw[:8]!r}  → {enc}")
print("  真实格式需由 Blender 无界面重新打开来判定，见下一步。")
