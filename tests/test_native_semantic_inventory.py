"""Actual sample HTTP -> platform inventory -> selected evidence readbacks."""

import importlib.util
import json
import os
import threading
from pathlib import Path

import httpx
import pytest
from test_native_inventory_paging import controls

from knowledge_layer.adapters.catalog import SCHEMA_VERSION
from knowledge_layer.adapters.catalog_mapping import binding_digest
from knowledge_layer.adapters.paging import NativeInventoryPager, continuation_request
from knowledge_layer.inventory import InventoryStore
from knowledge_layer.inventory_refresh import inventory_profile_digest
from knowledge_layer.store import KnowledgeStore


def load_sample():
    root = Path(__file__).resolve().parents[1]
    for name, filename in (("reference_adapter", "reference_adapter.py"),
                           ("native_semantic_sample", "native_semantic_adapter.py")):
        spec = importlib.util.spec_from_file_location(name, root / "examples" / filename)
        module = importlib.util.module_from_spec(spec)
        import sys
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def manifest(namespace, count=101):
    return {"metadata": {"dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
                         "generated_at": "2026-01-01T00:00:00Z", "project_name": namespace,
                         "dbt_version": "1.10.0", "invocation_id": "00000000-0000-0000-0000-000000000001",
                         "env": {"PRIVATE_EXCLUDED": "canary"}},
            "metrics": {f"metric.{namespace}.revenue_{index}": {
                "unique_id": f"metric.{namespace}.revenue_{index}", "name": "Revenue",
                "resource_type": "metric", "package_name": namespace, "path": "metrics/revenue.yml",
                "original_file_path": "models/metrics/revenue.yml", "fqn": [namespace, "Revenue"],
                "label": "Revenue",
                "description": "Revenue policy " + namespace,
                "type": "simple", "type_params": {"measure": {"name": "revenue"}},
                "config": {"meta": {"unit": "USD" if namespace == "finance" else "EUR",
                                    "unknown": None, "zero": 0, "empty": "", "false": False}},
                "depends_on": {"nodes": [f"semantic_model.{namespace}.ledger"]},
            } for index in range(count)}}


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    sample = load_sample()
    profile = {}
    for namespace in ("finance", "studio"):
        artifact = manifest(namespace)
        path = tmp_path / f"{namespace}.json"
        path.write_text(json.dumps(artifact))
        profile[namespace] = {"provider": "dbt", "namespace": namespace, "manifest": str(path),
                              "unique_ids": list(artifact["metrics"])}
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile))
    profile_path.chmod(0o600)
    monkeypatch.setenv("NATIVE_ADAPTER_PROFILE_FILE", str(profile_path))
    monkeypatch.setenv("ADAPTER_BEARER_TOKEN", "fixture-native-token")
    sample.NativeHandler.pager = NativeInventoryPager()
    server = sample.BoundedHTTPServer(("127.0.0.1", 0), sample.NativeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/discover", profile_path, profile
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


def post(endpoint, request, *, token="fixture-native-token"):
    return httpx.post(endpoint, json=request, headers={"Authorization": "Bearer " + token}, timeout=5)


def test_actual_sample_refuses_auth_scope_or_provider_and_never_leaks_profile(adapter):
    endpoint, _, _ = adapter
    for request, token, status in ((controls(), "invalid", 401),
                                   (controls(scope="missing"), "fixture-native-token", 403),
                                   (controls(provider="datahub"), "fixture-native-token", 403)):
        response = post(endpoint, request, token=token)
        assert response.status_code == status
        assert "fixture-native-token" not in response.text and "manifest" not in response.text


def test_actual_sample_reloads_scope_and_token_between_pages(adapter):
    endpoint, path, profile = adapter
    request = controls()
    first = post(endpoint, request)
    assert first.status_code == 200 and not first.json()["listing_complete"]
    profile["finance"]["namespace"] = "revised-authority"
    path.write_text(json.dumps(profile))
    refused = post(endpoint, continuation_request(request, first.json()))
    assert refused.status_code == 422 and refused.json()["error"] == "native_inventory_scan_lost"
    profile.pop("finance")
    path.write_text(json.dumps(profile))
    assert post(endpoint, continuation_request(request, first.json())).status_code == 403


def test_actual_sample_strict_request_json_and_read_only_artifact_failure(adapter):
    endpoint, _, profile = adapter
    response = httpx.post(endpoint, content=b'{"scope":"finance","scope":"studio"}',
                          headers={"Authorization": "Bearer fixture-native-token"}, timeout=5)
    assert response.status_code == 422
    Path(profile["finance"]["manifest"]).unlink()
    refused = post(endpoint, controls())
    assert refused.status_code == 422 and "finance.json" not in refused.text


@pytest.mark.parametrize("replacement", ["symlink", "fifo", "directory"])
def test_actual_sample_refuses_nonregular_or_followed_artifacts(adapter, tmp_path, replacement):
    endpoint, _, profile = adapter
    path = Path(profile["finance"]["manifest"])
    path.unlink()
    if replacement == "symlink":
        target = tmp_path / "other-artifact.json"
        target.write_text(json.dumps(manifest("finance")))
        path.symlink_to(target)
    elif replacement == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
    refused = post(endpoint, controls())
    assert refused.status_code == 422 and "finance.json" not in refused.text


def test_two_business_units_refresh_and_retrieve_full_conflicting_native_meaning(adapter, tmp_path, monkeypatch):
    endpoint, _, _ = adapter
    monkeypatch.setenv("KNOWLEDGE_ALLOW_INSECURE_ADAPTER_ENDPOINTS", "1")
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.sqlite'}", local_files_root=tmp_path)
    try:
        identities = []
        for namespace in ("finance", "studio"):
            name = "KNOWLEDGE_SOURCE_NATIVE_" + namespace.upper()
            protected = tmp_path / f"{namespace}-source.json"
            protected.write_text(json.dumps({"endpoint": endpoint, "provider": "dbt", "allowed_scopes": [namespace],
                "protocol_version": "1.3", "revision": "fixture.1", "source_identity": namespace,
                "bearer_token": "fixture-native-token"}))
            protected.chmod(0o600)
            monkeypatch.setenv(name + "_FILE", str(protected))
            source = store.create_connection("demo", kind="external_adapter", name=namespace, config={
                "secret_ref": name, "scope": namespace, "discovery_mode": "inventory",
                "retain_inventory_payloads": True})
            refreshed = store.refresh_connection("demo", source["id"])
            inventory = refreshed["inventory"]
            assert refreshed["snapshot"] is None and inventory["entity_count"] == inventory["evidence_count"] == 101
            assert inventory["page_count"] == 2 and inventory["status"] == "committed"
            reader = InventoryStore(store)
            page = reader.page("demo", source["id"], inventory["id"], collection="entities")
            entity = page["items"][0]
            # Use the same explicit selection API available to the harness.
            selected = reader.select("demo", source["id"], inventory["id"],
                                     expected_manifest_digest=inventory["manifest_digest"], entity_ids=[entity["id"]])
            assert len(selected["entities"]) == len(selected["evidence"]) == 1
            proof = selected["evidence"][0]["payload"]
            assert proof["native_record"]["name"] == "Revenue"
            assert proof["native_record"]["config"]["meta"] == {
                "unit": "USD" if namespace == "finance" else "EUR", "unknown": None, "zero": 0,
                "empty": "", "false": False}
            assert "env" not in proof["artifact_metadata"]
            assert proof["native_record"]["depends_on"]["nodes"] == [f"semantic_model.{namespace}.ledger"]
            assert selected["entities"][0]["payload"]["locally_executable"] is False
            identities.append(entity["id"])
        assert identities[0] != identities[1]
    finally:
        store.close()


def portable_package(provider, namespace="finance", count=101):
    return {"schema_version": SCHEMA_VERSION,
            "scope": {"provider": provider, "source_identity": "synthetic-" + provider,
                      "namespace": namespace, "scope_id": namespace, "selectors": ["domain:" + namespace]},
            "observation": {"exported_at": "2026-10-03T00:00:00Z", "source_observed_at": None,
                            "native_revision": 0, "field_coverage": {"metrics": "partial", "policies": "unknown"},
                            "redaction": "unknown"},
            "listing_complete": True, "next_cursor": None, "record_count": count,
            "records": [{"native_id": str(index), "native_type": "NativeMetric", "kind": "semantic_definition",
                         "name": "Revenue", "native_record": {"id": index, "name": "Revenue",
                             "expression": "sum(net_amount)" if namespace == "finance" else "sum(gross_amount)",
                             "unit": "USD" if namespace == "finance" else "EUR", "owner": "Finance",
                             "policy_refs": ["native-policy"], "relationships": [{"id": "unresolved-asset"}],
                             "unknown": None, "zero": 0, "empty": "", "false": False}}
                        for index in range(count)]}


def test_agnostic_exports_refresh_and_scope_substitution_cannot_establish_removals(adapter, tmp_path, monkeypatch):
    endpoint, native_path, native_profile = adapter
    monkeypatch.setenv("KNOWLEDGE_ALLOW_INSECURE_ADAPTER_ENDPOINTS", "1")
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'portable.sqlite'}", local_files_root=tmp_path)
    try:
        identities = []
        for provider, namespace in (("databricks", "finance"), ("atlan", "studio")):
            artifact = portable_package(provider, namespace)
            artifact_path = tmp_path / f"{provider}-export.json"
            artifact_path.write_text(json.dumps(artifact))
            profile = {"endpoint": endpoint, "provider": provider, "allowed_scopes": [namespace],
                       "protocol_version": "1.3", "revision": "fixture.1", "source_identity": "synthetic-" + provider,
                       "inventory_scope_bindings": {namespace: binding_digest(artifact["scope"])},
                       "bearer_token": "fixture-native-token"}
            native_profile[namespace] = {"provider": provider, "format": "catalog_export", "artifact": str(artifact_path),
                                         "expected_scope": artifact["scope"], "platform_profile": profile}
            native_path.write_text(json.dumps(native_profile))
            reference = "KNOWLEDGE_SOURCE_PORTABLE_" + provider.upper()
            source_path = tmp_path / f"{provider}-platform.json"
            source_path.write_text(json.dumps(profile))
            source_path.chmod(0o600)
            monkeypatch.setenv(reference + "_FILE", str(source_path))
            source = store.create_connection("demo", kind="external_adapter", name=provider, config={
                "secret_ref": reference, "scope": namespace, "discovery_mode": "inventory", "retain_inventory_payloads": True})
            refreshed = store.refresh_connection("demo", source["id"])
            manifest = refreshed["inventory"]
            assert manifest["entity_count"] == manifest["evidence_count"] == 101 and manifest["page_count"] == 2
            reader = InventoryStore(store)
            item = reader.page("demo", source["id"], manifest["id"], collection="entities")["items"][0]
            selected = reader.select("demo", source["id"], manifest["id"],
                                     expected_manifest_digest=manifest["manifest_digest"], entity_ids=[item["id"]])
            proof = selected["evidence"][0]["payload"]
            assert proof["native_record"]["unit"] == ("USD" if namespace == "finance" else "EUR")
            assert proof["observation"]["source_observed_at"] is None
            assert proof["observation"]["field_coverage"]["policies"] == "unknown"
            assert not selected["meaning_certified"] and not selected["agent_ready"]
            identities.append(item["id"])
            request = controls(provider=provider, scope=namespace, profile_digest=inventory_profile_digest(profile))
            # An unchanged operator target cannot import a different account.
            artifact["scope"]["source_identity"] = "substituted-account"
            artifact_path.write_text(json.dumps(artifact))
            failed = post(endpoint, request)
            assert failed.status_code == 422 and failed.json()["error"] == "catalog_scope_mismatch"
            assert reader.status("demo", source["id"], manifest["id"])["manifest_digest"] == manifest["manifest_digest"]
            # Changing the local expected population without updating the platform
            # binding must also fail before returning a first page.
            native_profile[namespace]["expected_scope"]["source_identity"] = "substituted-account"
            native_path.write_text(json.dumps(native_profile))
            failed = post(endpoint, request)
            assert failed.status_code == 422 and failed.json()["error"] == "catalog_scope_binding_changed"
            native_profile[namespace]["platform_profile"] = None
            native_path.write_text(json.dumps(native_profile))
            failed = post(endpoint, request)
            assert failed.status_code == 422 and failed.json()["error"] == "adapter_profile_invalid"
        assert identities[0] != identities[1]
    finally:
        store.close()
