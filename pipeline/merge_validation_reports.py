#!/usr/bin/env python3
"""Merge independent structure and visual validation reports into one repair input."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from validation_policy import as_non_blocking, is_evidence_only_failure, is_non_blocking_structure_issue


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--structure", required=True)
    parser.add_argument("--visual")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    structure_path = Path(args.structure).resolve()
    visual_path = Path(args.visual).resolve() if args.visual else None
    report_path = Path(args.report).resolve()
    structure = json.loads(structure_path.read_text(encoding="utf-8"))
    visual = (json.loads(visual_path.read_text(encoding="utf-8"))
              if visual_path and visual_path.is_file() else None)

    errors = []
    warnings = [{"phase": "structure", **item} for item in structure.get("warnings", [])]
    for item in structure.get("errors", []):
        record = {"phase": "structure", **item}
        if is_non_blocking_structure_issue(item):
            warnings.append(as_non_blocking(record, "metadata_or_validator_bookkeeping"))
        else:
            errors.append(record)
    info = [{"phase": "structure", **item} for item in structure.get("info", [])]
    views = []
    if visual:
        for item in visual.get("errors", []):
            record = {"phase": "requirements_and_visual", **item}
            definition = item.get("requirement") or item.get("criterion") or item
            evaluation = item.get("evaluation") or item
            if is_evidence_only_failure(definition, evaluation):
                warnings.append(as_non_blocking(record, "insufficient_evidence_or_metadata"))
            else:
                errors.append(record)
        warnings.extend({"phase": "requirements_and_visual", **item} for item in visual.get("warnings", []))
        info.extend({"phase": "requirements_and_visual", **item} for item in visual.get("info", []))
        views = visual.get("validation_views", [])

    report = {
        "schema": 2,
        "status": "failed" if errors else "passed",
        "repair_policy": "deliverable_defects_only",
        "validation_order": [
            "source_requirements",
            "reference_visual_consistency",
            "obvious_structure",
            "dimensions_counts_excel_consistency",
            "lighting_camera_advisory_only",
        ],
        "summary": {"errors": len(errors), "warnings": len(warnings), "info": len(info)},
        "errors": errors,
        "warnings": warnings,
        "info": info,
        "validation_views": views,
        "reports": {
            "structure": str(structure_path),
            "visual": str(visual_path) if visual_path else None,
        },
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"VALIDATION_REPORT={report_path}")
    print(f"VALIDATION_STATUS={report['status']} errors={len(errors)} warnings={len(warnings)}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
