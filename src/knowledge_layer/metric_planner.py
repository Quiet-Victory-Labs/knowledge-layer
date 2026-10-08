"""Finite, parameterized single-asset SQL plans for reviewed metric definitions.

The service integration must load the binding from its current authorized store;
caller-supplied approval flags are not authorization. This module performs no I/O,
executes no SQL and never accepts SQL, joins, functions or table names in requests.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from knowledge_layer.metric_semantics import (
    NUMERIC,
    _field_list,
    _object,
    _text,
    field_map,
    field_type,
    metric_payload_digest,
    validate_filters,
    validate_metric_payload,
)
from knowledge_layer.store import KnowledgeValidationError

SUPPORTED_DIALECTS = frozenset({"postgres", "clickhouse", "sqlite"})
MAX_SQL_BYTES = 32 * 1024


def _quote(identifier: str) -> str:
    if (not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 128
            or re.search(r"[\x00-\x1f\x7f]", identifier)):
        raise KnowledgeValidationError("Metric plan needs exact bounded physical identifiers")
    return '"' + identifier.replace('"', '""') + '"'


class _SQL:
    def __init__(self, dialect: str):
        self.dialect = dialect
        self.parameters = {}

    def parameter(self, value: Any, kind: str | None = None) -> str:
        name = f"p{len(self.parameters)}"
        if len(self.parameters) >= 400:
            raise KnowledgeValidationError("Metric plan parameter budget exceeded")
        self.parameters[name] = value
        if self.dialect == "postgres":
            return f"%({name})s"
        if self.dialect == "sqlite":
            return f":{name}"
        native = ("Bool" if type(value) is bool else "Int64" if type(value) is int
                  else "Float64" if type(value) is float else "String")
        result = "{" + name + ":" + native + "}"
        # ISO dates/aware timestamps are parsed independently of server timezone.
        if kind == "date":
            return f"toDate({result})"
        if kind == "timestamp":
            return f"parseDateTime64BestEffort({result}, 6, 'UTC')"
        return result

    def filters(self, filters: list[dict], fields: dict[str, dict]) -> list[str]:
        result = []
        operations = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
        for item in filters:
            column = _quote(item["field"])
            op = item["op"]
            kind = field_type(fields[item["field"]])
            # SQLite stores timestamps without a native datetime type. Compare
            # parsed instants rather than offset-bearing text lexicographically.
            # Parsing/precision remains an explicit native verification check.
            instant = self.dialect == "sqlite" and kind == "timestamp"
            compare_column = f"julianday({column})" if instant else column

            def operand(value, kind=kind, instant=instant):
                parameter = self.parameter(value, kind)
                return f"julianday({parameter})" if instant else parameter

            if op in {"is_null", "not_null"}:
                result.append(f"{column} IS {'NOT ' if op == 'not_null' else ''}NULL")
            elif op in {"in", "not_in"}:
                values = ", ".join(operand(value) for value in item["value"])
                result.append(f"{compare_column} {'NOT IN' if op == 'not_in' else 'IN'} ({values})")
            else:
                result.append(f"{compare_column} {operations[op]} {operand(item['value'])}")
        return result

    def expression(self, node: dict) -> str:
        if node["op"] == "field":
            return _quote(node["field"])
        if node["op"] == "literal":
            return self.parameter(node["value"])
        left, right = self.expression(node["left"]), self.expression(node["right"])
        symbol = {"add": "+", "subtract": "-", "multiply": "*", "divide": "/"}[node["op"]]
        if node["op"] == "divide":
            left = f"CAST({left} AS NUMERIC)" if self.dialect == "postgres" else f"toFloat64({left})" if self.dialect == "clickhouse" else f"CAST({left} AS REAL)"
        return f"({left} {symbol} {right})"

    def aggregate(self, aggregation: str, expression: str) -> str:
        if aggregation == "count_distinct":
            return f"uniqExact({expression})" if self.dialect == "clickhouse" else f"COUNT(DISTINCT {expression})"
        if aggregation == "count":
            return f"COUNT({expression})"
        function = aggregation + "OrNull" if self.dialect == "clickhouse" else aggregation.upper()
        return f"{function}({expression})"

    def count_condition(self, condition: str) -> str:
        return f"countIf({condition})" if self.dialect == "clickhouse" else f"COUNT(CASE WHEN {condition} THEN 1 END)"


def _time_bucket(sql: _SQL, time: dict, field: dict) -> str:
    column, grain, timezone = _quote(time["field"]), time["grain"], time["timezone"]
    kind = field_type(field)
    if kind == "date" and timezone != "UTC":
        raise KnowledgeValidationError("Date-only fields cannot supply timezone-specific instants")
    if sql.dialect == "sqlite":
        if timezone != "UTC":
            raise KnowledgeValidationError("SQLite metric time grouping supports UTC only")
        if grain == "week":
            return f"date({column}, '-' || ((CAST(strftime('%w', {column}) AS INTEGER) + 6) % 7) || ' days')"
        return f"date({column}{', ' + repr('start of ' + grain) if grain in {'month', 'year'} else ''})"
    if sql.dialect == "postgres":
        if kind == "timestamp" and not re.search(r"timestamptz|with time zone", str(field.get("native_type") or ""), re.IGNORECASE):
            raise KnowledgeValidationError("PostgreSQL time grouping requires a known timezone-aware physical timestamp")
        if kind == "date":
            return f"CAST(date_trunc({sql.parameter(grain)}, CAST({column} AS timestamp)) AS date)"
        return f"date_trunc({sql.parameter(grain)}, {column}, {sql.parameter(timezone)})"
    return f"dateTrunc({sql.parameter(grain)}, {column}, {sql.parameter(timezone)})"


def _expression_fields(node: dict) -> set[str]:
    if node["op"] == "field":
        return {node["field"]}
    if node["op"] == "literal":
        return set()
    return _expression_fields(node["left"]) | _expression_fields(node["right"])


def plan_metric_query(binding: dict[str, Any], metric: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """Plan only a caller-authorized current approved, digest-bound single asset.

