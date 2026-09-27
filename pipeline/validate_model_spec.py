#!/usr/bin/env python3
"""Validate and lock model_spec.json without using AI."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


STATUSES = {"confirmed", "derived", "assumption", "conflict"}
CLASSIFICATIONS = {"B", "X", "N"}
RULE_CATEGORIES = {
    "dimension", "count", "position", "spacing", "elevation", "thickness", "opening",
    "component", "shape", "material", "color", "function", "animation", "forbidden", "other",
}
VISUAL_CATEGORIES = {
    "silhouette", "layout", "proportion", "component", "opening", "shape", "material", "color",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def validate_legacy_sources(spec: dict, schema: int, errors: list[dict]) -> None:
    """Keep schema 1/2 tasks readable while new tasks use compact schema 3."""
    requirements = spec.get("source_requirements", [])
    if schema == 2 and not isinstance(requirements, list):
        errors.append({"code": "INVALID_SOURCE_REQUIREMENTS"})
        return
    ids: set[str] = set()
    for index, requirement in enumerate(requirements if isinstance(requirements, list) else []):
        if not isinstance(requirement, dict):
            errors.append({"code": "INVALID_SOURCE_REQUIREMENT", "index": index})
            continue
        requirement_id = str(requirement.get("id", "")).strip()
        if not requirement_id or requirement_id in ids:
            errors.append({"code": "INVALID_REQUIREMENT_ID", "index": index, "id": requirement_id})
        ids.add(requirement_id)
        if requirement.get("status") not in STATUSES:
            errors.append({"code": "INVALID_REQUIREMENT_STATUS", "id": requirement_id})


def validate_compact_sources(path: Path, spec: dict, errors: list[dict], warnings: list[dict]) -> None:
    inventory_path = path.with_name("source_inventory.json")
    inventory = []
    if not inventory_path.is_file():
        errors.append({"code": "MISSING_SOURCE_INVENTORY", "path": str(inventory_path)})
    else:
        try:
            inventory = load_json(inventory_path)
        except Exception as exc:
            errors.append({"code": "INVALID_SOURCE_INVENTORY", "message": str(exc)})
    if not isinstance(inventory, list):
        errors.append({"code": "INVALID_SOURCE_INVENTORY", "message": "source_inventory必须是数组"})
        inventory = []
    expected_ids = {str(item.get("id")) for item in inventory
                    if isinstance(item, dict) and item.get("id")}
    declared_count = spec.get("source_inventory_count")
    if declared_count != len(inventory):
        errors.append({"code": "SOURCE_INVENTORY_COUNT_MISMATCH",
                       "expected": declared_count, "actual": len(inventory)})
    if inventory_path.is_file() and isinstance(inventory, list):
        # Hash the canonical JSON content instead of the platform-specific file
        # bytes.  Windows may translate LF to CRLF while writing text files,
        # which must not make an otherwise identical inventory fail validation.
        canonical_inventory = json.dumps(inventory, ensure_ascii=False, indent=2)
        actual_hash = hashlib.sha256(canonical_inventory.encode("utf-8")).hexdigest()
        if spec.get("source_inventory_sha256") != actual_hash:
            errors.append({"code": "SOURCE_INVENTORY_HASH_MISMATCH",
                           "expected": spec.get("source_inventory_sha256"), "actual": actual_hash})

    classified: dict[str, str] = {}
    rows = spec.get("source_classification")
    if not isinstance(rows, list):
        errors.append({"code": "INVALID_SOURCE_CLASSIFICATION"})
        rows = []
    for index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != 2:
            errors.append({"code": "INVALID_CLASSIFICATION_ROW", "index": index, "row": row})
            continue
        source_id, category = map(str, row)
        if source_id in classified:
            errors.append({"code": "DUPLICATE_SOURCE_CLASSIFICATION", "source_id": source_id})
        if source_id not in expected_ids:
            errors.append({"code": "UNKNOWN_CLASSIFIED_SOURCE", "source_id": source_id})
        if category not in CLASSIFICATIONS:
            errors.append({"code": "INVALID_SOURCE_CATEGORY", "source_id": source_id,
                           "category": category})
        classified[source_id] = category
    missing = expected_ids - set(classified)
    if missing:
        errors.append({"code": "UNCLASSIFIED_SOURCE_ITEMS", "source_ids": sorted(missing)})

    requirements = spec.get("source_requirements")
    if not isinstance(requirements, list):
        errors.append({"code": "INVALID_SOURCE_REQUIREMENTS"})
        requirements = []
    if len(requirements) > 80:
        errors.append({"code": "TOO_MANY_SOURCE_REQUIREMENTS", "count": len(requirements), "limit": 80})
    ids: set[str] = set()
    covered: set[str] = set()
    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, dict):
            errors.append({"code": "INVALID_SOURCE_REQUIREMENT", "index": index})
            continue
        requirement_id = str(requirement.get("id", "")).strip()
        if not requirement_id or requirement_id in ids:
            errors.append({"code": "INVALID_REQUIREMENT_ID", "index": index, "id": requirement_id})
        ids.add(requirement_id)
        source_ids = requirement.get("src")
        if not isinstance(source_ids, list) or not source_ids:
            errors.append({"code": "MISSING_REQUIREMENT_SOURCES", "id": requirement_id})
            source_ids = []
        for source_id in map(str, source_ids):
            if classified.get(source_id) != "B":
                errors.append({"code": "REQUIREMENT_REFERENCES_NON_BLENDER_SOURCE",
                               "id": requirement_id, "source_id": source_id})
            covered.add(source_id)
        if not str(requirement.get("target", "")).strip():
            errors.append({"code": "MISSING_REQUIREMENT_TARGET", "id": requirement_id})
        if requirement.get("category") not in RULE_CATEGORIES:
            errors.append({"code": "INVALID_REQUIREMENT_CATEGORY", "id": requirement_id})
        check = str(requirement.get("check", "")).strip()
        if not check:
            errors.append({"code": "MISSING_REQUIREMENT_CHECK", "id": requirement_id})
        elif len(check) > 160:
            warnings.append({"code": "LONG_REQUIREMENT_CHECK", "id": requirement_id, "length": len(check)})
        if requirement.get("verify") not in {"visual", "geometry", "metadata"}:
            errors.append({"code": "INVALID_REQUIREMENT_VERIFICATION", "id": requirement_id})
        if requirement.get("level") not in {"hard", "advisory"}:
            errors.append({"code": "INVALID_REQUIREMENT_LEVEL", "id": requirement_id})
        if requirement.get("status") not in STATUSES:
            errors.append({"code": "INVALID_REQUIREMENT_STATUS", "id": requirement_id})
    missing_blender = {source_id for source_id, category in classified.items()
                       if category == "B" and source_id not in covered}
    if missing_blender:
        errors.append({"code": "BLENDER_SOURCE_WITHOUT_RULE", "source_ids": sorted(missing_blender)})


def validate_visual(spec: dict, schema: int, errors: list[dict], warnings: list[dict]) -> None:
    criteria = spec.get("visual_criteria", [])
    if not isinstance(criteria, list):
        errors.append({"code": "INVALID_VISUAL_CRITERIA"})
        return
    if schema == 3 and len(criteria) > 30:
        errors.append({"code": "TOO_MANY_VISUAL_CRITERIA", "count": len(criteria), "limit": 30})
    ids: set[str] = set()
    for index, criterion in enumerate(criteria):
        if not isinstance(criterion, dict):
            errors.append({"code": "INVALID_VISUAL_CRITERION", "index": index})
            continue
        criterion_id = str(criterion.get("id", "")).strip()
        if not criterion_id or criterion_id in ids:
            errors.append({"code": "INVALID_VISUAL_ID", "index": index, "id": criterion_id})
        ids.add(criterion_id)
        if schema == 3:
            if criterion.get("category") not in VISUAL_CATEGORIES:
                errors.append({"code": "INVALID_VISUAL_CATEGORY", "id": criterion_id})
            if not str(criterion.get("check", "")).strip():
                errors.append({"code": "MISSING_VISUAL_CHECK", "id": criterion_id})
            if criterion.get("level") not in {"hard", "advisory"}:
                errors.append({"code": "INVALID_VISUAL_LEVEL", "id": criterion_id})
        else:
            if not str(criterion.get("description", "")).strip():
                errors.append({"code": "MISSING_VISUAL_DESCRIPTION", "id": criterion_id})
            if criterion.get("importance") not in {"critical", "major", "minor"}:
                errors.append({"code": "INVALID_VISUAL_IMPORTANCE", "id": criterion_id})
        if criterion.get("status") not in STATUSES:
            errors.append({"code": "INVALID_VISUAL_STATUS", "id": criterion_id})


def validate_geometry_contract(spec: dict, errors: list[dict], warnings: list[dict]) -> None:
    overall = spec.get("overall", {})
    if not isinstance(overall, dict):
        errors.append({"code": "INVALID_OVERALL"})
        overall = {}
    for key in ("width_mm", "depth_mm", "height_mm"):
        item = overall.get(key)
        if item is None:
            warnings.append({"code": "MISSING_OVERALL", "field": key})
            continue
        if not isinstance(item, dict) or item.get("status") not in STATUSES:
            errors.append({"code": "INVALID_FACT", "field": f"overall.{key}"})
        elif item.get("value") is not None and (isinstance(item.get("value"), bool)
                                                  or not isinstance(item.get("value"), (int, float))):
            errors.append({"code": "INVALID_NUMBER", "field": f"overall.{key}"})
    scope = overall.get("scope", {}) if isinstance(overall, dict) else {}
    if scope and not isinstance(scope, dict):
        errors.append({"code": "INVALID_OVERALL_SCOPE"})
    elif isinstance(scope, dict):
        ground = scope.get("ground_z_mm")
        if ground is not None and (isinstance(ground, bool) or not isinstance(ground, (int, float))):
            errors.append({"code": "INVALID_GROUND_Z", "value": ground})

    counts = spec.get("expected_counts", {})
    if not isinstance(counts, dict):
        errors.append({"code": "INVALID_EXPECTED_COUNTS"})
        counts = {}
    for name, item in counts.items():
        value = item.get("value") if isinstance(item, dict) else item
        status = item.get("status") if isinstance(item, dict) else "confirmed"
        if status not in STATUSES:
            errors.append({"code": "INVALID_STATUS", "component_type": name, "status": status})
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            errors.append({"code": "INVALID_COUNT", "component_type": name, "value": value})

    features = spec.get("required_features", [])
    if not isinstance(features, list):
        errors.append({"code": "INVALID_REQUIRED_FEATURES"})
        features = []
    for feature in features:
        if not isinstance(feature, dict) or not feature.get("id") or not isinstance(feature.get("required"), bool):
            errors.append({"code": "INVALID_REQUIRED_FEATURE", "feature": feature})
            continue
        component_type = feature.get("component_type")
        if feature.get("required") and feature.get("status") in {"confirmed", "derived"} and component_type:
            fact = counts.get(component_type)
            count = fact.get("value") if isinstance(fact, dict) else fact
            if count == 0:
                errors.append({"code": "REQUIRED_FEATURE_COUNT_ZERO", "feature_id": feature.get("id"),
                               "component_type": component_type})
            elif count is None:
                warnings.append({"code": "REQUIRED_FEATURE_COUNT_UNKNOWN", "feature_id": feature.get("id"),
                                 "component_type": component_type})
    if not isinstance(spec.get("spatial_rules", []), list):
        errors.append({"code": "INVALID_SPATIAL_RULES"})
    quote_requirements = spec.get("quote_requirements", [])
    if not isinstance(quote_requirements, list):
        errors.append({"code": "INVALID_QUOTE_REQUIREMENTS"})
    elif spec.get("schema") == 3 and len(quote_requirements) > 15:
        errors.append({"code": "TOO_MANY_QUOTE_REQUIREMENTS",
                       "count": len(quote_requirements), "limit": 15})


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
        spec = load_json(path)
    except Exception as exc:
        spec = {}
        errors.append({"code": "INVALID_JSON", "message": str(exc)})

    schema = spec.get("schema") if isinstance(spec, dict) else None
    if schema not in {1, 2, 3}:
        errors.append({"code": "INVALID_SCHEMA", "message": "model_spec.schema必须为1、2或3"})
    elif schema == 3:
        validate_compact_sources(path, spec, errors, warnings)
    else:
        validate_legacy_sources(spec, schema, errors)
    validate_visual(spec, schema, errors, warnings)
    validate_geometry_contract(spec, errors, warnings)
    if spec.get("conflicts"):
        warnings.append({"code": "SOURCE_CONFLICTS", "count": len(spec["conflicts"])})

    current_hash = digest(path) if path.is_file() else ""
    if not args.lock and lock_path.is_file():
        locked_hash = lock_path.read_text(encoding="ascii", errors="ignore").strip()
        if locked_hash != current_hash:
            errors.append({"code": "SPEC_LOCK_MISMATCH", "expected": locked_hash, "actual": current_hash})
    report = {"schema": 3, "status": "failed" if errors else "passed",
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
