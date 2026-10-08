"""Cross-source regressions using synthetic metadata and policy fixtures."""

import json
from copy import deepcopy

import httpx
import pytest
from adapter_response_fixture import streamed_response

from knowledge_layer.connectors import external_adapter
from knowledge_layer.connectors.common import ConnectorError, stable_id, type_name
from knowledge_layer.curation import curate_source
from knowledge_layer.store import (
    KnowledgeStore,
    KnowledgeValidationError,
    validate_connection_config,
)


def _adapter(monkeypatch, payload, *, version="1.1"):
    monkeypatch.setenv("KNOWLEDGE_SOURCE_ENTERPRISE", json.dumps({
        "endpoint": "https://adapter.example/discover", "provider": "enterprise",
        "allowed_scopes": ["catalog"], "bearer_token": "not-source-evidence",
        "protocol_version": version,
    }))
    real_client = httpx.Client

    def respond(request):
        body = json.loads(request.content)
        assert body["protocol_version"] == version
        assert "asset_names" not in body
        return streamed_response(200, json=payload)

    monkeypatch.setattr(external_adapter.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    return {"secret_ref": "KNOWLEDGE_SOURCE_ENTERPRISE", "scope": "catalog"}  # pragma: allowlist secret - operator reference


def _payload():
    return {"protocol_version": "1.1", "listing_complete": True, "assets": [{
        "name": "Readings", "pointer": "measurements/readings", "revision": "unchanged-vendor-hint",
        "schema": [
            {"name": "sensor_id", "data_type": "bigint", "nullable": False, "field_id": 1},
            {"name": "temperature", "data_type": "decimal(10,2)", "nullable": True, "field_id": 2},
        ],
        "observations": {"schema_id": 4, "snapshot_id": 19, "schema_fingerprint": "a" * 64,
                         "row_count": 0, "row_count_basis": "snapshot_summary",
                         "primary_key": ["sensor_id"], "foreign_keys": [],
                         "source_observed_at": "2026-10-02T23:00:00Z", "lineage": ["raw/sensors"]},
    }]}


def _selection_payload():
    payload = _payload()
    other = deepcopy(payload["assets"][0])
    other.update(name="Other readings", pointer="measurements/other", revision="other-revision")
    payload["assets"].append(other)
    return payload


def test_asset_selection_preserves_every_selected_field_fact_and_declares_its_boundary(monkeypatch):
    payload = _selection_payload()
    payload["assets"][0]["schema"].extend(
        {"name": f"measure_{index}", "native_type": "numeric(18,9)", "nullable": None, "field_id": index + 1}
        for index in range(2, 67)
    )
    config = _adapter(monkeypatch, payload)
    full = external_adapter.discover(config)
    selected = external_adapter.discover({**config, "asset_names": ["Readings"]})
    assert selected["assets"] == [next(asset for asset in full["assets"] if asset["name"] == "Readings")]
    assert len(selected["assets"][0]["schema"]) == 67
    assert selected["assets"][0]["schema"][-1]["nullable"] is None
    assert selected["assets"][0]["observations"] == full["assets"][1]["observations"]
    assert selected["source"]["asset_selection"] == {
        "asset_names": ["Readings"], "complete": True, "completeness_scope": "requested_asset_names",
    }
    assert selected["source"]["location"] == full["source"]["location"]
    assert selected["capabilities"]["query_execution"] is False
    snapshot = curate_source("external_adapter", {**config, "asset_names": ["Readings"]})
    assert snapshot["discovery_complete"] is True
    assert snapshot["agent_ready"] is False
    assert snapshot["source"]["asset_selection"] == selected["source"]["asset_selection"]
    assert "not-source-evidence" not in json.dumps(snapshot)
    assert "https://adapter.example" not in json.dumps(snapshot)


def test_asset_selection_does_not_change_the_legacy_unselected_contract(monkeypatch):
    payload = _selection_payload()
    config = _adapter(monkeypatch, payload)
    normalized = validate_connection_config("external_adapter", config)
    assert normalized == {**config, "max_assets": 100, "max_columns": 5000, "retain_semantic_samples": False}
    full = external_adapter.discover(normalized)
    assert len(full["assets"]) == 2
    assert "asset_selection" not in full["source"]
    assert full["source"]["revision"] == stable_id(
        "revision", *[(asset["source_pointer"], asset["revision"]) for asset in full["assets"]],
    )


@pytest.mark.parametrize("names", [None, "Readings", [], ["Readings", "Readings"], [""], [" "],
                                    ["Readings\n"], [1], [{"name": "Readings"}], ["x" * 121],
                                    [f"asset-{index}" for index in range(101)]])
def test_invalid_asset_selection_refuses_before_profile_or_network_access(monkeypatch, names):
    config = {"secret_ref": "KNOWLEDGE_SOURCE_ENTERPRISE", "scope": "catalog", "asset_names": names}  # pragma: allowlist secret - operator reference
    monkeypatch.setattr(external_adapter, "_profile", lambda *_args: pytest.fail("Invalid selection must not read a profile"))
    with pytest.raises(ConnectorError) as error:
        external_adapter.discover(config)
    assert error.value.code == "invalid_asset_selection"
    with pytest.raises(KnowledgeValidationError, match="asset_names must contain"):
        validate_connection_config("external_adapter", config)


def test_exact_asset_selection_keeps_whitespace_unicode_and_case_without_normalizing(monkeypatch):
    payload = _payload()
    payload["assets"][0]["name"] = "  Readings Ω  "
    config = _adapter(monkeypatch, payload)
    normalized = validate_connection_config("external_adapter", {**config, "asset_names": ["  Readings Ω  "]})
    assert normalized["asset_names"] == ["  Readings Ω  "]
    assert external_adapter.discover(normalized)["assets"][0]["name"] == "  Readings Ω  "
    for wrong_name in ("Readings Ω", "  readings Ω  "):
        with pytest.raises(ConnectorError) as error:
            external_adapter.discover({**config, "asset_names": [wrong_name]})
        assert error.value.code == "adapter_asset_not_found"
    assert len(external_adapter.validate_asset_names([f"asset-{index}" for index in range(100)])) == 100


@pytest.mark.parametrize("names", [["Readings", "absent"], ["not-source-evidence"], ["readings"]])
def test_missing_requested_asset_refuses_the_whole_selection_without_echoing_profile_secrets(monkeypatch, names):
    config = _adapter(monkeypatch, _selection_payload())
    with pytest.raises(ConnectorError) as error:
        external_adapter.discover({**config, "asset_names": names})
    assert error.value.code == "adapter_asset_not_found"
    assert "not-source-evidence" not in str(error.value)
    assert "https://adapter.example" not in str(error.value)


def test_duplicate_asset_names_are_ambiguous_only_when_requested_and_never_silently_selected(monkeypatch):
    payload = _selection_payload()
    duplicate = deepcopy(payload["assets"][1])
    duplicate["pointer"] = "measurements/duplicate"
    payload["assets"].append(duplicate)
    config = _adapter(monkeypatch, payload)
    assert len(external_adapter.discover(config)["assets"]) == 3
    assert len(external_adapter.discover({**config, "asset_names": ["Readings"]})["assets"]) == 1
    with pytest.raises(ConnectorError) as error:
        external_adapter.discover({**config, "asset_names": ["Other readings"]})
    assert error.value.code == "adapter_asset_name_ambiguous"


@pytest.mark.parametrize("failure,code", [
    ("assets", "asset_limit_exceeded"), ("columns", "column_limit_exceeded"),
    ("schema", "adapter_response_invalid"), ("observations", "adapter_response_invalid"),
    ("foreign_key", "adapter_response_invalid"), ("incomplete", "adapter_listing_incomplete"),
    ("response_size", "adapter_response_too_large"),
])
def test_selection_cannot_hide_invalid_unselected_metadata_or_bypass_full_response_budgets(monkeypatch, failure, code):
    payload = _selection_payload()
    config = {**_adapter(monkeypatch, payload), "asset_names": ["Readings"]}
    if failure == "assets":
        config["max_assets"] = 1
    elif failure == "columns":
        config["max_columns"] = 2
    elif failure == "schema":
        payload["assets"][1]["schema"][0]["nullable"] = "unknown"
    elif failure == "observations":
        payload["assets"][1]["observations"]["row_count"] = -1
    elif failure == "foreign_key":
        payload["assets"][0]["observations"]["foreign_keys"] = [{
            "columns": ["sensor_id"], "target_pointer": "measurements/other", "target_columns": ["absent"],
        }]
    elif failure == "incomplete":
        payload["listing_complete"] = False
    else:
        payload["ignored"] = "x" * external_adapter.MAX_RESPONSE_BYTES
    with pytest.raises(ConnectorError) as error:
        external_adapter.discover(config)
    assert error.value.code == code


def test_selection_remains_within_operator_allowed_scope(monkeypatch):
    config = _adapter(monkeypatch, _selection_payload())
    with pytest.raises(ConnectorError) as error:
        external_adapter.discover({**config, "scope": "outside", "asset_names": ["Readings"]})
    assert error.value.code == "adapter_scope_denied"


def test_selection_revision_binds_the_canonical_allowlist_and_ignores_unselected_changes(monkeypatch):
    payload = _selection_payload()
    config = _adapter(monkeypatch, payload)
    selected_config = {**config, "asset_names": ["Readings"]}
    before = curate_source("external_adapter", selected_config)
    payload["assets"][1]["revision"] = "changed-other-revision"
    unchanged = curate_source("external_adapter", selected_config)
    assert unchanged["snapshot_id"] == before["snapshot_id"]
    assert unchanged["source"]["revision"] == before["source"]["revision"]
    all_config = {**config, "asset_names": ["Readings", "Other readings"]}
    selected_all = curate_source("external_adapter", all_config)
    reordered = curate_source("external_adapter", {**all_config, "asset_names": list(reversed(all_config["asset_names"]))})
    assert selected_all["snapshot_id"] != before["snapshot_id"]
    assert selected_all["snapshot_id"] == reordered["snapshot_id"]
    full = external_adapter.discover(config)
    explicit_all = external_adapter.discover(all_config)
    assert full["assets"] == explicit_all["assets"]
    assert full["source"]["revision"] != explicit_all["source"]["revision"]


def test_registry_retains_selection_and_stable_content_identity_on_explicit_refresh(monkeypatch):
    payload = _selection_payload()
    config = {**_adapter(monkeypatch, payload), "asset_names": ["Readings"]}
    store = KnowledgeStore(database_url="sqlite:///:memory:")
    try:
        options = next(item for item in store.connector_types() if item["id"] == "external_adapter")
        assert next(field for field in options["config_fields"] if field["name"] == "asset_names")["required"] is False
        source = store.create_connection("demo", kind="external_adapter", name="Scoped readings", config=config)
        assert source["config"]["asset_names"] == ["Readings"]
        first = store.refresh_connection("demo", source["id"])
        assert [asset["name"] for asset in first["snapshot"]["assets"]] == ["Readings"]
        assert first["snapshot"]["discovery_complete"] is True
        payload["assets"][1]["revision"] = "unrelated-domain-change"
        again = store.refresh_connection("demo", source["id"])
        assert again["snapshot"]["id"] == first["snapshot"]["id"]
        # An explicit refresh remains a new registry version; the selected
        # content identity does not change with an unrelated source asset.
        assert again["snapshot"]["version"] == first["snapshot"]["version"] + 1
        assert again["snapshot"]["source"]["asset_selection"]["completeness_scope"] == "requested_asset_names"
    finally:
        store.close()


def test_rich_metadata_survives_curation_without_certifying_meaning(monkeypatch):
    config = _adapter(monkeypatch, _payload())
    snapshot = curate_source("external_adapter", config)
    assert snapshot["discovery_complete"] is True
    assert snapshot["curation_complete"] is True
    asset = snapshot["assets"][0]
    assert asset["schema"][1]["native_type"] == "decimal(10,2)"
    assert asset["schema"][1]["field_id"] == 2
    assert asset["observations"]["row_count"] == 0
    assert asset["observations"]["row_count_basis"] == "snapshot_summary"
    assert asset["profile"]["profile_rows"] == 0
    evidence = next(row for row in snapshot["evidence"] if row["evidence_type"] == "asset_metadata")
    assert evidence["details"]["observations"]["primary_key"] == ["sensor_id"]
    field = next(row for row in snapshot["evidence"] if row["details"].get("field") == "temperature")
    assert field["details"]["native_type"] == "decimal(10,2)"
    assert snapshot["agent_ready"] is False and snapshot["meaning_certified"] is False
    assert "not-source-evidence" not in json.dumps(snapshot)


@pytest.mark.parametrize("change", ["physical_type", "field_id", "schema_id", "nested_schema", "key"])
def test_unchanged_vendor_hint_cannot_hide_metadata_drift(monkeypatch, change):
    payload = _payload()
    config = _adapter(monkeypatch, payload)
    before = curate_source("external_adapter", config)
    if change == "physical_type":
        payload["assets"][0]["schema"][1]["data_type"] = "decimal(18,6)"
    elif change == "field_id":
        payload["assets"][0]["schema"][1]["field_id"] = 3
    elif change == "schema_id":
        payload["assets"][0]["observations"]["schema_id"] = 5
    elif change == "nested_schema":
        payload["assets"][0]["observations"]["schema_fingerprint"] = "b" * 64
    else:
        payload["assets"][0]["observations"]["primary_key"] = None
    after = curate_source("external_adapter", {**config, "previous_snapshot": before})
    assert after["assets"][0]["revision"] != before["assets"][0]["revision"]
    assert after["drift"]["changed_assets"]
    assert after["evidence"][0]["id"] != before["evidence"][0]["id"]


def test_observation_clock_does_not_invent_schema_drift(monkeypatch):
    payload = _payload()
    config = _adapter(monkeypatch, payload)
    before = curate_source("external_adapter", config)
    payload["assets"][0]["observations"]["source_observed_at"] = "2026-10-03T00:00:00Z"
    after = curate_source("external_adapter", config)
    assert after["assets"][0]["revision"] == before["assets"][0]["revision"]
    assert after["assets"][0]["observations"]["source_observed_at"] != before["assets"][0]["observations"]["source_observed_at"]


def test_absent_nullability_counts_and_keys_remain_unknown(monkeypatch):
    payload = _payload()
    del payload["assets"][0]["schema"][0]["nullable"]
    del payload["assets"][0]["observations"]
    asset = external_adapter.discover(_adapter(monkeypatch, payload))["assets"][0]
    assert asset["schema"][0]["nullable"] is None
    assert asset["profile"]["row_count"] is None
    assert asset["profile"]["row_count_basis"] == "unknown"
    assert "primary_key" not in asset["observations"]


@pytest.mark.parametrize("mode", ["missing", "false", "cursor"])
def test_paginated_or_incomplete_listings_refuse_deletion_inference(monkeypatch, mode):
    payload = _payload()
    if mode == "missing":
        del payload["listing_complete"]
    elif mode == "false":
        payload["listing_complete"] = False
    else:
        payload["next_cursor"] = "another-page"
    config = _adapter(monkeypatch, payload)
    snapshot = curate_source("external_adapter", config)
    assert snapshot["status"] == "error"
    assert snapshot["errors"][0]["code"] == "adapter_listing_incomplete"
    assert snapshot["discovery_complete"] is False


@pytest.mark.parametrize("observation", [
    {"row_count": -1, "row_count_basis": "estimate"},
    {"row_count": 0},
    {"row_count_basis": "exact"},
    {"row_count": True, "row_count_basis": "exact"},
    {"primary_key": ["missing_id"]},
    {"primary_key": ["sensor_id", "sensor_id"]},
    {"source_observed_at": "2026-10-02T12:00:00"},
    {"lineage": ["https://source.example/signed?token=secret"]},
    {"foreign_keys": [{"columns": ["sensor_id"], "target_pointer": "sensors", "target_columns": []}]},
    {"raw_rows": [{"sensor_id": "private"}]},
])
def test_invalid_or_sensitive_metadata_refuses_without_echo(monkeypatch, observation):
    payload = _payload()
    payload["assets"][0]["observations"] = observation
    with pytest.raises(ConnectorError) as caught:
        external_adapter.discover(_adapter(monkeypatch, payload))
    assert caught.value.code == "adapter_response_invalid"
    assert "private" not in caught.value.public_message and "secret" not in caught.value.public_message


def test_declared_adapter_fk_is_technical_evidence_within_selected_scope(monkeypatch):
    payload = _payload()
    payload["assets"][0]["observations"]["foreign_keys"] = [{
        "columns": ["sensor_id"], "target_pointer": "sensors", "target_columns": ["sensor_id"],
    }]
    payload["assets"].append({"name": "Sensors", "pointer": "sensors", "schema": [
        {"name": "sensor_id", "data_type": "bigint", "nullable": False},
    ]})
    config = _adapter(monkeypatch, payload)
    snapshot = curate_source("external_adapter", config)
    relation = next(row for row in snapshot["relationships"] if row["predicate"] == "declared_foreign_key")
    assert relation["status"] == "observed_metadata"
    assert snapshot["meaning_certified"] is False
    assert not any(row.get("status") == "approved" for row in snapshot["proposals"])
    payload["assets"][0]["observations"]["foreign_keys"][0]["target_columns"] = ["nonexistent_key"]
    refused = curate_source("external_adapter", config)
    assert refused["errors"][0]["code"] == "adapter_response_invalid"


@pytest.mark.parametrize(("native", "normalized"), [
    ("interval day to second", "unknown"), ("point", "unknown"),
    ("Enum8('int'=1,'string'=2)", "text"), ("character varying(255)", "text"),
    ("Nullable(Decimal(18,6))", "number"), ("LowCardinality(Nullable(String))", "text"),
    ("UInt64", "integer"), ("Date32", "date"),
    ("timestamp[us, tz=UTC]", "timestamp"), ("datetime64(3, 'UTC')", "timestamp"),
    ("struct<4: value: optional int>", "structured"), ("map<string, int>", "structured"),
    ("BIGNUMERIC", "number"), ("integer[]", "structured"),
])
def test_native_type_families_do_not_confuse_substrings(native, normalized):
    assert type_name(native) == normalized


def _discovery(schema, profile=None):
    return {"source": {"id": "source", "name": "Enterprise", "kind": "external_adapter", "revision": "r1"},
            "assets": [{"id": "asset", "name": "Observations", "asset_type": "table",
                        "source_pointer": "adapter/test/scope/observations", "revision": "r1",
                        "schema": schema, "profile": profile or {}}], "capabilities": {}, "errors": []}


def test_measure_candidates_generalize_without_definitions_or_numeric_ids(monkeypatch):
    from knowledge_layer import curation

    names = ["actual_revenue", "temperature", "latency_ms", "volume", "cost_id", "score_code", "season", "week"]
    discovery = _discovery([{"name": name, "data_type": "number", "nullable": None} for name in names])
    monkeypatch.setitem(curation.CONNECTORS, "external_adapter", lambda _config: discovery)
    snapshot = curate_source("external_adapter", {})
    assert {metric["field"] for metric in snapshot["metrics"]} == {"actual_revenue", "temperature", "latency_ms", "volume"}
    assert all(metric["status"] == "candidate" and metric["certified"] is False for metric in snapshot["metrics"])
    assert all("aggregation" not in metric and "definition" not in metric for metric in snapshot["metrics"])


def test_large_schema_preserves_every_field_and_explicitly_refuses_over_budget(monkeypatch):
    from knowledge_layer import curation

    discovery = _discovery([{"name": f"column_{index}", "data_type": "unknown"} for index in range(2101)])
    monkeypatch.setitem(curation.CONNECTORS, "external_adapter", lambda _config: discovery)
    snapshot = curate_source("external_adapter", {})
    fields = [item for item in snapshot["evidence"] if item["evidence_type"] == "schema_and_profile"]
    assert len(fields) == 2101 and snapshot["curation_complete"] is True
    discovery["assets"][0]["schema"] *= 5
    refused = curate_source("external_adapter", {})
    assert refused["status"] == "error"
    assert refused["errors"][0]["code"] == "curation_budget_exceeded"


def test_retained_source_assertion_is_not_lost_after_large_schema(monkeypatch):
    from knowledge_layer import curation

    discovery = _discovery([{"name": f"column_{index}", "data_type": "unknown"} for index in range(2101)],
                           {"semantic_samples": {"entities": [{"entity_id": "source-entity", "label": "Observed label"}],
                                                 "edges": []}, "content_retained": True})
    monkeypatch.setitem(curation.CONNECTORS, "external_adapter", lambda _config: discovery)
    snapshot = curate_source("external_adapter", {})
    assert snapshot["curation_complete"] is True
    assert any(item.get("source_identifier") == "source-entity" for item in snapshot["concepts"])
    assert any(item["evidence_type"] == "sampled_source_entity" for item in snapshot["evidence"])


def test_adapter_duplicate_keys_cannot_override_incomplete_listing(monkeypatch):
    real_client = httpx.Client
    config = _adapter(monkeypatch, _payload())
    monkeypatch.setattr(external_adapter.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda _request: streamed_response(200, content=
            b'{"protocol_version":"1.1","listing_complete":false,"listing_complete":true,"assets":[]}')),
        **kwargs))
    with pytest.raises(ConnectorError) as caught:
        external_adapter.discover(config)
    assert caught.value.code == "adapter_response_invalid"


