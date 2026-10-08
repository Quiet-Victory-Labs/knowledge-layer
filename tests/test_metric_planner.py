"""Exact synthetic readbacks test SQL effects; no enterprise source is accessed."""

from __future__ import annotations

import copy
import sqlite3

import pytest

from knowledge_layer.metric_planner import plan_metric_query
from knowledge_layer.metric_semantics import metric_payload_digest, validate_metric_payload
from knowledge_layer.store import KnowledgeValidationError


@pytest.fixture
def metric():
    return {"name": "Recorded amount", "description": "Sum known amounts within the selected event cohort.",
            "measure_field": "amount", "aggregation": "sum", "unit": "USD",
            "schema_version": "metric-v2", "dimensions": ["team"], "filters": [], "time": None,
            "cohort": {"description": "One source row per unique id.", "key_fields": ["id"]},
            "additivity": {"kind": "additive", "non_additive_dimensions": []}}


@pytest.fixture
def binding(metric):
    fields = [{"name": "id", "data_type": "text", "native_type": "text", "field_id": 1},
              {"name": "amount", "data_type": "number", "native_type": "real", "field_id": 2},
              {"name": "expense", "data_type": "number", "native_type": "real", "field_id": 3},
              {"name": "events", "data_type": "integer", "native_type": "integer", "field_id": 4},
              {"name": "team", "data_type": "text", "native_type": "text", "field_id": 5},
              {"name": "occurred_at", "data_type": "timestamp", "native_type": "timestamp with time zone", "field_id": 6}]
    return {"connection_id": "source-1", "snapshot_id": "snapshot-1", "snapshot_version": 3,
            "asset_id": "asset-1", "table": {"name": "events"}, "schema": fields, "dialect": "sqlite",
            "source_state": {"connection_status": "discovery_complete", "agent_ready": True, "last_error": None},
            "approval": {"assertion_id": "approved-1", "status": "approved", "effective_on_current_snapshot": True,
                         "source_snapshot_id": "snapshot-1"},
            "metric_digest": metric_payload_digest(validate_metric_payload(metric, fields))}


@pytest.fixture
def database():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE events(id TEXT,amount REAL,expense REAL,events INTEGER,team TEXT,occurred_at TEXT)")
    connection.executemany("INSERT INTO events VALUES(?,?,?,?,?,?)", [
        ("a", 12, 4, 2, "A", "2026-10-03T12:00:00Z"),
        ("b", None, 1, 8, "A", "2026-10-03T13:00:00Z"),
        ("c", 0, 0, 0, "B", "2026-10-04T12:00:00Z"),
        ("d", None, None, None, "C", "2026-10-04T13:00:00Z"),
        ("e", 9, 3, None, "A", "2026-10-04T14:00:00Z"),
    ])
    yield connection
    connection.close()


def _rebind(binding, metric):
    binding["metric_digest"] = metric_payload_digest(validate_metric_payload(metric, binding["schema"]))


def _rows(database, plan):
    return [dict(row) for row in database.execute(plan["sql"], plan["parameters"])]


def test_real_sqlite_effect_preserves_null_zero_and_diagnostics(database, binding, metric):
    plan = plan_metric_query(binding, metric, {"group_by": ["team"]})
    rows = _rows(database, plan)
    assert [(r["dimension_0"], r["metric_value"], r["selected_rows"], r["known_measure_rows"], r["missing_measure_rows"], r["zero_measure_rows"])
            for r in rows] == [("A", 21, 3, 2, 1, 0), ("B", 0, 1, 1, 0, 1), ("C", None, 1, 0, 1, 0)]
    assert plan["status"] == "not_executed" and not plan["execution_verified"]
    assert plan["result_state"] == "not_computed"
    assert plan["limits"]["row_limit_bounds_scan"] is False
    assert database.execute(plan["diagnostics"]["grain_check_sql"], plan["parameters"]).fetchall() == []


@pytest.mark.parametrize("aggregation,expected", [("sum", None), ("avg", None), ("min", None), ("max", None), ("count", 0), ("count_distinct", 0)])
def test_empty_scalar_cohort_has_honest_aggregate_effect(database, binding, metric, aggregation, expected):
    metric["aggregation"] = aggregation
    metric["unit"] = "records" if aggregation.startswith("count") else "USD"
    metric["additivity"]["kind"] = "non_additive" if aggregation in {"avg", "count_distinct"} else "additive"
    _rebind(binding, metric)
    plan = plan_metric_query(binding, metric, {"filters": [{"field": "team", "op": "eq", "value": "absent"}]})
    row = _rows(database, plan)[0]
    assert row["metric_value"] == expected
    assert (row["selected_rows"], row["known_measure_rows"], row["missing_measure_rows"]) == (0, 0, 0)


