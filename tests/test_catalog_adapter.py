"""Portable catalog capture tests; no vendor account or native ACL qualification."""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from knowledge_layer.adapters.catalog import (
    MAX_BYTES,
    MAX_RECORDS,
    SCHEMA_VERSION,
    inventory_records,
    scope_digest,
)
from knowledge_layer.adapters.paging import NativeInventoryPager, continuation_request
from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.connectors.external_inventory import make_request
from knowledge_layer.inventory import MAX_RECORD_BYTES, InventoryStore, _encoded, _records
from knowledge_layer.store import KnowledgeStore

_DEFAULT_SCOPE = object()


def _scope(provider="datahub", namespace="finance"):
    return {"provider": provider, "source_identity": "fixture-account", "namespace": namespace,
            "scope_id": "business-unit-export", "selectors": ["glossary", "datasets"]}


def _record(identifier="urn:li:glossaryTerm:net-revenue", *, kind="semantic_definition"):
    return {"native_id": identifier, "native_type": "glossaryTerm", "kind": kind, "name": "Revenue",
            "native_record": {"urn": identifier, "properties": {"name": "Revenue",
                "description": "Recognized net revenue, including negative refunds.",
                "termSource": "INTERNAL", "sourceRef": None, "rawSchema": None,
                "customProperties": [{"key": "unit", "value": "USD", "associatedUrn": identifier}]},
                "ownership": {"owners": [{"owner": {"urn": "urn:li:corpuser:fixture-steward"},
                    "type": "DATAOWNER", "ownershipType": None}]},
                "policy": {"approved": True, "read_groups": ["finance"], "write": False},
                "lineage": {"upstream": ["urn:li:dataset:fixture-orders"], "verified": False},
                "metadata": {"nullable": None, "zero": 0, "empty": "", "disabled": False,
                             "present_empty": [], "nested": {"amount": -1.25}}}}


def _package(*records, scope=None):
    return {"schema_version": SCHEMA_VERSION, "scope": _scope() if scope is None else scope,
            "observation": {"exported_at": "2026-10-03T12:00:00Z", "source_observed_at": None,
                "native_revision": 0,
                "field_coverage": {"description": "complete", "ownership": "partial", "ACL": "unavailable",
                                   "lineage": "unknown"}, "redaction": "partial"},
            "listing_complete": True, "next_cursor": None, "record_count": len(records), "records": list(records)}