@pytest.mark.parametrize("locator", [
    {"format": "htm", "coordinate_basis": "extracted_text", "extracted_line_start": 2,
     "extracted_line_end": 3, "source_node_line_start": 5, "source_node_line_end": 5,
     "column_start": 10, "column_end": 4},
    {"format": "pdf", "coordinate_basis": "extracted_text", "page_start": 1, "page_end": 2,
     "source_line_start": 1, "source_line_end": 2},
])
def test_adapter_preserves_valid_cross_format_document_locators(monkeypatch, locator):
    payload = _payload()
    payload["assets"][0].update(asset_type="document", schema=[],
        document_terms=[{"term": "Revenue", "excerpt": "  Revenue follows acceptance.  ", "source_locator": locator}],
        document_coverage={"extraction_complete": True, "extraction_truncated": False,
                           "retention_complete": True, "retention_truncated": False})
    del payload["assets"][0]["observations"]
    config = {**_adapter(monkeypatch, payload), "retain_semantic_samples": True}
    asset = external_adapter.discover(config)["assets"][0]
    description = asset["profile"]["document_signals"]["candidate_descriptions"][0]
    assert description["source_locator"] == locator
    assert description["excerpt"] == "  Revenue follows acceptance.  "


@pytest.mark.parametrize("locator", [
    {"format": "pdf", "source_line_start": 8, "source_line_end": 4},
    {"format": "html", "source_node_line_start": 8, "source_node_line_end": 4},
    {"format": "md", "line_start": 2, "line_end": 2, "column_start": 10, "column_end": 4},
])
def test_adapter_rejects_reversed_document_coordinates(locator):
    with pytest.raises(ConnectorError):
        external_adapter._document_locator(locator)


