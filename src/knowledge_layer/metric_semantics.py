"""A small evidence-reviewable metric language, never an approval mechanism.

Legacy five-field definitions retain their shape. Version two declares grouping,
cohort, time, additivity and field units. Expressions are a finite numeric AST;
there is no free SQL, function dispatch, currency conversion or metric reference.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from knowledge_layer.connectors.common import type_name
from knowledge_layer.store import KnowledgeValidationError

AGGREGATIONS = frozenset({"sum", "avg", "min", "max", "count", "count_distinct"})
BASE_KEYS = frozenset({"name", "description", "measure_field", "aggregation", "unit"})
MAX_METRIC_BYTES = 16 * 1024
MAX_EXPRESSION_NODES = 31
MAX_EXPRESSION_DEPTH = 5
NUMERIC = frozenset({"integer", "number"})


def _object(value: Any, required: set[str] | frozenset[str], optional=frozenset()) -> None:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise KnowledgeValidationError("Metric contains missing or unknown fields")


def _text(value: Any, label: str, maximum: int, minimum: int = 1) -> str:
    if (not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum
            or re.search(r"[\x00-\x1f\x7f]", value)):
        raise KnowledgeValidationError(f"Metric {label} must be bounded nonblank text")
    return value.strip()


def _legacy_text(value: Any, label: str, maximum: int) -> str:
    # Existing reviewed definitions allow paragraphs; their canonical text must
    # not change merely because the version-two validator is now in the path.
    if not isinstance(value, str) or not 4 <= len(value.strip()) <= maximum:
        raise KnowledgeValidationError(f"metric {label} must be 4-{maximum} characters")
    return value.strip()


def _unit(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or not 1 <= len(value) <= 80:
        raise KnowledgeValidationError("Metric unit must be 1-80 characters")
    return value


def field_map(asset_fields: Any) -> dict[str, dict[str, Any]]:
    """Preserve actual physical field names; duplicate/unknown metadata refuses."""
    if isinstance(asset_fields, dict):
        asset_fields = [dict(value, name=name) if isinstance(value, dict) else {"name": name}
                        for name, value in asset_fields.items()]
    if isinstance(asset_fields, (set, frozenset, tuple)):
        asset_fields = list(asset_fields)
    if not isinstance(asset_fields, list) or len(asset_fields) > 5000:
        raise KnowledgeValidationError("Metric needs bounded current field metadata")
    result = {}
    for raw in asset_fields:
        item = {"name": raw} if isinstance(raw, str) else raw
        if not isinstance(item, dict):
            raise KnowledgeValidationError("Metric needs current field metadata")
        name = item.get("name")
        if (not isinstance(name, str) or not name.strip() or len(name) > 128
                or re.search(r"[\x00-\x1f\x7f]", name) or name in result):
            raise KnowledgeValidationError("Metric source fields must be distinct exact names")
        result[name] = dict(item)
    return result


def field_type(field: dict[str, Any]) -> str:
    native = field.get("native_type")
    physical = type_name(native) if isinstance(native, str) else "unknown"
    normalized = field.get("data_type")
    normalized = normalized if isinstance(normalized, str) and normalized in {*NUMERIC, "text", "boolean", "date", "timestamp", "structured"} else "unknown"
    if physical != "unknown" and normalized != "unknown" and physical != normalized:
        raise KnowledgeValidationError("Metric field physical and normalized types disagree")
    return physical if physical != "unknown" else normalized


def _field(value: Any, fields: dict[str, dict[str, Any]]) -> str:
    if not isinstance(value, str) or value not in fields:
        raise KnowledgeValidationError("Metric field is absent from the current asset")
    return value


def _field_list(value: Any, fields: dict[str, dict[str, Any]], *, minimum=0) -> list[str]:
    if not isinstance(value, list) or not minimum <= len(value) <= 8:
        raise KnowledgeValidationError("Metric field lists must contain at most eight exact names")
    result = [_field(item, fields) for item in value]
    if len(set(result)) != len(result):
        raise KnowledgeValidationError("Metric field lists must be distinct")
    if any(field_type(fields[name]) == "structured" for name in result):
        raise KnowledgeValidationError("Structured metric dimensions and keys are unsupported")
    return result


def _literal(value: Any, kind: str) -> Any:
    if kind in NUMERIC:
        if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
            raise KnowledgeValidationError("Numeric metric filters need finite numeric values")
        if type(value) is int and not -(2 ** 63) <= value < 2 ** 63:
            raise KnowledgeValidationError("Metric integer literals exceed the supported range")
        if kind == "integer" and type(value) is not int:
            raise KnowledgeValidationError("Integer metric filters need integer values")
    elif kind == "boolean":
        if type(value) is not bool:
            raise KnowledgeValidationError("Boolean metric filters need boolean values")
    elif kind in {"text", "date", "timestamp"}:
        if not isinstance(value, str) or len(value) > 500 or re.search(r"[\x00-\x1f\x7f]", value):
            raise KnowledgeValidationError("Metric filter text exceeds its bound")
        if kind in {"date", "timestamp"}:
            try:
                parsed = datetime.fromisoformat(value)
                if kind == "date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError
                if kind == "timestamp" and parsed.utcoffset() is None:
                    raise ValueError
            except ValueError:
                raise KnowledgeValidationError("Metric time filters need exact ISO dates or aware timestamps") from None
    else:
        raise KnowledgeValidationError("Metric filter type is unsupported or unknown")
    return value


def validate_filters(value: Any, fields: dict[str, dict[str, Any]], *, allowed: set[str] | None = None) -> list[dict]:
    if not isinstance(value, list) or len(value) > 16:
        raise KnowledgeValidationError("Metric permits at most sixteen conjunctive filters")
    result = []
    for item in value:
        _object(item, {"field", "op"}, {"value"})
        name, op = _field(item["field"], fields), item["op"]
        if allowed is not None and name not in allowed:
            raise KnowledgeValidationError("Query filter is outside the metric's declared dimensions/time/key scope")
        if not isinstance(op, str) or op not in {"eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "is_null", "not_null"}:
            raise KnowledgeValidationError("Metric filter operator is unsupported")
        clean = {"field": name, "op": op}
        if op in {"is_null", "not_null"}:
            if "value" in item:
                raise KnowledgeValidationError("Null predicates do not accept substituted values")
        else:
            if "value" not in item:
                raise KnowledgeValidationError("Metric filter value is missing")
            kind = field_type(fields[name])
            if op in {"gt", "gte", "lt", "lte"} and kind not in {*NUMERIC, "date", "timestamp"}:
                raise KnowledgeValidationError("Metric ordered comparison requires numeric or time fields")
            raw = item["value"]
            if op in {"in", "not_in"}:
                if not isinstance(raw, list) or not 1 <= len(raw) <= 20:
                    raise KnowledgeValidationError("Metric membership filter needs one to twenty values")
                clean["value"] = [_literal(part, kind) for part in raw]
            else:
                clean["value"] = _literal(raw, kind)
        result.append(clean)
    return result


def _expression(node: Any, fields: dict[str, dict], units: dict[str, str], *,
                depth=0, counter=None) -> tuple[dict, str, set[str]]:
    counter = counter if counter is not None else [0]
    counter[0] += 1
    if counter[0] > MAX_EXPRESSION_NODES or depth > MAX_EXPRESSION_DEPTH or not isinstance(node, dict):
        raise KnowledgeValidationError("Metric expression exceeds its finite AST budget")
    op = node.get("op")
    if op == "field":
        _object(node, {"op", "field"})
        name = _field(node["field"], fields)
        if field_type(fields[name]) not in NUMERIC or name not in units:
            raise KnowledgeValidationError("Expression operands need numeric types and explicit field units")
        return {"op": op, "field": name}, units[name], {name}
    if op == "literal":
        _object(node, {"op", "value"}, {"unit"})
        literal = _literal(node["value"], "number")
        unit = _unit(node.get("unit", "1"))
        return {"op": op, "value": literal, "unit": unit}, unit, set()
    if not isinstance(op, str) or op not in {"add", "subtract", "multiply", "divide"}:
        raise KnowledgeValidationError("Metric expression operation is unsupported")
    _object(node, {"op", "left", "right"})
    left, left_unit, left_fields = _expression(node["left"], fields, units, depth=depth + 1, counter=counter)
    right, right_unit, right_fields = _expression(node["right"], fields, units, depth=depth + 1, counter=counter)
    if op in {"add", "subtract"}:
        if left_unit != right_unit:
            raise KnowledgeValidationError("Metric additive operands need identical declared units")
        unit = left_unit
    elif op == "multiply":
        if "1" not in {left_unit, right_unit}:
            raise KnowledgeValidationError("Metric multiplication supports dimensionless scaling only")
        unit = right_unit if left_unit == "1" else left_unit
    else:
        if right["op"] != "literal" or right_unit != "1" or right["value"] == 0:
            raise KnowledgeValidationError("Row division supports nonzero dimensionless constants only; use an aligned aggregate ratio")
        unit = left_unit
    return {"op": op, "left": left, "right": right}, unit, left_fields | right_fields


def metric_payload_digest(payload: dict) -> str:
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                             allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise KnowledgeValidationError("Metric must contain valid bounded JSON") from None
    if len(encoded) > MAX_METRIC_BYTES:
        raise KnowledgeValidationError("Metric definition exceeds the 16 KiB limit")
    return hashlib.sha256(encoded).hexdigest()


def validate_metric_payload(payload: Any, asset_fields: Any) -> dict[str, Any]:
    fields = field_map(asset_fields)
    if not isinstance(payload, dict):
        raise KnowledgeValidationError("Metric payload must be an object")
    if payload.get("schema_version") == "metric-composite-v1":
        from knowledge_layer.composite_metrics import validate_composite

        return validate_composite(payload, fields)
    v2 = "schema_version" in payload
    extra = {"schema_version", "dimensions", "filters", "time", "cohort", "additivity"}
    _object(payload, BASE_KEYS | extra if v2 else BASE_KEYS, {"expression", "ratio", "field_units"} if v2 else set())
    if v2 and payload["schema_version"] != "metric-v2":
        raise KnowledgeValidationError("Metric schema version is unsupported")
    measure = _field(payload["measure_field"], fields)
    aggregation = payload["aggregation"]
    if not isinstance(aggregation, str) or aggregation not in AGGREGATIONS:
        raise KnowledgeValidationError("Metric aggregation is unsupported")
    result = {"name": _legacy_text(payload["name"], "name", 120),
              "description": _legacy_text(payload["description"], "description", 2000),
              "measure_field": measure, "aggregation": aggregation,
              "unit": _unit(payload["unit"])}
    if not v2:
        metric_payload_digest(result)
        return result
    dimensions = _field_list(payload["dimensions"], fields)
    cohort = payload["cohort"]
    _object(cohort, {"description", "key_fields"})
    result.update(schema_version="metric-v2", dimensions=dimensions,
                  filters=validate_filters(payload["filters"], fields),
                  cohort={"description": _text(cohort["description"], "cohort description", 1000, 4),
                          "key_fields": _field_list(cohort["key_fields"], fields, minimum=1)})
    time = payload["time"]
    if time is not None:
        _object(time, {"field", "grain", "timezone"})
        name = _field(time["field"], fields)
        if (field_type(fields[name]) not in {"date", "timestamp"} or not isinstance(time["grain"], str)
                or time["grain"] not in {"day", "week", "month", "year"}):
            raise KnowledgeValidationError("Metric time requires a date/timestamp and supported calendar grain")
        timezone = _text(time["timezone"], "timezone", 120)
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise KnowledgeValidationError("Metric timezone must be an IANA zone") from None
        time = {"field": name, "grain": time["grain"], "timezone": timezone}
    result["time"] = time
    additivity = payload["additivity"]
    _object(additivity, {"kind", "non_additive_dimensions"})
    kind = additivity["kind"]
    if not isinstance(kind, str) or kind not in {"additive", "semi_additive", "non_additive"}:
        raise KnowledgeValidationError("Metric additivity must be explicit")
    excluded = _field_list(additivity["non_additive_dimensions"], fields)
    allowed = {*dimensions, *((time["field"],) if time else ())}
    if not set(excluded) <= allowed or (kind == "additive" and excluded) or (kind == "semi_additive" and not excluded):
        raise KnowledgeValidationError("Metric non-additive dimensions must bind the declared grouping/time scope")
    if aggregation == "sum" and kind == "non_additive" and "ratio" not in payload:
        raise KnowledgeValidationError("Summing non-additive measures requires an unsupported explicit rollup rule")
    if aggregation in {"avg", "count_distinct"} and kind != "non_additive":
        raise KnowledgeValidationError("Averages/distinct counts are not additive aggregate results")
    result["additivity"] = {"kind": kind, "non_additive_dimensions": excluded}
    units = payload.get("field_units", {})
    if not isinstance(units, dict) or len(units) > 16:
        raise KnowledgeValidationError("Metric field units must be a bounded exact field map")
    units = {_field(name, fields): _unit(unit) for name, unit in units.items()}
    for name, unit in units.items():
        if fields[name].get("unit") is not None and fields[name]["unit"] != unit:
            raise KnowledgeValidationError("Metric declared unit contradicts the bound source field unit")
    expression = payload.get("expression", {"op": "field", "field": measure})
    if "expression" in payload:
        clean_expression, numerator_unit, used = _expression(expression, fields, units)
        if measure not in used:
            raise KnowledgeValidationError("Metric measure must participate in its expression")
        result["expression"] = clean_expression
    else:
        numerator_unit, used = units.get(measure, result["unit"]), {measure}
    if aggregation not in {"count", "count_distinct"} and field_type(fields[measure]) not in NUMERIC:
        raise KnowledgeValidationError("Numeric metric requires a known numeric source type")
    if aggregation in {"count", "count_distinct"} and result["unit"] != "records":
        raise KnowledgeValidationError("Count metric unit must be records")
    ratio = payload.get("ratio")
    if "ratio" in payload:
        _object(ratio, {"denominator_field", "denominator_aggregation", "cohort_policy"})
        denominator = _field(ratio["denominator_field"], fields)
        denominator_aggregation = ratio["denominator_aggregation"]
        if (aggregation != "sum" or not isinstance(denominator_aggregation, str)
                or denominator_aggregation not in {"sum", "count", "count_distinct"}
                or ratio["cohort_policy"] != "paired_non_null" or kind != "non_additive"):
            raise KnowledgeValidationError("Ratios require summed numerator, explicit denominator, paired cohort and non-additive output")
        if denominator_aggregation == "sum" and field_type(fields[denominator]) not in NUMERIC:
            raise KnowledgeValidationError("Ratio sum denominator requires a numeric source field")
        if measure not in units or denominator not in units:
            raise KnowledgeValidationError("Ratio operands require explicit declared field units")
        denominator_unit = units[denominator] if denominator_aggregation == "sum" else "records"
        output_unit = "ratio" if numerator_unit == denominator_unit else f"{numerator_unit}/{denominator_unit}"
        if result["unit"] != output_unit:
            raise KnowledgeValidationError("Ratio output unit must preserve its operand units")
        result["ratio"] = dict(ratio)
    elif aggregation not in {"count", "count_distinct"} and numerator_unit != result["unit"]:
        raise KnowledgeValidationError("Metric output unit must match its numeric expression")
    if units:
        result["field_units"] = units
    metric_payload_digest(result)
    return result
