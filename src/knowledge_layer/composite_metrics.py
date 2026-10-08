"""Finite arithmetic over aligned single-asset aggregate measures; plans only."""

from __future__ import annotations

import re
from typing import Any

from knowledge_layer.metric_semantics import (
    AGGREGATIONS,
    BASE_KEYS,
    MAX_EXPRESSION_DEPTH,
    MAX_EXPRESSION_NODES,
    NUMERIC,
    _field,
    _literal,
    _object,
    _text,
    field_type,
    metric_payload_digest,
)
from knowledge_layer.store import KnowledgeValidationError

SCHEMA = "metric-composite-v1"
MAX_MEASURES = 8
_ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


def _expression(node, units, *, depth=0, counter=None):
    counter = counter if counter is not None else [0]
    counter[0] += 1
    if not isinstance(node, dict) or depth > MAX_EXPRESSION_DEPTH or counter[0] > MAX_EXPRESSION_NODES:
        raise KnowledgeValidationError("Composite expression exceeds its finite AST budget")
    op = node.get("op")
    if op == "measure":
        _object(node, {"op", "alias"})
        alias = node["alias"]
        if not isinstance(alias, str) or alias not in units:
            raise KnowledgeValidationError("Composite expression must reference a declared aggregate alias")
        return {"op": op, "alias": alias}, units[alias], {alias}, False
    if op == "literal":
        _object(node, {"op", "value"})
        return {"op": op, "value": _literal(node["value"], "number")}, "1", set(), False
    if not isinstance(op, str) or op not in {"add", "subtract", "multiply", "divide"}:
        raise KnowledgeValidationError("Composite expression supports finite numeric aggregate arithmetic only")
    _object(node, {"op", "left", "right"})
    left, lu, la, ln = _expression(node["left"], units, depth=depth + 1, counter=counter)
    right, ru, ra, rn = _expression(node["right"], units, depth=depth + 1, counter=counter)
    if op in {"add", "subtract"}:
        if lu != ru:
            raise KnowledgeValidationError("Composite additive operands require identical declared units")
        unit = lu
    elif op == "multiply":
        if not {lu, ru} & {"1", "ratio"}:
            raise KnowledgeValidationError("Composite multiplication supports dimensionless scaling only")
        unit = ru if lu == "1" else lu if ru == "1" else ru if lu == "ratio" else lu
    else:
        unit = lu if ru == "1" else "ratio" if lu == ru else lu if ru == "ratio" else f"{lu}/{ru}"
    nonlinear = ln or rn or (op == "divide" and bool(ra)) or (op == "multiply" and bool(la) and bool(ra))
    return {"op": op, "left": left, "right": right}, unit, la | ra, nonlinear


