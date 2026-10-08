"""Offline schema-shaped dbt records; no compilation or source qualification."""

import hashlib
import json
from copy import deepcopy

import pytest

from knowledge_layer.adapters.dbt import (
    MAX_INVENTORY_BYTES,
    MAX_INVENTORY_RECORD_BYTES,
    MAX_INVENTORY_SELECTION,
    SCHEMA,
    import_manifest,
    inventory_records,
)
from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.inventory import InventoryStore, _records
from knowledge_layer.store import KnowledgeStore


def _artifact(**changes):
    value = {"metadata": {"dbt_schema_version": SCHEMA, "generated_at": "2026-10-03T12:00:00+00:00"},
             "metrics": {"metric.finance.revenue": {"unique_id": "metric.finance.revenue", "name": "revenue",
                         "description": "Recognized revenue; refunds excluded.", "type": "simple",
                         "type_params": {"measure": {"name": "net_amount"}}}}}
    value.update(changes)
    return json.dumps(value).encode()


def test_dbt_preserves_native_semantics_and_two_authorities_without_executing():
    raw = _artifact()
    finance = import_manifest(raw, namespace="finance", unique_ids=["metric.finance.revenue"])
    retail = import_manifest(raw, namespace="retail", unique_ids=["metric.finance.revenue"])
    left = finance["assets"][0]["upstream_definition"]
    right = retail["assets"][0]["upstream_definition"]
    assert left["namespace"] != right["namespace"]
    assert json.loads(left["formula"]) == json.loads(raw)["metrics"][left["id"]]
    assert left["review_status"] is None
    assert finance["locally_executable"] is False


@pytest.mark.parametrize("raw", [b'{"metadata":{},"metadata":{}}', b"[]", b"{}",
                                  _artifact(metadata={"dbt_schema_version": "v-next"}),
                                  _artifact(metadata={"dbt_schema_version": SCHEMA, "generated_at": "2026-10-03"})])
def test_invalid_unknown_and_untimed_artifacts_refuse(raw):
    with pytest.raises(ConnectorError):
        import_manifest(raw, namespace="finance", unique_ids=["metric.finance.revenue"])


def test_missing_selected_id_does_not_establish_deletion():
    with pytest.raises(ConnectorError, match="absent"):
        import_manifest(_artifact(), namespace="finance", unique_ids=["metric.finance.absent"])


def test_large_semantics_never_truncate_into_a_different_definition():
    with pytest.raises(ConnectorError, match="definition budget"):
        import_manifest(_artifact(metrics={"metric.finance.revenue": {"unique_id": "metric.finance.revenue",
                        "name": "revenue", "description": "x" * 5000}}),
                        namespace="finance", unique_ids=["metric.finance.revenue"])


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"])
def test_legacy_import_rejects_non_utf8_artifact_encodings(encoding):
    raw = _artifact().decode("utf-8").encode(encoding)
    with pytest.raises(ConnectorError) as error:
        import_manifest(raw, namespace="finance", unique_ids=["metric.finance.revenue"])
    assert error.value.code == "artifact_invalid"


@pytest.mark.parametrize("unselected", [
    b'{"duplicate":1,"duplicate":2}',
    b'{"private":"\\ud800"}',
    b'{"\\udfff":"private"}',
    b'{"private":NaN}',
    b'{"private":Infinity}',
    b'{"private":-Infinity}',
    b'{"private":1e999}',
])
def test_legacy_import_validates_unselected_json_and_sanitizes_errors(unselected):
    raw = _artifact()[:-1] + b',"unselected":' + unselected + b'}'
    with pytest.raises(ConnectorError) as error:
        import_manifest(raw, namespace="finance", unique_ids=["metric.finance.revenue"])
    assert error.value.code == "artifact_invalid"
    assert "private" not in str(error.value) and "unselected" not in str(error.value)
    assert error.value.__cause__ is None and error.value.__suppress_context__


