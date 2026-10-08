"""Aligned reviewed aggregate arithmetic preserves real cohorts, units and gates."""

from __future__ import annotations

import sqlite3
from copy import deepcopy

import pytest

from knowledge_layer.metric_planner import plan_metric_query
from knowledge_layer.metric_semantics import metric_payload_digest, validate_metric_payload
from knowledge_layer.store import KnowledgeValidationError


@pytest.fixture
def metric():
    return {"schema_version": "metric-composite-v1", "name": "Profit margin",
        "description": "Ratio of summed profit to summed revenue in one complete-case source cohort.",
        "measure_field": "amount", "aggregation": "composite", "unit": "ratio",
        "dimensions": ["team"], "filters": [], "time": None,
        "cohort": {"description": "One source event per exact id.", "key_fields": ["id"]},
        "additivity": {"kind": "non_additive", "non_additive_dimensions": []},
        "null_policy": "complete_case", "zero_division": "null",
        "measures": [{"alias": "revenue", "measure_field": "amount", "aggregation": "sum", "unit": "USD"},
                     {"alias": "expense", "measure_field": "expense", "aggregation": "sum", "unit": "USD"}],
        "expression": {"op": "divide", "left": {"op": "subtract",
            "left": {"op": "measure", "alias": "revenue"}, "right": {"op": "measure", "alias": "expense"}},
            "right": {"op": "measure", "alias": "revenue"}}}


@pytest.fixture
def binding(metric):
    fields = [{"name": "id", "data_type": "text", "native_type": "text"},
        {"name": "amount", "data_type": "number", "native_type": "real", "unit": "USD"},
        {"name": "expense", "data_type": "number", "native_type": "real", "unit": "USD"},
        {"name": "team", "data_type": "text", "native_type": "text"},
        {"name": "occurred_at", "data_type": "timestamp", "native_type": "timestamp with time zone"}]
    return {"connection_id": "source-1", "snapshot_id": "snapshot-1", "snapshot_version": 3,
        "asset_id": "asset-1", "table": {"name": "events"}, "schema": fields, "dialect": "sqlite",
        "source_state": {"connection_status": "discovery_complete", "agent_ready": True, "last_error": None},
        "approval": {"assertion_id": "review-1", "status": "approved", "effective_on_current_snapshot": True,
                     "source_snapshot_id": "snapshot-1"},
        "grain": {"status": "approved", "key_fields": ["id"]},
        "metric_digest": metric_payload_digest(validate_metric_payload(metric, fields))}


@pytest.fixture
def database():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE events(id TEXT, amount REAL, expense REAL, team TEXT, occurred_at TEXT)")
    connection.executemany("INSERT INTO events VALUES(?,?,?,?,?)", [
        ("a", 12, 4, "A", "2026-10-03T12:00:00Z"),
        ("b", None, 100, "A", "2026-10-03T13:00:00Z"),
        ("c", 0, 0, "B", "2026-10-04T12:00:00Z"),
        ("d", None, None, "C", "2026-10-04T13:00:00Z"),
        ("e", 9, 6, "A", "2026-10-04T14:00:00Z"),
        ("f", 0, -3, "D", "2026-10-04T15:00:00Z"),
        ("g", 0, 0, "E", "2026-10-04T16:00:00Z"),
        ("h", 5, 5, "E", "2026-10-04T17:00:00Z")])
    yield connection
    connection.close()


def rebind(binding, metric):
    binding["metric_digest"] = metric_payload_digest(validate_metric_payload(metric, binding["schema"]))


def rows(database, plan):
    return [dict(row) for row in database.execute(plan["sql"], plan["parameters"])]


def test_composite_aggregate_effect_is_ratio_of_sums_with_one_complete_case_cohort(database, binding, metric):
    plan = plan_metric_query(binding, metric, {"group_by": ["team"]})
    result = rows(database, plan)
    assert result[0]["metric_value"] == pytest.approx(11 / 21)
    assert result[0]["metric_value"] != pytest.approx(((12 - 4) / 12 + (9 - 6) / 9) / 2)
    assert (result[0]["selected_rows"], result[0]["known_measure_rows"], result[0]["missing_measure_rows"]) == (3, 2, 1)
    assert (result[0]["measure_0"], result[0]["measure_1"]) == (21, 10)
    assert result[1]["metric_value"] is None  # Observed zero denominator.
    assert result[2]["metric_value"] is None  # Unknown complete-case cohort.
    assert result[3]["metric_value"] is None  # Nonzero numerator divided by observed zero.
    assert result[4]["metric_value"] == 0  # Observed zero numerator, nonzero denominator.
    assert plan["status"] == "not_executed" and plan["execution_verified"] is False


