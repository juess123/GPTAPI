#!/usr/bin/env python3
"""Validate and lock model_spec.json without using AI."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


STATUSES = {"confirmed", "derived", "assumption", "conflict"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--lock", action="store_true")
    args = parser.parse_args()
    path = Path(args.spec).resolve()
    report_path = path.with_name("spec_validation_report.json")
    lock_path = path.with_name("model_spec.sha256")
    errors: list[dict] = []
    warnings: list[dict] = []

    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        spec = {}
        errors.append({"code": "INVALID_JSON", "message": str(exc)})

    if not isinstance(spec, dict) or spec.get("schema") != 1:
        errors.append({"code": "INVALID_SCHEMA", "message": "model_spec.schema必须为1"})
    overall = spec.get("overall", {}) if isinstance(spec, dict) else {}
    if not isinstance(overall, dict):
        errors.append({"code": "INVALID_OVERALL", "message": "overall必须是对象"})
        overall = {}
    for key in ("width_mm", "depth_mm", "height_mm"):
        item = overall.get(key)
        if item is None:
            warnings.append({"code": "MISSING_OVERALL", "field": key})
            continue
        if not isinstance(item, dict) or item.get("status") not in STATUSES:
            errors.append({"code": "INVALID_FACT", "field": f"overall.{key}"})
        elif item.get("value") is not None and not isinstance(item.get("value"), (int, float)):
            errors.append({"code": "INVALID_NUMBER", "field": f"overall.{key}"})

    counts = spec.get("expected_counts", {}) if isinstance(spec, dict) else {}
    if not isinstance(counts, dict):
        errors.append({"code": "INVALID_EXPECTED_COUNTS", "message": "expected_counts必须是对象"})
        counts = {}
    for name, item in counts.items():
        if isinstance(item, dict):
            value, status = item.get("value"), item.get("status")
        else:
            value, status = item, "confirmed"
        if status not in STATUSES:
            errors.append({"code": "INVALID_STATUS", "component_type": name, "status": status})
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            errors.append({"code": "INVALID_COUNT", "component_type": name, "value": value})

    for feature in spec.get("required_features", []):
        if not isinstance(feature, dict) or not feature.get("id") or not isinstance(feature.get("required"), bool):
            errors.append({"code": "INVALID_REQUIRED_FEATURE", "feature": feature})
            continue
        component_type = feature.get("component_type")
        if feature.get("required") and feature.get("status") in {"confirmed", "derived"} and component_type:
            count_fact = counts.get(component_type)
            count_value = count_fact.get("value") if isinstance(count_fact, dict) else count_fact
            if count_value == 0:
                errors.append({"code": "REQUIRED_FEATURE_COUNT_ZERO", "feature_id": feature.get("id"),
                               "component_type": component_type})
            elif count_value is None:
                warnings.append({"code": "REQUIRED_FEATURE_COUNT_UNKNOWN", "feature_id": feature.get("id"),
                                 "component_type": component_type})
    if spec.get("conflicts"):
        warnings.append({"code": "SOURCE_CONFLICTS", "count": len(spec["conflicts"])})

    current_hash = digest(path) if path.is_file() else ""
    if not args.lock and lock_path.is_file():
        locked_hash = lock_path.read_text(encoding="ascii", errors="ignore").strip()
        if locked_hash != current_hash:
            errors.append({"code": "SPEC_LOCK_MISMATCH", "expected": locked_hash, "actual": current_hash})
    report = {"schema": 1, "status": "failed" if errors else "passed",
              "spec": str(path), "sha256": current_hash,
              "errors": errors, "warnings": warnings}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if errors:
        print(f"MODEL_SPEC_STATUS=failed errors={len(errors)} report={report_path}")
        return 1
    if args.lock:
        lock_path.write_text(current_hash + "\n", encoding="ascii")
        print(f"MODEL_SPEC_LOCKED={lock_path} sha256={current_hash}")
    else:
        print(f"MODEL_SPEC_STATUS=passed sha256={current_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