def test_distinct_declared_fk_targets_and_nested_scope_remain_distinct(monkeypatch):
    payload = _payload()
    payload["assets"][0]["observations"]["foreign_keys"] = [
        {"columns": ["sensor_id"], "target_pointer": "sensors", "target_columns": ["id"]},
        {"columns": ["sensor_id"], "target_pointer": "sensors", "target_columns": ["alternate_id"]},
    ]
    payload["assets"].append({"name": "Sensors", "pointer": "sensors", "schema": [
        {"name": name, "data_type": "long", "nullable": False} for name in ("id", "alternate_id")]})
    config = _adapter(monkeypatch, payload)
    monkeypatch.setenv("KNOWLEDGE_SOURCE_ENTERPRISE", json.dumps({
        "endpoint": "https://adapter.example/discover", "provider": "enterprise/north",
        "allowed_scopes": ["warehouse/analytics"], "protocol_version": "1.1"}))
    config["scope"] = "warehouse/analytics"
    snapshot = curate_source("external_adapter", config)
    assert snapshot["discovery_complete"] is True
    relationships = [item for item in snapshot["relationships"] if item.get("predicate") == "declared_foreign_key"]
    assert len(relationships) == 2
    assert len({item["id"] for item in relationships}) == 2


def test_truncated_document_evidence_blocks_publication_separately_from_inventory(monkeypatch):
    from knowledge_layer import curation

    discovery = _discovery([], {"document_extraction": {"truncated": True}})
    monkeypatch.setitem(curation.CONNECTORS, "external_adapter", lambda _config: discovery)
    snapshot = curate_source("external_adapter", {})
    assert snapshot["discovery_complete"] is True
    assert snapshot["curation_complete"] is False and snapshot["curation_status"] == "partial_curation"
    store = KnowledgeStore(database_url="sqlite:///:memory:")
    try:
        source = store.create_connection("demo", kind="external_adapter", name="Truncated", config={
            "secret_ref": "KNOWLEDGE_SOURCE_ENTERPRISE", "scope": "catalog",  # pragma: allowlist secret - operator reference
        })
        store.save_snapshot("demo", source["id"], deepcopy(snapshot))
        assert "curation_incomplete" in store.publication_preview("demo", source["id"])["blockers"]
    finally:
        store.close()


