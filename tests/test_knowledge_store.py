from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import delete, select

import knowledge_layer.store as store_module
from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.store import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeStore,
    KnowledgeValidationError,
    validate_connection_config,
)


def test_combined_source_proposal_limit_bounds_semantic_and_contract_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "MAX_SOURCE_PROPOSALS", 2)
    store = _store(tmp_path)
    source = store.create_connection("demo", kind="local_files", name="Orders", config={})
    snapshot = store.save_snapshot("demo", source["id"], _snapshot())
    store.submit_proposal(
        "demo", source["id"], target_concept_id="concept-revenue",
        proposed_definition="Net revenue after approved refunds.", evidence_ids=["evidence-1"],
        rationale="The owner needs to review the revenue definition.",
    )
    store.submit_contract_assertion(
        "demo", source["id"], assertion_kind="dataset_description", asset_id="asset-1",
        payload={"description": "One source row per order."}, evidence_ids=["evidence-1"],
        rationale="The owner needs to review the dataset description.",
        expected_snapshot_id=snapshot["id"],
    )
    with pytest.raises(KnowledgeConflict, match="proposal limit"):
        store.submit_proposal(
            "demo", source["id"], target_concept_id="concept-revenue",
            proposed_definition="Another draft description.", evidence_ids=["evidence-1"],
            rationale="This draft would exceed the source proposal budget.",
        )
    assert len(store.get_snapshot("demo", source["id"])["proposals"]) == 2
    store.close()


def test_contributor_quota_and_steward_archive_restore_capacity(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "MAX_SOURCE_PROPOSALS", 4)
    monkeypatch.setattr(store_module, "MAX_SUBMITTER_PROPOSALS", 1)
    monkeypatch.setattr(store_module, "STEWARD_PROPOSAL_RESERVE", 1)
    operator = _store(tmp_path)
    first = operator.create_connection("demo", kind="local_files", name="Orders", config={})
    other = operator.create_connection("demo", kind="local_files", name="Private", config={})
    operator.save_snapshot("demo", first["id"], _snapshot())
    operator.save_snapshot("demo", other["id"], _snapshot())
    for subject, role in (("reader-1", "read"), ("reader-2", "read"), ("reader-3", "read"),
                          ("reader-4", "read"),
                          ("steward", "manage")):
        operator.grant_source_access("demo", first["id"], principal_type="user",
                                     principal_id=subject, role=role)
    operator.grant_source_access("demo", other["id"], principal_type="user",
                                 principal_id="steward", role="manage")
    guarded = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'knowledge.db'}",
                             initialize_schema=False, access_enforced=True)
    guarded.source_admin_group = "stewards"

    def submit(subject: str) -> dict:
        with principal_scope(Principal(subject, frozenset())):
            return guarded.submit_proposal(
                "demo", first["id"], target_concept_id="concept-revenue",
                proposed_definition=f"Definition proposed by {subject}.",
                evidence_ids=["evidence-1"], rationale="Review this source-backed definition.",
            )

    first_proposal = submit("reader-1")
    with pytest.raises(KnowledgeConflict, match="Contributor proposal limit"):
        submit("reader-1")
    submit("reader-2")
    submit("reader-3")
    # A contributor cannot consume the reserved steward capacity.
    with pytest.raises(KnowledgeConflict, match="reserved for steward"):
        submit("reader-4")
    with principal_scope(Principal("reader-1", frozenset())), pytest.raises(KnowledgeAccessDenied):
        guarded.archive_proposals("demo", first["id"], proposal_ids=[first_proposal["id"]],
                                  reason="Clear reviewed draft capacity.")
    with principal_scope(Principal("steward", frozenset({"stewards"}))):
        with pytest.raises(KnowledgeNotFound):
            guarded.archive_proposals("demo", other["id"], proposal_ids=[first_proposal["id"]],
                                      reason="Cross-source archive must fail.")
        result = guarded.archive_proposals("demo", first["id"], proposal_ids=[first_proposal["id"]],
                                           reason="This draft has been superseded.")
        assert result["archived_count"] == 1
        assert guarded.archive_proposals("demo", first["id"], proposal_ids=[first_proposal["id"]],
                                         reason="Idempotent retry after timeout.")["archived_count"] == 0
    replacement = submit("reader-1")
    assert replacement["id"] != first_proposal["id"]
    with principal_scope(Principal("reader-1", frozenset())):
        assert first_proposal["id"] not in {
            item["id"] for item in guarded.get_snapshot("demo", first["id"])["proposals"]
        }
    with operator.engine.connect() as conn:
        row = conn.execute(select(operator.proposals.c.archived_at, operator.proposals.c.archive_reason).where(
            operator.proposals.c.id == first_proposal["id"],
        )).one()
        assert row.archived_at and row.archive_reason == "This draft has been superseded."
    assert [event["event_type"] for event in operator.audit_page("demo", connection_id=first["id"])
            if event["event_type"].startswith("proposal.")].count("proposal.archive") == 1
    guarded.close()
    operator.close()


