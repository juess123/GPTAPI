#!/usr/bin/env python3
"""Validate a generated workbook against the locked task specification."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from openpyxl import load_workbook


LABEL_ONLY_KEYWORD_HINTS = (
    "项目标题", "项目名称", "关闭尺寸", "w×h×d", "编号", "日期",
)


def is_label_only_requirement(requirement: dict, keywords: list[str]) -> bool:
    """Fixed headings are advisory; missing business sections remain fatal."""
    if str(requirement.get("kind", "")).casefold() in {"label", "heading", "wording"}:
        return True
    return bool(keywords) and all(
        any(hint.casefold() in keyword.casefold() for hint in LABEL_ONLY_KEYWORD_HINTS)
        for keyword in keywords
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--xlsx", required=True)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    xlsx = Path(args.xlsx).resolve()
    spec_path = Path(args.spec).resolve()
    report_path = Path(args.report).resolve()
    errors: list[dict] = []
    warnings: list[dict] = []
    info: list[dict] = []

    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        wb = load_workbook(xlsx, read_only=True, data_only=False)
        visible = [ws for ws in wb.worksheets if ws.sheet_state == "visible"]
        if not visible:
            errors.append({"code": "NO_VISIBLE_WORKSHEET"})
        text_parts: list[str] = []
        nonempty = 0
        formulas = 0
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for cell in row:
                    value = cell.value
                    if value is None:
                        continue
                    nonempty += 1
                    text_parts.append(str(value))
                    if isinstance(value, str) and value.startswith("="):
                        formulas += 1
        wb.close()
        if nonempty == 0:
            errors.append({"code": "EMPTY_WORKBOOK"})
        searchable = "\n".join(text_parts).casefold()
        for requirement in spec.get("quote_requirements", []):
            if not isinstance(requirement, dict) or not requirement.get("required"):
                continue
            if requirement.get("status") not in {"confirmed", "derived"}:
                continue
            keywords = [str(value).strip() for value in requirement.get("keywords", []) if str(value).strip()]
            if not keywords:
                warnings.append({"code": "QUOTE_REQUIREMENT_WITHOUT_KEYWORDS",
                                 "requirement_id": requirement.get("id")})
                continue
            if not any(keyword.casefold() in searchable for keyword in keywords):
                record = {"code": "MISSING_QUOTE_LABEL_OR_KEYWORD",
                          "requirement_id": requirement.get("id"), "keywords": keywords,
                          "non_blocking": True,
                          "non_blocking_reason": "fixed_wording_does_not_justify_regeneration"}
                enforcement = str(requirement.get("enforcement", "")).casefold()
                if (requirement.get("fatal") is not True
                        and enforcement not in {"fatal", "hard_content"}
                        and is_label_only_requirement(requirement, keywords)):
                    warnings.append(record)
                else:
                    record["non_blocking"] = False
                    record.pop("non_blocking_reason", None)
                    errors.append(record)
        info.append({"code": "WORKBOOK_STATS", "sheets": wb.sheetnames,
                     "nonempty_cells": nonempty, "formula_cells": formulas})
    except Exception as exc:
        errors.append({"code": "XLSX_VALIDATOR_ERROR", "exception_type": type(exc).__name__,
                       "message": str(exc)})

    report = {"schema": 1, "status": "failed" if errors else "passed",
              "xlsx": str(xlsx), "model_spec": str(spec_path),
              "summary": {"errors": len(errors), "warnings": len(warnings), "info": len(info)},
              "errors": errors, "warnings": warnings, "info": info}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"XLSX_VALIDATION_REPORT={report_path}")
    print(f"XLSX_VALIDATION_STATUS={report['status']} errors={len(errors)} warnings={len(warnings)}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
