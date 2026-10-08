"""Typed definitions preserve declared business semantics without granting trust."""

from __future__ import annotations

import copy

import pytest

from knowledge_layer.metric_semantics import metric_payload_digest, validate_metric_payload
from knowledge_layer.store import KnowledgeValidationError


@pytest.fixture
def fields():
    return [
        {"name": "id", "data_type": "text", "native_type": "text"},
        {"name": "amount", "data_type": "number", "native_type": "double precision", "unit": "USD"},
        {"name": "expense", "data_type": "number", "native_type": "double precision", "unit": "USD"},
        {"name": "events", "data_type": "integer", "native_type": "bigint", "unit": "records"},
        {"name": "team", "data_type": "text", "native_type": "text"},
        {"name": "active", "data_type": "boolean", "native_type": "boolean"},
        {"name": "occurred_at", "data_type": "timestamp", "native_type": "timestamp with time zone"},
        {"name": "date", "data_type": "date", "native_type": "date"},
    ]


@pytest.fixture
def legacy():
    return {"name": "Recorded revenue", "description": "Sum known amounts on the selected source cohort.",
            "measure_field": "amount", "aggregation": "sum", "unit": "USD"}


@pytest.fixture
def metric(legacy):
    return {**legacy, "schema_version": "metric-v2", "dimensions": ["team"], "filters": [],
            "time": {"field": "occurred_at", "grain": "day", "timezone": "America/New_York"},
            "cohort": {"description": "One observed event per exact source id.", "key_fields": ["id"]},
            "additivity": {"kind": "additive", "non_additive_dimensions": []}}


def test_legacy_shape_and_digest_stay_compatible(legacy, fields):
    validated = validate_metric_payload(legacy, fields)
    assert validated == legacy and set(validated) == set(legacy)
    assert metric_payload_digest(validated) == metric_payload_digest(dict(reversed(list(legacy.items()))))
    assert validate_metric_payload(legacy, {field["name"] for field in fields}) == legacy


def test_legacy_paragraphs_and_exact_units_are_preserved(legacy, fields):
    legacy.update(description="First reviewed paragraph.\n\nSecond reviewed paragraph.", unit="  USD  ")
    assert validate_metric_payload(legacy, fields) == legacy


@pytest.mark.parametrize("unit", ["", " ", None, 0, "x" * 81])
def test_legacy_unit_validation_remains_compatible(legacy, fields, unit):
    legacy["unit"] = unit
    with pytest.raises(KnowledgeValidationError, match="Metric unit must be 1-80 characters"):
        validate_metric_payload(legacy, fields)


def test_malformed_metadata_or_ratio_enums_fail_as_validation(metric, fields):
    fields[1].update(data_type=["number"], native_type=None)
    with pytest.raises(KnowledgeValidationError, match="numeric"):
        validate_metric_payload(metric, fields)
    fields[1].update(data_type="number", native_type="double precision")
    metric.update(unit="USD/records", field_units={"amount": "USD", "events": "records"},
                  additivity={"kind": "non_additive", "non_additive_dimensions": []},
                  ratio={"denominator_field": "events", "denominator_aggregation": ["sum"],
                         "cohort_policy": "paired_non_null"})
    with pytest.raises(KnowledgeValidationError, match="Ratios require"):
        validate_metric_payload(metric, fields)


def test_version_two_preserves_filters_time_and_explicit_cohort(metric, fields):
    metric["filters"] = [
        {"field": "team", "op": "eq", "value": ""},
        {"field": "amount", "op": "gte", "value": 0},
        {"field": "active", "op": "eq", "value": False},
    ]
    validated = validate_metric_payload(metric, fields)
    assert validated == metric
    assert validated["filters"][0]["value"] == ""
    assert validated["filters"][1]["value"] == 0
    assert validated["filters"][2]["value"] is False


def test_null_time_is_intentional_and_not_invented(metric, fields):
    metric["time"] = None
    assert validate_metric_payload(metric, fields)["time"] is None


def test_current_field_identity_and_source_units_are_exact(metric, fields):
    metric["dimensions"] = ["Team"]
    with pytest.raises(KnowledgeValidationError, match="absent"):
        validate_metric_payload(metric, fields)
    metric["dimensions"] = ["team"]
    metric["field_units"] = {"amount": "EUR"}
    with pytest.raises(KnowledgeValidationError, match="contradicts"):
        validate_metric_payload(metric, fields)


def test_numeric_difference_and_scaling_have_explicit_units(metric, fields):
    metric["field_units"] = {"amount": "USD", "expense": "USD"}
    metric["expression"] = {"op": "multiply", "left": {
        "op": "subtract", "left": {"op": "field", "field": "amount"},
        "right": {"op": "field", "field": "expense"}},
        "right": {"op": "literal", "value": 0.5}}
    result = validate_metric_payload(metric, fields)
    assert result["expression"]["right"]["unit"] == "1"
    assert result["unit"] == "USD"