def test_archived_semantic_review_cannot_reappear_from_versioned_snapshot(tmp_path):
    store = _store(tmp_path)
    source = store.create_connection("demo", kind="local_files", name="Orders", config={})
    store.save_snapshot("demo", source["id"], _snapshot())
    proposal = store.submit_proposal(
        "demo", source["id"], target_concept_id="concept-revenue",
        proposed_definition="Net recognized revenue after refunds.",
        evidence_ids=["evidence-1"], rationale="The business owner checked the revenue grain.",
    )
    store.decide_proposal("demo", source["id"], proposal["id"], decision="approve",
                          rationale="The definition matches the reviewed evidence.")
    reviewed = store.get_snapshot("demo", source["id"])
    assert reviewed["concepts"][0]["definition"] == "Net recognized revenue after refunds."
    store.archive_proposals("demo", source["id"], proposal_ids=[proposal["id"]],
                            reason="The reviewed definition was superseded.")
    current = store.get_snapshot("demo", source["id"])
    assert proposal["id"] not in {item["id"] for item in current["proposals"]}
    assert current["concepts"][0].get("definition_proposal_id") is None
    assert current["concepts"][0].get("definition") != "Net recognized revenue after refunds."
    prior = store.get_snapshot("demo", source["id"], version=reviewed["version"])
    assert prior["concepts"][0]["definition"] == "Net recognized revenue after refunds."
    store.close()


def _snapshot(*, description: str = "Existing source definition") -> dict:
    return {
        "snapshot_id": "source-snapshot-1",
        "kind": "local_files",
        "status": "ready",
        "source": {"id": "source-fixture", "name": "Fixture", "kind": "local_files", "trust_state": "unverified"},
        "capabilities": {"metadata": True, "query_execution": False},
        "assets": [{"id": "asset-1", "name": "orders.csv", "asset_type": "table", "source_pointer": "/private/customer/orders.csv", "schema": []}],
        "concepts": [{"id": "concept-revenue", "name": "Revenue", "definition": description, "status": "candidate", "trust_state": "inferred", "evidence_ids": ["evidence-1"]}],
        "relationships": [],
        "proposals": [],
        "metrics": [{"id": "metric-revenue", "name": "Revenue", "status": "candidate", "certified": False, "unit": "USD", "grain": "order"}],
        "evidence": [{"id": "evidence-1", "asset_id": "asset-1", "summary": "Column and profile evidence", "source_pointer": "orders.csv:total"}],
        "drift": {"added_assets": [], "removed_assets": [], "changed_assets": [], "previous_snapshot_id": None},
        "errors": [],
    }


def _store(tmp_path) -> KnowledgeStore:
    return KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'knowledge.db'}", local_files_root=tmp_path / "fixtures")


