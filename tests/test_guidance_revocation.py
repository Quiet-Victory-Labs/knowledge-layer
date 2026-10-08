"""Withdrawal removes authority without disclosing unreadable retained meaning."""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from sqlalchemy import delete, select

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.guidance import GuidanceRegistry
from knowledge_layer.store import KnowledgeAccessDenied, KnowledgeNotFound, KnowledgeStore

TENANT = "demo"
SLUG = "reviewed-procedure"
RATIONALE = "The real steward reviewed the exact procedure and its pinned sources."
WITHDRAWAL = "The real steward withdrew the procedure until its dependencies are reviewed."
PRIVATE_INSTRUCTIONS = "Retained dependency meaning must not appear in a withdrawal acknowledgement."
PRIVATE_RULE = "Retained company rule must not appear in a withdrawal acknowledgement."


def _principal(subject="owner", *, steward=True, manager=True):
    return Principal(
        subject,
        frozenset({"stewards"}) if steward else frozenset(),
        frozenset({"knowledge.sources.manage"}) if manager else frozenset(),
    )


@contextmanager
def _operator(store):
    previous = store.access_enforced
    store.access_enforced = False
    try:
        yield
    finally:
        store.access_enforced = previous


def _grant(store, source_id, subject, role):
    with _operator(store):
        store.grant_source_access(TENANT, source_id, principal_type="user", principal_id=subject, role=role)


def _revoke_grant(store, source_id, subject="owner"):
    with _operator(store):
        store.revoke_source_access(TENANT, source_id, principal_type="user", principal_id=subject)


def _reviewed_source(store, tmp_path, name):
    directory = tmp_path / name
    directory.mkdir()
    (directory / "orders.csv").write_text("order_id,total\nO-1,12\n", encoding="utf-8")
    source = store.create_connection(TENANT, kind="local_files", name=name, config={"path": name})
    snapshot = store.refresh_connection(TENANT, source["id"])["snapshot"]
    asset = snapshot["assets"][0]
    evidence_id = next(item["id"] for item in snapshot["evidence"] if item.get("asset_id") == asset["id"])
    proposal = store.submit_contract_assertion(
        TENANT, source["id"], assertion_kind="dataset_description", asset_id=asset["id"],
        payload={"description": "One observed row per source order."}, evidence_ids=[evidence_id],
        rationale="The observed CSV inventory supports this bounded description.",
        expected_snapshot_id=snapshot["id"],
    )
    store.decide_proposal(TENANT, source["id"], proposal["id"], decision="approve",
                          rationale="The steward checked the observed metadata and source scope.")
    snapshot = store.get_snapshot(TENANT, source["id"])
    store.publish_source(TENANT, source["id"], expected_snapshot_id=snapshot["id"],
                         expected_snapshot_version=snapshot["version"], rationale=RATIONALE)
    snapshot = store.get_snapshot(TENANT, source["id"])
    assert snapshot["agent_ready"] is True
    return source["id"], snapshot


@pytest.fixture
def published_guidance(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}", local_files_root=tmp_path)
    store.source_admin_group = "stewards"
    try:
        scope_id, _ = _reviewed_source(store, tmp_path, "managed-scope")
        dependency_id, snapshot = _reviewed_source(store, tmp_path, "private-dependency")
        _grant(store, scope_id, "owner", "manage")
        _grant(store, dependency_id, "owner", "read")
        for source_id in (scope_id, dependency_id):
            _grant(store, source_id, "reader", "read")
        store.access_enforced = True
        registry = GuidanceRegistry(store)
        payload = {
            "title": "Reviewed procedure", "description": "A procedure with one separately granted dependency.",
            "business_units": [], "tasks": [], "connection_ids": [scope_id],
            "audience": {"company_wide": False, "subjects": ["owner", "reader"], "groups": []},
            "skills": [{"name": "retained-meaning", "description": "Use reviewed source meaning.",
                        "instructions": PRIVATE_INSTRUCTIONS, "required_capabilities": ["knowledge_context"]}],
            "rules": [{"key": "private-rule", "statement": PRIVATE_RULE, "value": False}],
            "dependencies": [{"connection_id": dependency_id, "snapshot_id": snapshot["id"],
                              "snapshot_version": snapshot["version"]}],
        }
        with principal_scope(_principal()):
            draft = registry.draft(TENANT, SLUG, payload, expected_revision=0)
            publication = registry.publish(TENANT, SLUG, revision=draft["revision"],
                                           expected_digest=draft["digest"], rationale=RATIONALE)
        yield store, registry, scope_id, dependency_id, publication
    finally:
        store.close()


