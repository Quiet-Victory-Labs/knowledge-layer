"""Paged discovery completion and authorization effects, using isolated registry files."""

from __future__ import annotations

import base64
import json
from copy import deepcopy

import pytest
from sqlalchemy import MetaData, create_engine, func, inspect, select, update

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.inventory import (
    HARD_LIMITS,
    MAX_PAGE_BYTES,
    InventoryStore,
    inventory_digest,
)
from knowledge_layer.inventory_schema import inventory_tables
from knowledge_layer.jobs import RefreshQueue
from knowledge_layer.migrations.versions import v0012_inventory
from knowledge_layer.refresh_fence import RefreshFenceLost, refresh_fence
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeStore,
    KnowledgeValidationError,
)

SCOPE = {"namespace": "finance", "selectors": ["warehouse.*"]}
POLICY = {"id": "metadata-only", "revision": None}
WATERMARK = {"kind": "unknown", "value": None}
LIMITS = {"max_pages": 100, "max_entities": 1000, "max_evidence": 1000,
          "max_total_bytes": 1024 * 1024}
OWNER = Principal("owner", frozenset(), frozenset({"knowledge.sources.manage"}))
PEER = Principal("peer", frozenset(), frozenset({"knowledge.sources.manage"}))
READER = Principal("reader", frozenset())


def source_policy(subjects=("owner", "peer", "reader")):
    return SourcePolicy.from_json(json.dumps({"version": 1, "sources": [{
        "tenant_id": "demo", "kind": "local_files", "selector": {"path": ".", "include": "**/*"},
        "allowed_subjects": list(subjects), "allowed_groups": [],
    }]}))


@pytest.fixture
def registry(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'inventory.db'}")
    source = store.create_connection("demo", kind="local_files", name="Inventory", config={})
    for subject, role in [("owner", "manage"), ("peer", "manage"), ("reader", "read")]:
        store.grant_source_access("demo", source["id"], principal_type="user", principal_id=subject, role=role)
    store.source_policy = source_policy()
    store.access_enforced = True
    yield store, source["id"], InventoryStore(store)
    store.close()


def begin(inventory, source_id, **kwargs):
    arguments = {"scope": deepcopy(SCOPE), "policy": deepcopy(POLICY),
                 "watermark": deepcopy(WATERMARK), "limits": deepcopy(LIMITS), **kwargs}
    return inventory.begin("demo", source_id, **arguments)


def entity(identifier="entity.1", *, refs=()):
    return {"id": identifier, "kind": "semantic_definition",
            "payload": {"name": "Revenue", "namespace": "finance", "native_version": None,
                        "source_observed_at": None, "definition": "Full native meaning.\nNo clock fabricated.",
                        "upstream_review_status": "approved", "unit": "USD", "formula": None},
            "evidence_ids": list(refs)}


def evidence(identifier="evidence.1"):
    return {"id": identifier, "kind": "upstream_semantic_definition",
            "payload": {"native_id": "term.1", "reference": "source/glossary/term.1",
                        "owner": None, "review_status": None, "native_version": None}}


def append(inventory, source_id, run, *, entities=None, evidence_rows=None, next_cursor=None,
           cursor=None, page_index=None, **kwargs):
    return inventory.append("demo", source_id, run["id"],
        page_index=run["page_count"] if page_index is None else page_index, cursor=cursor,
        next_cursor=next_cursor, entities=[] if entities is None else entities,
        evidence=[] if evidence_rows is None else evidence_rows,
        scope_digest=run["scope_digest"], policy_digest=run["policy_digest"],
        watermark_digest=run["watermark_digest"], **kwargs)


def commit(inventory, source_id, run, **kwargs):
    completion = {"listing_complete": True, "pages": run["page_count"],
                  "entities": run["entity_count"], "evidence": run["evidence_count"],
                  "manifest_digest": run["manifest_digest"]}
    return inventory.commit("demo", source_id, run["id"], completion=completion, **kwargs)


def current(inventory, source_id, run):
    return inventory.current("demo", source_id, scope_digest=run["scope_digest"], policy_digest=run["policy_digest"])