def test_cross_source_label_suggestions_stay_reviewable_and_tenant_scoped(tmp_path):
    store = _store(tmp_path)
    food = store.create_connection("demo", kind="local_files", name="Food", config={})
    device = store.create_connection("demo", kind="local_files", name="Device", config={})
    other = store.create_connection("other", kind="local_files", name="Private", config={})
    for connection, concept_id, tenant in (
        (food, "food-recall", "demo"),
        (device, "device-recall", "demo"),
        (other, "private-recall", "other"),
    ):
        snapshot = _snapshot()
        snapshot["concepts"] = [{
            "id": concept_id, "name": "Recall", "semantic_type": "business_entity",
            "status": "candidate", "evidence_ids": ["evidence-1"],
        }]
        store.save_snapshot(tenant, connection["id"], snapshot)
    matches = store.get_context("demo", term="Recall")["matches"]
    assert len(matches) == 2
    by_id = {match["concept"]["id"]: match for match in matches}
    assert by_id["food-recall"]["cross_source_candidates"][0]["concept_id"] == "device-recall"
    assert by_id["food-recall"]["cross_source_candidates"][0]["certified"] is False
    assert "private-recall" not in str(matches)
    store.close()


def test_versioned_graph_neighborhood_persists_evidence_and_tenant_boundary(tmp_path):
    store = _store(tmp_path)
    demo = store.create_connection("demo", kind="local_files", name="Orders", config={})
    snapshot = _snapshot()
    snapshot["concepts"].append({"id": "concept-order", "name": "Order", "status": "candidate"})
    snapshot["relationships"] = [{
        "id": "relationship-1", "subject_id": "concept-revenue", "predicate": "belongs_to",
        "object_id": "concept-order", "status": "candidate", "trust_state": "inferred",
        "evidence_ids": ["evidence-1"], "confidence": 0.7,
    }]
    store.save_snapshot("demo", demo["id"], snapshot)
    first = store.get_graph("demo", demo["id"], focus_id="concept-revenue", depth=2)
    assert first["version"] == 1
    assert {node["id"] for node in first["nodes"]} == {
        "concept-revenue", "concept-order", "evidence-1", "asset-1",
    }
    assert any(edge["predicate"] == "supported_by" for edge in first["edges"])
    assert next(edge for edge in first["edges"] if edge["id"] == "relationship-1")["status"] == "candidate"
    assert "/private/customer" not in str(first)
    with pytest.raises(KnowledgeNotFound):
        store.get_graph("other-tenant", demo["id"], focus_id="concept-revenue")

    snapshot["concepts"][0]["definition"] = "Approved source meaning"
    store.save_snapshot("demo", demo["id"], snapshot)
    assert store.get_graph("demo", demo["id"], focus_id="concept-revenue")["nodes"][0]["description"] == "Approved source meaning"
    assert store.get_graph("demo", demo["id"], focus_id="concept-revenue", version=1)["nodes"][0]["description"] == "Existing source definition"
    with store.engine.begin() as conn:
        conn.execute(delete(store.edges).where(store.edges.c.version == 1))
        conn.execute(delete(store.nodes).where(store.nodes.c.version == 1))
    legacy = store.get_graph("demo", demo["id"], focus_id="concept-revenue", version=1)
    assert legacy["nodes"][0]["description"] == "Existing source definition"
    assert any(edge["id"] == "relationship-1" for edge in legacy["edges"])
    store.close()