def test_registry_budget_counts_utf8_bytes_for_multilingual_source_documents():
    store = KnowledgeStore(database_url="sqlite:///:memory:")
    try:
        source = store.create_connection("demo", kind="local_files", name="International", config={})
        payload = {"status": "discovery_complete", "assets": [
            {"id": f"asset-{index}", "description": "漢" * 10000} for index in range(180)
        ]}
        with pytest.raises(KnowledgeValidationError, match="5 MiB"):
            store.save_snapshot("demo", source["id"], payload)
        assert store.get_snapshot("demo", source["id"]) is None
    finally:
        store.close()


def test_adapter_documents_bind_retained_chunks_and_declared_coverage_to_revision(monkeypatch):
    payload = {"protocol_version": "1.1", "listing_complete": True, "assets": [{
        "name": "Accounting policy", "pointer": "policy/accounting", "asset_type": "document",
        "revision": "unchanged", "document_coverage": {
            "extraction_complete": True, "extraction_truncated": False,
            "retention_complete": True, "retention_truncated": False,
        }, "document_terms": [{"term": "Policy", "excerpt": "Recognize revenue after delivery.",
            "source_locator": {"format": "pdf", "coordinate_basis": "extracted_page_text",
                               "page_start": 2, "page_end": 2, "extracted_line_start": 1,
                               "extracted_line_end": 1, "column_start": 1, "column_end": 33}}],
    }]}
    config = {**_adapter(monkeypatch, payload), "retain_semantic_samples": True}
    before = curate_source("external_adapter", config)
    assert before["curation_complete"] is True
    assert before["assets"][0]["profile"]["document_retention"]["complete"] is True
    payload["assets"][0]["document_terms"][0]["excerpt"] = "Recognize revenue after customer acceptance."
    after = curate_source("external_adapter", config)
    assert after["assets"][0]["revision"] != before["assets"][0]["revision"]
    assert after["evidence"][0]["id"] != before["evidence"][0]["id"]
    payload["assets"][0]["document_coverage"].update(retention_complete=False, retention_truncated=True)
    truncated = curate_source("external_adapter", config)
    assert truncated["curation_complete"] is False
    assert truncated["discovery_complete"] is True
    del payload["assets"][0]["document_coverage"]
    refused = curate_source("external_adapter", config)
    assert refused["errors"][0]["code"] == "adapter_response_invalid"