def _raw(package):
    return json.dumps(package, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _import(package):
    return inventory_records(_raw(package), expected_scope=package["scope"])


def _error(raw, *, scope=_DEFAULT_SCOPE, code=None):
    with pytest.raises(ConnectorError) as error:
        inventory_records(raw, expected_scope=_scope() if scope is _DEFAULT_SCOPE else scope)
    if code is not None:
        assert error.value.code == code
    assert error.value.source_pointer is None
    assert "fixture-account" not in str(error.value) and "fixture-steward" not in str(error.value)
    return error.value


def test_full_native_ownership_policy_lineage_and_missingness_are_only_captured_evidence():
    package = _package(_record())
    original = deepcopy(package)
    output = _import(package)
    assert set(output) == {"entities", "evidence", "watermark"}
    entity, proof = output["entities"][0], output["evidence"][0]
    assert entity["kind"] == "semantic_definition" and entity["evidence_ids"] == [proof["id"]]
    assert entity["payload"] == {"native_id": package["records"][0]["native_id"], "native_type": "glossaryTerm",
        "name": "Revenue", "source": package["scope"], "observation": package["observation"],
        "revision": hashlib.sha256(_raw(package["records"][0]["native_record"])).hexdigest(),
        "locally_executable": False, "source_clock_state": "unknown"}
    assert proof == {"id": proof["id"], "kind": "catalog_native_record", "payload": {
        "native_record": package["records"][0]["native_record"], "scope": package["scope"],
        "observation": package["observation"]}}
    preserved = proof["payload"]["native_record"]["metadata"]
    assert preserved["nullable"] is None and type(preserved["zero"]) is int and preserved["zero"] == 0
    assert preserved["empty"] == "" and preserved["disabled"] is False and preserved["present_empty"] == []
    assert "missing" not in preserved
    assert package == original
    assert not entity["payload"]["locally_executable"]


@pytest.mark.parametrize("kind", ["asset", "semantic_definition", "document", "relationship"])
def test_four_inventory_kinds_preserve_unresolved_native_references(kind):
    record = _record(kind=kind)
    record["native_record"]["relationship"] = {"source": "external:A", "target": "unselected:B",
        "join": "customer_id = person_id", "approved": False}
    output = _import(_package(record))
    assert output["entities"][0]["kind"] == kind
    assert output["evidence"][0]["payload"]["native_record"] == record["native_record"]
    assert "resolved" not in output["entities"][0]["payload"]


def test_names_conflicts_types_accounts_providers_and_units_do_not_collapse_identity():
    first = _record("same-native-id")
    second = deepcopy(first)
    second["native_type"] = "dataset"
    second["native_record"]["metadata"]["unit"] = "EUR"
    output = _import(_package(second, first))
    assert len({item["id"] for item in output["entities"]}) == 2
    assert {item["payload"]["name"] for item in output["entities"]} == {"Revenue"}
    identities = set()
    for change in ({}, {"provider": "collibra"}, {"namespace": "studio"}, {"source_identity": "other-account"}):
        scope = _scope() | change
        identities.add(_import(_package(first, scope=scope))["entities"][0]["id"])
    assert len(identities) == 4


def test_scope_selectors_are_canonical_for_matching_but_original_provenance_stays_exact():
    expected = _scope()
    package = _package(_record())
    package["scope"]["selectors"].reverse()
    output = inventory_records(_raw(package), expected_scope=expected)
    assert scope_digest(expected) == scope_digest(package["scope"])
    assert output["evidence"][0]["payload"]["scope"] == package["scope"]
    assert expected == _scope()


@pytest.mark.parametrize("key,value", [("provider", "collibra"), ("source_identity", "other-account"),
    ("namespace", "studio"), ("scope_id", "another-export"), ("selectors", ["glossary"])])
def test_artifact_cannot_redefine_any_operator_pinned_identity_or_coverage(key, value):
    package = _package(_record())
    package["scope"][key] = value
    _error(_raw(package), code="catalog_scope_mismatch")
    assert scope_digest(package["scope"]) != scope_digest(_scope())


@pytest.mark.parametrize("scope", [None, {}, _scope() | {"provider": None}, _scope() | {"provider": " spaced "},
    _scope() | {"scope_id": True}, _scope() | {"source_identity": ""}, _scope() | {"namespace": "x" * 121},
    _scope() | {"selectors": []}, _scope() | {"selectors": ["same", "same"]}, _scope() | {"selectors": [True]},
    _scope() | {"selectors": ["x" * 257]}, _scope() | {"selectors": [str(i) for i in range(201)]},
    _scope() | {"source_identity": "\ud800"}, _scope() | {"unexpected": "value"}])
def test_malformed_operator_scope_refuses_without_content(scope):
    with pytest.raises(ConnectorError) as error:
        scope_digest(scope)
    assert error.value.code == "catalog_scope_invalid"
    _error(_raw(_package(_record())), scope=scope, code="catalog_scope_invalid")


@pytest.mark.parametrize("key", ["schema_version", "scope", "observation", "listing_complete", "next_cursor", "record_count", "records"])
def test_all_package_fields_are_explicit_and_required(key):
    package = _package()
    del package[key]
    _error(_raw(package), code="catalog_export_invalid")


@pytest.mark.parametrize("changes", [{"env": {"credential": "fixture-private"}}, {"schema_version": "v-next"},
    {"records": {}}, {"record_count": True}, {"record_count": -1}, {"record_count": 1}, {"record_count": None}])
def test_unknown_global_environment_versions_and_false_counts_refuse(changes):
    _error(_raw(_package() | changes))


@pytest.mark.parametrize("changes", [{"listing_complete": False}, {"listing_complete": None},
    {"listing_complete": 1}, {"next_cursor": "more"}, {"next_cursor": False}, {"next_cursor": ""}])
def test_partial_unknown_or_cursor_exports_never_establish_empty_or_removal(changes):
    _error(_raw(_package() | changes), code="catalog_export_incomplete")


def test_declared_complete_scoped_empty_export_is_explicit_without_verified_vendor_claim():
    output = _import(_package())
    assert output["entities"] == output["evidence"] == []
    assert output["watermark"]["kind"] == "catalog_export_content_sha256"
    assert len(output["watermark"]["value"]) == 64
    assert set(output) == {"entities", "evidence", "watermark"}


@pytest.mark.parametrize("changes", [{"extra": None}, {"native_id": ""}, {"native_id": True},
    {"native_id": "x" * 513}, {"native_type": ""}, {"native_type": "x" * 121}, {"kind": "execute_sql"},
    {"kind": {}}, {"name": False}, {"name": "x" * 513}, {"native_record": None}, {"native_record": []}])
def test_invalid_record_refuses_entire_package(changes):
    package = _package(_record(), _record("second") | changes)
    _error(_raw(package))


@pytest.mark.parametrize("key", ["native_id", "native_type", "kind", "name", "native_record"])
def test_record_does_not_fabricate_absent_fields(key):
    record = _record()
    del record[key]
    _error(_raw(_package(record)))


def test_duplicate_exact_native_identity_refuses_even_when_names_and_payloads_differ():
    record = _record()
    duplicate = deepcopy(record)
    duplicate["name"] = "Different label"
    duplicate["native_record"] = {"different": True}
    _error(_raw(_package(record, duplicate)))


@pytest.mark.parametrize("name", [None, "", "  Revenue  ", "收入\n💰"])
def test_native_name_null_empty_unicode_and_whitespace_are_not_rewritten(name):
    record = _record()
    record["name"] = name
    assert _import(_package(record))["entities"][0]["payload"]["name"] == name


@pytest.mark.parametrize("revision", [None, "", "0", 0, -1, -(2**63), 2**63 - 1])
def test_native_revision_null_empty_numeric_zero_and_signed_bounds_are_preserved(revision):
    package = _package(_record())
    package["observation"]["native_revision"] = revision
    actual = _import(package)["evidence"][0]["payload"]["observation"]["native_revision"]
    assert type(actual) is type(revision) and actual == revision


@pytest.mark.parametrize("key,value", [("native_revision", True), ("native_revision", 1.0),
    ("native_revision", 2**63), ("native_revision", -(2**63) - 1), ("native_revision", "x" * 513),
    ("native_revision", []), ("exported_at", ""), ("source_observed_at", False),
    ("source_observed_at", "2026-10-03"), ("source_observed_at", "2026-10-03T12:00:00"),
    ("source_observed_at", "2026-02-30T12:00:00Z"), ("source_observed_at", "2026-10-03X12:00:00Z"),
    ("field_coverage", {}), ("field_coverage", {"owner": True}), ("field_coverage", {"owner": "certified"}),
    ("field_coverage", {"": "complete"}), ("field_coverage", {str(i): "unknown" for i in range(33)}),
    ("redaction", "complete"), ("redaction", None)])
def test_invalid_provenance_never_turns_into_guessed_defaults(key, value):
    package = _package(_record())
    package["observation"][key] = value
    _error(_raw(package))


@pytest.mark.parametrize("key", ["exported_at", "source_observed_at", "native_revision", "field_coverage", "redaction"])
def test_observation_fields_are_required_without_clock_or_coverage_substitution(key):
    package = _package(_record())
    del package["observation"][key]
    _error(_raw(package))


def test_native_clock_and_export_clock_are_separate_exact_declarations():
    package = _package(_record())
    package["observation"].update(exported_at=None, source_observed_at="2026-10-02T09:10:11.123456789-04:00")
    output = _import(package)
    assert output["entities"][0]["payload"]["source_clock_state"] == "declared"
    assert output["evidence"][0]["payload"]["observation"] == package["observation"]


@pytest.mark.parametrize("raw", [b"", b"\xff", b"[]", b"null", b'{}', b'{"a":1,"a":2}',
    b'{"private":NaN}', b'{"private":Infinity}', b'{"private":-Infinity}', b'{"private":1e999}',
    b'{"private":"\\ud800"}', b'{"\\udfff":"private"}', b"[" * 1500 + b"0" + b"]" * 1500])
def test_strict_json_refusals_are_fixed_and_content_free(raw):
    error = _error(raw)
    assert "private" not in str(error)


@pytest.mark.parametrize("replacement", [b'"private":1,"private":2', b'"private":NaN', b'"private":Infinity',
    b'"private":-Infinity', b'"private":1e999', b'"private":"\\ud800"', b'"\\udfff":null', b'"private":"\xff"'])
def test_invalid_nested_json_in_an_otherwise_valid_package_is_rejected_by_shared_decoder(replacement):
    record = _record()
    record["native_record"] = {"private": None}
    raw = _raw(_package(record)).replace(b'"private":null', replacement)
    error = _error(raw, code="catalog_export_invalid")
    assert "private" not in str(error) and error.__suppress_context__


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"])
def test_unicode_json_is_utf8_only(encoding):
    _error(_raw(_package()).decode().encode(encoding), code="catalog_export_invalid")


@pytest.mark.parametrize("raw", [None, "{}", bytearray(b"{}"), False])
def test_artifact_requires_explicit_bytes(raw):
    _error(raw, code="catalog_export_budget_exceeded")


def test_watermark_binds_exact_bytes_and_scope_not_a_native_clock_or_version():
    package = _package(_record("B"), _record("A"))
    raw = _raw(package)
    output = inventory_records(raw, expected_scope=package["scope"])
    spaced = inventory_records(raw + b"\n", expected_scope=package["scope"])
    assert spaced["entities"] == output["entities"] and spaced["evidence"] == output["evidence"]
    assert spaced["watermark"] != output["watermark"]
    reverse_expected = deepcopy(package["scope"])
    reverse_expected["selectors"].reverse()
    assert inventory_records(raw, expected_scope=reverse_expected) == output
    reordered = deepcopy(package)
    reordered["records"].reverse()
    assert _import(reordered)["entities"] == output["entities"]
    assert _import(reordered)["watermark"] != output["watermark"]
    modified = deepcopy(package)
    modified["records"][0]["native_record"]["properties"]["description"] = "Different meaning"
    revised = _import(modified)
    prior = {item["id"]: item for item in output["entities"]}
    assert any(item["payload"]["revision"] != prior[item["id"]]["payload"]["revision"] for item in revised["entities"])


def test_artifact_byte_boundary_accepts_exact_limit_but_refuses_one_byte_more():
    raw = _raw(_package())
    raw += b" " * (MAX_BYTES - len(raw))
    assert inventory_records(raw, expected_scope=_scope())["entities"] == []
    _error(raw + b" ", code="catalog_export_budget_exceeded")


def test_full_wrapped_record_byte_boundary_counts_provenance_and_unicode():
    package = _package(_record())
    record = package["records"][0]
    record["native_record"] = {"description": ""}
    base = _import(package)["evidence"][0]
    record["native_record"]["description"] = "é" * ((MAX_RECORD_BYTES - len(_encoded(base))) // 2)
    output = _import(package)
    proof = output["evidence"][0]
    gap = MAX_RECORD_BYTES - len(_encoded(proof))
    record["native_record"]["description"] += "x" * gap
    output = _import(package)
    assert len(_encoded(output["evidence"][0])) == MAX_RECORD_BYTES
    _records(output["evidence"], evidence=True)
    record["native_record"]["description"] += "x"
    _error(_raw(package), code="catalog_export_budget_exceeded")


def test_maximum_selected_records_retains_all_exact_ids_and_excess_refuses():
    records = [{"native_id": str(i), "native_type": "asset", "kind": "asset", "name": None, "native_record": {}}
               for i in range(MAX_RECORDS)]
    package = _package(*records)
    output = _import(package)
    assert len(output["entities"]) == len(output["evidence"]) == MAX_RECORDS
    assert {item["payload"]["native_id"] for item in output["entities"]} == {str(i) for i in range(MAX_RECORDS)}
    package["records"].append(records[-1] | {"native_id": "excess"})
    package["record_count"] += 1
    _error(_raw(package))


def test_emitted_capture_budget_counts_repeated_full_provenance_in_wrappers():
    record = _record()
    record["native_record"] = {"description": "x" * 30000}
    sample = _package(record)
    one = _import(sample)
    wrapper = len(_encoded(one["entities"][0])) + len(_encoded(one["evidence"][0]))
    count = MAX_BYTES // wrapper + 1
    records = [record | {"native_id": str(index)} for index in range(count)]
    package = _package(*records)
    assert len(_raw(package)) < MAX_BYTES
    _error(_raw(package), code="catalog_export_budget_exceeded")


def test_capture_envelope_and_array_punctuation_are_counted_beyond_individual_wrappers():
    count = 260
    record = {"native_id": "asset-0000", "native_type": "dataset", "kind": "asset", "name": None,
              "native_record": {"description": ""}}
    sample = _import(_package(record))
    base_width = len(_encoded(sample["entities"][0])) + len(_encoded(sample["evidence"][0]))
    width, extra = divmod(MAX_BYTES - 1, count)
    records = [record | {"native_id": f"asset-{i:04d}",
        "native_record": {"description": "x" * (width - base_width + (i < extra))}} for i in range(count)]
    raw = _raw(_package(*records))
    assert len(raw) < MAX_BYTES
    # Individually valid wrappers sum to one byte below the capture budget;
    # arrays, commas and the content watermark make the entire result larger.
    assert count * width + extra == MAX_BYTES - 1
    _error(raw, code="catalog_export_budget_exceeded")


@pytest.mark.parametrize("provider", ["datahub", "dbt"])
def test_two_vendor_families_survive_actual_paging_storage_and_exact_consumer_selection(provider):
    scope = _scope(provider)
    records = [_record(str(i)) for i in range(101)]
    if provider == "dbt":
        for record in records:
            record["native_type"] = "metric"
            record["native_record"] = {"unique_id": "metric.finance." + record["native_id"], "name": "Revenue",
                "resource_type": "metric", "package_name": "finance", "path": "metrics.yml",
                "original_file_path": "models/metrics.yml", "fqn": ["finance", "revenue"], "label": "Revenue",
                "description": "Full synthetic description " * 200,
                "type": "ratio", "type_params": {"numerator": {"name": "net_amount"}, "denominator": {"name": "order_count"}},
                "filter": {"where_sql_template": "country = 'US'"},
                "depends_on": {"nodes": ["semantic_model.finance.orders"]}, "config": {"enabled": False}}
    else:
        records[0]["native_record"]["properties"]["description"] = "Full synthetic description " * 200
    output = _import(_package(*records, scope=scope))
    now = datetime.now(UTC)
    request = make_request(provider=provider, scope=scope["scope_id"], profile_digest=scope_digest(scope),
        scan_id="offline-fixture", page_index=0, cursor=None,
        scan_started_at=(now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
        scan_expires_at=(now + timedelta(seconds=299)).isoformat().replace("+00:00", "Z"),
        inventory_limits={"max_pages": 20, "max_entities": 101, "max_evidence": 101, "max_total_bytes": MAX_BYTES})
    pager = NativeInventoryPager()
    store = KnowledgeStore(database_url="sqlite:///:memory:")
    try:
        source = store.create_connection("demo", kind="local_files", name="Offline catalog fixture", config={})
        inventory = InventoryStore(store)
        run = inventory.begin("demo", source["id"], scope={"namespace": scope["namespace"], "selectors": scope["selectors"]},
            policy={"id": "portable-fixture", "revision": scope_digest(scope)}, watermark=output["watermark"],
            limits=request["inventory_limits"])
        appended = run
        while True:
            page = pager.page(request, operator_digest=scope_digest(scope), load_records=lambda: output)
            appended = inventory.append("demo", source["id"], run["id"],
                **{key: page[key] for key in ("page_index", "cursor", "next_cursor", "entities", "evidence")},
                **{key: run[key] for key in ("scope_digest", "policy_digest", "watermark_digest")})
            if page["listing_complete"]:
                break
            request = continuation_request(request, page)
        committed = inventory.commit("demo", source["id"], run["id"], completion={"listing_complete": True,
            "pages": appended["page_count"], "entities": 101, "evidence": 101, "manifest_digest": appended["manifest_digest"]})
        assert committed["page_count"] > 1
        selected = inventory.select("demo", source["id"], run["id"], expected_manifest_digest=committed["manifest_digest"],
            entity_ids=[output["entities"][0]["id"]])
        assert selected["entities"][0]["payload"] == output["entities"][0]["payload"]
        assert selected["evidence"][0]["payload"] == output["evidence"][0]["payload"]
        assert selected["text_complete"] and selected["source_text_authority"] == "untrusted_evidence"
        assert not selected["agent_ready"] and not selected["meaning_certified"] and not selected["source_reads_executed"]
        assert store.get_snapshot("demo", source["id"]) is None
    finally:
        store.close()