def test_connection_config_rejects_paths_credentials_and_unscoped_secret_refs():
    with pytest.raises(KnowledgeValidationError):
        validate_connection_config("local_files", {"path": "../outside"})
    with pytest.raises(KnowledgeValidationError):
        validate_connection_config("local_files", {"root": "/private/customer"})
    with pytest.raises(KnowledgeValidationError):
        validate_connection_config("postgres", {"secret_ref": "DATABASE_URL"})
    with pytest.raises(KnowledgeValidationError):
        validate_connection_config("postgres", {"secret_ref": "KNOWLEDGE_SOURCE_DB", "dsn": "postgres://user:pass@host/db"})

    valid = validate_connection_config("postgres", {"secret_ref": "KNOWLEDGE_SOURCE_ORDERS"})
    assert valid == {"secret_ref": "KNOWLEDGE_SOURCE_ORDERS", "schema": "public", "connect_timeout": 5}


def test_tenant_scoping_hides_connections_snapshots_and_concepts(tmp_path):
    store = _store(tmp_path)
    demo = store.create_connection("demo", kind="local_files", name="Demo", config={})
    private = store.create_connection("northstar", kind="local_files", name="Northstar", config={})
    store.save_snapshot("northstar", private["id"], _snapshot())

    assert [row["id"] for row in store.list_connections("demo")] == [demo["id"]]
    assert store.get_connection("demo", private["id"]) is None
    assert store.get_snapshot("demo", private["id"]) is None
    assert store.get_concept("demo", "concept-revenue") is None
    store.close()


def test_refresh_passes_backend_root_and_prior_snapshot_and_persists_safe_version(tmp_path):
    root = tmp_path / "connectable"
    root.mkdir()
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'persistent.db'}", local_files_root=root)
    created = store.create_connection("demo", kind="local_files", name="Orders", config={"path": "."})
    observed = []

    def curate(kind, config):
        previous_snapshot = config.get("previous_snapshot")
        observed.append((kind, config, previous_snapshot))
        result = _snapshot()
        if previous_snapshot is not None:
            result["snapshot_id"] = "source-snapshot-2"
            result["drift"] = {"added_assets": [], "removed_assets": ["asset-1"], "changed_assets": [], "previous_snapshot_id": previous_snapshot["id"]}
            result["assets"] = []
        return result

    first = store.refresh_connection("demo", created["id"], curator=curate)
    second = store.refresh_connection("demo", created["id"], curator=curate)
    assert observed[0][0] == "local_files"
    assert observed[0][1]["root"] == str(root.resolve())
    assert observed[0][1]["source_id"].startswith("source_")
    assert observed[0][2] is None
    assert observed[1][2]["version"] == 1
    assert first["snapshot"]["version"] == 1
    assert second["snapshot"]["version"] == 2
    assert second["snapshot"]["drift"]["removed_assets"] == ["asset-1"]
    assert second["snapshot"]["drift"]["previous_snapshot_id"] == "source-snapshot-1"
    assert second["snapshot"]["assets"] == []
    assert "private" not in str(second["snapshot"])
    store.close()

    reopened = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'persistent.db'}", local_files_root=root)
    assert reopened.get_snapshot("demo", created["id"])["version"] == 2
    assert reopened.get_connection("demo", created["id"])["config"] == {
        "path": ".", "include": "**/*", "max_files": 100,
        "max_file_bytes": 16777216, "max_total_bytes": 67108864,
        "profile_rows": 5000, "max_columns": 256,
    }
    reopened.close()


def test_failed_refresh_retains_last_complete_snapshot_and_flags_agent_context(tmp_path):
    store = _store(tmp_path)
    connection = store.create_connection("demo", kind="local_files", name="Orders", config={})
    complete = _snapshot()
    complete.update({"status": "discovery_complete", "discovery_complete": True})
    store.save_snapshot("demo", connection["id"], complete)

    def failed(_kind, _config):
        return {
            "status": "error", "discovery_complete": False, "assets": [],
            "concepts": [], "relationships": [], "errors": [{"code": "source_unavailable"}],
        }

    result = store.refresh_connection("demo", connection["id"], curator=failed)
    assert result["connection"]["status"] == "error"
    assert result["connection"]["last_error"]
    assert result["snapshot"]["version"] == 1
    assert [item["name"] for item in result["snapshot"]["assets"]] == ["orders.csv"]
    assert store.get_snapshot("demo", connection["id"], version=2) is None
    context = store.get_context("demo", concept_id="concept-revenue")
    assert context["source_bindings"][0]["connection_status"] == "error"
    assert context["source_bindings"][0]["last_error"]
    store.close()


