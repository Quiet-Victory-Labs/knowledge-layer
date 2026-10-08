"""Exact numeric equality and truthful retention for bounded file evidence."""

from __future__ import annotations

import json
from decimal import Decimal, localcontext

import pytest

from knowledge_layer.connectors import local_files
from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.curation import curate_source


def _number(value):
    return json.loads(value, parse_float=Decimal)


@pytest.mark.parametrize(("left", "right"), [
    ("1", "1.00"), ("1000", "1e3"), ("1e400", "10e399"),
    ("1e-400", "10e-401"), ("0", "-0.0000"),
])
def test_equal_json_numbers_share_exact_distinct_and_probe_keys(left, right):
    first, second = _number(left), _number(right)
    assert local_files._canonical_value(first) == local_files._canonical_value(second)
    assert local_files._distinct_value(first) == local_files._distinct_value(second)


@pytest.mark.parametrize(("left", "right"), [
    ("true", "1"), ("false", "0"), ('"1"', "1"), ('"True"', "true"),
    ("1e400", "2e400"), ("1e-400", "2e-400"), ("0", "1e-1000000000"),
    ("1.00000000000000000000000000000001", "1.00000000000000000000000000000002"),
])
def test_exact_keys_keep_types_and_unrounded_numbers_distinct(left, right):
    first, second = _number(left), _number(right)
    with localcontext() as context:
        context.prec = 2
        assert local_files._canonical_value(first) != local_files._canonical_value(second)
        assert local_files._distinct_value(first) != local_files._distinct_value(second)
    assert len(local_files._canonical_value(second)) < 120


def test_structured_distinct_values_preserve_recursive_json_equality():
    first = {"nested": [Decimal("1.000"), {"value": Decimal("1e400")}], "enabled": True}
    equal = {"enabled": True, "nested": [1, {"value": Decimal("10e399")} ]}
    textual = {"enabled": True, "nested": ["1", {"value": Decimal("1e400")} ]}
    assert local_files._canonical_value(first) == local_files._canonical_value(equal)
    assert local_files._distinct_value(first) != local_files._distinct_value(textual)


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), float("inf"), float("nan")])
def test_nonfinite_internal_numbers_cannot_form_key_evidence(value):
    with pytest.raises(ConnectorError) as error:
        local_files._canonical_value(value)
    assert error.value.code == "invalid_json_number"


@pytest.mark.parametrize("suffix", ["json", "jsonl"])
def test_numeric_cardinality_does_not_certify_unique_representations(tmp_path, suffix):
    rows = [{"record_id": token} for token in ("1", "1.0", "1.00", "true", '"1"')]
    raw_rows = ['{"record_id":' + row["record_id"] + '}' for row in rows]
    body = "\n".join(raw_rows) if suffix == "jsonl" else "[" + ",".join(raw_rows) + "]"
    (tmp_path / f"records.{suffix}").write_text(body)
    snapshot = curate_source("local_files", {"root": str(tmp_path)})
    assert snapshot["discovery_complete"] is True
    profile = snapshot["assets"][0]["profile"]
    assert profile["distinct_counts"]["record_id"] == 3
    entity = next(concept for concept in snapshot["concepts"] if concept["semantic_type"] == "business_entity")
    assert entity["grain"]["observed_unique_key_fields"] == []
    assert entity["grain"]["candidate_key_fields"][0]["status"] == "candidate_by_name"


@pytest.mark.parametrize(("source", "target", "matches"), [
    ("1", "1.00", 1), ("1e400", "10e399", 1), ("1e400", "2e400", 0),
    ("1e-400", "2e-400", 0), ("0", "1e-1000000000", 0),
    ("1.00000000000000000000000000000001", "1.00000000000000000000000000000002", 0),
    ("true", "1", 0), ('"1"', "1", 0),
])
def test_bounded_jsonl_overlap_uses_exact_numeric_value_equality(tmp_path, source, target, matches):
    (tmp_path / "events.jsonl").write_text('{"record_id":' + source + '}\n')
    (tmp_path / "registry.jsonl").write_text('{"record_id":' + target + '}\n')
    discovery = local_files.discover({"root": str(tmp_path)})
    probe = next(item for item in discovery["join_probes"] if item["source_asset_id"] == discovery["assets"][0]["id"])
    assert probe["matched_source_rows"] == matches
    assert probe["source_nonnull_rows_probed"] == 1
    assert probe["target_distinct_values_probed"] == 1
    assert probe["complete_source_and_target"] is True
    assert probe["raw_values_retained"] is False
    assert all(asset["profile"]["content_retained"] is False for asset in discovery["assets"])


@pytest.mark.parametrize("suffix", ["json", "jsonl"])
@pytest.mark.parametrize("retain", [False, True])
def test_json_semantic_sample_retention_is_opted_in_and_disclosed(tmp_path, suffix, retain):
    rows = [{"entity_id": "private-source-id", "label": "Private source label"},
            {"subject_id": "private-source-id", "predicate": "belongs_to", "parent_id": "private-parent-id"}]
    body = "\n".join(json.dumps(row) for row in rows) if suffix == "jsonl" else json.dumps(rows)
    (tmp_path / f"entities.{suffix}").write_text(body)
    config = {"root": str(tmp_path), "retain_semantic_samples": retain}
    snapshot = curate_source("local_files", config)
    assert snapshot["discovery_complete"] is True
    profile = snapshot["assets"][0]["profile"]
    assert profile["content_retained"] is retain
    disclosed = [row["details"]["source_values_retained"] for row in snapshot["evidence"]
                 if row["evidence_type"] in {"asset_metadata", "schema_and_profile"}]
    assert disclosed and all(value is retain for value in disclosed)
    assert ("private-source-id" in json.dumps(snapshot)) is retain
    assert ("Private source label" in json.dumps(snapshot)) is retain
    if retain:
        assert profile["semantic_samples"]["entities"] == [rows[0]]
        assert profile["semantic_samples"]["edges"] == [rows[1]]
    else:
        assert "semantic_samples" not in profile


