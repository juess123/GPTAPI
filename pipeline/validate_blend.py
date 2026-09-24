"""Open a generated .blend and validate declared geometry/spatial constraints."""
from __future__ import annotations

import json
import math
import os
from collections import Counter, defaultdict
from pathlib import Path

import bpy
from mathutils import Vector


BLEND_IN = Path(os.environ["BLEND_IN"]).resolve()
REPORT = Path(os.environ["VALIDATION_REPORT"]).resolve()
MODEL_SPEC = Path(os.environ["MODEL_SPEC_PATH"]).resolve() if os.environ.get("MODEL_SPEC_PATH") else None
EPS_M = 1e-6

errors: list[dict] = []
warnings: list[dict] = []
info: list[dict] = []


def issue(bucket: list[dict], code: str, message: str, **details) -> None:
    bucket.append({"code": code, "message": message, **details})


def finite(values) -> bool:
    return all(math.isfinite(float(value)) for value in values)


def object_bounds(obj) -> tuple[Vector, Vector] | None:
    if obj.type not in {"MESH", "CURVE", "FONT", "SURFACE", "META"} or not obj.bound_box:
        return None
    points = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    low = Vector(tuple(min(point[i] for point in points) for i in range(3)))
    high = Vector(tuple(max(point[i] for point in points) for i in range(3)))
    return low, high


def union_bounds(items) -> tuple[Vector, Vector] | None:
    bounds = [bound for obj in items if (bound := object_bounds(obj)) is not None]
    if not bounds:
        return None
    low = Vector(tuple(min(bound[0][i] for bound in bounds) for i in range(3)))
    high = Vector(tuple(max(bound[1][i] for bound in bounds) for i in range(3)))
    return low, high


def dims_mm(bound) -> list[float]:
    return [(bound[1][i] - bound[0][i]) * 1000.0 for i in range(3)]


def axis_index(value: str) -> int:
    return {"X": 0, "Y": 1, "Z": 2}[str(value).upper()]


def component_objects() -> dict[str, list]:
    result = defaultdict(list)
    for obj in bpy.context.scene.objects:
        component_id = str(obj.get("component_id", "")).strip()
        if component_id:
            result[component_id].append(obj)
    return result


def check_metadata() -> list:
    relevant = []
    instance_ids = []
    for obj in bpy.context.scene.objects:
        if obj.type not in {"MESH", "CURVE", "FONT"}:
            continue
        has_classification = "quote_relevant" in obj and "exclude_from_quote" in obj
        if not has_classification:
            issue(errors, "UNCLASSIFIED_OBJECT", f"对象 {obj.name} 没有工程/辅助分类", object=obj.name)
            continue
        quote_relevant = bool(obj.get("quote_relevant"))
        excluded = bool(obj.get("exclude_from_quote"))
        if quote_relevant and excluded:
            issue(errors, "CONFLICTING_CLASSIFICATION", f"对象 {obj.name} 同时参与报价又被排除", object=obj.name)
        if not quote_relevant or excluded:
            continue
        relevant.append(obj)
        component_id = str(obj.get("component_id", "")).strip()
        instance_id = str(obj.get("instance_id", "")).strip()
        component_type = str(obj.get("component_type", "")).strip()
        if not component_id or not instance_id or not component_type:
            issue(errors, "MISSING_COMPONENT_METADATA", f"工程对象 {obj.name} 缮信息",
                  object=obj.name, component_id=component_id, instance_id=instance_id,
                  component_type=component_type)
        if instance_id:
            instance_ids.append(instance_id)
        bound = object_bounds(obj)
        if bound is None:
            issue(errors, "NO_GEOMETRY_BOUNDS", f"工程对象 {obj.name} 没有有效包围盒", object=obj.name)
            continue
        dimensions = dims_mm(bound)
        if not finite((*bound[0], *bound[1], *dimensions)):
            issue(errors, "NON_FINITE_GEOMETRY", f"对象 {obj.name} 包含非有限坐标", object=obj.name)
        elif min(dimensions) <= EPS_M * 1000:
            issue(errors, "ZERO_SIZE_OBJECT", f"对象 {obj.name} 存在零尺寸轴",
                  object=obj.name, dimensions_mm=dimensions)
    for instance_id, count in Counter(instance_ids).items():
        if count > 1:
            issue(errors, "DUPLICATE_INSTANCE_ID", f"instance_id {instance_id} 重复 {count} 次",
                  instance_id=instance_id, count=count)
    if not relevant:
        issue(errors, "NO_QUOTE_GEOMETRY", "场景中没有登记任何工程对象")
    return relevant