def test_declared_composite_keys_do_not_claim_each_component_unique(monkeypatch):
    from knowledge_layer import curation

    discovery = _discovery([{"name": name, "data_type": "text"} for name in ("country", "sequence")],
                           {"primary_key": ["country", "sequence"]})
    monkeypatch.setitem(curation.CONNECTORS, "external_adapter", lambda _config: discovery)
    snapshot = curate_source("external_adapter", {})
    entity = next(item for item in snapshot["concepts"] if item["semantic_type"] == "business_entity")
    assert {item["field"] for item in entity["grain"]["candidate_key_fields"]} == {"country", "sequence"}
    assert all(item["status"] == "declared_primary_key_component" for item in entity["grain"]["candidate_key_fields"])
    assert entity["grain"]["observed_unique_key_fields"] == []


def test_empty_profile_does_not_supply_observed_key_uniqueness(monkeypatch):
    from knowledge_layer import curation

    discovery = _discovery([{"name": "record_id", "data_type": "integer"}], {
        "row_count": 0, "row_count_is_complete": True, "profile_rows": 0,
        "null_counts": {"record_id": 0}, "distinct_counts": {"record_id": 0},
    })
    monkeypatch.setitem(curation.CONNECTORS, "external_adapter", lambda _config: discovery)
    snapshot = curate_source("external_adapter", {})
    entity = next(item for item in snapshot["concepts"] if item["semantic_type"] == "business_entity")
    assert entity["grain"]["observed_unique_key_fields"] == []
    assert entity["grain"]["candidate_key_fields"][0]["status"] == "candidate_by_name"