def _wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _node(kind, name="revenue", package="finance"):
    """Required native fields follow the official manifest v12 node schemas."""
    identifier = f"{kind}.{package}.{name}"
    node = {"name": name, "unique_id": identifier, "resource_type": kind, "package_name": package,
            "path": "semantics.yml", "original_file_path": "models/semantics.yml", "fqn": [package, name]}
    if kind == "metric":
        node.update(description="Recognized revenue; refunds excluded.", label="Revenue", type="simple",
                    type_params={"measure": {"name": "net_amount"}},
                    depends_on={"nodes": [f"semantic_model.{package}.orders"], "macros": []})
    elif kind == "semantic_model":
        node.update(model="ref('orders')", node_relation={"alias": "orders", "schema_name": "sales", "database": None},
                    defaults={"agg_time_dimension": "ordered_at"},
                    entities=[{"name": "order", "type": "primary", "expr": "order_id"}],
                    measures=[{"name": "net_amount", "agg": "sum", "expr": "net_amount",
                               "create_metric": False}],
                    dimensions=[{"name": "ordered_at", "type": "time", "type_params": {"time_granularity": "day"}}])
    elif kind == "exposure":
        node.update(type="dashboard", owner={"name": "Finance steward", "email": "fixture@example.invalid"},
                    description="Customer-owned dashboard.", depends_on={"nodes": [f"metric.{package}.revenue"], "macros": []},
                    url="https://fixture.example.invalid/revenue")
    return node


def _inventory_artifact(*nodes, metadata=None, **extra):
    document = {"metadata": {"dbt_schema_version": SCHEMA, "generated_at": "2026-10-03T12:00:00+00:00",
                            "project_name": "finance", "dbt_version": "1.8.0", "invocation_id": None}
                if metadata is None else metadata, "metrics": {}, "semantic_models": {}, "exposures": {}, **extra}
    collections = {"metric": "metrics", "semantic_model": "semantic_models", "exposure": "exposures"}
    for node in nodes:
        document[collections[node["resource_type"]]][node["unique_id"]] = node
    return _wire(document)


def test_inventory_retains_full_native_metrics_semantic_models_exposures_and_no_global_environment():
    metric, model, exposure = (_node("metric"), _node("semantic_model", "orders"), _node("exposure", "dashboard"))
    metric.update(type="ratio", type_params={"numerator": {"name": "net_amount"}, "denominator": {"name": "order_count"},
                                             "filter": {"where_sql_template": "country = 'US'"}},
                  config={"enabled": False, "meta": {"null": None, "zero": 0, "empty": "", "disabled": False,
                                                   "present": [], "nested": {"unit": "USD"}}})
    original = [deepcopy(metric), deepcopy(model), deepcopy(exposure)]
    metadata = {"dbt_schema_version": SCHEMA, "generated_at": "2026-10-03T12:00:00-04:00", "project_name": "",
                "dbt_version": "1.8.0", "invocation_id": None, "env": {"unretained_fixture_value": "secret-marker"},
                "user_id": "unretained-user", "adapter_type": "unretained-adapter"}
    output = inventory_records(_inventory_artifact(*original, metadata=metadata), namespace="finance",
                               unique_ids=[node["unique_id"] for node in original])
    assert set(output) == {"entities", "evidence", "watermark"}
    assert output["watermark"]["kind"] == "content_sha256"
    by_native = {proof["payload"]["native_record"]["unique_id"]: proof for proof in output["evidence"]}
    for node in original:
        proof = by_native[node["unique_id"]]
        assert proof["kind"] == "upstream_semantic_definition"
        assert proof["payload"]["native_record"] == node
        assert proof["payload"]["artifact_metadata"] == {key: metadata[key] for key in (
            "dbt_schema_version", "generated_at", "project_name", "dbt_version", "invocation_id")}
        entity = next(value for value in output["entities"] if value["payload"]["native_id"] == node["unique_id"])
        assert entity["kind"] == "semantic_definition" and entity["evidence_ids"] == [proof["id"]]
        assert entity["payload"] == {"native_id": node["unique_id"], "name": node["name"], "namespace": "finance",
            "authority": "dbt", "native_kind": node["resource_type"], "revision": hashlib.sha256(_wire(node)).hexdigest(),
            "source_observed_at": metadata["generated_at"], "source_clock_state": "known",
            "locally_executable": False, "native_execution": "delegated_customer_dbt_runtime"}
    assert "secret-marker" not in json.dumps(output) and "env" not in by_native[metric["unique_id"]]["payload"]["artifact_metadata"]
    native_meta = by_native[metric["unique_id"]]["payload"]["native_record"]["config"]["meta"]
    assert native_meta["null"] is None and type(native_meta["zero"]) is int and native_meta["zero"] == 0
    assert native_meta["empty"] == "" and native_meta["disabled"] is False and native_meta["present"] == []
    assert "missing" not in native_meta