def validate_composite(payload: Any, fields: dict[str, dict]) -> dict[str, Any]:
    from knowledge_layer.metric_semantics import validate_metric_payload

    _object(payload, BASE_KEYS | {"schema_version", "dimensions", "filters", "time", "cohort", "additivity",
        "measures", "expression", "null_policy", "zero_division"})
    if payload["schema_version"] != SCHEMA or payload["aggregation"] != "composite":
        raise KnowledgeValidationError("Composite metric needs its exact schema and aggregation marker")
    if payload["null_policy"] != "complete_case" or payload["zero_division"] != "null":
        raise KnowledgeValidationError("Composite metrics require one complete-case cohort and null zero division")
    measures = payload["measures"]
    if not isinstance(measures, list) or not 1 <= len(measures) <= MAX_MEASURES:
        raise KnowledgeValidationError("Composite metrics need one to eight bounded aggregate measures")
    clean_measures, units = [], {}
    for measure in measures:
        _object(measure, {"alias", "measure_field", "aggregation", "unit"})
        alias = measure["alias"]
        if not isinstance(alias, str) or not _ALIAS.fullmatch(alias) or alias in units:
            raise KnowledgeValidationError("Composite measure aliases must be distinct safe names")
        name = _field(measure["measure_field"], fields)
        aggregation = measure["aggregation"]
        if not isinstance(aggregation, str) or aggregation not in AGGREGATIONS:
            raise KnowledgeValidationError("Composite measure aggregation is unsupported")
        unit = _text(measure["unit"], "composite measure unit", 80)
        kind = field_type(fields[name])
        if kind in {"unknown", "structured"} or (aggregation not in {"count", "count_distinct"} and kind not in NUMERIC):
            raise KnowledgeValidationError("Composite measures need exact scalar/numeric physical types")
        if aggregation in {"count", "count_distinct"}:
            if unit != "records":
                raise KnowledgeValidationError("Composite count units must be records")
        elif fields[name].get("unit") is not None and fields[name]["unit"] != unit:
            raise KnowledgeValidationError("Composite measure unit contradicts the bound physical field")
        units[alias] = unit
        clean_measures.append({"alias": alias, "measure_field": name, "aggregation": aggregation, "unit": unit})
    anchor = _field(payload["measure_field"], fields)
    if anchor != clean_measures[0]["measure_field"]:
        raise KnowledgeValidationError("Composite anchor must equal its first declared measure field")
    expression, unit, used, nonlinear = _expression(payload["expression"], units)
    if used != set(units):
        raise KnowledgeValidationError("Every composite measure must participate in the reviewed expression")
    if _text(payload["unit"], "composite output unit", 80) != unit:
        raise KnowledgeValidationError("Composite output unit must preserve aggregate operand units")
    # Reuse version-two parsing for the ONE asset/group/filter/time/key scope.
    # There are no per-measure scope overrides to align or silently reconcile.
    kind = (payload.get("additivity") or {}).get("kind") if isinstance(payload.get("additivity"), dict) else None
    scope = validate_metric_payload({key: payload[key] for key in BASE_KEYS | {
        "dimensions", "filters", "time", "cohort", "additivity"}} | {
        "schema_version": "metric-v2", "aggregation": "count_distinct" if kind == "non_additive" else "count",
        "unit": "records"}, fields)
    if kind != "non_additive" and (nonlinear or any(m["aggregation"] in {"avg", "count_distinct", "min", "max"}
                                                   for m in clean_measures)):
        raise KnowledgeValidationError("Composite nonlinear/average/distinct/extreme results must declare non-additivity")
    result = {**scope, "schema_version": SCHEMA, "aggregation": "composite", "unit": unit,
        "measures": clean_measures, "expression": expression, "null_policy": "complete_case", "zero_division": "null"}
    metric_payload_digest(result)
    return result


