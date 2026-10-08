"""Portable mappings/CLI effects; synthetic exports are not vendor connectors."""

import hashlib
import json
import os
from copy import deepcopy

import pytest

from knowledge_layer.adapters.catalog import inventory_records
from knowledge_layer.adapters.catalog_cli import main
from knowledge_layer.adapters.catalog_mapping import binding_digest, map_export, validate_mapping
from knowledge_layer.connectors import external_adapter
from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.inventory_refresh import inventory_profile_digest


def contract(provider="atlan", count=1):
    return {"scope": {"provider": provider, "source_identity": "synthetic-account", "namespace": "finance",
                      "scope_id": "finance", "selectors": ["container:finance"]},
            "observation": {"exported_at": "2026-10-03T00:00:00Z", "source_observed_at": None,
                            "native_revision": 0, "field_coverage": {"definitions": "complete", "policies": "unknown"},
                            "redaction": "unknown"},
            "listing_complete": True, "next_cursor": None, "record_count": count}


def mapping():
    return {"records_pointer": "/entities", "id_pointer": "/guid", "id_encoding": "string",
            "type_pointer": "/typeName", "native_type": None, "name_pointer": "/attributes/name",
            "kind_by_type": {"AtlasGlossaryTerm": "semantic_definition", "Table": "asset"}}


def native():
    return {"guid": "fixture-term-1", "typeName": "AtlasGlossaryTerm", "attributes": {
        "name": "Revenue", "definition": "Exclude refunds", "unit": "USD", "owners": ["Finance"],
        "unknown": None, "zero": 0, "empty": "", "false": False,
        "policy": {"native_approval": "approved"}, "relationships": [{"guid": "unresolved-table"}]}}


def test_mapper_preserves_native_record_provenance_without_interpreting_approval_or_relationships():
    node, controls = native(), contract()
    encoded = map_export(json.dumps({"entities": [node]}).encode(), contract=controls, mapping=mapping())
    records = inventory_records(encoded, expected_scope=controls["scope"])
    assert records["evidence"][0]["payload"]["native_record"] == node
    assert records["entities"][0]["payload"]["source_clock_state"] == "unknown"
    assert records["entities"][0]["payload"]["observation"]["native_revision"] == 0
    assert records["entities"][0]["payload"]["locally_executable"] is False
    assert "missing" not in records["evidence"][0]["payload"]["native_record"]["attributes"]


@pytest.mark.parametrize("provider", ["databricks", "alation", "atlan", "collibra", "custom_catalog"])
def test_arbitrary_provider_exports_require_no_vendor_sdk(provider):
    node, controls, fields = native(), contract(provider), mapping()
    if provider == "alation":
        node = {"id": 0, "title": "Revenue", "description": "Refunds excluded", "custom_fields": []}
        fields.update(records_pointer="", id_pointer="/id", id_encoding="integer_decimal",
                      type_pointer=None, native_type="glossary_term", name_pointer="/title",
                      kind_by_type={"glossary_term": "semantic_definition"})
        supplied = [node]
    else:
        supplied = {"entities": [node]}
    encoded = map_export(json.dumps(supplied).encode(), contract=controls, mapping=fields)
    imported = inventory_records(encoded, expected_scope=controls["scope"])
    assert imported["evidence"][0]["payload"]["native_record"] == node
    assert imported["entities"][0]["payload"]["source"]["provider"] == provider
    if provider == "alation":
        assert imported["entities"][0]["payload"]["native_id"] == "0"
        assert type(imported["evidence"][0]["payload"]["native_record"]["id"]) is int


def test_pointer_escapes_and_array_index_read_exact_json_keys():
    fields = mapping()
    fields.update(records_pointer="/a~1b/~0records", id_pointer="/ids/0")
    node = native() | {"ids": ["native-exact-id"]}
    encoded = map_export(json.dumps({"a/b": {"~records": [node]}}).encode(), contract=contract(), mapping=fields)
    assert json.loads(encoded)["records"][0]["native_id"] == "native-exact-id"


