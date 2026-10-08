"""Exact inventory consumption preserves discovery authority and current grants."""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import delete, select, update

from knowledge_layer.access import principal_scope
from knowledge_layer.inventory import MAX_PAGE_BYTES, inventory_digest
from knowledge_layer.store import KnowledgeConflict, KnowledgeNotFound, KnowledgeValidationError

spec = importlib.util.spec_from_file_location(
    "selection_inventory_fixture", Path(__file__).with_name("test_inventory.py"))
assert spec and spec.loader
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
registry = base.registry


def _complete(inventory, source_id, *, entities=(), evidence=(), **options):
    run = base.begin(inventory, source_id, **options)
    pages = [(list(entities[start:start + 3]), []) for start in range(0, len(entities), 3)]
    pages += [([], list(evidence[start:start + 100])) for start in range(0, len(evidence), 100)]
    pages = pages or [([], [])]
    for index, (entity_rows, evidence_rows) in enumerate(pages):
        run = base.append(inventory, source_id, run, entities=entity_rows,
                          evidence_rows=evidence_rows, cursor=None if index == 0 else f"page.{index}",
                          next_cursor=f"page.{index + 1}" if index + 1 < len(pages) else None)
    return base.commit(inventory, source_id, run)


def _selection(inventory, source_id, run, ids=("entity.1",), **changes):
    return inventory.select("demo", source_id, run["id"],
                            expected_manifest_digest=changes.get("digest", run["manifest_digest"]),
                            entity_ids=list(ids))


def _state(store):
    tables = [store.connections, store.snapshots, store.proposals, store.publications,
              store.audit_events, store.inventory_runs, store.inventory_pages,
              store.inventory_entities, store.inventory_evidence, store.inventory_heads]
    with store.engine.connect() as conn:
        return {table.name: sorted(json.dumps(dict(row), sort_keys=True, ensure_ascii=False)
                                   for row in conn.execute(select(table)).mappings())
                for table in tables}


def test_selection_resolves_later_page_evidence_preserves_full_payload_and_writes_nothing(registry):
    store, source_id, inventory = registry
    entity = base.entity(refs=("proof.2", "proof.1"))
    entity["payload"]["definition"] = "\n完整 native meaning.\n" * 1200
    entity["payload"]["opaque_vendor"] = {"nested": [None, 0, False, ""], "agent_ready": True}
    proofs = [base.evidence("proof.1"), base.evidence("proof.2")]
    with principal_scope(base.OWNER):
        run = _complete(inventory, source_id, entities=[entity], evidence=proofs)
    before = _state(store)
    with principal_scope(base.READER):
        result = _selection(inventory, source_id, run)
    assert result["schema_version"] == "knowledge-inventory-selection-v1"
    assert result["inventory"]["id"] == run["id"]
    for pin in ("manifest_digest", "generation", "last_complete_scope_at", "scope_digest",
                "policy_digest", "watermark_digest", "source_identity_digest", "source_policy_digest"):
        assert result["inventory"][pin] == run[pin]
    assert result["inventory"]["watermark"] == {"kind": "unknown", "value": None}
    assert result["entities"] == [{**entity, "digest": inventory_digest(entity)}]
    assert {row["id"]: row for row in result["evidence"]} == {
        row["id"]: {**row, "digest": inventory_digest(row)} for row in proofs}
    assert result["text_complete"] is True
    assert result["meaning_certified"] is False and result["agent_ready"] is False
    assert result["source_reads_executed"] is False
    assert result["source_text_authority"] == "untrusted_evidence"
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= MAX_PAGE_BYTES
    assert _state(store) == before


def test_selection_returns_only_requested_entities_and_deduplicates_their_evidence(registry):
    _store, source_id, inventory = registry
    entities = [base.entity("chosen.a", refs=("shared", "specific")),
                base.entity("chosen.b", refs=("shared",)), base.entity("not-selected", refs=("private",))]
    evidence = [base.evidence(name) for name in ("shared", "specific", "private")]
    with principal_scope(base.OWNER):
        run = _complete(inventory, source_id, entities=entities, evidence=evidence)
    with principal_scope(base.READER):
        result = _selection(inventory, source_id, run, ids=("chosen.b", "chosen.a"))
    assert {row["id"] for row in result["entities"]} == {"chosen.a", "chosen.b"}
    assert {row["id"] for row in result["evidence"]} == {"shared", "specific"}
    assert len(result["evidence"]) == 2