def test_empty_and_grouped_filters_cannot_change_a_measure_cohort_independently(database, binding, metric):
    metric["filters"] = [{"field": "team", "op": "eq", "value": "A"}]
    rebind(binding, metric)
    hostile = "A' OR 1=1 --"
    plan = plan_metric_query(binding, metric, {"filters": [{"field": "team", "op": "eq", "value": hostile}]})
    assert hostile not in plan["sql"] and hostile in plan["parameters"].values()
    result = rows(database, plan)[0]
    assert result["metric_value"] is None and result["selected_rows"] == 0
    assert len(database.execute("SELECT * FROM events").fetchall()) == 8


def test_time_scope_and_real_grain_diagnostics_are_shared(database, binding, metric):
    metric["time"] = {"field": "occurred_at", "grain": "day", "timezone": "UTC"}
    rebind(binding, metric)
    plan = plan_metric_query(binding, metric, {"include_time": True,
        "time_range": {"start": "2026-10-03T00:00:00Z", "end": "2026-10-04T00:00:00Z"}})
    result = rows(database, plan)
    assert len(result) == 1 and result[0]["time_bucket"] == "2026-10-03"
    assert result[0]["metric_value"] == pytest.approx(2 / 3)
    database.execute("INSERT INTO events VALUES('a',1,1,'A',NULL)")
    database.execute("INSERT INTO events VALUES(NULL,1,1,'A',NULL)")
    plan = plan_metric_query(binding, metric, {})
    duplicates = list(database.execute(plan["diagnostics"]["grain_check_sql"], plan["parameters"]))
    assert dict(duplicates[0]) == {"id": "a", "rows_per_key": 2}
    assert rows(database, plan)[0]["missing_key_rows"] == 1


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse", "sqlite"])
def test_compilation_preserves_safe_null_division_parameters_and_no_execution(binding, metric, dialect):
    binding["dialect"] = dialect
    plan = plan_metric_query(binding, metric, {"filters": [{"field": "team", "op": "eq", "value": "A"}]})
    assert "NULLIF(" in plan["sql"] and "JOIN" not in plan["sql"]
    assert "'A'" not in plan["sql"] and "A" in plan["parameters"].values()
    assert plan["execution_verified"] is False
    if dialect == "postgres":
        assert " AS NUMERIC)" in plan["sql"] and "%(p0)s" in plan["sql"]
    elif dialect == "clickhouse":
        assert "sumOrNull(" in plan["sql"] and "toFloat64(" in plan["sql"] and "{p0:String}" in plan["sql"]


@pytest.mark.parametrize("mutation", [
    lambda m: m["measures"][0].update(alias="x); DROP TABLE events;--"),
    lambda m: m["measures"][1].update(alias="revenue"),
    lambda m: m["measures"][0].update(unit="EUR"),
    lambda m: m.update(unit="USD"),
    lambda m: m["expression"].update(op="execute_sql"),
    lambda m: m["expression"]["right"].update(alias="other_asset.revenue"),
    lambda m: m["measures"][0].update(filters=[]),
    lambda m: m["measures"][0].update(cohort={"key_fields": ["team"]}),
    lambda m: m["measures"][0].update(asset_id="other-asset"),
    lambda m: m.update(null_policy="independent_non_null"),
    lambda m: m.update(zero_division="zero"),
    lambda m: m.update(measures=[]),
    lambda m: m.update(measures=m["measures"] * 5),
    lambda m: m.update(expression={"op": "literal", "value": float("inf")}),
    lambda m: m.update(expression={"op": "literal", "value": True}),
    lambda m: m.update(expression={"op": "field", "field": "amount"}),
    lambda m: m.update(expression={"op": "measure", "alias": "revenue"}),
    lambda m: m["additivity"].update(kind="additive"),
    lambda m: m.update(measure_field="expense"),
    lambda m: m["cohort"].update(key_fields=[]),
])
def test_unsupported_or_unaligned_composite_semantics_refuse(binding, metric, mutation):
    mutation(metric)
    with pytest.raises(KnowledgeValidationError):
        validate_metric_payload(metric, binding["schema"])