def test_ratio_uses_paired_cohort_and_refuses_missing_or_zero_denominator_value(database, binding, metric):
    metric.update(unit="USD/records", field_units={"amount": "USD", "events": "records"},
                  ratio={"denominator_field": "events", "denominator_aggregation": "sum", "cohort_policy": "paired_non_null"})
    metric["additivity"]["kind"] = "non_additive"
    _rebind(binding, metric)
    rows = _rows(database, plan_metric_query(binding, metric, {"group_by": ["team"]}))
    assert rows[0]["metric_value"] == 6  # 12/2, neither 21/10 nor average(row ratios).
    assert (rows[0]["selected_rows"], rows[0]["known_measure_rows"], rows[0]["missing_measure_rows"]) == (3, 1, 2)
    assert rows[0]["known_numerator_rows"] == 2 and rows[0]["known_denominator_rows"] == 2
    assert rows[1]["metric_value"] is None and rows[1]["zero_denominator_rows"] == 1
    assert rows[2]["metric_value"] is None and rows[2]["missing_measure_rows"] == 1


def test_numeric_expression_effect_and_zero_are_preserved(database, binding, metric):
    metric.update(field_units={"amount": "USD", "expense": "USD"}, expression={"op": "divide", "left": {
        "op": "subtract", "left": {"op": "field", "field": "amount"}, "right": {"op": "field", "field": "expense"}},
        "right": {"op": "literal", "value": 2}})
    _rebind(binding, metric)
    rows = _rows(database, plan_metric_query(binding, metric, {"group_by": ["team"]}))
    assert rows[0]["metric_value"] == 7 and rows[0]["known_measure_rows"] == 2
    assert rows[1]["metric_value"] == 0 and rows[1]["zero_measure_rows"] == 1
    assert rows[2]["metric_value"] is None


def test_parameters_cannot_escape_sql_and_definition_filters_cannot_be_removed(database, binding, metric):
    metric["filters"] = [{"field": "team", "op": "eq", "value": "A"}]
    _rebind(binding, metric)
    hostile = "A' OR 1=1 --"
    plan = plan_metric_query(binding, metric, {"filters": [{"field": "team", "op": "eq", "value": hostile}]})
    assert hostile not in plan["sql"] and hostile in plan["parameters"].values()
    assert _rows(database, plan)[0]["selected_rows"] == 0
    assert len(database.execute("SELECT * FROM events").fetchall()) == 5


def test_grain_check_detects_duplicates_and_null_key_is_visible(database, binding, metric):
    database.execute("INSERT INTO events VALUES('a',100,0,1,'A',NULL)")
    database.execute("INSERT INTO events VALUES(NULL,1,0,1,'A',NULL)")
    plan = plan_metric_query(binding, metric, {})
    duplicates = [dict(row) for row in database.execute(plan["diagnostics"]["grain_check_sql"], plan["parameters"])]
    assert duplicates == [{"id": "a", "rows_per_key": 2}]
    assert _rows(database, plan)[0]["missing_key_rows"] == 1
    assert "grain_uniqueness_and_nonnull_keys" in plan["required_checks"]


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse", "sqlite"])
def test_supported_dialects_preserve_bindings_and_emit_unexecuted_parameterized_plans(binding, metric, dialect):
    binding["dialect"] = dialect
    plan = plan_metric_query(binding, metric, {"filters": [{"field": "team", "op": "eq", "value": "A"}]})
    assert plan["status"] == "not_executed" and plan["source_binding"]["snapshot_version"] == 3
    assert plan["required_fields"][0]["field_id"] is not None
    assert "'A'" not in plan["sql"]
    if dialect == "clickhouse":
        assert "sumOrNull(" in plan["sql"] and "{p0:String}" in plan["sql"]
    elif dialect == "postgres":
        assert "%(p0)s" in plan["sql"]
    else:
        assert ":p0" in plan["sql"]


@pytest.mark.parametrize("grain,expected", [("day", ["2026-10-03", "2026-10-04"]), ("week", ["2026-09-28"]), ("month", ["2026-10-01"]), ("year", ["2026-01-01"])])
def test_sqlite_utc_calendar_buckets_and_half_open_interval(database, binding, metric, grain, expected):
    metric["time"] = {"field": "occurred_at", "grain": grain, "timezone": "UTC"}
    _rebind(binding, metric)
    rows = _rows(database, plan_metric_query(binding, metric, {"include_time": True,
        "time_range": {"start": "2026-10-03T12:00:00Z", "end": "2026-10-04T14:00:00Z"}}))
    assert [row["time_bucket"] for row in rows] == expected
    assert sum(row["selected_rows"] for row in rows) == 4


