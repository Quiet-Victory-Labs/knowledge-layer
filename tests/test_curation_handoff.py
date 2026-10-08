"""Maintenance polling preserves source bounds, permissions and review states."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, update

from knowledge_layer import curation_handoff
from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.curation_handoff import maintenance_work
from knowledge_layer.schedules import RefreshSchedules
from knowledge_layer.store import (
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeStore,
    KnowledgeValidationError,
)


@pytest.fixture
def store(tmp_path):
    result = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'handoff.db'}")
    yield result
    result.close()


def _source(store, name="Orders", *, snapshot=True, concepts=1, fields=None):
    source_id = store.create_connection("demo", kind="local_files", name=name, config={})["id"]
    if snapshot:
        store.save_snapshot("demo", source_id, _payload(name, concepts=concepts, fields=fields), source_refresh=True)
    return source_id


def _payload(name="Orders", *, concepts=1, fields=None):
    fields = fields if fields is not None else [
        {"name": "order_id", "data_type": "text"}, {"name": "amount", "data_type": "number"},
    ]
    return {
        "snapshot_id": f"snapshot-{name}", "status": "discovery_complete", "discovery_complete": True,
        "curation_complete": True,
        "assets": [{"id": "asset-orders", "name": "Orders", "asset_type": "table", "schema": fields}],
        "concepts": [{"id": f"concept-{index}", "name": f"Order term {index}", "semantic_type": "source_entity",
                      "status": "candidate", "evidence_ids": ["metadata"], "provenance": [{"asset_id": "asset-orders"}]}
                     for index in range(concepts)],
        "evidence": [{"id": "metadata", "asset_id": "asset-orders", "evidence_type": "asset_metadata", "summary": "Observed order schema"},
                     *[{"id": field["name"], "asset_id": "asset-orders", "evidence_type": "field_metadata",
                        "summary": "Observed source field", "details": {"field": field["name"]}} for field in fields]],
        "proposals": [], "relationships": [], "metrics": [],
        "drift": {"added_assets": [], "changed_assets": [], "removed_assets": []},
    }


def _packet(store, source_id):
    return maintenance_work(store, "demo", source_id)["sources"][0]


def _publish(store, source_id):
    snapshot = store.get_snapshot("demo", source_id)
    proposal = store.submit_contract_assertion(
        "demo", source_id, assertion_kind="grain", asset_id="asset-orders",
        payload={"description": "One row per exact order identifier.", "key_fields": ["order_id"]},
        evidence_ids=["metadata"], rationale="A steward checked the explicit synthetic source grain.",
        expected_snapshot_id=snapshot["id"],
    )
    store.decide_proposal("demo", source_id, proposal["id"], decision="approve")
    current = store.get_snapshot("demo", source_id)
    store.publish_source("demo", source_id, expected_snapshot_id=current["id"],
                         expected_snapshot_version=current["version"],
                         rationale="Publish the reviewed synthetic order context for tests.")


def test_polling_is_derived_and_work_is_version_bound_without_unscoped_scan(store, monkeypatch):
    source_id = _source(store)
    before = store.get_snapshot("demo", source_id)
    monkeypatch.setattr(store, "get_concepts", lambda *_args, **_kwargs: pytest.fail("unscoped concept scan"))
    monkeypatch.setattr(store, "refresh_connection", lambda *_args, **_kwargs: pytest.fail("source read"))
    result = maintenance_work(store, "demo", source_id)
    packet = result["sources"][0]
    assert result["derived_only"] is True and result["source_reads_executed"] is False
    for track in ("meaning", "contract"):
        work = packet["interpretation"][track]
        assert 0 < len(work["work"]) <= 2 and work["count_state"] == "exact"
        for item in work["work"]:
            assert item["expected_snapshot_id"] == before["id"] and item["snapshot_version"] == before["version"]
            assert item["approval_required"] is True
    assert packet["review"]["current_approved_count"] == 0
    assert packet["publication"]["state"] == "not_published_current"
    after = store.get_snapshot("demo", source_id)
    assert (after["id"], after["version"], after["proposals"]) == (before["id"], before["version"], before["proposals"])


def test_no_snapshot_and_complete_empty_inventory_are_distinct(store):
    missing = _source(store, "Missing", snapshot=False)
    absent = _packet(store, missing)
    assert absent["discovery"]["asset_count"] is None and absent["discovery"]["complete"] is None
    assert absent["review"]["pending_count"] is None
    assert absent["interpretation"]["meaning"]["count"] is None
    empty = _source(store, "Empty", snapshot=False)
    store.save_snapshot("demo", empty, {"status": "discovery_complete", "discovery_complete": True,
                                      "assets": [], "concepts": [], "proposals": [], "evidence": []}, source_refresh=True)
    checked = _packet(store, empty)
    assert checked["discovery"]["complete"] is True and checked["discovery"]["asset_count"] == 0
    assert checked["interpretation"]["meaning"]["status"] == "empty"
    assert checked["interpretation"]["meaning"]["count"] == 0


def test_publication_and_interpretation_are_separate_tracks(store):
    source_id = _source(store)
    _publish(store, source_id)
    packet = _packet(store, source_id)
    assert packet["publication"]["state"] == "published_for_agents"
    assert packet["consumer_freshness"]["agent_ready"] is True
    assert packet["review"]["current_approved_count"] == 1
    assert packet["interpretation"]["meaning"]["status"] == "available"
    assert packet["review"]["approval_required"] is True


def test_failed_refresh_retains_discovery_and_age_but_blocks_interpretation(store):
    source_id = _source(store)
    _publish(store, source_id)
    before = store.get_snapshot("demo", source_id)
    store._mark_error("demo", source_id)
    packet = _packet(store, source_id)
    assert packet["discovery"]["snapshot_id"] == before["id"]
    assert packet["discovery"]["complete"] is True and packet["discovery"]["retained_after_error"] is True
    assert packet["consumer_freshness"]["clock_state"] == "known"
    assert packet["consumer_freshness"]["refresh_error"] is True
    assert packet["consumer_freshness"]["agent_ready"] is False
    assert packet["interpretation"]["contract"]["status"] == "blocked"
    assert packet["publication"]["state"] == "suspended"


def test_stale_review_reports_visible_impact_and_requires_new_snapshot_binding(store):
    source_id = _source(store)
    _publish(store, source_id)
    old = _packet(store, source_id)
    current = _payload("changed", fields=[{"name": "amount", "data_type": "number"}])
    store.save_snapshot("demo", source_id, current, source_refresh=True)
    packet = _packet(store, source_id)
    assert packet["review"]["stale_review_count"] == 1
    assert packet["review"]["current_approved_count"] == 0
    assert "stale_approved_assertions" in packet["blockers"]
    assert packet["impact"]["stale_visible_assertions"][0]["asset_id"] == "asset-orders"
    stale_work = old["interpretation"]["meaning"]["work"][0]
    with pytest.raises(KnowledgeConflict):
        store.submit_proposal("demo", source_id, target_concept_id="concept-0",
                              proposed_definition="An exact observed order in this source.", evidence_ids=["metadata"],
                              rationale="Only the current pinned snapshot is acceptable.",
                              expected_snapshot_id=stale_work["expected_snapshot_id"])


def test_bounded_scan_counts_are_lower_bounds_and_not_empty_census(store):
    source_id = _source(store, concepts=501)
    packet = _packet(store, source_id)
    meaning = packet["interpretation"]["meaning"]
    assert meaning["count"] == 200 and meaning["count_state"] == "lower_bound"
    assert meaning["scan_complete"] is False and meaning["more_available"] is True
    assert len(meaning["work"]) == 2


def test_no_tasks_in_capped_view_is_unknown_not_claimed_extra_work(store):
    source_id = _source(store, snapshot=False)
    payload = _payload(concepts=501)
    for concept in payload["concepts"]:
        concept["definition"] = "A source definition already exists."
    store.save_snapshot("demo", source_id, payload, source_refresh=True)
    result = _packet(store, source_id)["interpretation"]["meaning"]
    assert result["count"] == 0 and result["status"] == "unknown"
    assert result["more_available"] is None and result["scan_complete"] is False


def test_changed_snapshot_during_poll_blocks_work_and_consumer_ready_state(store, monkeypatch):
    source_id = _source(store)
    _publish(store, source_id)
    original = store.publication_preview

    def changed(tenant, connection_id):
        store.save_snapshot(tenant, connection_id, _payload("replacement"), source_refresh=True)
        return original(tenant, connection_id)

    monkeypatch.setattr(store, "publication_preview", changed)
    packet = _packet(store, source_id)
    assert "snapshot_changed_during_poll" in packet["blockers"]
    assert packet["interpretation"]["meaning"]["status"] == "blocked"
    assert packet["publication"]["state"] == "stale_poll" and packet["publication"]["eligible"] is False
    assert packet["consumer_freshness"]["agent_ready"] is False


def test_source_page_cursor_and_totals_cover_authorized_sources_only(store):
    source_ids = [_source(store, f"Source-{index}") for index in range(3)]
    for source_id in source_ids[:2]:
        store.grant_source_access("demo", source_id, principal_type="user", principal_id="reader", role="read")
    store.access_enforced = True
    with principal_scope(Principal("reader", frozenset())):
        first = maintenance_work(store, "demo", limit=1)
        second = maintenance_work(store, "demo", cursor=first["next_cursor"], limit=1)
        assert first["total_authorized_sources"] == 2 and second["total_authorized_sources"] == 2
        assert second["next_cursor"] is None
        assert {first["sources"][0]["connection_id"], second["sources"][0]["connection_id"]} == set(source_ids[:2])
        assert "Source-2" not in json.dumps(first) + json.dumps(second)
        for source_id in (source_ids[2], "nonexistent"):
            with pytest.raises(KnowledgeNotFound, match="^Connection was not found$"):
                maintenance_work(store, "demo", source_id)


def test_grant_revocation_during_poll_drops_body_and_counts(store, monkeypatch):
    source_id = _source(store, "Private-source")
    store.grant_source_access("demo", source_id, principal_type="user", principal_id="reader", role="read")
    store.access_enforced = True
    original = store.publication_preview

    def revoke(tenant, connection_id):
        preview = original(tenant, connection_id)
        with store.engine.begin() as conn:
            conn.execute(delete(store.source_grants))
        return preview

    monkeypatch.setattr(store, "publication_preview", revoke)
    with principal_scope(Principal("reader", frozenset())):
        result = maintenance_work(store, "demo")
    assert result["sources"] == [] and result["total_authorized_sources"] == 0
    assert "Private-source" not in json.dumps(result)


def test_reader_freshness_age_does_not_bypass_schedule_management(store):
    source_id = _source(store)
    RefreshSchedules(store).set("demo", source_id, {
        "cadence": "hourly", "timezone": "UTC", "minute": 0, "freshness_max_age_seconds": 60,
    })
    old = (datetime.now(UTC) - timedelta(seconds=120)).isoformat().replace("+00:00", "Z")
    with store.engine.begin() as conn:
        conn.execute(update(store.connections).values(last_complete_refresh_at=old))
    manager = _packet(store, source_id)["consumer_freshness"]
    assert manager["freshness_window_state"] == "overdue" and manager["max_age_seconds"] == 60
    store.grant_source_access("demo", source_id, principal_type="user", principal_id="reader", role="read")
    store.access_enforced = True
    with principal_scope(Principal("reader", frozenset())):
        reader = _packet(store, source_id)["consumer_freshness"]
    assert reader["age_seconds"] >= 120 and reader["freshness_window_state"] == "unknown"
    assert reader["max_age_seconds"] is None


@pytest.mark.parametrize("clock", [None, "invalid", "2026-10-03T12:00:00", "2099-01-01T00:00:00Z"])
def test_unknown_or_future_complete_clocks_remain_unknown(store, clock):
    source_id = _source(store)
    with store.engine.begin() as conn:
        conn.execute(update(store.connections).values(last_complete_refresh_at=clock))
    result = _packet(store, source_id)["consumer_freshness"]
    assert result["clock_state"] == "unknown" and result["age_seconds"] is None


def test_opaque_dependency_blocker_does_not_invent_hidden_counts_or_identities(store, monkeypatch):
    source_id = _source(store)
    original = store.get_connection

    def projected(tenant, connection_id):
        connection = original(tenant, connection_id)
        if connection:
            connection["snapshot"]["dependency_unavailable"] = True
        return connection

    monkeypatch.setattr(store, "get_connection", projected)
    packet = _packet(store, source_id)
    assert "dependency_unavailable" in packet["blockers"]
    assert packet["impact"]["hidden_dependency_state"] == "unavailable"
    assert packet["impact"]["hidden_dependency_count"] is None
    assert packet["publication"]["eligible"] is False


def test_payload_budget_refuses_instead_of_clipping_work(store, monkeypatch):
    source_id = _source(store)
    monkeypatch.setattr(curation_handoff, "MAX_HANDOFF_BYTES", 100)
    with pytest.raises(KnowledgeValidationError, match="select a source or reduce"):
        maintenance_work(store, "demo", source_id)


@pytest.mark.parametrize("kwargs", [
    {"limit": True}, {"limit": 0}, {"limit": 21}, {"connection_id": ""},
    {"connection_id": "x" * 65}, {"cursor": ""}, {"cursor": "x" * 2001},
    {"connection_id": "source", "cursor": "cursor"},
])
def test_invalid_poll_scope_and_limits_refuse(store, kwargs):
    with pytest.raises(KnowledgeValidationError):
        maintenance_work(store, "demo", **kwargs)
