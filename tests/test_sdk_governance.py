"""SDK paths preserve the registry's source, publication and tenant boundaries."""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.client import KnowledgeClient
from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.guidance import GuidanceRegistry
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeStore,
    KnowledgeValidationError,
)


@pytest.fixture
def store(tmp_path):
    fixtures = tmp_path / "files"
    fixtures.mkdir()
    (fixtures / "approved.csv").write_text("order_id,total\nO-1,12\n", encoding="utf-8")
    (fixtures / "unapproved.csv").write_text("employee_id,salary\nE-1,120000\n", encoding="utf-8")
    value = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}",
                           local_files_root=fixtures)
    yield value
    value.close()


def _policy():
    return SourcePolicy.from_json(json.dumps({"version": 1, "sources": [{
        "tenant_id": "customer", "kind": "local_files",
        "selector": {"path": ".", "include": "approved.csv"},
        "allowed_subjects": ["owner"], "allowed_groups": [],
    }]}))


def _client(store, tenant="customer"):
    return KnowledgeClient(store=store, tenant_id=tenant)


def _connect(client, name="Approved source"):
    return client.connect_source("local_files", {"path": ".", "include": "approved.csv"},
                                 name=name)


def _draft(client, connected):
    snapshot = client.store.get_snapshot(client.tenant_id, connected["connection_id"])
    asset = snapshot["assets"][0]
    evidence = next(item["id"] for item in snapshot["evidence"]
                    if item.get("asset_id") == asset["id"])
    return client.propose_assertion(
        connected["connection_id"], "dataset_description", asset["id"],
        {"description": "One observed source row per customer order."}, [evidence],
        "The owner checked this description against the source inventory.",
    )


@pytest.mark.parametrize("fails", [False, True])
def test_sdk_context_manager_disposes_injected_store_on_success_and_error(store, fails):
    client = _client(store)
    original_pool = store.engine.pool
    failure = RuntimeError("Synthetic caller failure")

    def use_client():
        with client as entered:
            assert entered is client and entered.store is store
            source = _connect(entered)
            assert entered.get_connection(source["connection_id"]) is not None
            if fails:
                raise failure

    if fails:
        with pytest.raises(RuntimeError) as error:
            use_client()
        assert error.value is failure
    else:
        use_client()
    # Closing an injected store retains the SDK's explicit close semantics:
    # the actual SQLAlchemy pool is disposed, rather than leaving handles live.
    assert store.engine.pool is not original_pool


def test_sdk_reconnect_cannot_read_an_unapproved_selector(store, monkeypatch):
    store.access_enforced = True
    store.source_admin_group = "stewards"
    store.source_policy = _policy()
    client = _client(store)
    with principal_scope(Principal("owner", frozenset({"stewards"}))):
        source = store.create_connection("customer", kind="local_files", name="Approved source",
                                         config={"path": ".", "include": "approved.csv"})

        def refuse_io(*args, **kwargs):
            pytest.fail("An unapproved selector reached source discovery")

        monkeypatch.setattr(store, "refresh_connection", refuse_io)
        with pytest.raises(KnowledgeAccessDenied, match="not approved"):
            client.connect_source("local_files", {"path": ".", "include": "unapproved.csv"},
                                  name="Approved source")
        assert store.get_connection("customer", source["id"])["snapshot"] is None


def test_sdk_reconnect_rejects_identity_settings_and_id_conflicts(store):
    client = _client(store)
    source = _connect(client)
    before = store.get_snapshot("customer", source["connection_id"])
    with pytest.raises(KnowledgeAccessDenied, match="different identity"):
        client.connect_source("local_files", {"path": ".", "include": "unapproved.csv"},
                              name="Approved source")
    with pytest.raises(KnowledgeValidationError, match="different settings"):
        client.connect_source("local_files", {"path": ".", "include": "approved.csv", "profile_rows": 10},
                              name="Approved source")
    with pytest.raises(KnowledgeValidationError, match="different connection ID"):
        client.connect_source("local_files", {"path": ".", "include": "approved.csv"},
                              name="Approved source", connection_id="another-connection")
    assert store.get_snapshot("customer", source["connection_id"])["id"] == before["id"]


def test_sdk_refresh_uses_current_policy_and_records_native_freshness(store, monkeypatch):
    client = _client(store)
    first = _connect(client)
    cid = first["connection_id"]
    connection = client.get_connection(cid)
    assert connection["last_refresh_attempt_at"]
    assert connection["last_complete_refresh_at"]
    assert first["snapshot"]["source"]["id"]
    repeated = _connect(client)
    assert repeated["connection_id"] == cid
    assert repeated["snapshot"]["version"] == first["snapshot"]["version"] + 1
    assert repeated["snapshot"]["source"]["id"] == first["snapshot"]["source"]["id"]
    store.source_policy = SourcePolicy(())

    def refuse_io(*args, **kwargs):
        pytest.fail("A withdrawn approval reached source discovery")

    monkeypatch.setattr(store, "refresh_connection", refuse_io)
    with pytest.raises(KnowledgeAccessDenied, match="not approved"):
        _connect(client)