def count(store, table):
    with store.engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def test_staging_pages_atomic_complete_and_exact_native_unknowns(registry):
    store, source_id, inventory = registry
    with principal_scope(OWNER):
        run = begin(inventory, source_id, expected_counts={"pages": 2, "entities": 1, "evidence": 1})
        assert current(inventory, source_id, run) is None
        first = append(inventory, source_id, run, entities=[entity(refs=["evidence.1"])], next_cursor="native.page.2")
        assert current(inventory, source_id, first) is None
        second = append(inventory, source_id, first, cursor="native.page.2", evidence_rows=[evidence()])
        completed = commit(inventory, source_id, second)
        assert current(inventory, source_id, completed) == completed
        assert completed["page_count"] == 2 and completed["entity_count"] == completed["evidence_count"] == 1
        assert completed["last_complete_scope_at"] == completed["completed_at"]
        assert completed["compatibility"] == {"legacy_snapshot": False, "meaning_certified": False,
            "agent_ready": False, "semantic_projection": "bounded_curation_required"}
        page = inventory.page("demo", source_id, completed["id"])
        assert page["items"][0]["payload"] == entity()["payload"]
        assert page["items"][0]["evidence_ids"] == ["evidence.1"]
        assert page["current"] and page["text_complete"] and not page["agent_ready"]
        source = store.get_connection("demo", source_id)
        assert source["last_refreshed_at"] is None and source["last_complete_refresh_at"] is None
    assert count(store, store.snapshots) == 0
    assert count(store, store.inventory_entities) == 1
    assert count(store, store.inventory_evidence) == 1


def test_identical_replay_is_idempotent_changed_replay_rolls_back(registry):
    store, source_id, inventory = registry
    with principal_scope(OWNER):
        run = begin(inventory, source_id)
        first = append(inventory, source_id, run, entities=[entity()], next_cursor="c1")
        replay = append(inventory, source_id, run, entities=[entity()], next_cursor="c1")
        assert replay["replayed"] and replay["manifest_digest"] == first["manifest_digest"]
        altered = entity()
        altered["payload"]["definition"] = "Changed content with reused null native version"
        with pytest.raises(KnowledgeConflict, match="replay changed"):
            append(inventory, source_id, run, entities=[altered], next_cursor="c1")
        assert inventory.status("demo", source_id, run["id"])["page_count"] == 1
    assert count(store, store.inventory_entities) == count(store, store.inventory_pages) == 1


@pytest.mark.parametrize("mutation", ["index", "cursor", "duplicate_entity", "duplicate_evidence", "scope", "policy", "watermark", "cycle"])
def test_invalid_later_page_is_atomic(registry, mutation):
    store, source_id, inventory = registry
    with principal_scope(OWNER):
        run = begin(inventory, source_id)
        first = append(inventory, source_id, run, entities=[entity()], evidence_rows=[evidence()], next_cursor="c1")
        second = append(inventory, source_id, first, cursor="c1", next_cursor="c2")
        arguments = {"cursor": "c2", "entities": [entity("entity.2")], "evidence_rows": [evidence("evidence.2")]}
        pins = second
        if mutation == "index":
            arguments["page_index"] = 3
        elif mutation == "cursor":
            arguments["cursor"] = "wrong"
        elif mutation == "duplicate_entity":
            arguments["entities"] = [entity()]
        elif mutation == "duplicate_evidence":
            arguments["evidence_rows"] = [evidence()]
        elif mutation in {"scope", "policy", "watermark"}:
            pins = deepcopy(second)
            pins[f"{mutation}_digest"] = "f" * 64
        else:
            arguments["next_cursor"] = "c1"
        with pytest.raises(KnowledgeConflict):
            append(inventory, source_id, pins, **arguments)
        assert inventory.status("demo", source_id, run["id"])["page_count"] == 2
    assert count(store, store.inventory_entities) == 1
    assert count(store, store.inventory_evidence) == 1
    assert count(store, store.inventory_pages) == 2