@pytest.mark.parametrize("ids", [[], ["entity.1", "entity.1"], [""], [1], [True],
                                     ["x" * 257], [f"entity.{i}" for i in range(21)]])
def test_invalid_selection_shape_refuses_without_changing_registry(registry, ids):
    store, source_id, inventory = registry
    with principal_scope(base.OWNER):
        run = _complete(inventory, source_id, entities=[base.entity()])
    before = _state(store)
    with principal_scope(base.READER), pytest.raises(KnowledgeValidationError):
        _selection(inventory, source_id, run, ids=ids)
    assert _state(store) == before


def test_missing_entity_and_stale_digest_refuse_whole_selection(registry):
    _store, source_id, inventory = registry
    with principal_scope(base.OWNER):
        run = _complete(inventory, source_id, entities=[base.entity()])
    with principal_scope(base.READER):
        with pytest.raises(KnowledgeNotFound):
            _selection(inventory, source_id, run, ids=("entity.1", "missing"))
        with pytest.raises(KnowledgeConflict):
            _selection(inventory, source_id, run, digest="0" * 64)


@pytest.mark.parametrize("unusable", ["staging", "aborted", "historical"])
def test_selection_requires_complete_current_manifest(registry, unusable):
    _store, source_id, inventory = registry
    with principal_scope(base.OWNER):
        if unusable == "historical":
            run = _complete(inventory, source_id, entities=[base.entity()])
            _complete(inventory, source_id)
        else:
            run = base.append(inventory, source_id, base.begin(inventory, source_id),
                              entities=[base.entity()])
            if unusable == "aborted":
                inventory.abort("demo", source_id, run["id"])
        with pytest.raises(KnowledgeConflict):
            _selection(inventory, source_id, run)


def test_incomplete_successor_keeps_current_selection_but_complete_removal_invalidates_it(registry):
    _store, source_id, inventory = registry
    with principal_scope(base.OWNER):
        old = _complete(inventory, source_id, entities=[base.entity()])
        staging = base.append(inventory, source_id, base.begin(inventory, source_id))
        assert _selection(inventory, source_id, old)["entities"][0]["id"] == "entity.1"
        new = base.commit(inventory, source_id, staging)
        with pytest.raises(KnowledgeConflict):
            _selection(inventory, source_id, old)
        assert inventory.page("demo", source_id, new["id"], collection="removed")["items"][0]["id"] == "entity.1"
        with pytest.raises(KnowledgeNotFound):
            _selection(inventory, source_id, new)


def test_selection_does_not_alias_same_native_id_from_other_manifest(registry):
    store, source_id, inventory = registry
    with principal_scope(base.OWNER):
        old = _complete(inventory, source_id, entities=[base.entity()])
        changed = base.entity()
        changed["payload"]["definition"] = "A different unit's meaning with the same native ID."
        other = _complete(inventory, source_id, entities=[changed],
                          scope={"namespace": "other-unit", "selectors": ["other.*"]})
        assert _selection(inventory, source_id, old)["entities"][0]["payload"] == base.entity()["payload"]
        assert _selection(inventory, source_id, other)["entities"][0]["payload"] == changed["payload"]
    store.access_enforced = False
    try:
        second = store.create_connection("demo", kind="local_files", name="Other source", config={})
        store.grant_source_access("demo", second["id"], principal_type="user", principal_id="reader", role="read")
    finally:
        store.access_enforced = True
    with principal_scope(base.READER), pytest.raises(KnowledgeNotFound):
        _selection(inventory, second["id"], old)


@pytest.mark.parametrize("corruption", ["deleted-proof", "missing-proof-reference"])
def test_corrupt_exact_evidence_references_refuse_instead_of_partial_packet(registry, corruption):
    store, source_id, inventory = registry
    with principal_scope(base.OWNER):
        run = _complete(inventory, source_id, entities=[base.entity(refs=("proof",))],
                        evidence=[base.evidence("proof")])
    with store.engine.begin() as conn:
        if corruption == "deleted-proof":
            conn.execute(delete(store.inventory_evidence).where(
                store.inventory_evidence.c.inventory_id == run["id"]))
        else:
            conn.execute(update(store.inventory_entities).where(
                store.inventory_entities.c.inventory_id == run["id"]).values(evidence_ids=["absent"]))
    with principal_scope(base.READER), pytest.raises(KnowledgeConflict):
        _selection(inventory, source_id, run)