def test_sdk_failed_refresh_keeps_last_complete_metadata_and_reports_error(store, monkeypatch):
    client = _client(store)
    source = _connect(client)
    monkeypatch.setattr("knowledge_layer.curation.curate_source", lambda *args: {
        "status": "error", "discovery_complete": False, "assets": [], "evidence": [],
        "errors": [{"code": "source_unavailable", "message": "Synthetic unavailable source"}],
    })
    failed = _connect(client)
    assert failed["status"] == "error"
    assert failed["snapshot_id"] == source["snapshot_id"]
    assert failed["snapshot"]["version"] == source["snapshot"]["version"]
    assert failed["assets_count"] == source["assets_count"]


def test_sdk_certification_never_approves_drafts_or_incomplete_discovery(store):
    client = _client(store)
    source = _connect(client)
    draft = _draft(client, source)
    before = store.get_snapshot("customer", source["connection_id"])
    with pytest.raises(KnowledgeConflict, match="no_current_owner_review"):
        client.certify_snapshot(source["connection_id"])
    current = store.get_snapshot("customer", source["connection_id"])
    assert current["version"] == before["version"]
    assert next(item for item in current["proposals"] if item["id"] == draft["id"])["status"] == "pending_review"
    with store.engine.connect() as conn:
        assert conn.execute(select(store.decisions)).all() == []
        assert conn.execute(select(store.publications)).all() == []
    store.save_snapshot("customer", source["connection_id"], {
        "snapshot_id": "incomplete", "status": "partial", "discovery_complete": False,
        "assets": [], "evidence": [], "concepts": [], "proposals": [], "errors": [],
    })
    with pytest.raises(KnowledgeConflict, match="discovery_incomplete"):
        client.certify_snapshot(source["connection_id"])


def test_sdk_publication_reports_persisted_flags_and_does_not_rewrite_snapshot(store):
    client = _client(store)
    source = _connect(client)
    approved = _draft(client, source)
    client.decide_assertion(source["connection_id"], approved["id"])
    pending = client.propose_assertion(
        source["connection_id"], "dataset_description", source["snapshot"]["assets"][0]["id"],
        {"description": "An unreviewed alternative source description."},
        approved["evidence_ids"], "This alternative still requires a separate human decision.",
    )
    before = store.get_snapshot("customer", source["connection_id"])
    result = client.certify_snapshot(source["connection_id"], reviewer="arbitrary-spoofed-steward")
    current = store.get_snapshot("customer", source["connection_id"])
    assert result["agent_ready"] == current["agent_ready"] == result["contract"]["agent_ready"] is True
    assert result["meaning_certified"] == current["meaning_certified"] is False
    assert result["publication"]["published_by"] == "operator"
    assert current["id"] == before["id"] and current["version"] == before["version"]
    assert next(item for item in current["proposals"] if item["id"] == pending["id"])["status"] == "pending_review"
    again = client.certify_snapshot(source["connection_id"])
    assert again["snapshot_version"] == before["version"] and again["agent_ready"] is True


@pytest.mark.parametrize("change", ["refresh", "withdraw"])
def test_sdk_certification_refuses_changed_state_during_contract_assembly(store, monkeypatch, change):
    client = _client(store)
    source = _connect(client)
    cid = source["connection_id"]
    draft = _draft(client, source)
    client.decide_assertion(cid, draft["id"])
    reviewed = store.get_snapshot("customer", cid)
    get_contract = client.get_semantic_contract

    def change_after_publication(connection_id):
        if change == "refresh":
            store.refresh_connection("customer", connection_id)
        else:
            store.revoke_publication("customer", connection_id,
                                     reason="The owner withdrew publication during response assembly.")
        return get_contract(connection_id)

    monkeypatch.setattr(client, "get_semantic_contract", change_after_publication)
    with pytest.raises(KnowledgeConflict, match="response assembly"):
        client.certify_snapshot(cid)
    current = store.get_snapshot("customer", cid)
    assert current["agent_ready"] is False
    assert current["version"] == reviewed["version"] + (change == "refresh")


