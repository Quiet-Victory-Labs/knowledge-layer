"""Reviewed relationships fail closed on source drift and permission changes."""

import json

import pytest

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.contracts import semantic_contract
from knowledge_layer.cross_source import relationship_plan
from knowledge_layer.federated import federated_evidence_graph
from knowledge_layer.store import (
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeStore,
    KnowledgeValidationError,
)


@pytest.fixture
def company(tmp_path):
    for directory, text in (("sales", "customer_id,amount\n1,20\n2,30\n"),
                            ("crm", "customer_id,customer_name\n1,Acme\n2,Beacon\n")):
        folder = tmp_path / directory
        folder.mkdir()
        (folder / "records.csv").write_text(text)
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    sources = {}
    for directory in ("sales", "crm"):
        source = store.create_connection("demo", name=directory, kind="local_files", config={"path": directory})
        store.refresh_connection("demo", source["id"])
        sources[directory] = source["id"]
    yield store, sources, tmp_path
    store.close()


def _publish(store, cid):
    snapshot = store.get_snapshot("demo", cid)
    store.publish_source("demo", cid, expected_snapshot_id=snapshot["id"],
                         expected_snapshot_version=snapshot["version"],
                         rationale="The owner reviewed these current source contracts.")
    return store.get_snapshot("demo", cid)


def _review_source(store, cid):
    snapshot = store.get_snapshot("demo", cid)
    asset = snapshot["assets"][0]
    handle = next(item["id"] for item in snapshot["evidence"]
                  if item.get("asset_id") == asset["id"] and item["evidence_type"] == "asset_metadata")
    draft = store.submit_contract_assertion(
        "demo", cid, assertion_kind="dataset_description", asset_id=asset["id"],
        payload={"description": "The source owner's bounded customer records."},
        evidence_ids=[handle], rationale="Owner confirms this observed dataset scope.",
        expected_snapshot_id=snapshot["id"],
    )
    store.decide_proposal("demo", cid, draft["id"], decision="approve")
    return _publish(store, cid)


def _join(store, sources, cardinality="many_to_one"):
    left = store.get_snapshot("demo", sources["sales"])
    right = store.get_snapshot("demo", sources["crm"])
    def handle(snapshot):
        return next(item["id"] for item in snapshot["evidence"] if item["evidence_type"] == "asset_metadata")
    return store.submit_contract_assertion(
        "demo", sources["sales"], assertion_kind="cross_source_join", asset_id=left["assets"][0]["id"],
        payload={"to_connection_id": sources["crm"], "to_snapshot_id": right["id"],
                 "to_asset_id": right["assets"][0]["id"], "from_columns": ["customer_id"],
                 "to_columns": ["customer_id"], "cardinality": cardinality,
                 "relationship_kind": "business_join", "from_namespace": "sales.customer",
                 "to_namespace": "crm.customer", "caveat": "CRM keys must be unique in the selected result scope."},
        evidence_ids=[handle(left)], evidence_refs=[{"connection_id": sources["crm"],
                    "snapshot_id": right["id"], "evidence_id": handle(right)}],
        rationale="Both owners reviewed this explicit namespace bridge.", expected_snapshot_id=left["id"],
    )


def test_published_join_is_plan_only_and_withdrawal_blocks_it(company):
    store, sources, _ = company
    target = _review_source(store, sources["crm"])
    proposal = _join(store, sources)
    assert proposal["payload"]["coverage"]["matched_rows"] is None
    store.decide_proposal("demo", sources["sales"], proposal["id"], decision="approve")
    _publish(store, sources["sales"])
    plan = relationship_plan(store, tenant_id="demo", connection_id=sources["sales"], assertion_id=proposal["id"])
    assert plan["execution_state"] == "not_executed"
    assert plan["status"] == "metadata_plan"
    assert plan["relationship"]["target_binding"]["snapshot_id"] == target["id"]
    graph = federated_evidence_graph(store, tenant_id="demo", connection_id=sources["sales"])
    assert any(edge["predicate"] == "cross_source_joins_to" for edge in graph["edges"])
    store.revoke_publication("demo", sources["crm"], reason="Owner withdrew this source's current publication.")
    contract = semantic_contract(store, tenant_id="demo", connection_id=sources["sales"])
    assert contract["cross_source_joins"] == []
    assert contract["agent_ready"] is False
    with pytest.raises(KnowledgeNotFound):
        relationship_plan(store, tenant_id="demo", connection_id=sources["sales"], assertion_id=proposal["id"])


def test_unpublished_dependency_refuses_and_refresh_cannot_inherit_review(company):
    store, sources, root = company
    with pytest.raises(KnowledgeConflict, match="published"):
        _join(store, sources)
    _review_source(store, sources["crm"])
    proposal = _join(store, sources)
    (root / "crm" / "records.csv").write_text("customer_id,customer_name\n1,Changed\n2,Beacon\n")
    store.refresh_connection("demo", sources["crm"])
    with pytest.raises(KnowledgeConflict, match="changed"):
        store.decide_proposal("demo", sources["sales"], proposal["id"], decision="approve")