@pytest.mark.parametrize("change", [
    {"records_pointer": "$..entities"}, {"id_pointer": "/guid~2"}, {"id_pointer": "/guid~"},
    {"native_type": "Table"}, {"type_pointer": None}, {"id_encoding": []},
    {"kind_by_type": {"Table": "approved_metric"}}, {"script": "import os"},
    {"url": "https://private.invalid"}, {"kind_by_type": {}},
])
def test_mapping_cannot_run_queries_scripts_network_or_implicit_classification(change):
    with pytest.raises(ConnectorError):
        validate_mapping(mapping() | change)


@pytest.mark.parametrize("fault", ["missing_id", "missing_name", "unknown_type", "wrong_id_type", "bool_id", "array_leading_zero"])
def test_missing_or_unmapped_native_fields_refuse_without_defaults(fault):
    node, fields = native(), mapping()
    if fault == "missing_id":
        node.pop("guid")
    elif fault == "missing_name":
        node["attributes"].pop("name")
    elif fault == "unknown_type":
        node["typeName"] = "NewCustomType"
    elif fault in {"wrong_id_type", "bool_id"}:
        node["guid"] = 1 if fault == "wrong_id_type" else False
        if fault == "bool_id":
            fields["id_encoding"] = "integer_decimal"
    else:
        node["ids"] = ["first"]
        fields["id_pointer"] = "/ids/00"
    with pytest.raises(ConnectorError):
        map_export(json.dumps({"entities": [node]}).encode(), contract=contract(), mapping=fields)


def test_binding_changes_for_identity_coverage_or_mapping_and_ignores_selector_order():
    scope = contract()["scope"]
    original = binding_digest(scope, mapping())
    for key, value in (("provider", "collibra"), ("source_identity", "different-account"),
                       ("namespace", "studio"), ("selectors", ["container:studio"])):
        assert binding_digest(scope | {key: value}, mapping()) != original
    changed = mapping() | {"name_pointer": None}
    assert binding_digest(scope, changed) != original
    extra = scope | {"selectors": ["container:finance", "container:sales"]}
    assert binding_digest(extra, mapping()) == binding_digest(extra | {"selectors": list(reversed(extra["selectors"]))}, mapping())


@pytest.mark.parametrize("bindings", [None, [], {}, {"missing": "a" * 64}, {"finance": "invalid"}, {"finance": True}])
def test_bad_platform_scope_binding_profiles_refuse(bindings, monkeypatch):
    profile = {"endpoint": "https://adapter.invalid/discover", "provider": "atlan", "protocol_version": "1.3",
               "allowed_scopes": ["finance"], "revision": "fixture.1", "source_identity": "fixture-account",
               "inventory_scope_bindings": bindings}
    monkeypatch.setattr(external_adapter, "read_source_secret", lambda *_args, **_kwargs: json.dumps(profile))
    with pytest.raises(ConnectorError):
        external_adapter._profile("KNOWLEDGE_SOURCE_FIXTURE", "finance")


def test_scope_bindings_change_current_profile_pin_but_native_credential_rotation_does_not():
    profile = {"endpoint": "https://adapter.invalid/discover", "provider": "atlan", "protocol_version": "1.3",
               "allowed_scopes": ["finance"], "revision": "fixture.1", "source_identity": "fixture-account"}
    original = inventory_profile_digest(profile)
    assert inventory_profile_digest(profile | {"bearer_token": "rotated-fixture-value"}) == original
    assert inventory_profile_digest(profile | {"inventory_scope_bindings": {"finance": "a" * 64}}) != original


def files(tmp_path):
    result = {}
    for name, value in (("input", {"entities": [native()]}), ("contract", contract()), ("mapping", mapping())):
        path = tmp_path / (name + ".json")
        path.write_text(json.dumps(value))
        result[name] = str(path)
    return result