def test_aligned_ratio_preserves_units_and_never_becomes_average_of_ratios(metric, fields):
    metric.update(unit="USD/records", field_units={"amount": "USD", "events": "records"})
    metric["additivity"] = {"kind": "non_additive", "non_additive_dimensions": []}
    metric["ratio"] = {"denominator_field": "events", "denominator_aggregation": "sum",
                       "cohort_policy": "paired_non_null"}
    assert validate_metric_payload(metric, fields)["ratio"] == metric["ratio"]
    metric["aggregation"] = "avg"
    with pytest.raises(KnowledgeValidationError, match="Ratios"):
        validate_metric_payload(metric, fields)


@pytest.mark.parametrize("mutation", [
    lambda m: m.update(schema_version="metric-v3"),
    lambda m: m.update(sql="SELECT secret FROM source"),
    lambda m: m.update(dimensions=["team", "team"]),
    lambda m: m.update(time={"field": "amount", "grain": "day", "timezone": "UTC"}),
    lambda m: m.update(time={"field": "date", "grain": "hour", "timezone": "UTC"}),
    lambda m: m.update(time={"field": "date", "grain": "day", "timezone": "Not/AZone"}),
    lambda m: m.update(cohort={"description": "Unknown key grain.", "key_fields": []}),
    lambda m: m.update(additivity={"kind": "semi_additive", "non_additive_dimensions": []}),
    lambda m: m.update(additivity={"kind": "non_additive", "non_additive_dimensions": []}),
    lambda m: m.update(filters=[{"field": "amount", "op": "eq", "value": True}]),
    lambda m: m.update(filters=[{"field": "amount", "op": "eq", "value": float("inf")}]),
    lambda m: m.update(filters=[{"field": "events", "op": "eq", "value": 1.5}]),
    lambda m: m.update(filters=[{"field": "team", "op": "eq", "value": None}]),
    lambda m: m.update(filters=[{"field": "team", "op": "gt", "value": "B"}]),
    lambda m: m.update(filters=[{"field": "amount", "op": "is_null", "value": 0}]),
    lambda m: m.update(filters=[{"field": "amount", "op": ["eq"], "value": 1}]),
    lambda m: m.update(filters=[{"field": "occurred_at", "op": "gte", "value": "2026-10-03T12:00:00"}]),
    lambda m: m.update(ratio=None),
])
def test_unsupported_or_ambiguous_semantics_fail_closed(metric, fields, mutation):
    mutation(metric)
    with pytest.raises(KnowledgeValidationError):
        validate_metric_payload(metric, fields)


@pytest.mark.parametrize("expression,units", [
    ({"op": "add", "left": {"op": "field", "field": "amount"}, "right": {"op": "field", "field": "events"}}, {"amount": "USD", "events": "records"}),
    ({"op": "divide", "left": {"op": "field", "field": "amount"}, "right": {"op": "field", "field": "expense"}}, {"amount": "USD", "expense": "USD"}),
    ({"op": "multiply", "left": {"op": "field", "field": "amount"}, "right": {"op": "field", "field": "expense"}}, {"amount": "USD", "expense": "USD"}),
    ({"op": "divide", "left": {"op": "field", "field": "amount"}, "right": {"op": "literal", "value": 0}}, {"amount": "USD"}),
    ({"op": "field", "field": "team"}, {"team": "1"}),
    ({"op": "function", "name": "execute"}, {}),
])
def test_expression_units_types_and_operation_subset_are_enforced(metric, fields, expression, units):
    metric.update(expression=expression, field_units=units)
    with pytest.raises(KnowledgeValidationError):
        validate_metric_payload(metric, fields)


def test_ast_and_payload_budgets_are_explicit(metric, fields):
    metric["field_units"] = {"amount": "USD"}
    expression = {"op": "field", "field": "amount"}
    for _ in range(7):
        expression = {"op": "multiply", "left": expression, "right": {"op": "literal", "value": 1}}
    metric["expression"] = expression
    with pytest.raises(KnowledgeValidationError, match="AST budget"):
        validate_metric_payload(metric, fields)
    metric.pop("expression")
    metric["filters"] = [{"field": "team", "op": "in", "value": ["x" * 500] * 20}] * 16
    with pytest.raises(KnowledgeValidationError, match="16 KiB"):
        validate_metric_payload(metric, fields)


def test_changed_units_expression_time_or_cohort_invalidate_content_digest(metric, fields):
    original = metric_payload_digest(validate_metric_payload(metric, fields))
    changed = copy.deepcopy(metric)
    changed["cohort"]["description"] += " Exclude archived events."
    assert metric_payload_digest(validate_metric_payload(changed, fields)) != original
    changed = copy.deepcopy(metric)
    changed["time"]["timezone"] = "UTC"
    assert metric_payload_digest(validate_metric_payload(changed, fields)) != original


def test_unknown_or_inconsistent_native_numeric_type_is_refused(metric, fields):
    fields[1].update(data_type="unknown", native_type=None)
    with pytest.raises(KnowledgeValidationError, match="numeric"):
        validate_metric_payload(metric, fields)
    fields[1].update(data_type="number", native_type="text")
    with pytest.raises(KnowledgeValidationError, match="disagree"):
        validate_metric_payload(metric, fields)