def _rows(store, table):
    with store.engine.connect() as conn:
        return [dict(row) for row in conn.execute(select(table)).mappings()]


def _head(store, registry):
    with store.engine.connect() as conn:
        return dict(conn.execute(select(registry.bundles).where(
            registry.bundles.c.tenant_id == TENANT, registry.bundles.c.slug == SLUG,
        )).mappings().one())


def _withdrawal_events(store):
    return [row for row in _rows(store, store.audit_events) if row["event_type"] == "guidance.revoke"]


def _assert_ack(ack, publication, dependency_id):
    assert ack == {
        "slug": SLUG, "revision": publication["revision"], "digest": publication["digest"],
        "state": "revoked", "published_revision": publication["revision"],
        "publication": {"revoked_at": ack["publication"]["revoked_at"], "revoked_by": "owner"},
        "use_as_instructions": False, "blocked_reasons": ["revoked"],
    }
    assert ack["publication"]["revoked_at"]
    encoded = json.dumps(ack)
    for inaccessible in (PRIVATE_INSTRUCTIONS, PRIVATE_RULE, dependency_id, RATIONALE):
        assert inaccessible not in encoded
    assert "payload" not in ack and "dependencies" not in ack


@pytest.mark.parametrize("dependency_read_lost", [False, True])
def test_withdrawal_ack_never_returns_retained_content(published_guidance, dependency_read_lost):
    store, registry, _scope_id, dependency_id, publication = published_guidance
    if dependency_read_lost:
        _revoke_grant(store, dependency_id)
    with principal_scope(_principal()):
        ack = registry.revoke(TENANT, SLUG, revision=1, reason=WITHDRAWAL)
    _assert_ack(ack, publication, dependency_id)


def test_lost_dependency_read_still_allows_authority_reduction_and_preserves_history(
    published_guidance, monkeypatch,
):
    store, registry, scope_id, dependency_id, publication = published_guidance
    _revoke_grant(store, dependency_id)
    revisions = _rows(store, registry.revisions)
    decisions = _rows(store, store.decisions)
    source_publications = _rows(store, store.publications)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Withdrawal must not read dependency content or execute a source")

    monkeypatch.setattr(registry, "_readable", forbidden)
    monkeypatch.setattr(registry, "_dependency_reasons", forbidden)
    monkeypatch.setattr(store, "get_snapshot", forbidden)
    monkeypatch.setattr(store, "refresh_connection", forbidden)
    with principal_scope(_principal()):
        ack = registry.revoke(TENANT, SLUG, revision=1, reason=WITHDRAWAL)
    _assert_ack(ack, publication, dependency_id)
    assert _rows(store, registry.revisions) == revisions
    assert _rows(store, store.decisions) == decisions
    assert _rows(store, store.publications) == source_publications
    head = _head(store, registry)
    assert head["published_revision"] == 1 and head["published_digest"] == publication["digest"]
    assert head["revoke_reason"] == WITHDRAWAL and head["revoked_by"] == "owner"
    events = _withdrawal_events(store)
    assert len(events) == 1 and events[0]["actor"] == "owner"
    assert events[0]["details"] == {"slug": SLUG, "revision": 1, "digest": publication["digest"]}
    # The publication head remains available for authorized history review,
    # while a different authorized reader can no longer consume its guidance.
    monkeypatch.undo()
    with principal_scope(_principal("reader", steward=False, manager=False)):
        resolved = registry.resolve(TENANT, connection_ids=[scope_id])
        assert resolved["status"] == "blocked" and resolved["bundles"] == []
        assert resolved["blocked"][0]["reasons"] == ["revoked"]
        assert PRIVATE_INSTRUCTIONS not in json.dumps(resolved)
    with _operator(store):
        history = registry.get(TENANT, SLUG, revision=1)
        assert history["payload"] == publication["payload"]
        assert history["state"] == "revoked" and history["use_as_instructions"] is False


