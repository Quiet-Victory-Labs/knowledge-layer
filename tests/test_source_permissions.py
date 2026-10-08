"""Whole-source permission gates never leave derivative metadata visible."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select, update

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.inventory import InventoryStore
from knowledge_layer.retrieval import search_knowledge
from knowledge_layer.source_permissions import digest, manifest_digest
from knowledge_layer.source_policy import SourcePolicy, source_identity
from knowledge_layer.store import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeStore,
)

OWNER = Principal("owner", frozenset(), frozenset({"knowledge.sources.manage"}))
READER = Principal("reader", frozenset({"analysts"}))
OUTSIDER = Principal("outsider", frozenset({"analysts"}))
SELECTOR = {"path": ".", "include": "**/*"}


def _policy(manifests, *, subjects=(), groups=("analysts",), state="complete", basis="operator_partition",
            checked=None, expires=None, trusted=()):
    checked = checked or datetime.now(UTC) - timedelta(seconds=1)
    expires = expires or checked + timedelta(seconds=300)
    identity = source_identity("local_files", SELECTOR)
    return {"version": 2, "sources": [{"tenant_id": "demo", "kind": "local_files",
        "selector": deepcopy(SELECTOR), "allowed_subjects": ["owner"], "allowed_groups": []}],
        "metadata_permissions": {"enforce": True, "trusted_native_exporters": list(trusted),
        "attestations": [{"tenant_id": "demo", "kind": "local_files", "selector": deepcopy(SELECTOR),
            "source_identity_digest": digest(identity), "basis": basis, "authority": "customer-exporter",
            "revision": "acl-r1", "state": state, "checked_at": checked.isoformat(),
            "expires_at": expires.isoformat(), "allowed_subjects": list(subjects),
            "allowed_groups": list(groups), "manifest_digests": list(manifests),
            "resource_manifest_digest": manifest_digest(manifests)}]}}


def _manifests(store, source_id):
    with store.engine.connect() as conn:
        return [row["source_permission_manifest_digest"] for row in conn.execute(select(
            store.snapshots.c.payload).where(store.snapshots.c.connection_id == source_id)
            .order_by(store.snapshots.c.version)).scalars()]


@pytest.fixture
def registry(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'permissions.db'}")
    source = store.create_connection("demo", kind="local_files", name="Private files", config={})
    store.save_snapshot("demo", source["id"], {"snapshot_id": "private-v1", "status": "ready",
        "source": {"id": "source.private"},
        "assets": [{"id": "asset.private", "name": "Private payroll", "schema": [{"name": "salary"}]}],
        "concepts": [{"id": "concept.private", "name": "Secret salary"}],
        "evidence": [{"id": "evidence.private", "source_pointer": "salary", "value": "confidential"}]})
    for subject, role in (("owner", "manage"), ("reader", "read")):
        store.grant_source_access("demo", source["id"], principal_type="user", principal_id=subject, role=role)
    store.source_policy = SourcePolicy.from_json(json.dumps(_policy(_manifests(store, source["id"]))))
    store.access_enforced = True
    yield store, source["id"]
    store.close()


def _assert_hidden(store, source_id):
    assert store.list_connections("demo") == []
    assert store.page_connections("demo")["connections"] == []
    assert store.count_connections("demo") == 0
    assert store.get_connection("demo", source_id) is None
    assert store.get_snapshot("demo", source_id) is None
    assert store.get_snapshot("demo", source_id, version=1) is None
    assert store.get_concepts("demo") == []
    assert store.search_concept_candidates("demo", "Secret") == []
    assert list(store.iter_current_evidence_sources("demo")) == []
    assert not search_knowledge(store, tenant_id="demo", query="Secret")["results"]
    with pytest.raises(KnowledgeNotFound):
        store.get_graph("demo", source_id)
    with pytest.raises(KnowledgeNotFound):
        store.publication_preview("demo", source_id)


def test_audience_is_separate_from_management_and_intersects_registry_grant(registry):
    store, source_id = registry
    with principal_scope(READER):
        assert store.get_snapshot("demo", source_id)["id"] == "private-v1"
        assert store.search_concept_candidates("demo", "Secret")
        assert list(store.iter_current_evidence_sources("demo"))
        assert store.get_graph("demo", source_id)["connection_id"] == source_id
    with principal_scope(OUTSIDER):
        _assert_hidden(store, source_id)
    with principal_scope(OWNER):
        _assert_hidden(store, source_id)


@pytest.mark.parametrize("state", ["unknown", "partial", "stale", "withdrawn"])
def test_incomplete_permission_denies_every_shared_read(registry, state):
    store, source_id = registry
    store.source_policy = SourcePolicy.from_json(json.dumps(_policy(_manifests(store, source_id), state=state)))
    with principal_scope(READER):
        _assert_hidden(store, source_id)


@pytest.mark.parametrize("time_case", ["expired", "future"])
def test_permission_clock_refuses_expired_and_future_evidence(registry, time_case):
    store, source_id = registry
    checked = datetime.now(UTC) + timedelta(seconds=30 if time_case == "future" else -600)
    store.source_policy = SourcePolicy.from_json(json.dumps(_policy(_manifests(store, source_id),
        checked=checked, expires=checked + timedelta(seconds=300))))
    with principal_scope(READER):
        _assert_hidden(store, source_id)


def test_rotation_withdrawal_and_invalid_loader_do_not_reuse_old_authority(registry):
    store, source_id = registry
    current = _policy(_manifests(store, source_id))
    store.source_policy_loader = lambda: SourcePolicy.from_json(json.dumps(current))
    with principal_scope(READER):
        assert store.count_connections("demo") == 1
        current["metadata_permissions"]["attestations"] = []
        _assert_hidden(store, source_id)
        current.clear()
        with pytest.raises(KnowledgeAccessDenied, match="Current operator"):
            store.get_connection("demo", source_id)


def test_enforcement_cannot_silently_downgrade_during_rotation(registry):
    store, source_id = registry
    with principal_scope(READER):
        assert store.count_connections("demo") == 1
        store.source_policy = SourcePolicy.from_json(json.dumps({"version": 1, "sources": []}))
        _assert_hidden(store, source_id)


def test_installed_enforcement_is_latched_before_the_first_loader_request(registry):
    store, source_id = registry
    store.source_policy_loader = lambda: SourcePolicy.from_json(json.dumps({"version": 1, "sources": []}))
    with principal_scope(OWNER):
        _assert_hidden(store, source_id)


def test_hidden_manager_can_enqueue_and_retry_onboarding_without_metadata_or_duplicates(registry):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from knowledge_layer.api import create_knowledge_router
    from knowledge_layer.jobs import RefreshQueue
    from knowledge_layer.source_onboarding import connect_source, refresh_source

    store, source_id = registry
    app = FastAPI()
    app.include_router(create_knowledge_router(store=store, durable_jobs=True))
    with principal_scope(OWNER), TestClient(app) as client:
        result = client.post(f"/api/knowledge/connections/{source_id}/refresh")
        assert result.status_code == 202
        job = result.json()["job"]
        assert RefreshQueue(store).get("demo", job["id"])["status"] == "queued"
        ack = refresh_source(store, tenant_id="demo", allowed_kinds=frozenset({"local_files"}),
                             connection_id=source_id, durable_jobs=True)
        assert ack["connection"] == {"id": source_id, "metadata_unavailable": True}
        for _ in range(2):
            reused = connect_source(store, tenant_id="demo", allowed_kinds=frozenset({"local_files"}),
                kind="local_files", name="Private files", config={}, durable_jobs=True)
            assert reused["reused_existing"] is True and reused["job"]["id"] == job["id"]
        assert "salary" not in json.dumps([result.json(), ack, reused])
        _assert_hidden(store, source_id)
    with store.engine.connect() as conn:
        assert len(conn.execute(select(store.connections.c.id)).all()) == 1


@pytest.mark.parametrize("field,value", [("errors", [{"source_pointer": "secret-payroll.pdf"}]),
    ("profiling", {"description": "private source detail"}),
    ("concepts", [{"id": "new-private-concept", "definition": "private native meaning"}]),
    ("metrics", [{"id": "private-metric", "description": "private native metric"}])])
def test_new_native_metadata_outside_asset_lists_invalidates_permission(registry, field, value):
    store, source_id = registry
    with store.engine.connect() as conn:
        previous = deepcopy(conn.execute(select(store.snapshots.c.payload)).scalar_one())
    pin = previous["source_permission_manifest_digest"]
    previous[field] = value
    store.access_enforced = False
    store.save_snapshot("demo", source_id, previous, source_refresh=True)
    assert store.get_snapshot("demo", source_id)["source_permission_manifest_digest"] != pin
    store.access_enforced = True
    with principal_scope(READER):
        _assert_hidden(store, source_id)


def test_legacy_partial_manifest_versions_are_not_accepted(registry):
    store, source_id = registry
    with store.engine.begin() as conn:
        payload = conn.execute(select(store.snapshots.c.payload)).scalar_one()
        payload.pop("source_permission_manifest_version")
        conn.execute(update(store.snapshots).values(payload=payload))
    with principal_scope(READER):
        _assert_hidden(store, source_id)


def test_local_review_preserves_native_lineage_but_stale_parent_refuses(registry):
    store, source_id = registry
    policy = _policy(_manifests(store, source_id), subjects=("owner",), groups=("analysts",))
    store.source_policy = SourcePolicy.from_json(json.dumps(policy))
    with principal_scope(OWNER):
        initial = store.get_snapshot("demo", source_id)
        annotated = {**initial, "snapshot_id": "local-review", "concepts": [
            {"id": "local-annotation", "definition": "Steward supplied interpretation of the same native resources."}]}
        reviewed = store._save_review_snapshot("demo", source_id, annotated,
            parent=(initial["id"], initial["version"]))
        assert reviewed["source_permission_manifest_digest"] == initial["source_permission_manifest_digest"]
        assert store.get_snapshot("demo", source_id)["id"] == "local-review"
        with pytest.raises(KnowledgeConflict, match="snapshot changed"):
            store._save_review_snapshot("demo", source_id, annotated,
                parent=(initial["id"], initial["version"]))


def test_legacy_demo_review_does_not_manufacture_native_permission_lineage(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'legacy.db'}")
    try:
        source = store.create_connection("demo", kind="local_files", name="Legacy demo", config={})
        store.save_snapshot("demo", source["id"], {"snapshot_id": "legacy", "status": "ready"})
        with store.engine.begin() as conn:
            native = dict(conn.execute(select(store.snapshots.c.payload)).scalar_one())
            for control in ("source_permission_manifest_version", "source_permission_manifest_digest",
                            "source_permission_identity_digest"):
                native.pop(control, None)
            conn.execute(update(store.snapshots).values(payload=native))
        initial = store.get_snapshot("demo", source["id"])
        reviewed = store._save_review_snapshot("demo", source["id"],
            {**initial, "snapshot_id": "reviewed-legacy", "concepts": [
                {"id": "meaning", "definition": "A reviewed interpretation of legacy demo metadata."}]},
            parent=(initial["id"], initial["version"]))
        assert reviewed["id"] == "reviewed-legacy"
        assert "source_permission_manifest_version" not in reviewed
        store.grant_source_access("demo", source["id"], principal_type="user", principal_id="reader", role="read")
        store.source_policy = SourcePolicy.from_json(json.dumps({"version": 2, "sources": [],
            "metadata_permissions": {"enforce": True, "trusted_native_exporters": [], "attestations": []}}))
        store.access_enforced = True
        with principal_scope(READER):
            _assert_hidden(store, source["id"])
    finally:
        store.close()


def test_withdrawn_source_approval_and_retargeted_identity_deny(registry):
    store, source_id = registry
    payload = _policy(_manifests(store, source_id))
    payload["sources"] = []
    store.source_policy = SourcePolicy.from_json(json.dumps(payload))
    with principal_scope(READER):
        _assert_hidden(store, source_id)
    store.source_policy = SourcePolicy.from_json(json.dumps(_policy(_manifests(store, source_id))))
    with store.engine.begin() as conn:
        conn.execute(update(store.connections).where(store.connections.c.id == source_id)
                     .values(config={"path": "payroll", "include": "**/*"}))
    with principal_scope(READER):
        _assert_hidden(store, source_id)


def test_inventory_complete_and_historical_manifests_are_exhaustive(registry):
    store, source_id = registry
    inventory = InventoryStore(store)
    store.access_enforced = False
    run = inventory.begin("demo", source_id,
        scope={"namespace": "finance", "selectors": ["terms"]},
        policy={"id": "metadata-only", "revision": None}, watermark={"kind": "unknown", "value": None},
        limits={"max_pages": 10, "max_entities": 10, "max_evidence": 10, "max_total_bytes": 65536})
    run = inventory.append("demo", source_id, run["id"], page_index=0, cursor=None, next_cursor=None,
        entities=[{"id": "native.term", "kind": "semantic_definition", "payload": {"name": "Secret inventory"},
                   "evidence_ids": []}], evidence=[],
        **{key: run[key] for key in ("scope_digest", "policy_digest", "watermark_digest")})
    run = inventory.commit("demo", source_id, run["id"], completion={"listing_complete": True,
        "pages": 1, "entities": 1, "evidence": 0, "manifest_digest": run["manifest_digest"]})
    store.access_enforced = True
    with principal_scope(READER):
        _assert_hidden(store, source_id)
        with pytest.raises(KnowledgeNotFound):
            inventory.page("demo", source_id, run["id"])
    store.source_policy = SourcePolicy.from_json(json.dumps(_policy(
        [*_manifests(store, source_id), run["manifest_digest"]])))
    with principal_scope(READER):
        assert inventory.page("demo", source_id, run["id"])["items"][0]["payload"]["name"] == "Secret inventory"
        assert store.get_snapshot("demo", source_id)


def test_table_selector_order_is_identity_equivalent_and_broadening_denies(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'tables.db'}")
    source = store.create_connection("demo", kind="postgres", name="Tables",
        config={"secret_ref": "KNOWLEDGE_SOURCE_PARTITION", "schema": "public", "tables": ["z", "a"]})
    store.save_snapshot("demo", source["id"], {"snapshot_id": "tables-v1", "status": "ready"})
    store.grant_source_access("demo", source["id"], principal_type="user", principal_id="reader", role="read")
    payload = _policy(_manifests(store, source["id"]))
    selector = {"secret_ref": "KNOWLEDGE_SOURCE_PARTITION", "schema": "public", "tables": ["a", "z"]}
    payload["sources"][0].update(kind="postgres", selector=selector)
    payload["metadata_permissions"]["attestations"][0].update(kind="postgres", selector=selector,
        source_identity_digest=digest(source_identity("postgres", selector)))
    store.source_policy = SourcePolicy.from_json(json.dumps(payload))
    store.access_enforced = True
    with principal_scope(READER):
        assert store.get_snapshot("demo", source["id"])
    with store.engine.begin() as conn:
        conn.execute(update(store.connections).where(store.connections.c.id == source["id"])
                     .values(config={**selector, "tables": ["a", "z", "private"]}))
    with principal_scope(READER):
        assert store.get_snapshot("demo", source["id"]) is None
    store.close()


def test_new_or_unqualified_historical_resources_hide_entire_source(registry):
    store, source_id = registry
    store.access_enforced = False
    store.save_snapshot("demo", source_id, {"snapshot_id": "private-v2", "status": "ready",
        "assets": [{"id": "new.document", "name": "New private document"}]})
    store.access_enforced = True
    with principal_scope(READER):
        _assert_hidden(store, source_id)
    store.source_policy = SourcePolicy.from_json(json.dumps(_policy(_manifests(store, source_id))))
    with principal_scope(READER):
        assert store.get_snapshot("demo", source_id, version=1)["id"] == "private-v1"
        assert store.get_snapshot("demo", source_id)["id"] == "private-v2"
    with store.engine.begin() as conn:
        row = conn.execute(select(store.snapshots.c.row_id, store.snapshots.c.payload).where(
            store.snapshots.c.connection_id == source_id, store.snapshots.c.version == 1)).mappings().one()
        payload = deepcopy(row["payload"])
        payload.pop("source_permission_manifest_digest")
        conn.execute(update(store.snapshots).where(store.snapshots.c.row_id == row["row_id"]).values(payload=payload))
    with principal_scope(READER):
        _assert_hidden(store, source_id)


def test_authorized_manager_can_repair_hidden_source_without_receiving_source_text(registry):
    store, source_id = registry
    with principal_scope(OWNER):
        result = store.refresh_connection("demo", source_id, curator=lambda *_: {
            "snapshot_id": "private-v2", "status": "ready", "assets": [{"name": "REPAIR SECRET"}]})
        assert result["snapshot"] is None
        assert result["connection"]["metadata_unavailable"] is True
        assert "SECRET" not in json.dumps(result)
        _assert_hidden(store, source_id)


def test_source_approval_revoked_during_discovery_prevents_commit(registry):
    store, source_id = registry
    policy = _policy(_manifests(store, source_id))
    store.source_policy_loader = lambda: SourcePolicy.from_json(json.dumps(policy))

    def revoked(*_args):
        policy["sources"] = []
        return {"snapshot_id": "revoked-read", "status": "ready", "assets": [{"name": "REVOKED"}]}

    with principal_scope(OWNER), pytest.raises(KnowledgeAccessDenied):
        store.refresh_connection("demo", source_id, curator=revoked)
    with store.engine.connect() as conn:
        assert conn.execute(select(store.snapshots.c.snapshot_id).where(
            store.snapshots.c.connection_id == source_id)).scalars().all() == ["private-v1"]


def test_native_exporter_requires_explicit_trust(registry):
    store, source_id = registry
    for trusted, visible in (((), False), (("customer-exporter",), True)):
        store.source_policy = SourcePolicy.from_json(json.dumps(_policy(_manifests(store, source_id),
            basis="native_exporter", trusted=trusted)))
        with principal_scope(READER):
            assert (store.get_connection("demo", source_id) is not None) is visible


def test_shared_sql_gate_uses_one_query_and_compiles_for_postgres(registry):
    from sqlalchemy.dialects import postgresql

    store, _source_id = registry
    statements = []
    observer = lambda _conn, _cursor, statement, *_args: statements.append(statement)
    event.listen(store.engine, "before_cursor_execute", observer)
    try:
        with principal_scope(READER):
            assert store.count_connections("demo") == 1
            assert len(statements) == 1
            store.backend = "postgresql"
            clause = store._access_clause("demo", store.connections.c.id)
            select(store.connections.c.id).where(clause).compile(dialect=postgresql.dialect())
    finally:
        store.backend = "sqlite"
        event.remove(store.engine, "before_cursor_execute", observer)


def test_connector_cannot_supply_authoritative_manifest_or_identity_pins(registry):
    store, source_id = registry
    store.access_enforced = False
    store.save_snapshot("demo", source_id, {"snapshot_id": "forged", "status": "ready",
        "assets": [{"name": "new native document"}],
        "source_permission_manifest_digest": _manifests(store, source_id)[0],
        "source_permission_identity_digest": "f" * 64})
    stored = store.get_snapshot("demo", source_id)
    assert stored["source_permission_manifest_digest"] != _manifests(store, source_id)[0]
    assert stored["source_permission_identity_digest"] != "f" * 64
    store.access_enforced = True
    with principal_scope(READER):
        _assert_hidden(store, source_id)


@pytest.mark.parametrize("mutation", ["identity", "manifest", "lifetime", "audience", "unknown", "limit"])
def test_malformed_attestations_fail_closed_at_parse(registry, mutation):
    store, source_id = registry
    payload = _policy(_manifests(store, source_id))
    item = payload["metadata_permissions"]["attestations"][0]
    if mutation == "identity":
        item["selector"]["include"] = "*.pdf"
    elif mutation == "manifest":
        item["resource_manifest_digest"] = "0" * 64
    elif mutation == "lifetime":
        item["expires_at"] = (datetime.fromisoformat(item["checked_at"]) + timedelta(seconds=901)).isoformat()
    elif mutation == "audience":
        item["allowed_groups"] = []
    elif mutation == "unknown":
        item["permission_mirroring"] = True
    elif mutation == "limit":
        item["manifest_digests"] = [f"{i:064x}" for i in range(257)]
        item["resource_manifest_digest"] = manifest_digest(item["manifest_digests"])
    with pytest.raises(ValueError):
        SourcePolicy.from_json(json.dumps(payload))