def parse_spec() -> dict:
    if MODEL_SPEC is not None:
        if not MODEL_SPEC.is_file():
            issue(errors, "MISSING_EXTERNAL_MODEL_SPEC", "外部锁定的 model_spec.json 不存在",
                  path=str(MODEL_SPEC))
            return {}
        try:
            spec = json.loads(MODEL_SPEC.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            issue(errors, "INVALID_EXTERNAL_MODEL_SPEC", "外部 model_spec.json 无法读取",
                  path=str(MODEL_SPEC), error=str(exc))
            return {}
        if not isinstance(spec, dict):
            issue(errors, "INVALID_EXTERNAL_MODEL_SPEC", "外部 model_spec.json 顶层必须是对象")
            return {}
        info.append({"code": "SPEC_SOURCE", "source": "external_locked_file", "path": str(MODEL_SPEC)})
        return spec
    raw = bpy.context.scene.get("validation_spec_json")
    if not isinstance(raw, str) or not raw.strip():
        issue(errors, "MISSING_VALIDATION_SPEC", "场景缺少 validation_spec_json")
        return {}
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        issue(errors, "INVALID_VALIDATION_SPEC", f"validation_spec_json 不是有效JSON：{exc}")
        return {}
    if not isinstance(spec, dict):
        issue(errors, "INVALID_VALIDATION_SPEC", "validation_spec_json 顶层必须是对象")
        return {}
    return spec


def overall_objects(spec: dict, relevant: list) -> list:
    """Resolve the declared overall-dimension scope without affecting quote checks."""
    eligible = []
    for obj in relevant:
        if bool(obj.get("exclude_from_overall", False)):
            continue
        if bool(obj.get("exclude_from_overall_dimensions", False)):
            continue
        if "include_in_overall" in obj and not bool(obj.get("include_in_overall")):
            continue
        if "include_in_overall_dimensions" in obj and not bool(obj.get("include_in_overall_dimensions")):
            continue
        eligible.append(obj)

    overall = spec.get("overall", {})
    scope = overall.get("scope", {}) if isinstance(overall, dict) else {}
    if not isinstance(scope, dict):
        scope = {}

    names = (scope.get("objects") or scope.get("object_names")
             or overall.get("objects") or overall.get("object_names")
             or spec.get("overall_object_names"))
    component_ids = (scope.get("component_ids") or overall.get("component_ids")
                     or spec.get("overall_include_component_ids"))
    component_types = (scope.get("component_types") or overall.get("component_types")
                       or spec.get("overall_include_component_types"))

    if isinstance(names, (list, tuple, set)) and names:
        allowed = {str(value) for value in names}
        selected = [obj for obj in eligible if obj.name in allowed]
        selector = "object_names"
    elif isinstance(component_ids, (list, tuple, set)) and component_ids:
        allowed = {str(value) for value in component_ids}
        selected = [obj for obj in eligible if str(obj.get("component_id", "")) in allowed]
        selector = "component_ids"
    elif isinstance(component_types, (list, tuple, set)) and component_types:
        allowed = {str(value) for value in component_types}
        selected = [obj for obj in eligible if str(obj.get("component_type", "")) in allowed]
        selector = "component_types"
    else:
        selected = eligible
        selector = "object_flags"

    if not selected:
        issue(errors, "EMPTY_OVERALL_SCOPE", "总体尺寸范围中没有可计算包围盒的对象",
              selector=selector, eligible_count=len(eligible))
    info.append({"code": "OVERALL_SCOPE", "selector": selector,
                 "object_count": len(selected), "objects": [obj.name for obj in selected]})
    return selected


def check_overall(spec: dict, relevant: list) -> None:
    scoped = overall_objects(spec, relevant)
    bound = union_bounds(scoped)
    if bound is None:
        return
    actual = dict(zip(("width_mm", "depth_mm", "height_mm"), dims_mm(bound)))
    overall = spec.get("overall", {})
    scope = overall.get("scope", {}) if isinstance(overall, dict) else {}
    if not isinstance(scope, dict):
        scope = {}
    height_reference = str(
        scope.get("height_reference")
        or overall.get("height_reference")
        or spec.get("overall_height_reference", "")
    ).lower()
    if height_reference == "ground":
        ground_mm = float(scope.get("ground_z_mm", spec.get("overall_ground_z_mm",
                          spec.get("ground_z_mm", 0.0))))
        actual["height_mm"] = bound[1].z * 1000.0 - ground_mm
    info.append({"code": "ACTUAL_OVERALL", "actual": actual,
                 "height_reference": height_reference or "bounds"})
    for key, expected in overall.items():
        if key not in actual or not isinstance(expected, dict) or expected.get("value") is None:
            continue
        status = str(expected.get("status", "assumption"))
        wanted = float(expected["value"])
        tolerance = float(expected.get("tolerance_mm", 5.0))
        difference = actual[key] - wanted
        record = {"dimension": key, "expected_mm": wanted, "actual_mm": actual[key],
                  "difference_mm": difference, "tolerance_mm": tolerance, "status": status}
        if abs(difference) > tolerance:
            bucket = errors if status == "confirmed" else warnings
            issue(bucket, "OVERALL_DIMENSION_MISMATCH", f"总体尺寸 {key} 超出允许误差", **record)
    max_expected = max((float(value.get("value", 0)) for value in overall.values()
                        if isinstance(value, dict)), default=0)
    if max_expected:
        for obj in scoped:
            bound_obj = object_bounds(obj)
            if bound_obj and max(dims_mm(bound_obj)) > max_expected * 5:
                issue(errors, "ABNORMALLY_LARGE_OBJECT", f"对象 {obj.name} 尺寸异常巨大",
                      object=obj.name, dimensions_mm=dims_mm(bound_obj), reference_mm=max_expected)


def check_counts(spec: dict, relevant: list) -> None:
    actual = defaultdict(set)
    for obj in relevant:
        actual[str(obj.get("component_type", ""))].add(str(obj.get("component_id", "")))
    expected_counts = spec.get("expected_counts", {})
    if not isinstance(expected_counts, dict):
        issue(errors, "INVALID_EXPECTED_COUNTS", "expected_counts 必须是对象")
        return
    for component_type, expected in expected_counts.items():
        status = "confirmed"
        if isinstance(expected, dict):
            status = str(expected.get("status", "assumption"))
            expected_value = expected.get("value")
        else:
            expected_value = expected
        if expected_value is None or status == "conflict":
            issue(warnings, "COUNT_NOT_ENFORCED", f"{component_type} 数量未作为硬约束",
                  component_type=component_type, status=status, expected=expected)
            continue
        if isinstance(expected_value, bool) or not isinstance(expected_value, (int, float)):
            issue(errors, "INVALID_EXPECTED_COUNT", f"{component_type} 的期望数量格式无效",
                  component_type=component_type, received=expected)
            continue
        wanted = int(expected_value)
        count = len(actual.get(str(component_type), set()))
        if count != wanted:
            bucket = errors if status in {"confirmed", "derived"} else warnings
            issue(bucket, "COMPONENT_COUNT_MISMATCH", f"{component_type} 数量不一致",
                  component_type=component_type, expected=wanted, actual=count, status=status)


def check_required_features(spec: dict, relevant: list) -> None:
    actual_types = {str(obj.get("component_type", "")) for obj in relevant}
    for feature in spec.get("required_features", []):
        if not isinstance(feature, dict) or not feature.get("required"):
            continue
        if feature.get("status") not in {"confirmed", "derived"}:
            continue
        component_type = str(feature.get("component_type") or "").strip()
        if component_type and component_type not in actual_types:
            issue(errors, "MISSING_REQUIRED_FEATURE", "缺少锁定标准要求的必备构件",
                  feature_id=feature.get("id"), component_type=component_type)


def overlap_amount(a, b, axis: int) -> float:
    return min(a[1][axis], b[1][axis]) - max(a[0][axis], b[0][axis])


def check_spatial(spec: dict) -> None:
    components = component_objects()
    bounds = {key: union_bounds(value) for key, value in components.items()}
    ground = float(spec.get("ground_z_mm", 0.0))
    for rule in spec.get("spatial_rules", []):
        if not isinstance(rule, dict):
            continue
        kind = str(rule.get("kind", rule.get("type", rule.get("rule", ""))))
        subject_id = str(rule.get("subject", rule.get("a", "")))
        host_id = str(rule.get("host", rule.get("other",
                      rule.get("object", rule.get("target", rule.get("reference", rule.get("b", "")))))))
        subject = bounds.get(subject_id)
        host = bounds.get(host_id) if host_id else None
        tolerance = float(rule.get("tolerance_mm", 5.0)) / 1000.0
        if subject is None:
            issue(errors, "MISSING_RULE_SUBJECT", f"空间规则找不到 {subject_id}", rule=rule)
            continue
        if kind == "floor_contact":
            difference_mm = subject[0].z * 1000.0 - ground
            if abs(difference_mm) > tolerance * 1000.0:
                issue(errors, "FLOOR_CONTACT_FAILED", f"{subject_id} 未正确接触地面",
                      component_id=subject_id, difference_mm=difference_mm, rule=rule)
            continue
        if host is None:
            issue(errors, "MISSING_RULE_HOST", f"空间规则找不到 {host_id}", rule=rule)
            continue
        if kind == "inside_host":
            axes = rule.get("axes", ["X", "Z"])
            failed = []
            for axis_name in axes:
                axis = axis_index(axis_name)
                if subject[0][axis] < host[0][axis] - tolerance or subject[1][axis] > host[1][axis] + tolerance:
                    failed.append(axis_name)
            if failed:
                issue(errors, "OUTSIDE_HOST", f"{subject_id} 超出 {host_id} 边界",
                      component_id=subject_id, host_id=host_id, axes=failed, rule=rule)
        elif kind == "near_host":
            axis = axis_index(rule.get("axis", "Y"))
            gaps = [abs(subject[0][axis] - host[1][axis]), abs(subject[1][axis] - host[0][axis])]
            gap_mm = min(gaps) * 1000.0
            if overlap_amount(subject, host, axis) >= 0:
                gap_mm = 0.0
            if gap_mm > float(rule.get("max_gap_mm", 5.0)):
                issue(errors, "HOST_GAP_TOO_LARGE", f"{subject_id} 与 {host_id} 距离过大",
                      component_id=subject_id, host_id=host_id, gap_mm=gap_mm, rule=rule)
        elif kind in {"left_of", "right_of", "above", "below"}:
            axis = 2 if kind in {"above", "below"} else 0
            subject_center = (subject[0][axis] + subject[1][axis]) / 2
            host_center = (host[0][axis] + host[1][axis]) / 2
            valid = ((kind in {"left_of", "below"} and subject_center < host_center - tolerance)
                     or (kind in {"right_of", "above"} and subject_center > host_center + tolerance))
            if not valid:
                issue(errors, "RELATIVE_POSITION_FAILED", f"{subject_id} 未满足 {kind} {host_id}", rule=rule)
        elif kind == "no_overlap":
            overlaps = [overlap_amount(subject, host, axis) for axis in range(3)]
            if all(value > tolerance for value in overlaps):
                issue(errors, "FORBIDDEN_OVERLAP", f"{subject_id} 与 {host_id} 发生禁止穿插",
                      overlap_mm=[value * 1000.0 for value in overlaps], rule=rule)


try:
    spec = parse_spec()
    relevant_objects = check_metadata()
    check_overall(spec, relevant_objects)
    check_counts(spec, relevant_objects)
    check_required_features(spec, relevant_objects)
    check_spatial(spec)
except Exception as exc:
    issue(errors, "VALIDATOR_INTERNAL_ERROR", "验证器内部异常，已转换为报告而不是中断",
          exception_type=type(exc).__name__, error=str(exc))

report = {
    "schema": 1,
    "blend": str(BLEND_IN),
    "blender_version": bpy.app.version_string,
    "status": "failed" if errors else "passed",
    "summary": {"errors": len(errors), "warnings": len(warnings), "info": len(info)},
    "errors": errors,
    "warnings": warnings,
    "info": info,
}
REPORT.parent.mkdir(parents=True, exist_ok=True)
REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"VALIDATION_REPORT={REPORT}")
print(f"VALIDATION_STATUS={report['status']} errors={len(errors)} warnings={len(warnings)}")
if errors:
    raise RuntimeError(f"模型验证失败：{len(errors)} 个错误；详见 {REPORT}")