def test_sqlite_time_predicates_compare_instants_not_offset_text(database, binding, metric):
    database.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", ("offset", 3, 1, 1, "A", "2026-10-03T08:00:00-04:00"))
    metric["time"] = {"field": "occurred_at", "grain": "day", "timezone": "UTC"}
    _rebind(binding, metric)
    plan = plan_metric_query(binding, metric, {"time_range": {
        "start": "2026-10-03T12:00:00Z", "end": "2026-10-03T13:00:00Z"}})
    row = _rows(database, plan)[0]
    assert (row["selected_rows"], row["metric_value"]) == (2, 15)
    assert "native_temporal_parsing_and_precision" in plan["required_checks"]
    equal = plan_metric_query(binding, metric, {"filters": [
        {"field": "occurred_at", "op": "in", "value": ["2026-10-03T14:00:00+02:00"]}]})
    assert _rows(database, equal)[0]["selected_rows"] == 2


def test_timezones_are_explicit_and_unqualified_naive_or_sqlite_local_time_refuses(binding, metric):
    metric["time"] = {"field": "occurred_at", "grain": "day", "timezone": "America/New_York"}
    _rebind(binding, metric)
    with pytest.raises(KnowledgeValidationError, match="UTC only"):
        plan_metric_query(binding, metric, {"include_time": True})
    binding["dialect"] = "postgres"
    plan = plan_metric_query(binding, metric, {"include_time": True})
    assert "America/New_York" in plan["parameters"].values()
    binding["schema"][-1]["native_type"] = "timestamp without time zone"
    with pytest.raises(KnowledgeValidationError, match="timezone-aware"):
        plan_metric_query(binding, metric, {"include_time": True})


def test_semi_additive_balance_cannot_be_summed_across_time(binding, metric):
    metric["time"] = {"field": "occurred_at", "grain": "day", "timezone": "UTC"}
    metric["additivity"] = {"kind": "semi_additive", "non_additive_dimensions": ["occurred_at"]}
    _rebind(binding, metric)
    with pytest.raises(KnowledgeValidationError, match="non-additive"):
        plan_metric_query(binding, metric, {})
    assert plan_metric_query(binding, metric, {"include_time": True})["status"] == "not_executed"


@pytest.mark.parametrize("mutation", [
    lambda b: b.update(dialect="snowflake"),
    lambda b: b["approval"].update(status="candidate"),
    lambda b: b["approval"].update(effective_on_current_snapshot=False),
    lambda b: b["approval"].update(source_snapshot_id="old-snapshot"),
    lambda b: b.update(metric_digest="0" * 64),
    lambda b: b["source_state"].update(agent_ready=False),
    lambda b: b["source_state"].update(connection_status="error"),
    lambda b: b["source_state"].update(last_error="failed read"),
    lambda b: b.update(snapshot_version=True),
])
def test_current_approved_binding_is_required(binding, metric, mutation):
    mutation(binding)
    with pytest.raises(KnowledgeValidationError):
        plan_metric_query(binding, metric, {})


@pytest.mark.parametrize("query", [
    {"sql": "SELECT * FROM events"}, {"joins": [{"table": "other"}]},
    {"group_by": ["expense"]}, {"limit": True}, {"limit": 1001}, {"include_time": 1},
    {"filters": [{"field": "expense", "op": "gt", "value": 0}]},
    {"time_range": {"start": "2026-10-04", "end": "2026-10-03"}},
])
def test_requests_cannot_expand_semantics_or_access(binding, metric, query):
    with pytest.raises(KnowledgeValidationError):
        plan_metric_query(binding, metric, query)


def test_result_limit_includes_extra_row_to_detect_truncation(database, binding, metric):
    plan = plan_metric_query(binding, metric, {"group_by": ["team"], "limit": 1})
    assert len(_rows(database, plan)) == 2 and plan["limits"]["max_result_rows"] == 1


def test_unreviewed_definition_mutation_and_decimal_loss_refuse(binding, metric):
    changed = copy.deepcopy(metric)
    changed["description"] += " Unapproved new cohort."
    with pytest.raises(KnowledgeValidationError, match="approved binding"):
        plan_metric_query(binding, changed, {})
    metric.update(expression={"op": "field", "field": "amount"}, field_units={"amount": "USD"})
    binding["schema"][1]["native_type"] = "numeric(18,2)"
    _rebind(binding, metric)
    with pytest.raises(KnowledgeValidationError, match="Exact decimal"):
        plan_metric_query(binding, metric, {})