@pytest.mark.parametrize("mutation", ["none", "unterminated", "digest", "counts", "expected", "evidence", "incomplete"])
def test_commit_requires_complete_manifest_without_advancing_clock(registry, mutation):
    store, source_id, inventory = registry
    with principal_scope(OWNER):
        expected = {"pages": 2, "entities": None, "evidence": None} if mutation == "expected" else None
        run = begin(inventory, source_id, expected_counts=expected)
        if mutation != "none":
            run = append(inventory, source_id, run, entities=[entity(refs=["absent"] if mutation == "evidence" else [])],
                         next_cursor="next" if mutation == "unterminated" else None)
        completion = {"listing_complete": mutation != "incomplete", "pages": max(run["page_count"], 1),
                      "entities": run["entity_count"], "evidence": run["evidence_count"], "manifest_digest": run["manifest_digest"]}
        if mutation == "digest":
            completion["manifest_digest"] = "f" * 64
        elif mutation == "counts":
            completion["entities"] += 1
        with pytest.raises((KnowledgeConflict, KnowledgeValidationError)):
            inventory.commit("demo", source_id, run["id"], completion=completion)
        assert current(inventory, source_id, run) is None
        assert inventory.status("demo", source_id, run["id"])["completed_at"] is None
    assert count(store, store.inventory_heads) == 0


def test_older_concurrent_run_cannot_replace_new_complete_generation(registry):
    _store, source_id, inventory = registry
    with principal_scope(OWNER):
        old, new = begin(inventory, source_id), begin(inventory, source_id)
        old = append(inventory, source_id, old, entities=[entity("old")])
        new = append(inventory, source_id, new, entities=[entity("new")])
        completed = commit(inventory, source_id, new)
        with pytest.raises(KnowledgeConflict, match="newer complete inventory"):
            commit(inventory, source_id, old)
        assert current(inventory, source_id, completed)["id"] == new["id"]


def test_complete_empty_scope_removes_old_entities_only_after_commit(registry):
    _store, source_id, inventory = registry
    with principal_scope(OWNER):
        original = commit(inventory, source_id, append(inventory, source_id, begin(inventory, source_id), entities=[entity()]))
        empty = begin(inventory, source_id)
        empty = append(inventory, source_id, empty)
        assert current(inventory, source_id, empty)["id"] == original["id"]
        completed = commit(inventory, source_id, empty)
        assert inventory.page("demo", source_id, completed["id"])["items"] == []
        removed = inventory.page("demo", source_id, completed["id"], collection="removed")
        assert [r["id"] for r in removed["items"]] == ["entity.1"]
        assert not inventory.page("demo", source_id, original["id"])["current"]


def test_different_scope_or_policy_cannot_establish_deletion(registry):
    _store, source_id, inventory = registry
    with principal_scope(OWNER):
        original = commit(inventory, source_id, append(inventory, source_id, begin(inventory, source_id), entities=[entity()]))
        for arguments in [{"scope": {"namespace": "support", "selectors": ["support.*"]}},
                          {"policy": {"id": "metadata-only", "revision": "new"}}]:
            run = commit(inventory, source_id, append(inventory, source_id, begin(inventory, source_id, **arguments)))
            with pytest.raises(KnowledgeConflict, match="comparable"):
                inventory.page("demo", source_id, run["id"], collection="removed")
        assert current(inventory, source_id, original)["id"] == original["id"]


def test_staging_is_owner_bound_committed_is_source_readable_manage_not_read(registry):
    _store, source_id, inventory = registry
    with principal_scope(OWNER):
        run = begin(inventory, source_id)
    with principal_scope(PEER):
        with pytest.raises(KnowledgeNotFound):
            inventory.status("demo", source_id, run["id"])
        with pytest.raises(KnowledgeNotFound):
            append(inventory, source_id, run)
    with principal_scope(READER), pytest.raises(KnowledgeAccessDenied):
        begin(inventory, source_id)
    with principal_scope(OWNER):
        completed = commit(inventory, source_id, append(inventory, source_id, run, entities=[entity()]))
    with principal_scope(READER):
        assert current(inventory, source_id, completed)["id"] == completed["id"]
        assert inventory.page("demo", source_id, completed["id"])["items"]
    with principal_scope(PEER), pytest.raises(KnowledgeNotFound):
        inventory.abort("demo", source_id, completed["id"])