@pytest.mark.parametrize("denial", [
    "missing_verified_principal", "missing_steward", "read_only_scope", "missing_scope_grant",
    "missing_scoped_source", "outside_audience",
])
def test_withdrawal_preserves_steward_manage_source_and_audience_boundaries(published_guidance, denial):
    store, registry, scope_id, dependency_id, _publication = published_guidance
    _revoke_grant(store, dependency_id)
    actor = _principal()
    expected_error = KnowledgeNotFound
    if denial == "missing_verified_principal":
        actor = None
        expected_error = KnowledgeAccessDenied
    elif denial == "missing_steward":
        actor = _principal(steward=False)
        expected_error = KnowledgeAccessDenied
    elif denial == "read_only_scope":
        _grant(store, scope_id, "owner", "read")
    elif denial == "missing_scope_grant":
        _revoke_grant(store, scope_id)
    elif denial == "missing_scoped_source":
        with store.engine.begin() as conn:
            conn.execute(delete(store.connections).where(store.connections.c.id == scope_id))
    elif denial == "outside_audience":
        _grant(store, scope_id, "outsider", "manage")
        actor = _principal("outsider")
    head = _head(store, registry)
    with principal_scope(actor), pytest.raises(expected_error):
        registry.revoke(TENANT, SLUG, revision=1, reason=WITHDRAWAL)
    assert _head(store, registry) == head
    assert _withdrawal_events(store) == []


def test_real_steward_role_keeps_existing_manager_authority(published_guidance):
    store, registry, _scope_id, dependency_id, publication = published_guidance
    _revoke_grant(store, dependency_id)
    # Existing store authorization accepts the verified steward group as a
    # manager role; the separate source manage grant is still mandatory.
    with principal_scope(_principal(manager=False)):
        ack = registry.revoke(TENANT, SLUG, revision=1, reason=WITHDRAWAL)
    _assert_ack(ack, publication, dependency_id)


def test_lost_dependency_read_withdrawal_is_idempotent(published_guidance):
    store, registry, _scope_id, dependency_id, _publication = published_guidance
    _revoke_grant(store, dependency_id)
    with principal_scope(_principal()):
        first = registry.revoke(TENANT, SLUG, revision=1, reason=WITHDRAWAL)
        second = registry.revoke(TENANT, SLUG, revision=1, reason=WITHDRAWAL)
    assert second == first
    assert len(_withdrawal_events(store)) == 1


def test_lost_dependency_read_withdrawal_audit_failure_rolls_back(published_guidance, monkeypatch):
    store, registry, scope_id, dependency_id, _publication = published_guidance
    _revoke_grant(store, dependency_id)
    head = _head(store, registry)
    original = store._audit

    def fail_withdrawal(conn, tenant, source_id, event_type, actor, details):
        original(conn, tenant, source_id, event_type, actor, details)
        if event_type == "guidance.revoke":
            raise RuntimeError("Injected withdrawal audit failure")

    monkeypatch.setattr(store, "_audit", fail_withdrawal)
    with principal_scope(_principal()), pytest.raises(RuntimeError, match="withdrawal audit failure"):
        registry.revoke(TENANT, SLUG, revision=1, reason=WITHDRAWAL)
    assert _head(store, registry) == head
    assert _withdrawal_events(store) == []
    with principal_scope(_principal("reader", steward=False, manager=False)):
        assert registry.resolve(TENANT, connection_ids=[scope_id])["status"] == "ready"
