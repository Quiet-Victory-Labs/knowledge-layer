"""Delegated publication preserves reviewed local metadata and current authority.

These are synthetic local CSV and SQLite registry behavior tests. They do not
qualify customer sources, OIDC token verification or PostgreSQL concurrency.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from knowledge_layer import autonomy, curation
from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.autonomy import ACTION, SCHEMA_VERSION, AutonomyRegistry
from knowledge_layer.jobs import RefreshQueue
from knowledge_layer.refresh_fence import RefreshFenceLost, refresh_fence
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeStore,
    KnowledgeValidationError,
)
from knowledge_layer.task_context import revalidate_context, task_context

OWNER = Principal("owner", frozenset({"stewards"}), frozenset({"knowledge.sources.manage"}))
SERVICE = Principal("publisher", frozenset(),
                    frozenset({"knowledge.autonomy", "knowledge.sources.manage"}))
OTHER = Principal("other", frozenset(), SERVICE.scopes)
RATIONALE = "The steward reviewed this exact scope and existing approved meaning."


def _source_policy(subjects=("owner", "publisher", "other"), groups=()):
    return SourcePolicy.from_json(json.dumps({"version": 1, "sources": [{
        "tenant_id": "demo", "kind": "local_files",
        "selector": {"path": ".", "include": "*.csv"},
        "allowed_subjects": list(subjects), "allowed_groups": list(groups),
    }]}))


@contextmanager
def _operator(store):
    enforced = store.access_enforced
    store.access_enforced = False
    try:
        yield
    finally:
        store.access_enforced = enforced


def _rows(store, table):
    with store.engine.connect() as conn:
        return [dict(row) for row in conn.execute(select(table)).mappings()]


def _snapshot(fixture):
    with principal_scope(OWNER):
        return fixture["store"].get_snapshot("demo", fixture["source"])


def _refresh(fixture):
    with principal_scope(OWNER):
        return fixture["store"].refresh_connection("demo", fixture["source"])["snapshot"]


def _payload(*, budget=2, subject="publisher", expiry=None):
    return {"schema_version": SCHEMA_VERSION, "action": ACTION, "subject": subject,
            "expires_at": expiry or (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "max_publications": budget,
            "purpose": "Publish only the exact unchanged meaning reviewed by this source steward."}


def _draft(fixture, *, budget=2, subject="publisher", expected_revision=0, **changes):
    with principal_scope(OWNER):
        return fixture["registry"].draft("demo", fixture["source"],
                                         {**_payload(budget=budget, subject=subject), **changes},
                                         expected_revision=expected_revision)


def _approve(fixture, policy):
    with principal_scope(OWNER):
        return fixture["registry"].approve(
            "demo", fixture["source"], revision=policy["revision"],
            expected_digest=policy["digest"], rationale=RATIONALE,
        )


def _establish(fixture, **kwargs):
    return _approve(fixture, _draft(fixture, **kwargs))


def _apply(fixture, policy, *, principal=SERVICE, snapshot=None, **changes):
    snapshot = snapshot or _snapshot(fixture)
    arguments = {"revision": policy["revision"], "expected_digest": policy["digest"],
                 "expected_snapshot_id": snapshot["id"],
                 "expected_snapshot_version": snapshot["version"], **changes}
    with principal_scope(principal):
        return fixture["registry"].apply("demo", fixture["source"], **arguments)


def _revoke(fixture, policy):
    with principal_scope(OWNER):
        return fixture["registry"].revoke(
            "demo", fixture["source"], revision=policy["revision"],
            expected_digest=policy["digest"], reason="The steward withdrew delegated publication authority.",
        )


def _review_description(fixture, description):
    store, cid = fixture["store"], fixture["source"]
    with principal_scope(OWNER):
        snapshot = store.get_snapshot("demo", cid)
        asset = snapshot["assets"][0]
        handle = next(item["id"] for item in snapshot["evidence"]
                      if item.get("asset_id") == asset["id"])
        proposal = store.submit_contract_assertion(
            "demo", cid, assertion_kind="dataset_description", asset_id=asset["id"],
            payload={"description": description}, evidence_ids=[handle],
            rationale="The source owner reviewed the local CSV inventory scope.",
            expected_snapshot_id=snapshot["id"],
        )
        store.decide_proposal("demo", cid, proposal["id"], decision="approve", rationale=RATIONALE)
    return proposal["id"]


@pytest.fixture
def reviewed_source(tmp_path):
    csv = tmp_path / "orders.csv"
    csv.write_text("order_id,total\nO-1,12\nO-2,20\n", encoding="utf-8")
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}",
                           local_files_root=tmp_path)
    source = store.create_connection("demo", kind="local_files", name="Synthetic orders",
                                     config={"path": ".", "include": "*.csv"})
    for subject in (OWNER.subject, SERVICE.subject, OTHER.subject):
        store.grant_source_access("demo", source["id"], principal_type="user",
                                  principal_id=subject, role="manage")
    store.source_admin_group = "stewards"
    store.source_policy = _source_policy()
    store.access_enforced = True
    fixture = {"store": store, "source": source["id"], "csv": csv,
               "registry": AutonomyRegistry(store, execution_enabled=True)}
    try:
        _refresh(fixture)
        fixture["approved"] = _review_description(fixture, "One source record per customer order.")
        yield fixture
    finally:
        store.close()


def test_unchanged_refresh_publication_is_exact_idempotent_and_never_executes_source(
    reviewed_source, monkeypatch,
):
    fixture, store = reviewed_source, reviewed_source["store"]
    before = _rows(store, store.decisions)
    policy = _establish(fixture)
    original = _snapshot(fixture)
    current = _refresh(fixture)
    assert current["version"] > original["version"] and current["id"] != original["id"]
    assert current["agent_ready"] is False

    def forbidden(*_args, **_kwargs):
        pytest.fail("Delegated publication executed source discovery or a semantic decision")

    monkeypatch.setattr(store, "refresh_connection", forbidden)
    monkeypatch.setattr(store, "decide_proposal", forbidden)
    monkeypatch.setitem(curation.CONNECTORS, "local_files", forbidden)
    result = _apply(fixture, policy, snapshot=current)
    assert result["result"] == "published" and result["source_executed"] is False
    assert result["run"]["actor"] == SERVICE.subject
    assert result["run"]["owns_publication"] is True
    ready = _snapshot(fixture)
    assert ready["agent_ready"] is True and ready["meaning_certified"] is False
    assert ready["publication"]["published_by"] == SERVICE.subject
    assert _rows(store, store.decisions) == before
    assert {item["id"] for item in ready["proposals"] if item.get("decision") == "approve"} == {
        fixture["approved"],
    }
    first = _apply(fixture, policy, snapshot=current)
    assert first["result"] == "already_applied" and first["run"]["id"] == result["run"]["id"]
    assert len(_rows(store, store.autonomy_runs)) == 1
    assert _rows(store, store.autonomy_revisions)[0]["publication_count"] == 1
    events = _rows(store, store.audit_events)
    assert sum(event["event_type"] == "autonomy.apply" for event in events) == 1
    publication = next(event for event in events if event["event_type"] == "source.publish")
    assert publication["actor"] == SERVICE.subject
    assert publication["details"]["delegation"] == {
        "policy_revision": policy["revision"], "policy_digest": policy["digest"],
    }


def test_budget_applies_to_new_publications_but_allows_retry(reviewed_source):
    fixture, store = reviewed_source, reviewed_source["store"]
    policy = _establish(fixture, budget=1)
    first = _apply(fixture, policy)
    assert _apply(fixture, policy)["run"]["id"] == first["run"]["id"]
    _refresh(fixture)
    with pytest.raises(KnowledgeConflict, match="budget"):
        _apply(fixture, policy)
    assert _snapshot(fixture)["agent_ready"] is False
    assert _rows(store, store.autonomy_revisions)[0]["publication_count"] == 1
    assert len(_rows(store, store.autonomy_runs)) == len(_rows(store, store.publications)) == 1


def test_draft_is_not_authority_and_service_cannot_approve_or_revoke(reviewed_source):
    fixture = reviewed_source
    policy = _draft(fixture)
    with pytest.raises(KnowledgeConflict, match="inactive"):
        _apply(fixture, policy)
    with principal_scope(SERVICE), pytest.raises(KnowledgeAccessDenied, match="steward"):
        fixture["registry"].approve("demo", fixture["source"], revision=policy["revision"],
                                      expected_digest=policy["digest"], rationale=RATIONALE)
    policy = _approve(fixture, policy)
    with principal_scope(SERVICE), pytest.raises(KnowledgeAccessDenied, match="steward"):
        fixture["registry"].revoke("demo", fixture["source"], revision=policy["revision"],
                                    expected_digest=policy["digest"], reason=RATIONALE)


@pytest.mark.parametrize("principal", [
    Principal("publisher", frozenset(), frozenset({"knowledge.sources.manage"})),
    Principal("publisher", frozenset(), frozenset({"knowledge.autonomy"})),
    OTHER,
])
def test_scope_and_exact_service_subject_are_required(reviewed_source, principal):
    policy = _establish(reviewed_source)
    with pytest.raises(KnowledgeAccessDenied):
        _apply(reviewed_source, policy, principal=principal)
    assert _rows(reviewed_source["store"], reviewed_source["store"].publications) == []


@pytest.mark.parametrize("mode", ["disabled", "unenforced", "unverified"])
def test_no_execution_opt_in_operator_fallback_or_unverified_principal(reviewed_source, mode):
    fixture, store = reviewed_source, reviewed_source["store"]
    policy = _establish(fixture)
    snapshot = _snapshot(fixture)
    if mode == "disabled":
        fixture["registry"].execution_enabled = False
    elif mode == "unenforced":
        store.access_enforced = False
    if mode == "unverified":
        with pytest.raises(KnowledgeAccessDenied):
            fixture["registry"].apply(
                "demo", fixture["source"], revision=policy["revision"],
                expected_digest=policy["digest"], expected_snapshot_id=snapshot["id"],
                expected_snapshot_version=snapshot["version"],
            )
    else:
        with pytest.raises(KnowledgeAccessDenied):
            _apply(fixture, policy, snapshot=snapshot)
    assert _rows(store, store.autonomy_runs) == []


@pytest.mark.parametrize("role", ["read", None, "group_only"])
def test_current_direct_manage_grant_is_required(reviewed_source, role):
    fixture, store = reviewed_source, reviewed_source["store"]
    policy = _establish(fixture)
    with _operator(store):
        if role == "read":
            store.grant_source_access("demo", fixture["source"], principal_type="user",
                                      principal_id=SERVICE.subject, role="read")
        else:
            store.revoke_source_access("demo", fixture["source"], principal_type="user",
                                       principal_id=SERVICE.subject)
            if role == "group_only":
                store.grant_source_access("demo", fixture["source"], principal_type="group",
                                          principal_id="publishing-services", role="manage")
    principal = Principal(SERVICE.subject, frozenset({"publishing-services"}), SERVICE.scopes)
    with pytest.raises((KnowledgeAccessDenied, KnowledgeNotFound)):
        _apply(fixture, policy, principal=principal)
    assert _rows(store, store.autonomy_runs) == []


@pytest.mark.parametrize("change", ["removed", "group_only", "changed_approval"])
def test_current_explicit_operator_policy_and_exact_binding_are_required(reviewed_source, change):
    fixture = reviewed_source
    policy = _establish(fixture)
    if change == "removed":
        fixture["store"].source_policy = None
    elif change == "group_only":
        fixture["store"].source_policy = _source_policy(("owner", "other"), ("publishing-services",))
    else:
        fixture["store"].source_policy = _source_policy(("owner", "publisher", "other", "new-reader"))
    principal = Principal(SERVICE.subject, frozenset({"publishing-services"}), SERVICE.scopes)
    with pytest.raises((KnowledgeAccessDenied, KnowledgeConflict)):
        _apply(fixture, policy, principal=principal)
    assert _rows(fixture["store"], fixture["store"].autonomy_runs) == []


def test_unknown_subject_cannot_be_delegated_even_with_group_approval(reviewed_source):
    fixture = reviewed_source
    fixture["store"].source_policy = _source_policy(groups=("publishing-services",))
    with pytest.raises(KnowledgeAccessDenied, match="explicit operator"):
        _draft(fixture, subject="unapproved-service")


def test_expired_policy_refuses_and_has_no_operator_renewal(reviewed_source, monkeypatch):
    fixture = reviewed_source
    policy = _establish(fixture)
    expired = datetime.fromisoformat(policy["payload"]["expires_at"]) + timedelta(seconds=1)
    monkeypatch.setattr(autonomy, "_clock", lambda: expired)
    with pytest.raises(KnowledgeConflict, match="expired"):
        _apply(fixture, policy)
    with principal_scope(OWNER):
        assert fixture["registry"].get("demo", fixture["source"])["state"] == "expired"
    assert _rows(fixture["store"], fixture["store"].autonomy_runs) == []


@pytest.mark.parametrize("change", ["source", "new_approval", "archive", "ledger"])
def test_reviewed_meaning_changes_cannot_inherit_delegation(reviewed_source, change):
    fixture, store = reviewed_source, reviewed_source["store"]
    policy = _establish(fixture)
    if change == "source":
        fixture["csv"].write_text("order_id,total,status\nO-1,12,booked\n", encoding="utf-8")
        _refresh(fixture)
    elif change == "new_approval":
        _review_description(fixture, "One order record; total is the observed local order amount.")
    elif change == "archive":
        with principal_scope(OWNER):
            store.archive_proposals("demo", fixture["source"], proposal_ids=[fixture["approved"]],
                                    reason="The steward withdrew this exact dataset description.")
    else:
        with store.engine.begin() as conn:
            conn.execute(update(store.decisions).where(
                store.decisions.c.proposal_id == fixture["approved"],
            ).values(proposal_digest="0" * 64))
    with pytest.raises(KnowledgeConflict):
        _apply(fixture, policy)
    assert _rows(store, store.publications) == [] and _rows(store, store.autonomy_runs) == []


def test_draft_cannot_be_approved_after_review_binding_changes(reviewed_source):
    policy = _draft(reviewed_source)
    _review_description(reviewed_source, "One independently reviewed order record per row.")
    with pytest.raises(KnowledgeConflict, match="binding"):
        _approve(reviewed_source, policy)
    assert _rows(reviewed_source["store"], reviewed_source["store"].autonomy_revisions)[0]["approved_at"] is None


@pytest.mark.parametrize("changes", [
    {"expected_digest": "0" * 64},
    {"expected_snapshot_id": "a-different-reviewed-snapshot"},
    {"expected_snapshot_version": 999},
])
def test_exact_policy_and_current_snapshot_pins_are_required(reviewed_source, changes):
    policy = _establish(reviewed_source)
    with pytest.raises(KnowledgeConflict):
        _apply(reviewed_source, policy, **changes)
    assert _rows(reviewed_source["store"], reviewed_source["store"].autonomy_runs) == []


def test_active_refresh_and_missing_refresh_claim_block_publication(reviewed_source):
    fixture = reviewed_source
    policy = _establish(fixture)
    with refresh_fence("unclaimed-job", "synthetic-worker", 1), pytest.raises(RefreshFenceLost):
        _apply(fixture, policy)
    with principal_scope(OWNER):
        RefreshQueue(fixture["store"]).enqueue("demo", fixture["source"])
    with pytest.raises(KnowledgeConflict, match="current complete reviewed"):
        _apply(fixture, policy)
    assert _rows(fixture["store"], fixture["store"].autonomy_runs) == []


def test_revocation_withdraws_owned_publication_and_cannot_reactivate_revision(reviewed_source):
    fixture, store = reviewed_source, reviewed_source["store"]
    policy = _establish(fixture)
    _apply(fixture, policy)
    before = _rows(store, store.decisions)
    revoked = _revoke(fixture, policy)
    assert revoked["withdrawn_publications"] == 1
    assert _snapshot(fixture)["agent_ready"] is False
    assert _rows(store, store.decisions) == before
    with pytest.raises(KnowledgeConflict, match="withdrawn"):
        _apply(fixture, policy)
    with pytest.raises(KnowledgeConflict, match="reactivated"):
        _approve(fixture, policy)
    assert _revoke(fixture, policy)["result"] == "already_withdrawn"


def test_human_publication_is_neither_taken_over_charged_nor_withdrawn(reviewed_source):
    fixture, store = reviewed_source, reviewed_source["store"]
    policy = _establish(fixture)
    snapshot = _snapshot(fixture)
    with principal_scope(OWNER):
        store.publish_source("demo", fixture["source"], expected_snapshot_id=snapshot["id"],
                             expected_snapshot_version=snapshot["version"], rationale=RATIONALE)
    applied = _apply(fixture, policy, snapshot=snapshot)
    assert applied["result"] == "already_published" and applied["run"]["owns_publication"] is False
    assert _rows(store, store.autonomy_revisions)[0]["publication_count"] == 0
    assert _snapshot(fixture)["publication"]["published_by"] == OWNER.subject
    assert _revoke(fixture, policy)["withdrawn_publications"] == 0
    assert _snapshot(fixture)["agent_ready"] is True


def test_human_withdrawal_prevents_successful_idempotent_retry(reviewed_source):
    fixture = reviewed_source
    policy = _establish(fixture)
    _apply(fixture, policy)
    with principal_scope(OWNER):
        fixture["store"].revoke_publication("demo", fixture["source"], reason=RATIONALE)
    with pytest.raises(KnowledgeConflict, match="withdrawn|current complete reviewed"):
        _apply(fixture, policy)
    assert _snapshot(fixture)["agent_ready"] is False


@pytest.mark.parametrize("withdrawal", ["removed", "empty", "owner_removed", "service_removed"])
def test_connector_withdrawal_preserves_inspection_and_steward_kill_switch(reviewed_source, withdrawal):
    fixture, store = reviewed_source, reviewed_source["store"]
    policy = _establish(fixture)
    _apply(fixture, policy)
    runs, decisions = _rows(store, store.autonomy_runs), _rows(store, store.decisions)
    with principal_scope(OWNER):
        previous = task_context(store, tenant_id="demo", query="orders", connection_ids=[fixture["source"]])
    store.source_policy = (None if withdrawal == "removed" else SourcePolicy(()) if withdrawal == "empty"
                           else _source_policy(("publisher", "other")) if withdrawal == "owner_removed"
                           else _source_policy(("owner", "other")))
    with principal_scope(OWNER):
        # Retained metadata access still follows metadata grants. Connector I/O
        # approval is checked independently and cannot obstruct withdrawal.
        assert store.get_snapshot("demo", fixture["source"])["agent_ready"] is True
        assert fixture["registry"].get("demo", fixture["source"])["state"] == "active"
        with pytest.raises(KnowledgeConflict, match="Context changed"):
            revalidate_context(store, tenant_id="demo", receipt=previous["receipt"])
    with pytest.raises((KnowledgeAccessDenied, KnowledgeConflict)):
        _apply(fixture, policy)
    result = _revoke(fixture, policy)
    assert result["withdrawn_publications"] == 1 and _snapshot(fixture)["agent_ready"] is False
    with principal_scope(OWNER):
        assert fixture["registry"].get("demo", fixture["source"])["state"] == "revoked"
    assert _revoke(fixture, policy)["result"] == "already_withdrawn"
    assert _rows(store, store.autonomy_runs) == runs and _rows(store, store.decisions) == decisions


def test_connector_withdrawal_does_not_create_new_authority(reviewed_source):
    fixture = reviewed_source
    policy = _draft(fixture)
    fixture["store"].source_policy = SourcePolicy(())
    with principal_scope(OWNER):
        assert fixture["registry"].get("demo", fixture["source"])["state"] == "draft"
    with pytest.raises(KnowledgeAccessDenied):
        _approve(fixture, policy)
    with pytest.raises(KnowledgeAccessDenied):
        _draft(fixture, expected_revision=policy["revision"])
    with principal_scope(OTHER), pytest.raises(KnowledgeAccessDenied, match="steward"):
        fixture["registry"].revoke("demo", fixture["source"], revision=policy["revision"],
            expected_digest=policy["digest"], reason=RATIONALE)


@pytest.mark.parametrize("damage", ["payload", "digest"])
def test_corrupt_policy_refuses_use_but_allows_pinned_steward_withdrawal(reviewed_source, damage):
    fixture, store = reviewed_source, reviewed_source["store"]
    policy = _establish(fixture)
    _apply(fixture, policy)
    original_runs = _rows(store, store.autonomy_runs)
    values = ({"payload": {**policy["payload"], "subject": "unexpected-subject"}}
              if damage == "payload" else {"digest": "a" * 64})
    with store.engine.begin() as db:
        db.execute(update(store.autonomy_revisions).where(
            store.autonomy_revisions.c.connection_id == fixture["source"]).values(**values))
    with pytest.raises(KnowledgeConflict, match="integrity"):
        _apply(fixture, policy)
    with principal_scope(OWNER):
        with pytest.raises(KnowledgeConflict, match="integrity"):
            fixture["registry"].get("demo", fixture["source"])
        with pytest.raises(KnowledgeConflict, match="changed before withdrawal"):
            fixture["registry"].revoke("demo", fixture["source"], revision=policy["revision"],
                expected_digest="b" * 64, reason=RATIONALE)
    assert _snapshot(fixture)["agent_ready"] is True
    assert _revoke(fixture, policy)["withdrawn_publications"] == 1
    assert _snapshot(fixture)["agent_ready"] is False
    assert _rows(store, store.autonomy_runs) == original_runs


def test_replacement_policy_revocation_also_withdraws_prior_owned_publications(reviewed_source):
    fixture, store = reviewed_source, reviewed_source["store"]
    first = _establish(fixture)
    _apply(fixture, first)
    second = _establish(fixture, expected_revision=first["revision"])
    _refresh(fixture)
    _apply(fixture, second)
    with pytest.raises(KnowledgeConflict):
        _apply(fixture, first)
    withdrawn = _revoke(fixture, second)
    assert withdrawn["withdrawn_publications"] == 2
    assert all(publication["revoked_at"] is not None for publication in _rows(store, store.publications))
    assert _snapshot(fixture)["agent_ready"] is False


def test_expiry_during_publication_rolls_back_all_mutations(reviewed_source, monkeypatch):
    fixture, store = reviewed_source, reviewed_source["store"]
    policy = _establish(fixture)
    before = _rows(store, store.audit_events)
    decisions = _rows(store, store.decisions)
    original = store._publish_source_in_transaction
    expiry = datetime.fromisoformat(policy["payload"]["expires_at"]) + timedelta(seconds=1)

    def expire_after_publication(*args, **kwargs):
        result = original(*args, **kwargs)
        monkeypatch.setattr(autonomy, "_clock", lambda: expiry)
        return result

    monkeypatch.setattr(store, "_publish_source_in_transaction", expire_after_publication)
    with pytest.raises(KnowledgeConflict, match="expired before commit"):
        _apply(fixture, policy)
    assert _rows(store, store.publications) == [] and _rows(store, store.autonomy_runs) == []
    assert _rows(store, store.autonomy_revisions)[0]["publication_count"] == 0
    assert _rows(store, store.audit_events) == before
    assert _rows(store, store.decisions) == decisions
    assert _snapshot(fixture)["agent_ready"] is False


def test_operator_policy_change_during_commit_rolls_back_all_mutations(reviewed_source, monkeypatch):
    fixture, store = reviewed_source, reviewed_source["store"]
    policy = _establish(fixture)
    before = _rows(store, store.audit_events)
    original = store._publish_source_in_transaction

    def change_policy_after_publication(*args, **kwargs):
        result = original(*args, **kwargs)
        store.source_policy = _source_policy(("owner", "publisher", "other", "new-reader"))
        return result

    monkeypatch.setattr(store, "_publish_source_in_transaction", change_policy_after_publication)
    with pytest.raises(KnowledgeConflict, match="policy|binding|operator approval"):
        _apply(fixture, policy)
    assert _rows(store, store.publications) == [] and _rows(store, store.autonomy_runs) == []
    assert _rows(store, store.autonomy_revisions)[0]["publication_count"] == 0
    assert _rows(store, store.audit_events) == before


@pytest.mark.parametrize("change", ["expiry", "operator_policy"])
def test_last_audit_cannot_outlive_current_policy_authority(reviewed_source, monkeypatch, change):
    fixture, store = reviewed_source, reviewed_source["store"]
    policy = _establish(fixture)
    before = _rows(store, store.audit_events)
    original = store._audit
    expiry = datetime.fromisoformat(policy["payload"]["expires_at"]) + timedelta(seconds=1)

    def change_authority_after_audit(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[3] == "autonomy.apply":
            if change == "expiry":
                monkeypatch.setattr(autonomy, "_clock", lambda: expiry)
            else:
                store.source_policy = _source_policy(("owner", "publisher", "other", "new-reader"))
        return result

    monkeypatch.setattr(store, "_audit", change_authority_after_audit)
    with pytest.raises(KnowledgeConflict, match="expired|operator approval"):
        _apply(fixture, policy)
    assert _rows(store, store.publications) == [] and _rows(store, store.autonomy_runs) == []
    assert _rows(store, store.autonomy_revisions)[0]["publication_count"] == 0
    assert _rows(store, store.audit_events) == before


@pytest.mark.parametrize("revision", [0, -1, True, "1"])
def test_get_rejects_invalid_explicit_revision(reviewed_source, revision):
    _establish(reviewed_source)
    with principal_scope(OWNER), pytest.raises(KnowledgeValidationError):
        reviewed_source["registry"].get("demo", reviewed_source["source"], revision=revision)


@pytest.mark.parametrize("changes", [
    {"action": "approve_new_meaning"}, {"schema_version": "unknown"},
    {"subject": "publisher\nowner"}, {"max_publications": True}, {"max_publications": 0},
    {"max_publications": 1001}, {"expires_at": "2026-10-03T12:00:00"},
    {"expires_at": "invalid"}, {"unexpected_authority": "steward"},
])
def test_malformed_or_broadened_policy_is_rejected(reviewed_source, changes):
    with pytest.raises(KnowledgeValidationError):
        _draft(reviewed_source, **changes)
    assert _rows(reviewed_source["store"], reviewed_source["store"].autonomy_policies) == []