@pytest.mark.parametrize("operation", ["current", "status", "entities", "evidence", "removed"])
def test_grant_revocation_hides_every_read(registry, operation):
    store, source_id, inventory = registry
    with principal_scope(OWNER):
        original = commit(inventory, source_id, append(inventory, source_id, begin(inventory, source_id), entities=[entity()]))
        run = commit(inventory, source_id, append(inventory, source_id, begin(inventory, source_id)))
    store.access_enforced = False
    store.revoke_source_access("demo", source_id, principal_type="user", principal_id="reader")
    store.access_enforced = True
    with principal_scope(READER), pytest.raises(KnowledgeNotFound):
        if operation == "current":
            current(inventory, source_id, run)
        elif operation == "status":
            inventory.status("demo", source_id, original["id"])
        else:
            inventory.page("demo", source_id, run["id"], collection=operation)


def test_source_policy_identity_and_error_revalidated_but_budget_is_not_identity(registry):
    store, source_id, inventory = registry
    with principal_scope(OWNER):
        run = commit(inventory, source_id, append(inventory, source_id, begin(inventory, source_id), entities=[entity()]))
        with store.engine.begin() as conn:
            configuration = dict(store.get_connection("demo", source_id)["config"])
            configuration["max_files"] = 3
            conn.execute(update(store.connections).where(store.connections.c.id == source_id).values(config=configuration))
        assert current(inventory, source_id, run)["id"] == run["id"]
        store.source_policy = source_policy(("owner",))
        with pytest.raises(KnowledgeConflict, match="approval policy changed"):
            current(inventory, source_id, run)
        new = commit(inventory, source_id, append(inventory, source_id, begin(inventory, source_id)))
        with pytest.raises(KnowledgeConflict, match="comparable"):
            inventory.page("demo", source_id, new["id"], collection="removed")
        with store.engine.begin() as conn:
            conn.execute(update(store.connections).where(store.connections.c.id == source_id).values(status="error"))
        with pytest.raises(KnowledgeConflict, match="unavailable"):
            current(inventory, source_id, new)
        store.source_policy = SourcePolicy(())
        with pytest.raises(KnowledgeAccessDenied):
            inventory.page("demo", source_id, new["id"])


def test_current_source_identity_change_blocks_old_inventory_even_with_new_approval(registry):
    store, source_id, inventory = registry
    with principal_scope(OWNER):
        old = commit(inventory, source_id, append(inventory, source_id, begin(inventory, source_id), entities=[entity()]))
        with store.engine.begin() as conn:
            configuration = deepcopy(store.get_connection("demo", source_id)["config"])
            configuration["path"] = "another-source"
            conn.execute(update(store.connections).where(store.connections.c.id == source_id).values(config=configuration))
        store.source_policy = SourcePolicy.from_json(json.dumps({"version": 1, "sources": [{
            "tenant_id": "demo", "kind": "local_files", "selector": {"path": "another-source", "include": "**/*"},
            "allowed_subjects": ["owner"], "allowed_groups": [],
        }]}))
        with pytest.raises(KnowledgeConflict, match="identity or approval policy changed"):
            current(inventory, source_id, old)
        new = commit(inventory, source_id, append(inventory, source_id, begin(inventory, source_id)))
        with pytest.raises(KnowledgeConflict, match="comparable"):
            inventory.page("demo", source_id, new["id"], collection="removed")


def test_reused_unknown_native_version_changed_content_changes_manifest_and_exact_record(registry):
    _store, source_id, inventory = registry
    with principal_scope(OWNER):
        old = commit(inventory, source_id, append(inventory, source_id, begin(inventory, source_id), entities=[entity()]))
        changed = entity()
        changed["payload"]["definition"] = "New native meaning, same unavailable native version."
        new = commit(inventory, source_id, append(inventory, source_id, begin(inventory, source_id), entities=[changed]))
        assert new["manifest_digest"] != old["manifest_digest"]
        assert inventory.page("demo", source_id, new["id"])["items"][0]["payload"] == changed["payload"]
        assert not new["compatibility"]["meaning_certified"]
        assert inventory.page("demo", source_id, new["id"], collection="removed")["items"] == []