def test_inventory_keeps_same_names_distinct_across_native_ids_kinds_and_namespaces():
    nodes = [_node("metric", package="finance"), _node("metric", package="retail"), _node("semantic_model")]
    raw = _inventory_artifact(*nodes)
    identifiers = [node["unique_id"] for node in nodes]
    left = inventory_records(raw, namespace="division/a", unique_ids=identifiers)
    right = inventory_records(raw, namespace="division/b", unique_ids=identifiers)
    assert len({entity["id"] for entity in left["entities"]}) == 3
    assert len({proof["id"] for proof in left["evidence"]}) == 3
    assert {item["id"] for item in left["entities"]}.isdisjoint(item["id"] for item in right["entities"])
    assert {item["id"] for item in left["evidence"]}.isdisjoint(item["id"] for item in right["evidence"])
    assert left["watermark"] != right["watermark"]
    assert [item["payload"]["native_id"] for item in left["entities"]] == sorted(identifiers)
    assert left == inventory_records(raw, namespace="division/a", unique_ids=list(reversed(identifiers)))


def test_inventory_watermark_binds_exact_artifact_and_scope_but_content_revision_is_stable():
    selected, other = _node("metric"), _node("metric", "orders")
    raw = _inventory_artifact(selected, other)
    output = inventory_records(raw, namespace="finance", unique_ids=[selected["unique_id"]])
    whitespace = inventory_records(raw + b"\n", namespace="finance", unique_ids=[selected["unique_id"]])
    all_selected = inventory_records(raw, namespace="finance", unique_ids=[node["unique_id"] for node in (selected, other)])
    assert output["entities"] == whitespace["entities"] and output["evidence"] == whitespace["evidence"]
    assert len(output["watermark"]["value"]) == 64
    assert output["watermark"] != whitespace["watermark"] != all_selected["watermark"]
    changed = deepcopy(selected)
    changed["type_params"]["measure"]["name"] = "recognized_net_amount"
    revised = inventory_records(_inventory_artifact(changed, other), namespace="finance", unique_ids=[selected["unique_id"]])
    assert revised["entities"][0]["id"] == output["entities"][0]["id"]
    assert revised["evidence"][0]["id"] == output["evidence"][0]["id"]
    assert revised["entities"][0]["payload"]["revision"] != output["entities"][0]["payload"]["revision"]
    assert revised["evidence"][0]["payload"]["native_record"] == changed