def test_permission_revocation_hides_dependency_metadata_and_blocks_publication(company):
    store, sources, _ = company
    _review_source(store, sources["crm"])
    proposal = _join(store, sources)
    store.decide_proposal("demo", sources["sales"], proposal["id"], decision="approve")
    _publish(store, sources["sales"])
    store.grant_source_access("demo", sources["sales"], principal_type="user", principal_id="reader", role="read")
    store.access_enforced = True
    with principal_scope(Principal("reader", frozenset(), frozenset())):
        snapshot = store.get_snapshot("demo", sources["sales"])
        encoded = json.dumps(snapshot)
        assert sources["crm"] not in encoded
        assert "crm.customer" not in encoded
        assert "customer_name" not in encoded
        assert snapshot["agent_ready"] is False
        assert snapshot["dependency_unavailable"] is True
        assert not any(item.get("assertion_kind") == "cross_source_join" for item in snapshot["proposals"])


def test_attested_audience_revocation_hides_reviewed_dependency_despite_both_grants(company):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from knowledge_layer.source_permissions import digest, manifest_digest
    from knowledge_layer.source_policy import SourcePolicy, source_identity

    store, sources, _ = company
    _review_source(store, sources["crm"])
    proposal = _join(store, sources)
    store.decide_proposal("demo", sources["sales"], proposal["id"], decision="approve")
    _publish(store, sources["sales"])
    approvals, attestations = [], []
    checked = datetime.now(UTC) - timedelta(seconds=1)
    for name, source_id in sources.items():
        store.grant_source_access("demo", source_id, principal_type="user", principal_id="reader", role="read")
        source = store.get_connection("demo", source_id)
        identity = source_identity(source["kind"], source["config"])
        selector = json.loads(identity)[1]
        with store.engine.connect() as conn:
            manifests = sorted({row["source_permission_manifest_digest"] for row in conn.execute(select(
                store.snapshots.c.payload).where(store.snapshots.c.connection_id == source_id)).scalars()})
        approvals.append({"tenant_id": "demo", "kind": source["kind"], "selector": selector,
                          "allowed_subjects": ["owner"], "allowed_groups": []})
        attestations.append({"tenant_id": "demo", "kind": source["kind"], "selector": selector,
            "source_identity_digest": digest(identity), "basis": "operator_partition", "authority": "operator",
            "revision": "permission-1", "state": "complete", "checked_at": checked.isoformat(),
            "expires_at": (checked + timedelta(seconds=300)).isoformat(),
            "allowed_subjects": ["reader" if name == "sales" else "native-only"], "allowed_groups": [],
            "manifest_digests": manifests, "resource_manifest_digest": manifest_digest(manifests)})
    store.source_policy = SourcePolicy.from_json(json.dumps({"version": 2, "sources": approvals,
        "metadata_permissions": {"enforce": True, "trusted_native_exporters": [], "attestations": attestations}}))
    store.access_enforced = True
    with principal_scope(Principal("reader", frozenset())):
        snapshot = store.get_snapshot("demo", sources["sales"])
        encoded = json.dumps(snapshot)
        assert sources["crm"] not in encoded
        assert "crm.customer" not in encoded
        assert "customer_name" not in encoded
        assert snapshot["dependency_unavailable"] is True
        assert snapshot["agent_ready"] is False


def test_fanout_requires_result_grain(company):
    store, sources, _ = company
    _review_source(store, sources["crm"])
    draft = _join(store, sources, cardinality="many_to_many")
    store.decide_proposal("demo", sources["sales"], draft["id"], decision="approve")
    _publish(store, sources["sales"])
    plan = relationship_plan(store, tenant_id="demo", connection_id=sources["sales"], assertion_id=draft["id"])
    assert plan["blocked_reasons"] == ["fanout_requires_explicit_result_grain"]


def test_caller_cannot_inject_bindings_or_claim_identity_by_similarity(company):
    store, sources, _ = company
    _review_source(store, sources["crm"])
    draft = _join(store, sources)
    payload = draft["payload"]
    with pytest.raises(KnowledgeValidationError, match="complete typed"):
        store.submit_contract_assertion(
            "demo", sources["sales"], assertion_kind="cross_source_join", asset_id=draft["asset_id"],
            payload=payload, evidence_ids=draft["evidence_ids"], evidence_refs=draft["evidence_refs"],
            rationale="Attempt to replay server-derived authority as input.",
            expected_snapshot_id=store.get_snapshot("demo", sources["sales"])["id"],
        )


def test_branching_dependency_projection_refuses_at_work_budget(company, monkeypatch):
    from knowledge_layer.cross_source import MAX_DEPENDENCY_PROJECTIONS
    from knowledge_layer.store import KnowledgeConflict

    store, sources, _ = company
    projections = []
    # Simulate a repeated diamond expansion at the real registry read boundary:
    # fanout must be bounded even when every branch stays below depth eight.
    def diamond(tenant, cid, version=None):
        projections.append(cid)
        level = int(cid) if cid.isdigit() else 0
        if level < 7:
            for _ in range(3):
                store.get_snapshot(tenant, str(level + 1))
        return {"level": level}
    original = store._get_snapshot
    monkeypatch.setattr(store, "_get_snapshot", diamond)
    with pytest.raises(KnowledgeConflict, match="64 snapshot"):
        store.get_snapshot("demo", "0")
    assert len(projections) == MAX_DEPENDENCY_PROJECTIONS
    # A failed request does not consume another request's traversal allowance.
    monkeypatch.setattr(store, "_get_snapshot", original)
    assert store.get_snapshot("demo", sources["sales"]) is not None
