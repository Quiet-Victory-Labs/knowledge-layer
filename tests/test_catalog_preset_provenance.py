"""Captured catalog translation must not manufacture native provenance or coverage."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from knowledge_layer.adapters.catalog import inventory_records
from knowledge_layer.adapters.catalog_presets import get_preset_mapping, translate_catalog_export
from knowledge_layer.connectors.common import ConnectorError

SCOPE = {
    "provider": "openmetadata",
    "source_identity": "synthetic-metadata-service",
    "namespace": "fixture",
    "scope_id": "tables",
    "selectors": ["database:fixture"],
}
RECORD = {"id": "table-1", "name": "customers", "tableType": "Regular"}


def translate(payload=None, **kwargs):
    return translate_catalog_export(
        {"data": [deepcopy(RECORD)]} if payload is None else payload,
        "openmetadata",
        scope=kwargs.pop("scope", SCOPE),
        listing_complete=kwargs.pop("listing_complete", True),
        **kwargs,
    )


def test_unknown_provenance_remains_unknown_through_inventory_import():
    raw = translate()
    package = json.loads(raw)
    assert package["observation"] == {
        "exported_at": None,
        "source_observed_at": None,
        "native_revision": None,
        "field_coverage": {"definitions": "unknown", "policies": "unknown"},
        "redaction": "unknown",
    }
    imported = inventory_records(raw, expected_scope=SCOPE)
    assert imported["entities"][0]["payload"]["source_clock_state"] == "unknown"
    assert imported["evidence"][0]["payload"]["native_record"] == RECORD
    assert translate() == raw


def test_declared_scope_observation_and_native_values_survive_exactly():
    scope = {**SCOPE, "selectors": ["database:z", "database:a"]}
    observation = {
        "exported_at": "2026-10-01T10:00:00Z",
        "source_observed_at": "2026-09-30T10:00:00Z",
        "native_revision": 0,
        "field_coverage": {"definitions": "partial", "policies": "unavailable"},
        "redaction": "partial",
    }
    expected_scope, expected_observation = deepcopy(scope), deepcopy(observation)
    payload = {"data": [{**RECORD, "description": "", "policy": None, "total": 99}]}
    package = json.loads(translate(payload, scope=scope, observation=observation))
    assert package["scope"] == expected_scope
    assert package["observation"] == expected_observation
    assert package["records"][0]["native_record"] == payload["data"][0]
    assert scope == expected_scope and observation == expected_observation


def test_partial_observation_fills_only_unknown_defaults():
    package = json.loads(translate(observation={"native_revision": "captured-revision"}))
    assert package["observation"]["native_revision"] == "captured-revision"
    assert package["observation"]["exported_at"] is None
    assert package["observation"]["source_observed_at"] is None
    assert package["observation"]["field_coverage"]["definitions"] == "unknown"
    assert package["observation"]["redaction"] == "unknown"


@pytest.mark.parametrize("complete", [None, False, 1, "true"])
def test_explicit_complete_listing_is_required(complete):
    with pytest.raises(ConnectorError) as caught:
        translate(listing_complete=complete)
    assert caught.value.code == "catalog_export_incomplete"


def test_omitted_completeness_is_not_inferred_from_a_plain_list():
    with pytest.raises(ConnectorError) as caught:
        translate_catalog_export([{"id": 1, "name": "customers"}], "alation", scope={**SCOPE, "provider": "alation"})
    assert caught.value.code == "catalog_export_incomplete"


@pytest.mark.parametrize("cursor", ["next-page", "", False, 0])
def test_producer_continuation_refuses_even_with_explicit_complete_listing(cursor):
    with pytest.raises(ConnectorError) as caught:
        translate(next_cursor=cursor)
    assert caught.value.code == "catalog_export_incomplete"


@pytest.mark.parametrize("scope", [None, {}, {"source_identity": "warehouse"}, {**SCOPE, "selectors": []}])
def test_native_scope_is_required_without_invented_identity(scope):
    with pytest.raises(ConnectorError) as caught:
        translate(scope=scope)
    assert caught.value.code == "catalog_scope_invalid"


@pytest.mark.parametrize("metadata", [
    {"paging": {"total": 100, "after": "next-page"}},
    {"paging": {"total": 2}},
    {"total": 0},
    {"total": True},
    {"offset": 1},
    {"offset": False},
    {"pagination": {"nextPageToken": "continue"}},
    {"links": {"next": "https://catalog.example.test/next-page"}},
    {"meta": {"pageInfo": {"hasNextPage": True}}},
    {"pageInfo": {"hasNextPage": None}},
    {"hasMore": True},
    {"isLast": False},
    {"listing_complete": False},
    {"paging": "unavailable"},
])
def test_upstream_partial_or_inconsistent_page_cannot_be_declared_complete(metadata):
    with pytest.raises(ConnectorError) as caught:
        translate({"data": [RECORD], **metadata})
    assert caught.value.code == "catalog_export_incomplete"


def test_consistent_final_page_metadata_is_accepted():
    package = json.loads(translate({"data": [RECORD], "paging": {
        "total": 1, "after": None, "offset": 0, "hasMore": False,
    }}))
    assert package["listing_complete"] is True
    assert package["next_cursor"] is None
    assert package["record_count"] == 1


def test_complete_empty_population_requires_real_records_pointer_and_declaration():
    package = json.loads(translate({"data": [], "paging": {"total": 0, "after": None}}))
    assert package["record_count"] == 0
    with pytest.raises(ConnectorError) as caught:
        translate({"paging": {"total": 0}})
    assert caught.value.code == "catalog_mapping_field_missing"


def test_custom_nested_record_mapping_cannot_hide_parent_paging():
    mapping = get_preset_mapping("openmetadata")
    mapping["records_pointer"] = "/response/data"
    with pytest.raises(ConnectorError) as caught:
        translate({"response": {"data": [RECORD], "paging": {"after": "next-page"}}}, mapping=mapping)
    assert caught.value.code == "catalog_export_incomplete"


def test_custom_array_pointer_cannot_hide_page_cursor():
    mapping = get_preset_mapping("openmetadata")
    mapping["records_pointer"] = "/pages/0/data"
    with pytest.raises(ConnectorError) as caught:
        translate({"pages": [{"data": [RECORD], "paging": {"after": "next-page"}}]}, mapping=mapping)
    assert caught.value.code == "catalog_export_incomplete"


@pytest.mark.parametrize("observation", [
    {"source_observed_at": "2026-10-08"},
    {"native_revision": True},
    {"field_coverage": {"definitions": True}},
    {"redaction": "assumed-none"},
    {"unrecognized": "producer-field"},
])
def test_invalid_provenance_is_not_silently_repaired(observation):
    with pytest.raises(ConnectorError) as caught:
        translate(observation=observation)
    assert caught.value.code == "catalog_export_invalid"


@pytest.mark.parametrize("payload", [
    b'{"data": [], "data": []}',
    b'{"data": [{"id":"table-1","name":"customers","tableType":"Regular","score":NaN}]}',
    {"data": [{**RECORD, "score": float("nan")}]},
])
def test_ambiguous_or_nonfinite_raw_exports_refuse(payload):
    with pytest.raises(ConnectorError) as caught:
        translate(payload)
    assert caught.value.code == "catalog_export_invalid"


@pytest.mark.parametrize("provider", ["datahub", "alation", "openmetadata-other"])
def test_builtin_preset_cannot_relabel_another_native_provider(provider):
    with pytest.raises(ConnectorError) as caught:
        translate(scope={**SCOPE, "provider": provider})
    assert caught.value.code == "catalog_scope_mismatch"


@pytest.mark.parametrize("preset,payload,provider", [
    ("ALATION", [{"id": 1, "name": "orders"}], "alation"),
    ("alation", [{"id": 1, "name": "orders"}], "ALATION"),
    ("alation_terms", [{"id": 1, "title": "Orders"}], "alation-terms"),
    ("alation-tables", {"tables": [{"id": 1, "name": "orders", "table_type": "TABLE"}]}, "alation_tables"),
    ("alation_terms", [{"id": 1, "title": "Orders"}], "alation-tables"),
    ("collibra-direct", [{"id": "1", "name": "Orders", "type": {"name": "Table"}}], "COLLIBRA_DIRECT"),
    ("collibra", {"results": [{"id": "1", "name": "Orders", "type": {"name": "Table"}}]}, "collibra-direct"),
    ("openmetadata", {"data": [RECORD]}, "openmetadata_terms"),
])
def test_provider_aliases_match_preset_family_without_rewriting_identity(preset, payload, provider):
    scope = {**SCOPE, "provider": provider}
    package = json.loads(translate_catalog_export(payload, preset, scope=scope, listing_complete=True))
    assert package["scope"] == scope
    assert package["record_count"] == 1


def test_reviewed_custom_mapping_preserves_actual_provider_without_preset_inference():
    scope = {**SCOPE, "provider": "customer-specific-catalog"}
    package = json.loads(translate(scope=scope, mapping=get_preset_mapping("openmetadata")))
    assert package["scope"] == scope