def test_inventory_full_definition_beyond_legacy_limit_survives_actual_paged_registry_storage():
    native = _node("metric")
    native["description"] = "Recognized net revenue.\n" * 400
    raw = _inventory_artifact(native)
    output = inventory_records(raw, namespace="finance", unique_ids=[native["unique_id"]])
    with pytest.raises(ConnectorError, match="definition budget"):
        import_manifest(raw, namespace="finance", unique_ids=[native["unique_id"]])
    store = KnowledgeStore(database_url="sqlite:///:memory:")
    try:
        source = store.create_connection("demo", kind="local_files", name="Offline inventory fixture", config={})
        inventory = InventoryStore(store)
        run = inventory.begin("demo", source["id"], scope={"namespace": "finance", "selectors": [native["unique_id"]]},
            policy={"id": "dbt-fixture", "revision": None}, watermark=output["watermark"],
            limits={"max_pages": 2, "max_entities": 1, "max_evidence": 1, "max_total_bytes": 32768})
        first = inventory.append("demo", source["id"], run["id"], page_index=0, cursor=None, next_cursor="second",
            entities=output["entities"], evidence=[], **{key: run[key] for key in ("scope_digest", "policy_digest", "watermark_digest")})
        second = inventory.append("demo", source["id"], run["id"], page_index=1, cursor="second", next_cursor=None,
            entities=[], evidence=output["evidence"], **{key: first[key] for key in ("scope_digest", "policy_digest", "watermark_digest")})
        committed = inventory.commit("demo", source["id"], run["id"], completion={"listing_complete": True,
            "pages": 2, "entities": 1, "evidence": 1, "manifest_digest": second["manifest_digest"]})
        evidence_page = inventory.page("demo", source["id"], committed["id"], collection="evidence")
        assert evidence_page["items"][0]["payload"]["native_record"] == native
        assert not evidence_page["agent_ready"] and committed["compatibility"]["meaning_certified"] is False
        assert store.get_snapshot("demo", source["id"]) is None
    finally:
        store.close()


@pytest.mark.parametrize("clock", ["missing", None])
def test_inventory_unknown_clock_is_explicit_and_metadata_absence_is_preserved(clock):
    metadata = {"dbt_schema_version": SCHEMA}
    if clock != "missing":
        metadata["generated_at"] = clock
    node = _node("metric")
    output = inventory_records(_inventory_artifact(node, metadata=metadata), namespace="finance", unique_ids=[node["unique_id"]])
    assert output["entities"][0]["payload"]["source_observed_at"] is None
    assert output["entities"][0]["payload"]["source_clock_state"] == "unknown"
    assert output["evidence"][0]["payload"]["artifact_metadata"] == metadata
    assert output["watermark"]["kind"] == "content_sha256"


@pytest.mark.parametrize("clock", ["", "2026-10-03", "2026-10-03T12:00:00", "not-a-clock", 0, False, [], {}, "2026-99-03T12:00:00Z"])
def test_inventory_malformed_present_clock_refuses_instead_of_fabricating_freshness(clock):
    native = _node("metric")
    with pytest.raises(ConnectorError) as error:
        inventory_records(_inventory_artifact(native, metadata={"dbt_schema_version": SCHEMA, "generated_at": clock}),
                          namespace="finance", unique_ids=[native["unique_id"]])
    assert error.value.code == "artifact_clock_unknown"


@pytest.mark.parametrize("collection", ["metrics", "semantic_models", "exposures"])
@pytest.mark.parametrize("invalid", [None, [], False, "mapping-is-required"])
def test_inventory_malformed_collections_refuse_even_when_selected_id_is_elsewhere(collection, invalid):
    document = json.loads(_inventory_artifact(_node("metric")))
    document[collection] = invalid
    with pytest.raises(ConnectorError):
        inventory_records(_wire(document), namespace="finance", unique_ids=["metric.finance.revenue"])