def test_term_context_can_target_one_source_when_names_overlap(tmp_path):
    store = _store(tmp_path)
    first = store.create_connection("demo", kind="local_files", name="First", config={})
    second = store.create_connection("demo", kind="local_files", name="Second", config={})
    for connection, concept_id in ((first, "concept-first"), (second, "concept-second")):
        snapshot = _snapshot()
        snapshot["concepts"][0].update({"id": concept_id, "name": "Return Window"})
        store.save_snapshot("demo", connection["id"], snapshot)
    result = store.get_context("demo", term="Return Window", connection_id=second["id"])
    assert [item["concept"]["id"] for item in result["matches"]] == ["concept-second"]
    assert store.get_context("demo", concept_id="concept-first", connection_id=second["id"]) is None
    store.close()


def test_approved_semantic_correction_changes_agent_context_without_rewriting_prior_version(tmp_path):
    store = _store(tmp_path)
    connection = store.create_connection("demo", kind="local_files", name="Orders", config={})
    original = _snapshot()
    store.save_snapshot("demo", connection["id"], original)
    proposal = store.submit_proposal(
        "demo",
        connection["id"],
        target_concept_id="concept-revenue",
        proposed_definition="Revenue is the net order amount after approved refunds.",
        evidence_ids=["evidence-1"],
        rationale="The source definition omitted approved refund adjustments.",
    )
    assert proposal["status"] == "pending_review"
    assert proposal["certified"] is False
    assert store.get_context("demo", concept_id="concept-revenue")["concept"]["definition"] == "Existing source definition"

    result = store.decide_proposal("demo", connection["id"], proposal["id"], decision="approve", rationale="Evidence verified by steward.")
    current = store.get_context("demo", concept_id="concept-revenue")
    assert current["concept"]["definition"] == "Revenue is the net order amount after approved refunds."
    assert current["concept"]["definition_status"] == "approved"
    assert current["concept"]["definition_proposal_id"] == proposal["id"]
    assert current["concept"]["definition_evidence_ids"] == ["evidence-1"]
    assert current["snapshot_version"] == 2
    assert result["proposal"]["status"] == "approved"
    assert result["proposal"]["certified"] is False

    prior = store.get_snapshot("demo", connection["id"], version=1)
    assert prior["concepts"][0]["definition"] == "Existing source definition"
    assert prior["version"] == 1
    assert store.get_snapshot("demo", connection["id"])["version"] == 2
    store.close()


def test_relationship_review_requires_current_directed_edge_and_its_evidence(tmp_path):
    store = _store(tmp_path)
    connection = store.create_connection("demo", kind="local_files", name="Orders", config={})
    snapshot = _snapshot()
    snapshot["concepts"].append({"id": "concept-order", "name": "Order", "status": "candidate"})
    snapshot["relationships"] = [{
        "id": "relationship-1", "subject_id": "concept-revenue", "predicate": "belongs_to",
        "object_id": "concept-order", "status": "candidate", "evidence_ids": ["evidence-1"],
    }]
    store.save_snapshot("demo", connection["id"], snapshot)
    mapping = {
        "relationship_id": "relationship-1", "subject_id": "concept-revenue",
        "predicate": "belongs_to", "object_id": "concept-order",
    }
    for invalid in ({**mapping, "object_id": "invented"}, {**mapping, "predicate": "owns"}):
        with pytest.raises(KnowledgeValidationError, match="current relationship"):
            store.submit_proposal(
                "demo", connection["id"], target_concept_id="concept-revenue",
                proposed_mapping=invalid, evidence_ids=["evidence-1"],
                rationale="A source relationship needs review.",
            )
    proposal = store.submit_proposal(
        "demo", connection["id"], target_concept_id="concept-revenue",
        proposed_mapping=mapping, evidence_ids=["evidence-1"],
        rationale="The cited relationship has the stated direction.",
    )
    assert proposal["status"] == "pending_review"
    assert store.get_snapshot("demo", connection["id"])["relationships"][0]["status"] == "candidate"
    store.decide_proposal(
        "demo", connection["id"], proposal["id"], decision="approve",
        rationale="The source relationship was checked by a steward.",
    )
    assert store.get_snapshot("demo", connection["id"])["relationships"][0]["status"] == "approved"
    store.close()