def test_manifest_binds_actual_source_and_policy_even_for_identical_pages(registry):
    store, source_id, inventory = registry
    with principal_scope(OWNER):
        old = commit(inventory, source_id, append(inventory, source_id, begin(inventory, source_id), entities=[entity()]))
        store.source_policy = source_policy(("owner",))
        new = commit(inventory, source_id, append(inventory, source_id, begin(inventory, source_id), entities=[entity()]))
        assert new["source_identity_digest"] == old["source_identity_digest"]
        assert new["source_policy_digest"] != old["source_policy_digest"]
        assert new["manifest_digest"] != old["manifest_digest"]


def test_revoked_manager_cannot_append_or_commit_existing_staging(registry):
    store, source_id, inventory = registry
    with principal_scope(OWNER):
        run = append(inventory, source_id, begin(inventory, source_id), entities=[entity()])
    store.access_enforced = False
    store.revoke_source_access("demo", source_id, principal_type="user", principal_id="owner")
    store.access_enforced = True
    with principal_scope(OWNER):
        with pytest.raises(KnowledgeNotFound):
            append(inventory, source_id, run)
        with pytest.raises(KnowledgeNotFound):
            commit(inventory, source_id, run)
    assert count(store, store.inventory_heads) == 0


def test_keyset_cursor_bound_to_inventory_collection_and_full_text(registry):
    _store, source_id, inventory = registry
    with principal_scope(OWNER):
        rows = [entity(f"entity.{i:02}") for i in range(8)]
        for row in rows:
            row["payload"]["definition"] = "\n" + "完整 native full text " * 1500
        run = begin(inventory, source_id)
        # Two large records fit each bounded write page, then byte-bounded reads.
        for i in range(4):
            run = append(inventory, source_id, run, entities=rows[i * 2:i * 2 + 2],
                         cursor=None if i == 0 else f"p{i}", next_cursor=f"p{i+1}" if i < 3 else None)
        run = commit(inventory, source_id, run)
        first = inventory.page("demo", source_id, run["id"], limit=100)
        assert len(json.dumps(first, ensure_ascii=False).encode()) <= MAX_PAGE_BYTES
        assert first["next_cursor"]
        seen = first["items"]
        cursor = first["next_cursor"]
        while cursor:
            page = inventory.page("demo", source_id, run["id"], after=cursor)
            seen.extend(page["items"])
            cursor = page["next_cursor"]
        assert [r["id"] for r in seen] == [r["id"] for r in rows]
        assert all(a["payload"] == b["payload"] for a, b in zip(seen, rows, strict=True))
        with pytest.raises(KnowledgeValidationError, match="mismatched"):
            inventory.page("demo", source_id, run["id"], collection="evidence", after=first["next_cursor"])


def test_cursor_duplicate_members_and_unresolved_markers_refuse(registry):
    _store, source_id, inventory = registry
    with principal_scope(OWNER):
        run = commit(inventory, source_id, append(inventory, source_id, begin(inventory, source_id), entities=[entity()]))
        duplicate = '{"inventory_id":' + json.dumps(run["id"]) + ',"collection":"entities","collection":"entities","after":"entity.1"}'
        after = base64.urlsafe_b64encode(duplicate.encode()).decode()
        with pytest.raises(KnowledgeValidationError):
            inventory.page("demo", source_id, run["id"], after=after)
        after = base64.urlsafe_b64encode(json.dumps({"inventory_id": run["id"], "collection": "entities", "after": "absent"}).encode()).decode()
        with pytest.raises(KnowledgeValidationError, match="does not resolve"):
            inventory.page("demo", source_id, run["id"], after=after)