def test_cli_map_check_receipt_and_pins_never_write_input_or_fetch_sources(tmp_path, capsys):
    paths = files(tmp_path)
    arguments = ["--input", paths["input"], "--contract", paths["contract"], "--mapping", paths["mapping"]]
    assert main(["map", *arguments]) == 0
    package = json.loads(capsys.readouterr().out)
    assert package["records"][0]["native_record"] == native()
    assert main(["check", *arguments]) == 0
    checked = json.loads(capsys.readouterr().out)
    raw = (tmp_path / "input.json").read_bytes()
    encoded = map_export(raw, contract=contract(), mapping=mapping())
    assert checked["input_sha256"] == hashlib.sha256(raw).hexdigest()
    assert checked["export_sha256"] == hashlib.sha256(encoded).hexdigest()
    assert checked["input_sha256"] != checked["export_sha256"]
    assert checked["counts"] == {"entities": 1, "evidence": 1}
    assert checked["qualification"]["vendor_account"] is False
    assert checked["qualification"]["source_completeness"] is False
    assert checked["qualification"]["clock_live_validated"] is False
    assert "Revenue" not in json.dumps(checked) and "synthetic-account" not in json.dumps(checked)
    profile = {"endpoint": "https://adapter.invalid/discover", "provider": "atlan", "protocol_version": "1.3",
               "allowed_scopes": ["finance"], "revision": "fixture.1", "source_identity": "fixture-account",
               "bearer_token": "private-fixture-canary"}
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile))
    original = profile_path.read_bytes()
    assert main(["pins", "--contract", paths["contract"], "--mapping", paths["mapping"], "--profile", str(profile_path)]) == 0
    pins = json.loads(capsys.readouterr().out)
    expected = deepcopy(profile) | {"inventory_scope_bindings": {"finance": binding_digest(contract()["scope"], mapping())}}
    assert pins["profile_digest"] == inventory_profile_digest(expected)
    assert profile_path.read_bytes() == original
    assert "private-fixture-canary" not in json.dumps(pins) and "adapter.invalid" not in json.dumps(pins)


@pytest.mark.parametrize("change", [
    {"allowed_scopes": "finance"}, {"allowed_scopes": ["finance", 7]},
    {"endpoint": "not-a-url"}, {"revision": None}, {"source_identity": ""},
    {"inventory_scope_bindings": {"outside": "bad"}}, {"inventory_scope_bindings": None},
    {"inventory_scope_bindings": {"finance": "bad"}}, {"bearer_token": False},
])
def test_cli_pins_refuses_profiles_rejected_by_runtime(tmp_path, capsys, change):
    paths = files(tmp_path)
    profile = {"endpoint": "https://adapter.invalid/discover", "provider": "atlan", "protocol_version": "1.3",
               "allowed_scopes": ["finance"], "revision": "fixture.1", "source_identity": "fixture-account", **change}
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile))
    original = path.read_bytes()
    assert main(["pins", "--contract", paths["contract"], "--profile", str(path)]) == 1
    checked = json.loads(capsys.readouterr().out)
    assert checked == {"result": "failed", "error_code": "adapter_profile_invalid"}
    assert path.read_bytes() == original


@pytest.mark.parametrize("change,code", [
    ({"observation": None}, "catalog_export_invalid"),
    ({"listing_complete": False}, "catalog_export_incomplete"),
    ({"next_cursor": "unconsumed"}, "catalog_export_incomplete"),
    ({"record_count": "invalid"}, "catalog_export_invalid"),
    ({"record_count": 2}, "catalog_contract_mismatch"),
    ({"observation": contract()["observation"] | {"native_revision": "different"}}, "catalog_contract_mismatch"),
])
def test_cli_check_refuses_invalid_or_contradictory_contract_declarations(tmp_path, capsys, change, code):
    paths = files(tmp_path)
    portable = map_export((tmp_path / "input.json").read_bytes(), contract=contract(), mapping=mapping())
    (tmp_path / "input.json").write_bytes(portable)
    (tmp_path / "contract.json").write_text(json.dumps(contract() | change))
    assert main(["check", "--input", paths["input"], "--contract", paths["contract"]]) == 1
    assert json.loads(capsys.readouterr().out) == {"result": "failed", "error_code": code}


@pytest.mark.parametrize("mode", ["symlink", "fifo", "directory", "malformed", "oversized_contract"])
def test_cli_input_refusals_are_bounded_and_content_free(tmp_path, capsys, mode):
    paths = files(tmp_path)
    path = tmp_path / "input.json"
    path.unlink()
    if mode == "symlink":
        path.symlink_to(paths["contract"])
    elif mode == "fifo":
        os.mkfifo(path)
    elif mode == "directory":
        path.mkdir()
    else:
        path.write_text('private-canary-not-json')
        if mode == "oversized_contract":
            (tmp_path / "contract.json").write_text('x' * 65537)
    assert main(["check", "--input", str(path), "--contract", paths["contract"]]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["result"] == "failed" and "private-canary" not in output
    assert str(tmp_path) not in output