def test_decision_requires_snapshot_evidence_and_conflicting_redecision_is_rejected(tmp_path):
    store = _store(tmp_path)
    connection = store.create_connection("demo", kind="local_files", name="Orders", config={})
    snapshot = _snapshot()
    snapshot["proposals"] = [{"id": "source-proposal", "proposal_type": "mapping", "subject_id": "concept-revenue", "predicate": "maps_to", "object_id": "asset-1", "evidence_ids": ["missing-evidence"], "status": "candidate"}]
    store.save_snapshot("demo", connection["id"], snapshot)
    with pytest.raises(KnowledgeValidationError):
        store.decide_proposal("demo", connection["id"], "source-proposal", decision="approve")

    snapshot["proposals"][0]["evidence_ids"] = ["evidence-1"]
    store.save_snapshot("demo", connection["id"], snapshot)
    first = store.decide_proposal("demo", connection["id"], "source-proposal", decision="approve")
    again = store.decide_proposal("demo", connection["id"], "source-proposal", decision="approve")
    assert first["decision"]["decision"] == again["decision"]["decision"] == "approve"
    assert store.get_snapshot("demo", connection["id"])["version"] == 3
    with pytest.raises(KnowledgeConflict):
        store.decide_proposal("demo", connection["id"], "source-proposal", decision="reject")
    store.close()


def test_snapshot_sanitization_strips_credential_like_fields_without_losing_trust_and_evidence(tmp_path):
    store = _store(tmp_path)
    connection = store.create_connection("demo", kind="iceberg", name="Orders", config={"catalog": "grocery", "namespace": "public"})
    payload = _snapshot()
    payload["source"]["access_token"] = "never-persist-this"
    payload["evidence"][0]["details"] = {"password": "do-not-store", "excerpt": "A safe summary"}
    result = store.save_snapshot("demo", connection["id"], payload)
    serialized = str(result)
    assert "never-persist-this" not in serialized
    assert "do-not-store" not in serialized
    assert result["concepts"][0]["trust_state"] == "inferred"
    assert result["concepts"][0]["evidence_ids"] == ["evidence-1"]
    assert result["evidence"][0]["details"]["excerpt"] == "A safe summary"
    assert result["assets"][0]["source_pointer"] == "[redacted absolute path]"
    store.close()


def test_real_juniper_curation_stays_candidate_only_and_excludes_hidden_truth():
    fixture_root = Path(__file__).resolve().parents[1] / "data" / "connectable"
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=fixture_root)
    connection = store.create_connection(
        "demo",
        kind="local_files",
        name="Juniper Market",
        config={"path": "juniper"},
    )

    result = store.refresh_connection("demo", connection["id"])
    snapshot = result["snapshot"]
    assert snapshot["assets"]
    assert snapshot["discovery_complete"] is True
    assert snapshot["meaning_certified"] is False
    assert snapshot["agent_ready"] is False
    assert result["connection"]["stage"] == "discovery_complete"
    assert all(metric.get("certified") is False for metric in snapshot["metrics"])
    assert "truth_manifest" not in str(snapshot)
    assert "ORD-J1001" not in str(snapshot)
    store.close()
