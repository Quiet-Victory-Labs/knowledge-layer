"""Governance mutations produce private, bounded, source-scoped audit exports."""

from __future__ import annotations

import hashlib
import json
import stat

import pytest

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.audit_export_cli import export_events
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import KnowledgeAccessDenied, KnowledgeStore


def test_governance_events_export_without_secret_or_source_content(tmp_path):
    (tmp_path / "orders.csv").write_text("order_id,total\nO-1,12\n", encoding="utf-8")
    database = f"sqlite:///{tmp_path / 'registry.db'}"
    store = KnowledgeStore(database_url=database, local_files_root=tmp_path, access_enforced=True)
    store.source_admin_group = "stewards"
    store.source_policy = SourcePolicy.from_json(json.dumps({"version": 1, "sources": [{
        "tenant_id": "demo", "kind": "local_files", "selector": {"path": ".", "include": "**/*"},
        "allowed_subjects": ["owner-1"], "allowed_groups": [],
    }]}))
    owner = Principal("owner-1", frozenset({"stewards"}))
    with principal_scope(owner):
        source = store.create_connection("demo", kind="local_files", name="Orders", config={"path": "."})
        source_id = source["id"]
        discovered = store.refresh_connection("demo", source_id)["snapshot"]
        asset = discovered["assets"][0]
        evidence_id = next(item["id"] for item in discovered["evidence"]
                           if item.get("asset_id") == asset["id"])
        proposal = store.submit_contract_assertion(
            "demo", source_id, assertion_kind="dataset_description", asset_id=asset["id"],
            payload={"description": "Sensitive source text must stay out of the audit export."},
            evidence_ids=[evidence_id], rationale="The owner can explain this source.",
            expected_snapshot_id=discovered["id"],
        )
        store.decide_proposal("demo", source_id, proposal["id"], decision="approve",
                              rationale="Owner reviewed the available evidence.")
        reviewed = store.get_snapshot("demo", source_id)
        store.publish_source(
            "demo", source_id, expected_snapshot_id=reviewed["id"],
            expected_snapshot_version=reviewed["version"],
            rationale="Owner permits agents to use this reviewed source.",
        )
        store.revoke_publication("demo", source_id, reason="Owner withdrew this source.")
        with pytest.raises(KnowledgeAccessDenied):
            store.audit_page("demo")
        with pytest.raises(KnowledgeAccessDenied):
            list(store.iter_audit_events("demo"))

    operator = KnowledgeStore(database_url=database, initialize_schema=False)
    operator.grant_source_access("demo", source_id, principal_type="group",
                                 principal_id="analysts", role="read", granted_by="access-admin")
    operator.revoke_source_access("demo", source_id, principal_type="group",
                                  principal_id="analysts", revoked_by="access-admin")
    events = operator.audit_page("demo", connection_id=source_id)
    types = [event["event_type"] for event in events]
    assert types == ["source.create", "source.grant", "source.snapshot", "proposal.submit", "proposal.decide",
                     "source.snapshot", "source.publish", "source.withdraw_publication",
                     "source.grant", "source.revoke_grant"]
    assert events[3]["actor"] == "owner-1"
    assert events[-1]["actor"] == "access-admin"
    assert operator.audit_page("other", connection_id=source_id) == []
    assert operator.audit_page("demo", connection_id="other-source") == []
    assert len(operator.audit_page("demo", limit=2)) == 2
    assert len(list(operator.iter_audit_events("demo", connection_id=source_id))) == len(events)

    output = tmp_path / "audit.jsonl"
    result = export_events(operator, output, tenant_id="demo", connection_id=source_id)
    content = output.read_bytes()
    assert result["events"] == len(events)
    assert result["sha256"] == hashlib.sha256(content).hexdigest()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert b"Sensitive source text" not in content
    assert b"Owner reviewed the available evidence" not in content
    assert [json.loads(line)["id"] for line in content.splitlines()] == [event["id"] for event in events]
    with pytest.raises(FileExistsError):
        export_events(operator, output, tenant_id="demo")
    assert output.read_bytes() == content
    operator.close()
    store.close()


def test_audit_failure_rolls_back_grant(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}")
    source_id = store.create_connection("demo", kind="local_files", name="Orders", config={})["id"]
    store.audit_events.drop(store.engine)
    with pytest.raises(Exception, match="knowledge_audit_events"):
        store.grant_source_access("demo", source_id, principal_type="group",
                                  principal_id="analysts", role="read")
    with store.engine.connect() as conn:
        assert conn.execute(store.source_grants.select()).fetchall() == []


def test_audit_failure_rolls_back_proposal_archive(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}")
    source = store.create_connection("demo", kind="local_files", name="Orders", config={})
    store.save_snapshot("demo", source["id"], {
        "status": "discovery_complete", "discovery_complete": True,
        "assets": [{"id": "asset-orders", "name": "Orders"}],
        "concepts": [{"id": "concept-orders", "name": "Orders"}],
        "evidence": [{"id": "evidence-orders", "asset_id": "asset-orders", "summary": "Schema evidence"}],
    })
    proposal = store.submit_proposal(
        "demo", source["id"], target_concept_id="concept-orders",
        proposed_definition="One record per order.", evidence_ids=["evidence-orders"],
        rationale="The source owner should review this grain.",
    )
    store.audit_events.drop(store.engine)
    with pytest.raises(Exception, match="knowledge_audit_events"):
        store.archive_proposals("demo", source["id"], proposal_ids=[proposal["id"]],
                                reason="This review draft was superseded.")
    with store.engine.connect() as conn:
        archived_at = conn.execute(store.proposals.select().where(
            store.proposals.c.id == proposal["id"],
        )).mappings().one()["archived_at"]
    assert archived_at is None
    store.close()
    store.close()