@pytest.mark.parametrize("raw", [
    b"", b"[]", b"null", b"{}", b'{"metadata":null}', b'\xff',
    b'{"metadata":{"dbt_schema_version":"secret-marker"}}',
    b'{"metadata":{},"metadata":{}}',
    b'{"metadata":{"dbt_schema_version":"' + SCHEMA.encode() + b'"},"env":{"a":1,"a":2}}',
    b'{"metadata":{"dbt_schema_version":"' + SCHEMA.encode() + b'"},"env":{"surrogate":"\\ud800"}}',
    b'{"metadata":{"dbt_schema_version":"' + SCHEMA.encode() + b'"},"env":{"\\udfff":true}}',
    b'{"metadata":{"dbt_schema_version":"' + SCHEMA.encode() + b'"},"env":{"value":NaN}}',
    b'{"metadata":{"dbt_schema_version":"' + SCHEMA.encode() + b'"},"env":{"value":Infinity}}',
    b'{"metadata":{"dbt_schema_version":"' + SCHEMA.encode() + b'"},"env":{"value":-Infinity}}',
    b'{"metadata":{"dbt_schema_version":"' + SCHEMA.encode() + b'"},"env":{"value":1e999}}',
    b'{"metadata":{"dbt_schema_version":"' + SCHEMA.encode() + b'"},"env":{"value":' + b'[' * 1500 + b'0' + b']' * 1500 + b'}}',
])
def test_inventory_strict_json_and_schema_errors_are_sanitized(raw):
    with pytest.raises(ConnectorError) as error:
        inventory_records(raw, namespace="finance", unique_ids=["metric.finance.revenue"])
    assert "secret-marker" not in str(error.value)


@pytest.mark.parametrize("raw", ["{}", bytearray(b"{}"), None, False])
def test_inventory_only_accepts_explicit_bytes(raw):
    with pytest.raises(ConnectorError):
        inventory_records(raw, namespace="finance", unique_ids=["metric.finance.revenue"])


@pytest.mark.parametrize("namespace", [None, False, "", " ", "finance\n", "x" * 121, "\ud800"])
def test_inventory_invalid_namespaces_refuse(namespace):
    with pytest.raises(ConnectorError) as error:
        inventory_records(_inventory_artifact(_node("metric")), namespace=namespace, unique_ids=["metric.finance.revenue"])
    assert error.value.code == "artifact_scope_invalid"


@pytest.mark.parametrize("identifiers", [None, "metric.finance.revenue", [], [None], [False], [""], [" "], ["bad\n"],
                                       ["x" * 257], ["\ud800"], ["metric.finance.revenue"] * 2,
                                       [f"metric.fixture.m{index}" for index in range(MAX_INVENTORY_SELECTION + 1)]])
def test_inventory_invalid_or_excessive_selected_ids_refuse(identifiers):
    with pytest.raises(ConnectorError) as error:
        inventory_records(_inventory_artifact(_node("metric")), namespace="finance", unique_ids=identifiers)
    assert error.value.code == "artifact_scope_invalid"


@pytest.mark.parametrize("invalid", [None, [], False, {"unique_id": "other", "name": "revenue"},
                                    {"unique_id": "metric.finance.revenue", "name": None},
                                    {"unique_id": "metric.finance.revenue", "name": ""},
                                    {"unique_id": "metric.finance.revenue", "name": "bad\x00"},
                                    {"unique_id": "metric.finance.revenue", "name": "revenue", "resource_type": "exposure"}])
def test_inventory_selected_native_identity_and_name_must_be_exact(invalid):
    document = json.loads(_inventory_artifact(_node("metric")))
    document["metrics"]["metric.finance.revenue"] = invalid
    with pytest.raises(ConnectorError):
        inventory_records(_wire(document), namespace="finance", unique_ids=["metric.finance.revenue"])


def test_inventory_missing_selection_and_duplicate_collection_identity_never_establish_removal():
    native = _node("metric")
    with pytest.raises(ConnectorError) as error:
        inventory_records(_inventory_artifact(native), namespace="finance", unique_ids=[native["unique_id"], "metric.finance.absent"])
    assert error.value.code == "artifact_scope_incomplete"
    document = json.loads(_inventory_artifact(native))
    document["exposures"][native["unique_id"]] = deepcopy(native)
    with pytest.raises(ConnectorError) as error:
        inventory_records(_wire(document), namespace="finance", unique_ids=[native["unique_id"]])
    assert error.value.code == "artifact_invalid"