def test_sdk_reviewer_label_cannot_grant_publication_authority(store):
    client = _client(store)
    source = _connect(client)
    draft = _draft(client, source)
    client.decide_assertion(source["connection_id"], draft["id"])
    store.grant_source_access("customer", source["connection_id"], principal_type="user",
                              principal_id="manager", role="manage")
    store.source_policy = _policy()
    store.source_admin_group = "stewards"
    store.access_enforced = True
    with (principal_scope(Principal("manager", frozenset(), frozenset({"knowledge.sources.manage"}))),
          pytest.raises(KnowledgeAccessDenied, match="steward")):
        client.certify_snapshot(source["connection_id"], reviewer="data_steward")


def test_sdk_mcp_tools_are_bound_to_the_client_tenant(store):
    from mcp.server.mcpserver.exceptions import ToolError

    client = _client(store)
    own = _connect(client, "Customer source")
    foreign = _connect(_client(store, "demo"), "Foreign demo source")
    server = client.get_mcp_server(enable_source_management=True, enable_autonomy=True)
    guidance = {
        "title": "Customer instructions", "description": "Only this customer audience.",
        "business_units": [], "tasks": [],
        "audience": {"company_wide": True, "subjects": [], "groups": []},
        "skills": [], "rules": [{"key": "source-writes", "statement": "Source writes are forbidden.",
                                   "value": False}], "dependencies": [],
    }
    for tenant, title in (("customer", "Customer instructions"), ("demo", "Foreign instructions")):
        GuidanceRegistry(store).draft(tenant, "research", {**guidance, "title": title})

    async def run():
        tools = await server.list_tools()
        assert all("tenant_id" not in tool.input_schema.get("properties", {}) for tool in tools)
        listed = (await server.call_tool("list_knowledge_sources", {})).structured_content
        assert listed["tenant"] == "customer"
        assert [item["id"] for item in listed["connections"]] == [own["connection_id"]]
        for name, arguments in (
            ("get_semantic_contract", {"connection_id": own["connection_id"]}),
            ("get_ontology_export", {"connection_id": own["connection_id"]}),
            ("get_workspace_graph", {}),
            ("get_workspace_ontology", {}),
            ("get_maintenance_work", {}),
            ("get_guidance", {"slug": "research"}),
            ("get_task_context", {"query": "order", "connection_ids": [own["connection_id"]]}),
            ("get_inventory", {"connection_id": own["connection_id"], "operation": "scopes", "payload": {}}),
        ):
            result = (await server.call_tool(name, arguments)).structured_content
            assert result is not None, name
            assert "Foreign" not in json.dumps(result), name
        for name, arguments in (
            ("get_semantic_contract", {"connection_id": foreign["connection_id"]}),
            ("get_ontology_export", {"connection_id": foreign["connection_id"]}),
            ("get_task_context", {"query": "order", "connection_ids": [foreign["connection_id"]]}),
            ("get_inventory", {"connection_id": foreign["connection_id"], "operation": "scopes", "payload": {}}),
            ("build_knowledge", {"operation": "start", "arguments": {"connection_ids": [foreign["connection_id"]]}}),
            ("get_autonomy_policy", {"connection_id": foreign["connection_id"]}),
        ):
            with pytest.raises(ToolError):
                await server.call_tool(name, arguments)
        created = (await server.call_tool("connect_knowledge_source", {
            "name": "Bound MCP source", "kind": "local_files", "config": {"path": ".", "include": "approved.csv"},
        })).structured_content
        cid = created["connection"]["id"]
        assert store.get_connection("customer", cid) is not None
        assert store.get_connection("demo", cid) is None

    asyncio.run(run())


@pytest.mark.parametrize("method", ["translate_catalog", "import_catalog"])
def test_sdk_catalog_translation_is_explicit_nonpersistent_and_preserves_provenance(store, method):
    client = _client(store)
    scope = {"provider": "alation", "source_identity": "customer-production-catalog",
             "namespace": "finance", "scope_id": "orders", "selectors": ["orders"]}
    observation = {"source_observed_at": "2026-10-08T12:00:00Z", "native_revision": "export-v2"}
    result = getattr(client, method)([{"id": 1, "name": "orders", "table_type": "TABLE"}],
                                     catalog_format="alation", scope=scope, observation=observation,
                                     listing_complete=True)
    assert result["operation"] == "translation" and result["persisted"] is False
    assert result["scope"] == scope
    assert result["package"]["observation"]["source_observed_at"] == observation["source_observed_at"]
    assert result["package"]["observation"]["native_revision"] == observation["native_revision"]
    assert result["package"]["observation"]["exported_at"] is None
    assert client.list_connections() == []
    with pytest.raises(ConnectorError) as exc:
        getattr(client, method)([], catalog_format="alation", scope=scope, listing_complete=False)
    assert exc.value.code == "catalog_export_incomplete"