@pytest.mark.parametrize("mutation", ["row_count", "duplicate", "record_bytes", "page_bytes", "total_entities", "total_bytes", "nan", "unknown_kind"])
def test_budgets_and_malformed_pages_refuse_without_partial_rows(registry, mutation):
    store, source_id, inventory = registry
    with principal_scope(OWNER):
        limits = deepcopy(LIMITS)
        if mutation == "total_entities":
            limits["max_entities"] = 0
        elif mutation == "total_bytes":
            limits["max_total_bytes"] = 1
        run = begin(inventory, source_id, limits=limits)
        rows = [entity()]
        if mutation == "row_count":
            rows = [entity(str(i)) for i in range(101)]
        elif mutation == "duplicate":
            rows *= 2
        elif mutation == "record_bytes":
            rows[0]["payload"]["definition"] = "x" * 65536
        elif mutation == "page_bytes":
            rows = [entity(str(i)) for i in range(6)]
            for row in rows:
                row["payload"]["definition"] = "x" * 50000
        elif mutation == "nan":
            rows[0]["payload"]["invalid"] = float("nan")
        elif mutation == "unknown_kind":
            rows[0]["kind"] = "approved_metric"
        with pytest.raises(KnowledgeValidationError):
            append(inventory, source_id, run, entities=rows)
        assert inventory.status("demo", source_id, run["id"])["page_count"] == 0
    assert count(store, store.inventory_entities) == count(store, store.inventory_pages) == 0


def test_claim_is_auto_captured_exact_generation_bound_and_expiry_blocks_commit(registry):
    store, source_id, inventory = registry
    queue = RefreshQueue(store)
    with principal_scope(OWNER):
        queue.enqueue("demo", source_id)
    store.access_enforced = False  # Only the trusted worker can claim the queued request.
    job = queue.claim("worker")
    store.access_enforced = True
    with principal_scope(OWNER), refresh_fence(job["id"], "worker", job["attempt"]):
        run = begin(inventory, source_id)
        run = append(inventory, source_id, run, entities=[entity()])
    with principal_scope(OWNER), pytest.raises(KnowledgeConflict, match="claim changed"):
        commit(inventory, source_id, run)
    with store.engine.begin() as conn:
        conn.execute(update(store.refresh_jobs).where(store.refresh_jobs.c.id == job["id"]).values(lease_until="2000-01-01T00:00:00Z"))
    with principal_scope(OWNER), refresh_fence(job["id"], "worker", job["attempt"]), pytest.raises(RefreshFenceLost):
        commit(inventory, source_id, run)
    assert count(store, store.inventory_heads) == 0


def test_abort_does_not_advance_current_or_complete_clock(registry):
    _store, source_id, inventory = registry
    with principal_scope(OWNER):
        run = begin(inventory, source_id)
        aborted = inventory.abort("demo", source_id, run["id"])
        assert aborted["status"] == "aborted" and aborted["completed_at"] is None
        assert current(inventory, source_id, run) is None
        with pytest.raises(KnowledgeConflict):
            append(inventory, source_id, run)


def test_scope_pins_are_canonical_and_unknown_watermarks_never_fabricated(registry):
    _store, source_id, inventory = registry
    with principal_scope(OWNER):
        run = begin(inventory, source_id, scope={"namespace": "finance", "selectors": ["z", "a"]})
        assert run["scope"] == {"namespace": "finance", "selectors": ["a", "z"]}
        assert run["scope_digest"] == inventory_digest(run["scope"])
        assert run["watermark"]["value"] is None
        with pytest.raises(KnowledgeValidationError):
            begin(inventory, source_id, watermark={"kind": "unknown", "value": "invented"})
        with pytest.raises(KnowledgeValidationError):
            begin(inventory, source_id, limits={**LIMITS, "max_pages": HARD_LIMITS["max_pages"] + 1})


def test_migration_creates_shared_inventory_tables_and_forward_only_history(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as conn:
            monkeypatch.setattr(v0012_inventory.op, "get_bind", lambda: conn)
            v0012_inventory.upgrade()
            expected = inventory_tables(MetaData())
            actual = inspect(conn)
            assert set(actual.get_table_names()) == {t.name for t in expected.values()}
            for table in expected.values():
                assert {c["name"] for c in actual.get_columns(table.name)} == set(table.c.keys())
                assert actual.get_pk_constraint(table.name)["constrained_columns"] == [c.name for c in table.primary_key]
        assert v0012_inventory.revision == "0012_inventory"
        assert v0012_inventory.down_revision == "0011_company_guidance"
        with pytest.raises(RuntimeError, match="forward-only"):
            v0012_inventory.downgrade()
    finally:
        engine.dispose()