def test_selection_refuses_more_than_200_distinct_proofs(registry):
    _store, source_id, inventory = registry
    proofs = [base.evidence(f"proof.{i}") for i in range(220)]
    entities = [base.entity(f"entity.{i}", refs=[row["id"] for row in proofs[i * 20:(i + 1) * 20]])
                for i in range(11)]
    with principal_scope(base.OWNER):
        run = _complete(inventory, source_id, entities=entities, evidence=proofs)
    with principal_scope(base.READER), pytest.raises(KnowledgeValidationError):
        _selection(inventory, source_id, run, ids=[row["id"] for row in entities])


def test_oversized_complete_selection_refuses_without_clipping_or_writes(registry):
    store, source_id, inventory = registry
    entities = [base.entity(f"large.{i}") for i in range(5)]
    for row in entities:
        row["payload"]["definition"] = "full native text " * 3700
    with principal_scope(base.OWNER):
        run = _complete(inventory, source_id, entities=entities)
    before = _state(store)
    with principal_scope(base.READER), pytest.raises(KnowledgeValidationError):
        _selection(inventory, source_id, run, ids=[row["id"] for row in entities])
    assert _state(store) == before


@pytest.mark.parametrize("operation", ["select", "scopes"])
def test_revoked_source_grant_hides_all_selection_and_scope_metadata(registry, operation):
    store, source_id, inventory = registry
    with principal_scope(base.OWNER):
        run = _complete(inventory, source_id, entities=[base.entity()])
    store.access_enforced = False
    try:
        store.revoke_source_access("demo", source_id, principal_type="user", principal_id="reader")
    finally:
        store.access_enforced = True
    with principal_scope(base.READER), pytest.raises(KnowledgeNotFound):
        if operation == "select":
            _selection(inventory, source_id, run)
        else:
            inventory.scopes("demo", source_id)


def test_selection_and_scope_discovery_never_materialize_legacy_snapshot(registry, monkeypatch):
    store, source_id, inventory = registry
    with principal_scope(base.OWNER):
        run = _complete(inventory, source_id, entities=[base.entity()])
    def forbidden(*_args, **_kwargs):
        pytest.fail("discovery consumption materialized a legacy snapshot")
    monkeypatch.setattr(store, "get_connection", forbidden)
    monkeypatch.setattr(store, "get_snapshot", forbidden)
    with principal_scope(base.READER):
        assert _selection(inventory, source_id, run)["entities"]
        assert inventory.scopes("demo", source_id)["items"]


def test_scope_discovery_pages_current_heads_without_caller_known_digests(registry):
    store, source_id, inventory = registry
    with principal_scope(base.OWNER):
        manifests = [_complete(inventory, source_id,
                               scope={"namespace": f"unit.{i}", "selectors": ["orders"]})
                     for i in range(5)]
        replacement = _complete(inventory, source_id, scope=manifests[0]["scope"])
    before = _state(store)
    seen, cursor = {}, None
    with principal_scope(base.READER):
        for _ in range(4):
            page = inventory.scopes("demo", source_id, after=cursor, limit=2)
            assert page["schema_version"] == "knowledge-inventory-v1"
            assert page["connection_id"] == source_id
            assert len(page["items"]) <= 2
            for row in page["items"]:
                assert row["id"] not in seen
                assert row["compatibility"]["meaning_certified"] is False
                assert row["generation"] >= 1 and row["last_complete_scope_at"] is not None
                seen[row["id"]] = row
            cursor = page["next_cursor"]
            if cursor is None:
                break
        else:
            pytest.fail("scope pagination failed to terminate")
    assert set(seen) == {replacement["id"], *(row["id"] for row in manifests[1:])}
    assert _state(store) == before


@pytest.mark.parametrize("limit", [0, -1, 21, True, "2"])
def test_scope_page_limits_are_bounded_strict_integers(registry, limit):
    _store, source_id, inventory = registry
    with principal_scope(base.READER), pytest.raises(KnowledgeValidationError):
        inventory.scopes("demo", source_id, limit=limit)


