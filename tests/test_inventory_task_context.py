"""Published inventory meaning in actual isolated task packets and receipts.

These tests use synthetic catalog observations and real registry operations.
They qualify consumer effects, not a customer's source ACLs or deployment.
"""

import base64
import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from test_inventory_citations import (
    assertion,
    commit_catalog,
    setup_case,
)
from test_inventory_citations import (
    publish as publish_dataset,
)
from test_inventory_concepts import configure_access, count, draft, publish

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.agent_api import build_agent_api
from knowledge_layer.context_receipts import CONFIG_NAME
from knowledge_layer.guidance import GuidanceRegistry
from knowledge_layer.inventory_concepts import InventoryConcepts
from knowledge_layer.jobs import RefreshQueue
from knowledge_layer.store import (
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeStore,
    KnowledgeValidationError,
)
from knowledge_layer.task_context import revalidate_context, task_context


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.delenv(CONFIG_NAME, raising=False)
    monkeypatch.delenv(CONFIG_NAME + "_FILE", raising=False)
    fixture = setup_case(tmp_path)
    fixture["root"] = tmp_path
    fixture["concepts"] = InventoryConcepts(fixture["store"])
    try:
        yield fixture
    finally:
        fixture["store"].close()


def resolve(case, published=None, *, sources=None, query="Net revenue", **kwargs):
    if published is not None:
        kwargs.setdefault("inventory_selection", [{
            "connection_id": case["catalog"], "concept_ids": [published["id"]],
        }])
    return task_context(case["store"], tenant_id="demo", query=query,
                        connection_ids=sources or [case["catalog"]], **kwargs)


def revalidate(case, receipt, *, store=None):
    return revalidate_context(store or case["store"], tenant_id="demo", receipt=receipt)