def test_unextractable_document_preserves_inventory_without_publishing_knowledge(monkeypatch):
    from knowledge_layer import curation

    discovery = _discovery([], {"document_extraction": {"complete": False, "truncated": False},
                               "document_retention": {"enabled": False, "complete": None}})
    monkeypatch.setitem(curation.CONNECTORS, "external_adapter", lambda _config: discovery)
    snapshot = curate_source("external_adapter", {})
    assert snapshot["discovery_complete"] is True
    assert snapshot["curation_complete"] is False
    assert snapshot["curation_errors"][0]["code"] == "document_evidence_unavailable"


def test_missing_object_size_is_unknown_not_an_empty_object(monkeypatch):
    pytest.importorskip("boto3")
    from knowledge_layer.connectors import object_store

    monkeypatch.setenv("KNOWLEDGE_SOURCE_OBJECTS", json.dumps({"bucket": "sample-bucket"}))

    class Client:
        def list_objects_v2(self, **_kwargs):
            return {"Contents": [{"Key": "events.jsonl"}], "IsTruncated": False}

        def close(self):
            pass

    monkeypatch.setattr(object_store.boto3, "client", lambda *_args, **_kwargs: Client())
    snapshot = object_store.discover({"secret_ref": "KNOWLEDGE_SOURCE_OBJECTS"})  # pragma: allowlist secret - operator reference
    assert snapshot["assets"][0]["profile"]["size_bytes"] is None