@pytest.mark.parametrize("cursor", ["not-base64", "", "x" * 2001, "e30="])
def test_invalid_scope_cursor_refuses(registry, cursor):
    _store, source_id, inventory = registry
    with principal_scope(base.READER), pytest.raises(KnowledgeValidationError):
        inventory.scopes("demo", source_id, after=cursor)


def test_unknown_scope_inventory_differs_from_complete_empty_scope(registry):
    _store, source_id, inventory = registry
    with principal_scope(base.READER):
        absent = inventory.scopes("demo", source_id)
        assert absent["items"] == [] and absent["next_cursor"] is None
    with principal_scope(base.OWNER):
        empty = _complete(inventory, source_id)
    with principal_scope(base.READER):
        known = inventory.scopes("demo", source_id)
        assert len(known["items"]) == 1
        assert known["items"][0]["id"] == empty["id"]
        assert known["items"][0]["entity_count"] == 0
        assert known["items"][0]["status"] == "committed"


def test_maximum_20_entities_and_200_shared_evidence_records_are_usable(registry):
    _store, source_id, inventory = registry
    proofs = [base.evidence(f"proof.{i}") for i in range(200)]
    entities = [base.entity(f"entity.{i}", refs=[row["id"] for row in
                                               proofs[(i % 10) * 20:(i % 10 + 1) * 20]])
                for i in range(20)]
    with principal_scope(base.OWNER):
        run = _complete(inventory, source_id, entities=entities, evidence=proofs)
    with principal_scope(base.READER):
        packet = _selection(inventory, source_id, run, ids=[row["id"] for row in entities])
        assert len(packet["entities"]) == 20
        assert len(packet["evidence"]) == 200
        assert packet["text_complete"] is True


@pytest.mark.parametrize("collection", ["entity", "evidence"])
def test_payload_corruption_cannot_keep_an_existing_record_digest(registry, collection):
    store, source_id, inventory = registry
    with principal_scope(base.OWNER):
        run = _complete(inventory, source_id, entities=[base.entity(refs=("proof",))],
                        evidence=[base.evidence("proof")])
    table = store.inventory_entities if collection == "entity" else store.inventory_evidence
    with store.engine.begin() as conn:
        conn.execute(update(table).where(table.c.inventory_id == run["id"]).values(
            payload={"native_version": None, "definition": "Changed bytes without changing the digest."}))
    with principal_scope(base.READER), pytest.raises(KnowledgeConflict, match="integrity"):
        _selection(inventory, source_id, run)


def test_scope_cursor_is_bound_to_source_and_exact_existing_marker(registry):
    store, source_id, inventory = registry
    with principal_scope(base.OWNER):
        for index in range(2):
            _complete(inventory, source_id, scope={"namespace": f"unit.{index}", "selectors": ["orders"]})
    with principal_scope(base.READER):
        cursor = inventory.scopes("demo", source_id, limit=1)["next_cursor"]
        assert cursor is not None
    store.access_enforced = False
    try:
        other = store.create_connection("demo", kind="local_files", name="Other scoped source", config={})
        store.grant_source_access("demo", other["id"], principal_type="user", principal_id="reader", role="read")
    finally:
        store.access_enforced = True
    with principal_scope(base.READER):
        with pytest.raises(KnowledgeValidationError, match="mismatched"):
            inventory.scopes("demo", other["id"], after=cursor)
        token = json.loads(base64.urlsafe_b64decode(cursor))
        token["after"] = ["0" * 64, "0" * 64]
        forged = base64.urlsafe_b64encode(json.dumps(token).encode()).decode()
        with pytest.raises(KnowledgeValidationError, match="resolve"):
            inventory.scopes("demo", source_id, after=forged)