def test_inventory_accepts_ten_thousand_exact_selected_native_records_deterministically():
    nodes = [_node("metric", name=f"revenue_{index:05d}") for index in range(MAX_INVENTORY_SELECTION)]
    output = inventory_records(_inventory_artifact(*nodes), namespace="finance", unique_ids=[node["unique_id"] for node in reversed(nodes)])
    assert len(output["entities"]) == len(output["evidence"]) == MAX_INVENTORY_SELECTION
    assert [item["payload"]["native_id"] for item in output["entities"]] == [node["unique_id"] for node in nodes]
    assert [proof["payload"]["native_record"] for proof in output["evidence"]] == nodes
    assert len({item["id"] for item in output["entities"]}) == MAX_INVENTORY_SELECTION


def test_inventory_artifact_exact_byte_budget_accepts_then_refuses_excess():
    node = _node("metric")
    base = _inventory_artifact(node)
    raw = base + b" " * (MAX_INVENTORY_BYTES - len(base))
    assert inventory_records(raw, namespace="finance", unique_ids=[node["unique_id"]])["evidence"][0]["payload"]["native_record"] == node
    with pytest.raises(ConnectorError) as error:
        inventory_records(raw + b" ", namespace="finance", unique_ids=[node["unique_id"]])
    assert error.value.code == "artifact_size_exceeded"


@pytest.mark.parametrize("content", ["ASCII text", "Unicode 💶 text"])
def test_inventory_wrapped_evidence_exact_byte_budget_never_truncates(content):
    node = _node("metric")
    node["description"] = content
    original = inventory_records(_inventory_artifact(node), namespace="finance", unique_ids=[node["unique_id"]])
    padding = MAX_INVENTORY_RECORD_BYTES - len(_wire(original["evidence"][0]))
    node["description"] += "x" * padding
    output = inventory_records(_inventory_artifact(node), namespace="finance", unique_ids=[node["unique_id"]])
    assert len(_wire(output["evidence"][0])) == MAX_INVENTORY_RECORD_BYTES
    assert _records(output["evidence"], evidence=True)[0]["payload"]["native_record"] == node
    node["description"] += "x"
    with pytest.raises(ConnectorError) as error:
        inventory_records(_inventory_artifact(node), namespace="finance", unique_ids=[node["unique_id"]])
    assert error.value.code == "artifact_record_too_large"


def test_inventory_record_budget_includes_safe_metadata_wrapper_and_no_partial_selection():
    small, large = _node("metric", "a_small"), _node("metric", "z_large")
    metadata = {"dbt_schema_version": SCHEMA, "generated_at": None, "project_name": "x" * MAX_INVENTORY_RECORD_BYTES}
    with pytest.raises(ConnectorError) as error:
        inventory_records(_inventory_artifact(small, large, metadata=metadata), namespace="finance",
                          unique_ids=[small["unique_id"], large["unique_id"]])
    assert error.value.code == "artifact_record_too_large"
    large["description"] = "x" * MAX_INVENTORY_RECORD_BYTES
    with pytest.raises(ConnectorError):
        inventory_records(_inventory_artifact(small, large), namespace="finance",
                          unique_ids=[small["unique_id"], large["unique_id"]])


def test_inventory_native_unicode_and_identity_boundary_framing_are_preserved():
    node = _node("metric", name="résumé💶")
    node["description"] = "  Exact café text.\n\nSecond paragraph.  "
    node["config"] = {"meta": {"known_zero": 0.0, "known_negative_zero": -0.0, "disabled": False}}
    output = inventory_records(_inventory_artifact(node), namespace="division/日本", unique_ids=[node["unique_id"]])
    assert output["entities"][0]["payload"]["name"] == node["name"]
    assert output["evidence"][0]["payload"]["native_record"] == node
    assert output["entities"][0]["id"].isascii()
    a = deepcopy(node)
    b = deepcopy(node)
    a["unique_id"] = "c"
    b["unique_id"] = "bc"
    left = inventory_records(_inventory_artifact(a), namespace="ab", unique_ids=["c"])
    right = inventory_records(_inventory_artifact(b), namespace="a", unique_ids=["bc"])
    assert left["entities"][0]["id"] != right["entities"][0]["id"]