@pytest.mark.parametrize("suffix", ["json", "jsonl"])
def test_fields_after_profile_sample_are_discovered_and_missing_is_distinct(tmp_path, suffix):
    rows = [{"record_id": 0}, {"record_id": 1, "new_measure": 12}]
    body = json.dumps(rows) if suffix == "json" else "\n".join(json.dumps(row) for row in rows)
    (tmp_path / f"records.{suffix}").write_text(body, encoding="utf-8")
    snapshot = curate_source("local_files", {"root": str(tmp_path), "profile_rows": 1})
    assert snapshot["discovery_complete"] is True
    asset = snapshot["assets"][0]
    assert {field["name"] for field in asset["schema"]} == {"record_id", "new_measure"}
    assert asset["profile"]["row_count"] == 2 and asset["profile"]["profile_rows"] == 1
    assert asset["profile"]["null_counts"]["new_measure"] == 0
    assert asset["profile"]["missing_counts"]["new_measure"] == 1
    assert asset["profile"]["schema_type_basis"] == "bounded_profile"


def test_null_missing_empty_and_zero_have_separate_profile_counts(tmp_path):
    body = ('{"record_id":0,"value":0}\n{"record_id":1,"value":null}\n'
            '{"record_id":2}\n{"record_id":3,"value":""}')
    (tmp_path / "records.jsonl").write_text(body, encoding="utf-8")
    snapshot = curate_source("local_files", {"root": str(tmp_path)})
    assert snapshot["discovery_complete"] is True
    profile = snapshot["assets"][0]["profile"]
    assert profile["row_count"] == 4
    assert profile["null_counts"]["value"] == 1
    assert profile["missing_counts"]["value"] == 1
    assert profile["empty_counts"]["value"] == 1
    assert profile["distinct_counts"]["value"] == 2  # Observed zero and empty string.


@pytest.mark.parametrize(("filename", "body", "code"), [
    ("duplicate.csv", "id,id\n1,2\n", "invalid_column_names"),
    ("blank.csv", "id,\n1,2\n", "invalid_column_names"),
    ("overflow.csv", "id\n1\n2,unlabelled-value\n", "malformed_csv_record"),
    ("duplicate.jsonl", '{"id":1,"id":2}\n', "duplicate_json_key"),
    ("duplicate.json", '[{"id":1,"id":2}]', "duplicate_json_key"),
    ("nonobject.json", '[{"id":1},false]', "invalid_json_record"),
    ("nan.jsonl", '{"measurement":NaN}\n', "invalid_json_number"),
])
def test_corrupt_structured_files_do_not_silently_lose_fields_or_claim_complete(tmp_path, filename, body, code):
    (tmp_path / filename).write_text(body, encoding="utf-8")
    snapshot = curate_source("local_files", {"root": str(tmp_path), "profile_rows": 1})
    assert snapshot["discovery_complete"] is False
    assert snapshot["errors"][0]["code"] == code
    assert "unlabelled-value" not in json.dumps(snapshot)