@pytest.mark.parametrize("operation", ["select", "scopes"])
@pytest.mark.parametrize("change", ["grant", "policy", "error", "head"])
def test_changes_during_response_assembly_refuse_before_return(registry, monkeypatch, operation, change):
    from knowledge_layer import inventory as module

    store, source_id, inventory = registry
    with principal_scope(base.OWNER):
        run = _complete(inventory, source_id, entities=[base.entity()])
        replacement = base.append(inventory, source_id, base.begin(inventory, source_id))
    real_encoded, injected = module._encoded, []

    def change_after_encoding(value):
        encoded = real_encoded(value)
        is_selection = isinstance(value, dict) and value.get("schema_version") == "knowledge-inventory-selection-v1"
        is_scopes = isinstance(value, dict) and "items" in value and "next_cursor" in value
        if not injected and ((operation == "select" and is_selection) or (operation == "scopes" and is_scopes)):
            injected.append(change)
            if change == "grant":
                with store.engine.begin() as conn:
                    conn.execute(delete(store.source_grants).where(
                        store.source_grants.c.connection_id == source_id,
                        store.source_grants.c.principal_id == "reader"))
            elif change == "policy":
                store.source_policy = base.source_policy(("owner", "reader"))
            elif change == "error":
                with store.engine.begin() as conn:
                    conn.execute(update(store.connections).where(
                        store.connections.c.id == source_id).values(status="error"))
            else:
                with principal_scope(base.OWNER):
                    base.commit(inventory, source_id, replacement)
        return encoded

    monkeypatch.setattr(module, "_encoded", change_after_encoding)
    expected = KnowledgeNotFound if change == "grant" else KnowledgeConflict
    with principal_scope(base.READER), pytest.raises(expected):
        if operation == "select":
            _selection(inventory, source_id, run)
        else:
            inventory.scopes("demo", source_id)
    assert injected == [change]


@pytest.mark.parametrize("operation", ["select", "scopes"])
def test_retargeted_source_cannot_reuse_inventory_even_with_new_operator_approval(registry, operation):
    store, source_id, inventory = registry
    with principal_scope(base.OWNER):
        run = _complete(inventory, source_id, entities=[base.entity()])
    with store.engine.begin() as conn:
        config = dict(conn.execute(select(store.connections.c.config).where(
            store.connections.c.id == source_id)).scalar_one())
        config["path"] = "different-native-source"
        conn.execute(update(store.connections).where(store.connections.c.id == source_id).values(config=config))
    store.source_policy = base.SourcePolicy.from_json(json.dumps({"version": 1, "sources": [{
        "tenant_id": "demo", "kind": "local_files", "selector": {
            "path": "different-native-source", "include": "**/*"},
        "allowed_subjects": ["owner", "reader"], "allowed_groups": [],
    }]}))
    with principal_scope(base.READER), pytest.raises(KnowledgeConflict, match="identity or approval policy changed"):
        if operation == "select":
            _selection(inventory, source_id, run)
        else:
            inventory.scopes("demo", source_id)


def test_scope_response_byte_budget_refuses_without_clipping_or_writes(registry):
    store, source_id, inventory = registry
    selectors = [f"selector.{index:03}." + "x" * 240 for index in range(200)]
    with principal_scope(base.OWNER):
        for index in range(6):
            _complete(inventory, source_id, scope={"namespace": f"wide.{index}", "selectors": selectors})
    before = _state(store)
    with principal_scope(base.READER):
        with pytest.raises(KnowledgeValidationError, match="256 KiB"):
            inventory.scopes("demo", source_id, limit=20)
        smaller = inventory.scopes("demo", source_id, limit=2)
        assert len(smaller["items"]) == 2 and smaller["next_cursor"] is not None
        assert len(smaller["items"][0]["scope"]["selectors"]) == 200
    assert _state(store) == before


def test_empty_scope_response_rechecks_source_error_after_assembly(registry, monkeypatch):
    from knowledge_layer import inventory as module

    store, source_id, inventory = registry
    real_encoded, changed = module._encoded, []
    def fail_empty_source(value):
        result = real_encoded(value)
        if isinstance(value, dict) and value.get("items") == [] and "next_cursor" in value and not changed:
            changed.append(True)
            with store.engine.begin() as conn:
                conn.execute(update(store.connections).where(
                    store.connections.c.id == source_id).values(status="error"))
        return result
    monkeypatch.setattr(module, "_encoded", fail_empty_source)
    with principal_scope(base.READER), pytest.raises(KnowledgeConflict, match="unavailable"):
        inventory.scopes("demo", source_id)
    assert changed == [True]