def objects(value):
    """Find complete typed concept packets without assuming their nesting path."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


def inventory_packets(packet):
    return [item for item in objects(packet["contexts"])
            if item.get("schema_version") == "knowledge-inventory-concept-v1"]


def revoke_reader(case):
    store = case["store"]
    enforced = store.access_enforced
    try:
        store.access_enforced = False
        store.revoke_source_access("demo", case["catalog"], principal_type="user", principal_id="reader")
    finally:
        store.access_enforced = enforced


def reviewed_dataset(case, *, refs=None):
    assertion(case, approve=True, kind="metric", refs=refs)
    publish_dataset(case)
    assert case["store"].get_snapshot("demo", case["target"])["agent_ready"] is True


@pytest.mark.parametrize("selection", [
    [], {}, "all", [None],
    [{"connection_id": "selected", "concept_ids": []}],
    [{"connection_id": "selected", "concept_ids": "inventory_concept_" + "0" * 32}],
    [{"connection_id": "selected", "concept_ids": [""]}],
    [{"connection_id": "selected", "concept_ids": ["legacy-concept"]}],
    [{"connection_id": "selected", "concept_ids": ["inventory_concept_" + "A" * 32]}],
    [{"connection_id": "selected", "concept_ids": ["inventory_concept_" + "a" * 31]}],
    [{"connection_id": "selected", "concept_ids": ["inventory_concept_" + "a" * 32] * 2}],
    [{"connection_id": "selected", "concept_ids": ["inventory_concept_" + "a" * 32], "extra": True}],
    [{"connection_id": "selected", "concept_ids": ["inventory_concept_" + "a" * 32]}] * 2,
    [{"connection_id": "selected", "concept_ids": [f"inventory_concept_{number:032x}" for number in range(21)]}],
])
def test_selection_shape_refuses_before_source_or_concept_reads(case, selection, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("An invalid selection reached source or meaning reads")

    monkeypatch.setattr(case["store"], "get_connection", forbidden)
    monkeypatch.setattr(case["store"], "get_context", forbidden)
    with pytest.raises(KnowledgeValidationError):
        resolve(case, sources=["selected"], inventory_selection=deepcopy(selection))


def test_selection_cannot_extend_or_duplicate_selected_source_scope(case):
    published = publish(case)
    selection = [{"connection_id": case["catalog"], "concept_ids": [published["id"]]}]
    with pytest.raises(KnowledgeValidationError):
        resolve(case, sources=[case["target"]], inventory_selection=selection)
    for sources in ([case["catalog"]] * 2, [f"scope-{number}" for number in range(9)]):
        with pytest.raises(KnowledgeValidationError):
            resolve(case, sources=sources, inventory_selection=selection)


@pytest.mark.parametrize("duplicate", [False, True], ids=["total-over-twenty", "cross-source-duplicate"])
def test_selection_global_budget_and_identity_refuse_before_reads(case, monkeypatch, duplicate):
    def forbidden(*args, **kwargs):
        pytest.fail("A globally invalid selection reached source reads")

    first = [f"inventory_concept_{number:032x}" for number in range(11)]
    second = [first[0]] if duplicate else [f"inventory_concept_{number:032x}" for number in range(11, 21)]
    monkeypatch.setattr(case["store"], "get_connection", forbidden)
    with pytest.raises(KnowledgeValidationError):
        resolve(case, sources=["finance", "sales"], inventory_selection=[
            {"connection_id": "finance", "concept_ids": first},
            {"connection_id": "sales", "concept_ids": second},
        ])


def test_explicit_inventory_selection_preserves_full_meaning_and_exact_authority(case):
    suffix = "Refunds belong to finance; pipeline remains a separate sales measure."
    definition = "The approved finance scope preserves this complete business definition. " * 24 + suffix
    assert 1200 < len(definition) <= 4000
    published = publish(case, draft(case, definition=definition, concept_kind="metric_definition"))
    packet = resolve(case, published, query="Explain the selected finance definition")
    assert packet["schema_version"] == "task-context-v2"
    assert packet["status"] == "context_available" and packet["blockers"] == []
    assert packet["answerability"] == "business_meaning_available"
    assert packet["meaning_available"] is True and packet["query_coverage"] == "not_established"
    selected = inventory_packets(packet)
    assert len(selected) == 1 and selected[0] == published
    assert selected[0]["concept"]["definition"] == definition
    assert selected[0]["concept"]["definition"].endswith(suffix)
    assert selected[0]["evidence_ref"] == case["ref"]
    assert selected[0]["native_authority"] == "untrusted_evidence"
    assert selected[0]["source_agent_ready"] is False and selected[0]["locally_executable"] is False
    assert "snapshot_id" not in selected[0] and "snapshot" not in selected[0]
    assert packet["execution_state"] == "not_executed" and packet["rules_enforced"] is False
    assert case["store"].get_snapshot("demo", case["catalog"]) is None
    assert not case["store"].get_connection("demo", case["catalog"])["agent_ready"]
    pin = next(item for item in packet["sources"] if item.get("basis") == "inventory")
    assert pin["connection_id"] == case["catalog"] and pin["concept_ids"] == [published["id"]]
    assert len(pin["effective_digest"]) == 64
    assert pin["source_agent_ready"] is False and pin["locally_executable"] is False
    assert "snapshot_id" not in pin and "snapshot_version" not in pin
    concept_pin = pin["concept_pins"][0]
    for key in ("evidence_ref", "proposal_digest", "entity_digest", "evidence_digest", "inventory"):
        assert concept_pin[key] == published[key]
    assert concept_pin["concept_id"] == published["id"]
    assert concept_pin["observed_at"] == published["inventory"]["last_complete_scope_at"]
    assert case["store"].get_connection("demo", case["catalog"])["last_complete_refresh_at"] is None
    assert concept_pin["observed_at"] is not None
    assert len(concept_pin["publication_digest"]) == len(concept_pin["decision_digest"]) == 64
    receipt = packet["receipt"]
    assert receipt["schema_version"] == "context-receipt-v1"
    assert receipt["request"]["inventory_selection"] == [{
        "connection_id": case["catalog"], "concept_ids": [published["id"]],
    }]
    assert len(json.dumps(receipt, ensure_ascii=False).encode()) <= 4096
    checked = revalidate(case, receipt)
    assert checked["status"] == "current" and checked["answerability"] == "business_meaning_available"
    assert checked["source_permissions_verified"] is False


def test_query_resolution_uses_published_inventory_meaning_without_explicit_selection(case):
    published = publish(case)
    packet = resolve(case)
    assert packet["schema_version"] == "task-context-v2"
    assert packet["status"] == "context_available"
    assert packet["answerability"] == "business_meaning_available"
    assert [item["id"] for item in inventory_packets(packet)] == [published["id"]]
    assert set(packet["receipt"]["request"]) == {"query", "connection_ids", "business_unit", "task"}
    assert revalidate(case, packet["receipt"])["status"] == "current"
    unrelated = resolve(case, query="An absent business term")
    assert inventory_packets(unrelated) == []
    assert unrelated["answerability"] == "not_established"
    assert unrelated["status"] == "no_direct_match" and unrelated["meaning_available"] is False


def test_metadata_only_inventory_no_match_has_no_fabricated_snapshot_or_authority(case):
    packet = resolve(case)
    assert packet["schema_version"] == "task-context-v2"
    assert packet["status"] == "no_direct_match" and packet["blockers"] == []
    assert packet["answerability"] == "not_established" and packet["meaning_available"] is False
    assert inventory_packets(packet) == []
    assert not any(item.get("basis") == "snapshot" for item in packet["sources"])
    assert case["store"].get_snapshot("demo", case["catalog"]) is None
    assert revalidate(case, packet["receipt"])["answerability"] == "not_established"


def test_failed_inventory_without_matching_concepts_is_unavailable_not_empty(case):
    packet = resolve(case, query="An unmatched finance question")
    assert packet["status"] == "no_direct_match"
    with case["store"].engine.begin() as conn:
        conn.execute(update(case["store"].connections).where(case["store"].connections.c.id == case["catalog"])
                     .values(status="error"))
    with pytest.raises(KnowledgeConflict, match="unavailable"):
        resolve(case, query="An unmatched finance question")
    with pytest.raises(KnowledgeConflict, match="unavailable"):
        revalidate(case, packet["receipt"])


def test_active_refresh_without_matching_concepts_does_not_sign_checked_empty(case):
    packet = resolve(case, query="An unmatched finance question")
    RefreshQueue(case["store"]).enqueue("demo", case["catalog"])
    with pytest.raises(KnowledgeConflict, match="active refresh"):
        resolve(case, query="An unmatched finance question")
    with pytest.raises(KnowledgeConflict):
        revalidate(case, packet["receipt"])


def test_null_inventory_selection_is_omission_and_does_not_sign_a_fifth_request_key(case):
    publish(case)
    omitted = resolve(case)
    explicit_null = resolve(case, inventory_selection=None)
    assert explicit_null["receipt"]["request"] == omitted["receipt"]["request"]
    assert "inventory_selection" not in explicit_null["receipt"]["request"]
    assert explicit_null["receipt"]["context_digest"] == omitted["receipt"]["context_digest"]
    assert revalidate(case, explicit_null["receipt"])["status"] == "current"


def test_unpublished_exact_selection_refuses_and_same_named_meanings_stay_distinct(case):
    unpublished = draft(case)
    with pytest.raises((KnowledgeNotFound, KnowledgeConflict)):
        resolve(case, unpublished)
    first = publish(case, unpublished)
    second = publish(case, draft(case, definition="Sales net revenue counts a different population of bookings."))
    packet = resolve(case, first)
    assert [item["id"] for item in inventory_packets(packet)] == [first["id"]]
    assert second["concept"]["definition"] not in json.dumps(packet["contexts"])


def test_twenty_selected_concepts_are_complete_and_have_revalidatable_bounded_receipt(case):
    published = [publish(case, draft(case, name=f"Finance meaning {number}")) for number in range(20)]
    selection = [{"connection_id": case["catalog"], "concept_ids": [item["id"] for item in published]}]
    packet = resolve(case, inventory_selection=selection)
    assert {item["id"] for item in inventory_packets(packet)} == {item["id"] for item in published}
    assert len(inventory_packets(packet)) == 20
    assert len(json.dumps(packet["receipt"], ensure_ascii=False).encode()) <= 4096
    assert revalidate(case, packet["receipt"])["status"] == "current"


def test_mixed_inventory_and_published_dataset_receipt_preserves_both_bases(case):
    published = publish(case)
    reviewed_dataset(case)
    packet = resolve(case, published, sources=[case["catalog"], case["target"]])
    assert packet["schema_version"] == "task-context-v2" and packet["status"] == "context_available"
    assert packet["answerability"] == "requires_source_execution"
    assert {item["basis"] for item in packet["sources"]} == {"snapshot", "inventory"}
    assert inventory_packets(packet) == [published]
    assert revalidate(case, packet["receipt"])["status"] == "current"
    case["store"].revoke_publication("demo", case["target"], reason="The dataset owner withdrew this metric authority.")
    with pytest.raises(KnowledgeConflict):
        revalidate(case, packet["receipt"])
    blocked = resolve(case, published, sources=[case["catalog"], case["target"]])
    assert blocked["status"] == "blocked" and inventory_packets(blocked) == [published]
    assert blocked["meaning_available"] is True
    assert blocked["answerability"] != "requires_source_execution"


@pytest.mark.parametrize("publish_snapshot", [False, True], ids=["unpublished-dataset", "published-dataset"])
def test_same_source_dataset_and_inventory_overlay_keep_separate_authority(case, publish_snapshot):
    published = publish(case)
    catalog_path = case["root"] / "catalog"
    catalog_path.mkdir()
    (catalog_path / "orders.csv").write_text("order_id,net_amount\n10,17.25\n11,0\n")
    snapshot = case["store"].refresh_connection("demo", case["catalog"])["snapshot"]
    if publish_snapshot:
        asset = snapshot["assets"][0]
        handle = next(item["id"] for item in snapshot["evidence"]
                      if item.get("asset_id") == asset["id"] and (item.get("details") or {}).get("field") == "net_amount")
        reviewed_dataset({**case, "target": case["catalog"], "asset": asset["id"], "handle": handle}, refs=[])
    packet = resolve(case, published)
    assert packet["schema_version"] == "task-context-v2"
    assert len(packet["sources"]) == 2
    assert {item["basis"] for item in packet["sources"]} == {"snapshot", "inventory"}
    assert {item["connection_id"] for item in packet["sources"]} == {case["catalog"]}
    assert packet["meaning_available"] is True and inventory_packets(packet) == [published]
    if publish_snapshot:
        assert packet["status"] == "context_available"
        assert packet["answerability"] == "requires_source_execution"
        assert revalidate(case, packet["receipt"])["status"] == "current"
    else:
        assert packet["status"] == "blocked" and packet["blockers"]
        assert packet["answerability"] == "not_established"
        with pytest.raises(KnowledgeConflict):
            revalidate(case, packet["receipt"])


def test_snapshot_only_request_retains_legacy_packet_and_four_field_receipt(case):
    reviewed_dataset(case)
    packet = resolve(case, sources=[case["target"]])
    assert packet["schema_version"] == "task-context-v1"
    assert set(packet["receipt"]["request"]) == {"query", "connection_ids", "business_unit", "task"}
    assert revalidate(case, packet["receipt"])["status"] == "current"


def test_explicit_same_source_handles_do_not_resolve_unselected_inventory_meaning(case, monkeypatch):
    selected = publish(case)
    unselected = publish(case, draft(case, definition="Another reviewed population with the same name."))
    catalog_path = case["root"] / "catalog"
    catalog_path.mkdir()
    (catalog_path / "orders.csv").write_text("order_id,net_amount\n10,17.25\n11,0\n")
    case["store"].refresh_connection("demo", case["catalog"])
    original = InventoryConcepts.get

    def selected_only(self, tenant, cid, *, concept_id):
        assert concept_id != unselected["id"], "Explicit selection resolved an unselected primary meaning"
        return original(self, tenant, cid, concept_id=concept_id)

    monkeypatch.setattr(InventoryConcepts, "get", selected_only)
    packet = resolve(case, selected)
    assert inventory_packets(packet) == [selected]
    assert packet["status"] == "blocked" and packet["meaning_available"] is True


def test_scoped_company_guidance_can_accompany_inventory_without_snapshot_dependencies(case):
    published = publish(case)
    registry = GuidanceRegistry(case["store"])
    guidance = registry.draft("demo", "finance-research", {
        "title": "Finance research", "description": "Preserve each finance definition's population.",
        "business_units": [], "tasks": [], "connection_ids": [case["catalog"]],
        "audience": {"company_wide": True, "subjects": [], "groups": []},
        "skills": [{"name": "finance-meaning", "description": "Explain source-specific finance meaning.",
            "instructions": "Explain the selected finance population before comparing figures.",
            "required_capabilities": ["knowledge_context"]}],
        "rules": [{"key": "source-writes", "statement": "Source writes are forbidden.", "value": False}],
        "dependencies": [],
    })
    registry.publish("demo", "finance-research", revision=guidance["revision"],
                     expected_digest=guidance["digest"],
                     rationale="The steward reviewed this exact company guidance and its source scope.")
    packet = resolve(case, published)
    assert packet["guidance"]["status"] == "ready" and packet["status"] == "context_available"
    assert packet["guidance"]["bundles"][0]["payload"]["dependencies"] == []
    assert packet["answerability"] == "business_meaning_available" and packet["rules_enforced"] is False
    assert revalidate(case, packet["receipt"])["status"] == "current"
    registry.revoke("demo", "finance-research", revision=guidance["revision"],
                    reason="The steward withdrew this finance research procedure.")
    with pytest.raises(KnowledgeConflict):
        revalidate(case, packet["receipt"])


@pytest.mark.parametrize("change", ["replacement", "withdrawal", "refresh"])
def test_inventory_receipt_refuses_current_authority_changes_and_retains_history(case, change):
    published = publish(case)
    receipt = resolve(case, published)["receipt"]
    if change == "replacement":
        commit_catalog(case, definition="A changed current native catalog observation.")
    elif change == "withdrawal":
        case["concepts"].withdraw("demo", case["catalog"], concept_id=published["id"],
                                 reason="The steward withdrew this inventory definition.")
    else:
        RefreshQueue(case["store"]).enqueue("demo", case["catalog"])
    with pytest.raises((KnowledgeConflict, KnowledgeNotFound)):
        revalidate(case, receipt)
    assert count(case["store"], case["store"].inventory_concept_proposals) == 1
    assert count(case["store"], case["store"].inventory_concept_publications) == 1


def test_denied_and_foreign_principal_never_receive_selected_meaning(case):
    owner, _, reader = configure_access(case)
    with principal_scope(owner):
        published = publish(case)
    with principal_scope(reader):
        receipt = resolve(case, published)["receipt"]
    with principal_scope(Principal("denied", frozenset())):
        with pytest.raises(KnowledgeNotFound):
            resolve(case, published)
        with pytest.raises(KnowledgeNotFound):
            revalidate(case, receipt)
    revoke_reader(case)
    with principal_scope(reader), pytest.raises((KnowledgeNotFound, KnowledgeConflict)):
        revalidate(case, receipt)


@pytest.mark.parametrize("change", ["replacement", "withdrawal", "refresh", "grant"])
def test_task_final_guidance_assembly_cannot_return_changed_inventory_authority(case, monkeypatch, change):
    owner, _, reader = configure_access(case)
    with principal_scope(owner):
        published = publish(case)
    original, calls = GuidanceRegistry.resolve, 0

    def during_final_guidance(self, *args, **kwargs):
        nonlocal calls
        result = original(self, *args, **kwargs)
        calls += 1
        if calls == 2:
            if change == "grant":
                revoke_reader(case)
            else:
                with principal_scope(owner):
                    if change == "replacement":
                        commit_catalog(case, definition="A successor committed during task assembly.")
                    elif change == "withdrawal":
                        case["concepts"].withdraw("demo", case["catalog"], concept_id=published["id"],
                                                 reason="The steward withdrew meaning during response assembly.")
                    else:
                        RefreshQueue(case["store"]).enqueue("demo", case["catalog"])
        return result

    monkeypatch.setattr(GuidanceRegistry, "resolve", during_final_guidance)
    with principal_scope(reader), pytest.raises((KnowledgeConflict, KnowledgeNotFound)):
        resolve(case, published)
    assert calls == 2
    with case["store"].engine.connect() as conn:
        retained = conn.execute(select(case["store"].inventory_concept_publications).where(
            case["store"].inventory_concept_publications.c.concept_id == published["id"])).mappings().one()
    assert retained["proposal_digest"] == published["proposal_digest"]
    assert bool(retained["revoked_at"]) == (change == "withdrawal")


def test_agent_http_accepts_inventory_selection_and_revalidates_complete_meaning(case):
    published = publish(case, draft(case, definition="A complete local finance definition. " * 40))
    with TestClient(build_agent_api(case["store"])) as client:
        response = client.post("/task-context", json={
            "query": "Net revenue", "connection_ids": [case["catalog"]],
            "inventory_selection": [{"connection_id": case["catalog"], "concept_ids": [published["id"]]}],
        })
        assert response.status_code == 200
        packet = response.json()
        assert inventory_packets(packet) == [published]
        checked = client.post("/task-context/revalidate", json=packet["receipt"])
        assert checked.status_code == 200 and checked.json()["status"] == "current"


def test_shared_registry_replica_revalidates_inventory_selection_and_refuses_withdrawal(tmp_path, monkeypatch):
    monkeypatch.delenv(CONFIG_NAME + "_FILE", raising=False)
    monkeypatch.setenv(CONFIG_NAME, json.dumps({"version": 1, "registry_id": "inventory-task-fixture",
        "active_key_id": "fixture", "keys": [{"id": "fixture",
        "secret": base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")}]}))
    fixture = setup_case(tmp_path)
    fixture["concepts"] = InventoryConcepts(fixture["store"])
    peer = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.sqlite'}",
                          local_files_root=tmp_path, initialize_schema=False)
    try:
        published = publish(fixture)
        packet = resolve(fixture, published)
        assert packet["receipt"]["schema_version"] == "context-receipt-v2"
        assert packet["receipt_route"] == "shared_registry_replicas"
        assert revalidate(fixture, packet["receipt"], store=peer)["status"] == "current"
        fixture["concepts"].withdraw("demo", fixture["catalog"], concept_id=published["id"],
                                     reason="The steward withdrew this shared-registry definition.")
        with pytest.raises((KnowledgeConflict, KnowledgeNotFound)):
            revalidate(fixture, packet["receipt"], store=peer)
    finally:
        peer.close()
        fixture["store"].close()