def plan_composite(binding, metric, *, fields, dialect, group_by, include_time, query_filters, limit):
    from knowledge_layer.metric_planner import _SQL, MAX_SQL_BYTES, _quote, _time_bucket

    grain = binding.get("grain")
    keys = metric["cohort"]["key_fields"]
    if (not isinstance(grain, dict) or grain.get("status") != "approved"
            or grain.get("key_fields") != keys):
        raise KnowledgeValidationError("Composite planning requires the current approved exact cohort grain")
    measure_fields = {measure["measure_field"] for measure in metric["measures"]}
    if dialect in {"sqlite", "clickhouse"} and any(re.search(r"decimal|numeric",
            str(fields[name].get("native_type") or ""), re.IGNORECASE) for name in measure_fields):
        raise KnowledgeValidationError("Exact decimal composite arithmetic requires PostgreSQL")
    _object(binding["table"], {"name"}, {"schema"})
    table = _quote(binding["table"]["name"])
    if binding["table"].get("schema") is not None:
        table = _quote(binding["table"]["schema"]) + "." + table
    sql = _SQL(dialect)
    filters = metric["filters"] + query_filters
    conditions = sql.filters(filters, fields)
    time = metric["time"]
    columns = sorted(measure_fields | set(group_by) | set(keys) | {f["field"] for f in filters}
        | ({time["field"]} if include_time else set()))
    where = " WHERE " + " AND ".join(f"({condition})" for condition in conditions) if conditions else ""
    base = f"WITH base AS (SELECT {', '.join(_quote(name) for name in columns)} FROM {table}{where})"
    grouping = [_quote(name) for name in group_by]
    dimensions = [f"{_quote(name)} AS {_quote('dimension_' + str(i))}" for i, name in enumerate(group_by)]
    dimension_aliases = [_quote("dimension_" + str(i)) for i in range(len(group_by))]
    if include_time:
        bucket = _time_bucket(sql, time, fields[time["field"]])
        grouping.append(bucket)
        dimensions.append(bucket + ' AS "time_bucket"')
        dimension_aliases.append('"time_bucket"')
    complete = " AND ".join(f"{_quote(name)} IS NOT NULL" for name in sorted(measure_fields))
    aliases = {measure["alias"]: "measure_" + str(i) for i, measure in enumerate(metric["measures"])}
    aggregates = [sql.aggregate(measure["aggregation"],
        f"CASE WHEN {complete} THEN {_quote(measure['measure_field'])} END") + " AS " + _quote(aliases[measure["alias"]])
        for measure in metric["measures"]]
    diagnostics = {"selected_rows": "COUNT(*)", "known_measure_rows": sql.count_condition(complete),
        "missing_measure_rows": sql.count_condition(f"NOT ({complete})"),
        "missing_key_rows": sql.count_condition(" OR ".join(f"{_quote(name)} IS NULL" for name in keys))}
    aggregated = " SELECT " + ", ".join(dimensions + aggregates + [value + " AS " + _quote(name)
        for name, value in diagnostics.items()]) + " FROM base"
    if grouping:
        aggregated += " GROUP BY " + ", ".join(grouping)

    def compile_expression(node):
        if node["op"] == "measure":
            return _quote(aliases[node["alias"]])
        if node["op"] == "literal":
            return sql.parameter(node["value"])
        left, right = compile_expression(node["left"]), compile_expression(node["right"])
        if node["op"] == "divide":
            left = (f"CAST({left} AS NUMERIC)" if dialect == "postgres" else
                    f"toFloat64({left})" if dialect == "clickhouse" else f"CAST({left} AS REAL)")
            return f"({left} / NULLIF({right}, 0))"
        return f"({left} " + {"add": "+", "subtract": "-", "multiply": "*"}[node["op"]] + f" {right})"

    formula = compile_expression(metric["expression"])
    selected = dimension_aliases + [f'CASE WHEN "known_measure_rows" = 0 THEN NULL ELSE {formula} END AS "metric_value"']
    selected += [_quote(alias) for alias in aliases.values()] + [_quote(name) for name in diagnostics]
    statement = base + ", aggregated AS (" + aggregated + ") SELECT " + ", ".join(selected) + " FROM aggregated"
    if dimension_aliases:
        statement += " ORDER BY " + ", ".join(dimension_aliases)
    statement += f" LIMIT {limit + 1}"
    grain_sql = base + " SELECT " + ", ".join(_quote(name) for name in keys) + ', COUNT(*) AS "rows_per_key" FROM base GROUP BY ' + ", ".join(_quote(name) for name in keys) + " HAVING COUNT(*) > 1 LIMIT 101"
    if any(len(value.encode()) > MAX_SQL_BYTES for value in (statement, grain_sql)):
        raise KnowledgeValidationError("Composite SQL exceeds the 32 KiB budget")
    return {"status": "not_executed", "execution_verified": False, "result_state": "not_computed",
        "dialect": dialect, "sql": statement, "parameters": sql.parameters,
        "parameter_style": {"postgres": "psycopg_named", "sqlite": "sqlite_named", "clickhouse": "clickhouse_typed"}[dialect],
        "source_binding": {key: binding[key] for key in ("connection_id", "snapshot_id", "snapshot_version", "asset_id", "metric_digest")},
        "approval": dict(binding["approval"]), "metric": metric, "measure_columns": aliases,
        "required_fields": [{"name": name, "native_type": fields[name].get("native_type"),
            "data_type": fields[name].get("data_type"), "field_id": fields[name].get("field_id"),
            "nullable": fields[name].get("nullable")} for name in columns],
        "diagnostics": {"columns": list(diagnostics), "state": "not_computed", "grain_check_sql": grain_sql,
            "grain_check_required": True, "grain_check_zero_duplicate_rows_required": True},
        "limits": {"max_result_rows": limit, "fetch_rows": limit + 1, "truncation_detection": "extra_row", "row_limit_bounds_scan": False},
        "arithmetic": {"division": "postgres_numeric" if dialect == "postgres" else "binary64",
            "null_policy": "complete_case", "zero_division": "null"},
        "required_checks": ["current_principal_and_source_policy", "latest_source_schema_and_native_types",
            "source_read_only_execution_permission", "grain_uniqueness_and_nonnull_keys", "native_precision_and_overflow",
            "native_temporal_parsing_and_precision", "provider_explain_scan_budget", "result_row_budget",
            "selected_equals_known_plus_missing", "zero_is_observed_not_missing"],
        "warning": "An approved composite definition authorizes meaning, not source execution. All measures share one complete-case cohort. "
            "Do not interpret results if grain, precision, permission, schema or truncation checks fail."}