Root transports must construct ``binding`` from the current store, not request
JSON. Physical table/schema and field metadata are exact source facts. Approval,
publication and source error checks are independent of execution permission.
"""
    _object(binding, {"connection_id", "snapshot_id", "snapshot_version", "asset_id", "table", "schema",
                      "source_state", "approval", "metric_digest"}, {"dialect", "grain"})
    dialect = binding.get("dialect")
    if not isinstance(dialect, str) or dialect not in SUPPORTED_DIALECTS:
        raise KnowledgeValidationError("Metric planning supports postgres, clickhouse and sqlite only")
    for key in ("connection_id", "snapshot_id", "asset_id"):
        _text(binding[key], key, 128)
    if type(binding["snapshot_version"]) is not int or binding["snapshot_version"] < 1:
        raise KnowledgeValidationError("Metric plan needs a current snapshot version")
    state, approval = binding["source_state"], binding["approval"]
    if (not isinstance(state, dict) or state.get("agent_ready") is not True or state.get("last_error")
            or not isinstance(state.get("connection_status"), str)
            or state.get("connection_status") not in {"ready", "discovery_complete"}):
        raise KnowledgeValidationError("Metric source is not currently published and available")
    if (not isinstance(approval, dict) or approval.get("status") != "approved"
            or approval.get("effective_on_current_snapshot") is not True
            or approval.get("source_snapshot_id") != binding["snapshot_id"]
            or not isinstance(approval.get("assertion_id"), str) or not approval["assertion_id"]):
        raise KnowledgeValidationError("Metric plan requires an effective current approved assertion")
    fields = field_map(binding["schema"])
    metric = validate_metric_payload(metric, fields)
    if binding["metric_digest"] != metric_payload_digest(metric):
        raise KnowledgeValidationError("Metric content does not match its current approved binding")
    _object(request, set(), {"group_by", "filters", "include_time", "time_range", "limit"})
    dimensions = metric.get("dimensions", [])
    group_by = _field_list(request.get("group_by", []), fields)
    if not set(group_by) <= set(dimensions):
        raise KnowledgeValidationError("Metric query dimensions were not declared in the reviewed definition")
    include_time = request.get("include_time", False)
    if type(include_time) is not bool:
        raise KnowledgeValidationError("Metric include_time must be boolean")
    time = metric.get("time")
    if include_time and not time:
        raise KnowledgeValidationError("Metric has no declared time binding")
    limit = request.get("limit", 100)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise KnowledgeValidationError("Metric output limit must be 1-1000")
    allowed_filters = {*dimensions, *metric.get("cohort", {}).get("key_fields", []), *((time["field"],) if time else ())}
    query_filters = validate_filters(request.get("filters", []), fields, allowed=allowed_filters)
    if "time_range" in request:
        value = request["time_range"]
        _object(value, {"start", "end"})
        if not time:
            raise KnowledgeValidationError("Metric has no declared time binding")
        range_filters = validate_filters([
            {"field": time["field"], "op": "gte", "value": value["start"]},
            {"field": time["field"], "op": "lt", "value": value["end"]},
        ], fields)
        if datetime.fromisoformat(value["start"]) >= datetime.fromisoformat(value["end"]):
            raise KnowledgeValidationError("Metric time interval must be nonempty and increasing")
        query_filters += range_filters
    if metric.get("additivity", {}).get("kind") == "semi_additive":
        selected = {*group_by, *((time["field"],) if include_time else ())}
        if not set(metric["additivity"]["non_additive_dimensions"]) <= selected:
            raise KnowledgeValidationError("Metric query would aggregate across a declared non-additive dimension")
    if metric.get("schema_version") == "metric-composite-v1":
        from knowledge_layer.composite_metrics import plan_composite

        return plan_composite(binding, metric, fields=fields, dialect=dialect, group_by=group_by,
            include_time=include_time, query_filters=query_filters, limit=limit)
    measure = metric["measure_field"]
    kind = field_type(fields[measure])
    if metric["aggregation"] not in {"count", "count_distinct"} and kind not in NUMERIC:
        raise KnowledgeValidationError("Metric arithmetic requires known numeric physical fields")
    if kind in {"unknown", "structured"}:
        raise KnowledgeValidationError("Metric scalar physical type is unknown or unsupported")
    expression = metric.get("expression", {"op": "field", "field": measure})
    used_fields = _expression_fields(expression)
    ratio = metric.get("ratio")
    if ratio:
        used_fields.add(ratio["denominator_field"])
    if dialect in {"sqlite", "clickhouse"} and (ratio or "expression" in metric) and any(
        re.search(r"decimal|numeric", str(fields[name].get("native_type") or ""), re.IGNORECASE) for name in used_fields
    ):
        raise KnowledgeValidationError("Exact decimal expression/ratio semantics require PostgreSQL in this bounded planner")
    _object(binding["table"], {"name"}, {"schema"})
    table = _quote(binding["table"]["name"])
    if binding["table"].get("schema") is not None:
        table = _quote(binding["table"]["schema"]) + "." + table
    sql = _SQL(dialect)
    conditions = sql.filters(metric.get("filters", []) + query_filters, fields)
    columns = sorted(used_fields | set(group_by) | {item["field"] for item in metric.get("filters", []) + query_filters}
                     | set(metric.get("cohort", {}).get("key_fields", [])))
    if include_time:
        columns = sorted(set(columns) | {time["field"]})
    where = " WHERE " + " AND ".join(f"({condition})" for condition in conditions) if conditions else ""
    base = f"WITH base AS (SELECT {', '.join(_quote(column) for column in columns)} FROM {table}{where})"
    grouping = [_quote(name) for name in group_by]
    select_dimensions = [f"{_quote(name)} AS {_quote('dimension_' + str(index))}" for index, name in enumerate(group_by)]
    if include_time:
        bucket = _time_bucket(sql, time, fields[time["field"]])
        grouping.append(bucket)
        select_dimensions.append(bucket + ' AS "time_bucket"')
    measure_expression = sql.expression(expression)
    required = " AND ".join(f"{_quote(name)} IS NOT NULL" for name in sorted(_expression_fields(expression)))
    numerator = measure_expression
    if ratio:
        denominator = _quote(ratio["denominator_field"])
        required += f" AND {denominator} IS NOT NULL"
        numerator = f"CASE WHEN {required} THEN {measure_expression} END"
        denominator_value = f"CASE WHEN {required} THEN {denominator} END"
        numerator_aggregate = sql.aggregate(metric["aggregation"], numerator)
        denominator_aggregate = sql.aggregate(ratio["denominator_aggregation"], denominator_value)
        cast = (f"CAST({numerator_aggregate} AS NUMERIC)" if dialect == "postgres"
                else f"toFloat64({numerator_aggregate})" if dialect == "clickhouse"
                else f"CAST({numerator_aggregate} AS REAL)")
        metric_sql = f"{cast} / NULLIF({denominator_aggregate}, 0)"
    else:
        metric_sql = sql.aggregate(metric["aggregation"], numerator)
    diagnostics = {
        "selected_rows": "COUNT(*)",
        "known_measure_rows": sql.count_condition(required),
        "missing_measure_rows": sql.count_condition(f"NOT ({required})"),
    }
    if kind in NUMERIC:
        diagnostics["zero_measure_rows"] = sql.count_condition(f"({required}) AND ({measure_expression}) = 0")
    if ratio:
        diagnostics.update(
            known_numerator_rows=sql.count_condition(" AND ".join(f"{_quote(name)} IS NOT NULL" for name in sorted(_expression_fields(expression)))),
            known_denominator_rows=sql.count_condition(f"{denominator} IS NOT NULL"),
            zero_denominator_rows=sql.count_condition(f"({required}) AND {denominator} = 0") if field_type(fields[ratio["denominator_field"]]) in NUMERIC else None,
        )
        diagnostics = {key: value for key, value in diagnostics.items() if value is not None}
    key_fields = metric.get("cohort", {}).get("key_fields", [])
    if key_fields:
        diagnostics["missing_key_rows"] = sql.count_condition(" OR ".join(f"{_quote(name)} IS NULL" for name in key_fields))
    selected = select_dimensions + [metric_sql + ' AS "metric_value"'] + [value + " AS " + _quote(name) for name, value in diagnostics.items()]
    statement = base + " SELECT " + ", ".join(selected) + " FROM base"
    if grouping:
        statement += " GROUP BY " + ", ".join(grouping) + " ORDER BY " + ", ".join(_quote("dimension_" + str(index)) for index in range(len(group_by)))
        if include_time:
            statement += (", " if group_by else "") + '"time_bucket"'
    statement += f" LIMIT {limit + 1}"
    grain_sql = None
    if key_fields:
        grain_sql = base + " SELECT " + ", ".join(_quote(name) for name in key_fields) + ', COUNT(*) AS "rows_per_key" FROM base GROUP BY ' + ", ".join(_quote(name) for name in key_fields) + " HAVING COUNT(*) > 1 LIMIT 101"
    if any(len(value.encode("utf-8")) > MAX_SQL_BYTES for value in [statement, grain_sql or ""]):
        raise KnowledgeValidationError("Metric SQL exceeds its 32 KiB budget")
    return {
        "status": "not_executed", "execution_verified": False, "result_state": "not_computed",
        "dialect": dialect, "sql": statement, "parameters": sql.parameters,
        "parameter_style": {"postgres": "psycopg_named", "sqlite": "sqlite_named", "clickhouse": "clickhouse_typed"}[dialect],
        "source_binding": {key: binding[key] for key in ("connection_id", "snapshot_id", "snapshot_version", "asset_id", "metric_digest")},
        "approval": dict(approval), "metric": metric,
        "required_fields": [{"name": name, "native_type": fields[name].get("native_type"),
                             "data_type": fields[name].get("data_type"), "field_id": fields[name].get("field_id"),
                             "nullable": fields[name].get("nullable")} for name in columns],
        "diagnostics": {"columns": list(diagnostics), "state": "not_computed", "grain_check_sql": grain_sql,
                        "grain_check_required": bool(key_fields), "grain_check_zero_duplicate_rows_required": True},
        "limits": {"max_result_rows": limit, "fetch_rows": limit + 1, "truncation_detection": "extra_row",
                   "row_limit_bounds_scan": False},
        "arithmetic": {"division": "postgres_numeric" if dialect == "postgres" else "binary64"},
        "required_checks": ["current_principal_and_source_policy", "latest_source_schema_and_native_types",
                            "source_read_only_execution_permission", "grain_uniqueness_and_nonnull_keys",
                            "native_precision_and_overflow", "native_temporal_parsing_and_precision",
                            "provider_explain_scan_budget", "result_row_budget",
                            "selected_equals_known_plus_missing", "zero_is_observed_not_missing"],
        "warning": "An approved definition authorizes meaning, not source execution. LIMIT bounds output, not scans. "
                   "Do not interpret results if grain, schema, permission, precision or truncation checks fail.",
    }