@pytest.mark.parametrize("mutation", [
    lambda b: b.update(metric_digest="0" * 64),
    lambda b: b["approval"].update(status="candidate"),
    lambda b: b["approval"].update(source_snapshot_id="older"),
    lambda b: b["source_state"].update(agent_ready=False),
    lambda b: b.update(grain=None),
    lambda b: b["grain"].update(status="candidate"),
    lambda b: b["grain"].update(key_fields=["team"]),
    lambda b: b.update(dialect="snowflake"),
])
def test_composite_preserves_approved_current_binding_and_exact_grain(binding, metric, mutation):
    mutation(binding)
    with pytest.raises(KnowledgeValidationError):
        plan_metric_query(binding, metric, {})


def test_digest_mutation_decimal_and_multi_source_compilation_refuse(binding, metric):
    changed = deepcopy(metric)
    changed["description"] += " Different reviewed population."
    with pytest.raises(KnowledgeValidationError, match="approved binding"):
        plan_metric_query(binding, changed, {})
    with pytest.raises(KnowledgeValidationError):
        plan_metric_query(binding, metric, {"joins": [{"source_id": "other"}]})
    binding["schema"][1]["native_type"] = "numeric(18,2)"
    with pytest.raises(KnowledgeValidationError, match="decimal"):
        plan_metric_query(binding, metric, {})
    binding["dialect"] = "postgres"
    assert plan_metric_query(binding, metric, {})["status"] == "not_executed"


def test_aligned_count_aggregate_and_explicit_units_have_real_effect(database, binding, metric):
    metric["measures"][1] = {"alias": "count", "measure_field": "id", "aggregation": "count", "unit": "records"}
    metric["expression"] = {"op": "divide", "left": {"op": "measure", "alias": "revenue"},
                            "right": {"op": "measure", "alias": "count"}}
    metric["unit"] = "USD/records"
    rebind(binding, metric)
    result = rows(database, plan_metric_query(binding, metric, {"group_by": ["team"]}))
    assert result[0]["metric_value"] == 10.5 and result[0]["measure_1"] == 2
    assert result[1]["metric_value"] == 0 and result[2]["metric_value"] is None


def test_dimensionless_literal_scaling_preserves_ratio_units(database, binding, metric):
    metric["expression"] = {"op": "multiply", "left": metric["expression"],
                            "right": {"op": "literal", "value": 100}}
    rebind(binding, metric)
    plan = plan_metric_query(binding, metric, {"group_by": ["team"]})
    assert 100 in plan["parameters"].values()
    assert rows(database, plan)[0]["metric_value"] == pytest.approx(100 * 11 / 21)


def test_eight_measures_are_supported_and_ast_budget_is_finite(database, binding, metric):
    metric["measures"] = [{"alias": f"value{i}", "measure_field": "amount", "aggregation": "sum", "unit": "USD"}
                          for i in range(8)]
    nodes = [{"op": "measure", "alias": item["alias"]} for item in metric["measures"]]
    while len(nodes) > 1:
        nodes = [{"op": "add", "left": nodes[i], "right": nodes[i + 1]} for i in range(0, len(nodes), 2)]
    metric.update(expression=nodes[0], unit="USD", additivity={"kind": "additive", "non_additive_dimensions": []})
    rebind(binding, metric)
    assert rows(database, plan_metric_query(binding, metric, {"group_by": ["team"]}))[0]["metric_value"] == 168
    for _ in range(7):
        metric["expression"] = {"op": "multiply", "left": metric["expression"], "right": {"op": "literal", "value": 1}}
    with pytest.raises(KnowledgeValidationError, match="AST budget"):
        validate_metric_payload(metric, binding["schema"])
    # A shallow tree can exceed the node budget without exceeding depth five.
    nodes = [{"op": "measure", "alias": item["alias"]} for item in metric["measures"]] * 4
    while len(nodes) > 1:
        nodes = [{"op": "add", "left": nodes[i], "right": nodes[i + 1]} for i in range(0, len(nodes), 2)]
    metric["expression"] = nodes[0]
    with pytest.raises(KnowledgeValidationError, match="AST budget"):
        validate_metric_payload(metric, binding["schema"])