def test_retention_opt_in_without_sample_content_does_not_claim_retained_values(tmp_path):
    (tmp_path / "measurements.jsonl").write_text('{"temperature":12}\n')
    snapshot = curate_source("local_files", {"root": str(tmp_path), "retain_semantic_samples": True})
    assert snapshot["assets"][0]["profile"]["content_retained"] is False
    assert all(not row["details"]["source_values_retained"] for row in snapshot["evidence"]
               if row["evidence_type"] in {"asset_metadata", "schema_and_profile"})


@pytest.mark.parametrize("suffix", ["json", "jsonl"])
def test_long_same_prefix_identifiers_refuse_instead_of_collapsing_entities(tmp_path, suffix):
    prefix = "private-collision-prefix-" + "x" * 120
    rows = [{"entity_id": prefix + suffix, "label": "Example"} for suffix in ("-a", "-b")]
    body = "\n".join(json.dumps(row) for row in rows) if suffix == "jsonl" else json.dumps(rows)
    (tmp_path / f"entities.{suffix}").write_text(body)
    config = {"root": str(tmp_path)}
    off = curate_source("local_files", config)
    assert off["discovery_complete"] is True
    assert off["assets"][0]["profile"]["content_retained"] is False
    refused = curate_source("local_files", {**config, "retain_semantic_samples": True})
    assert refused["discovery_complete"] is False
    assert refused["errors"][0]["code"] == "semantic_sample_limit_exceeded"
    assert not any(concept["semantic_type"] == "source_entity" for concept in refused["concepts"])
    assert "private-collision-prefix" not in json.dumps(refused)


@pytest.mark.parametrize(("field", "limit"), [("entity_id", 120), ("label", 120),
                                               ("subject_id", 120), ("parent_id", 120), ("predicate", 80)])
def test_every_eligible_semantic_string_limit_refuses_oversize_without_echo(tmp_path, field, limit):
    row = ({"entity_id": "child", "label": "Example"} if field in {"entity_id", "label"}
           else {"subject_id": "child", "parent_id": "parent", "predicate": "belongs_to"})
    row[field] = "private-overlong-" + "x" * limit
    (tmp_path / "entities.jsonl").write_text(json.dumps(row) + "\n")
    snapshot = curate_source("local_files", {"root": str(tmp_path), "retain_semantic_samples": True})
    assert snapshot["discovery_complete"] is False
    assert snapshot["errors"][0]["code"] == "semantic_sample_limit_exceeded"
    assert "private-overlong" not in json.dumps(snapshot)


def test_spaced_semantic_identifiers_remain_exact_and_disclosed(tmp_path):
    rows = [{"entity_id": " child ", "label": " Example "},
            {"entity_id": "child", "label": "Other identity"},
            {"entity_id": " parent ", "label": "Parent"},
            {"subject_id": " child ", "predicate": "belongs_to", "parent_id": " parent "}]
    (tmp_path / "entities.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    snapshot = curate_source("local_files", {"root": str(tmp_path), "retain_semantic_samples": True})
    assert snapshot["discovery_complete"] is True
    profile = snapshot["assets"][0]["profile"]
    assert profile["content_retained"] is True
    assert profile["semantic_samples"]["entities"] == rows[:3]
    assert profile["semantic_samples"]["edges"] == [rows[3]]
    entities = [concept for concept in snapshot["concepts"] if concept["semantic_type"] == "source_entity"]
    assert {entity["source_identifier"] for entity in entities} == {" child ", "child", " parent "}
    spaced = next(entity for entity in entities if entity["source_identifier"] == " child ")
    assert spaced["name"] == " Example "
    assert all(row["details"]["source_values_retained"] for row in snapshot["evidence"]
               if row["evidence_type"] in {"asset_metadata", "schema_and_profile"})


def test_semantic_sample_budget_reports_only_considered_rows(tmp_path):
    rows = [{"entity_id": f"source-entity-{index}", "label": f"Entity {index}"} for index in range(100)]
    (tmp_path / "entities.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    snapshot = curate_source("local_files", {"root": str(tmp_path), "profile_rows": 100,
                                            "retain_semantic_samples": True})
    assert snapshot["discovery_complete"] is True
    profile = snapshot["assets"][0]["profile"]
    assert profile["row_count"] == profile["profile_rows"] == 100
    samples = profile["semantic_samples"]
    assert samples["sampled_rows"] == samples["row_limit"] == 50
    assert samples["entities"] == rows[:50]
    assert samples["is_sampled"] is True and samples["truncated"] is True
    assert profile["content_retained"] is True
    assert sum(concept["semantic_type"] == "source_entity" for concept in snapshot["concepts"]) == 50
