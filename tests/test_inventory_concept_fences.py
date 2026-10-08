"""Consumer and final-response effects over real synthetic registry state."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_inventory_citations import commit_catalog, setup_case
from test_inventory_concepts import configure_access, count, draft, page, publish

from knowledge_layer.access import principal_scope
from knowledge_layer.access_log import source_read_trace
from knowledge_layer.agent_api import build_agent_api
from knowledge_layer.inventory_concepts import InventoryConcepts
from knowledge_layer.jobs import RefreshQueue
from knowledge_layer.mcp import build_knowledge_mcp_server
from knowledge_layer.store import KnowledgeConflict, KnowledgeNotFound


@pytest.fixture
def case(tmp_path):
    fixture = setup_case(tmp_path)
    fixture["concepts"] = InventoryConcepts(fixture["store"])
    try:
        yield fixture
    finally:
        fixture["store"].close()


def access_case(case):
    owner, manager, reader = configure_access(case)
    # Changing operator approval invalidates the earlier inventory policy pin.
    # Commit a fresh real manifest under the current owner before using it.
    with principal_scope(owner):
        commit_catalog(case)
    return owner, manager, reader


def revoke_as_operator(case, subject):
    store = case["store"]
    enforced = store.access_enforced
    try:
        store.access_enforced = False
        store.revoke_source_access(
            "demo", case["catalog"], principal_type="user", principal_id=subject
        )
    finally:
        store.access_enforced = enforced


@pytest.mark.parametrize("surface", ["advanced_mcp", "agent_http"])
def test_long_definition_survives_actual_direct_id_consumer_surface(case, surface):
    suffix = "Refunds belong to finance; pipeline remains a separate sales measure."
    definition = "The finance definition preserves its complete approved scope. " * 24 + suffix
    assert 1200 < len(definition) <= 4000
    published = publish(case, draft(case, definition=definition))

    if surface == "advanced_mcp":
        async def consume():
            server = build_knowledge_mcp_server(case["store"])
            result = await server.call_tool(
                "get_concept_context", {"concept_id": published["id"]}
            )
            assert not result.is_error
            return result.structured_content

        context = asyncio.run(consume())
    else:
        with TestClient(build_agent_api(case["store"])) as client:
            response = client.get("/context", params={
                "concept_id": published["id"], "connection_id": case["catalog"]
            })
            assert response.status_code == 200
            context = response.json()

    assert context["concept"]["definition"] == definition
    assert context["concept"]["definition"].endswith(suffix)
    assert context["evidence_ref"] == case["ref"]
    assert context["publication_state"] == "published"
    assert context["native_authority"] == "untrusted_evidence"
    assert "snapshot_id" not in context and "snapshot" not in context
    assert case["store"].get_snapshot("demo", case["catalog"]) is None


@pytest.mark.parametrize("job_state", ["queued", "running", "retry_wait"])
def test_refresh_requested_after_publication_suspends_consumers(case, job_state):
    published = publish(case)
    queue = RefreshQueue(case["store"])
    job = queue.enqueue("demo", case["catalog"])
    if job_state != "queued":
        claimed = queue.claim("inventory-concept-fence")
        assert claimed["id"] == job["id"]
        if job_state == "retry_wait":
            assert queue.finish(
                job["id"], "inventory-concept-fence", attempt=claimed["attempt"],
                error_code="discovery_failed"
            )
    assert queue.get("demo", job["id"])["status"] == job_state

    for consume in (
        lambda: case["concepts"].get(
            "demo", case["catalog"], concept_id=published["id"]
        ),
        lambda: page(case),
    ):
        with pytest.raises(KnowledgeConflict, match="active refresh"):
            consume()
    with case["store"].engine.connect() as conn:
        publication = conn.execute(select(case["store"].inventory_concept_publications)).mappings().one()
    assert publication["revoked_at"] is None
    assert publication["proposal_digest"] == published["proposal_digest"]


@pytest.mark.parametrize("operation", ["propose", "review", "publish"])
@pytest.mark.parametrize("change", ["head", "access"])
def test_final_mutation_fence_refuses_stale_authority_and_preserves_history(
    case, monkeypatch, operation, change
):
    owner, _, _ = access_case(case)
    concepts, store = case["concepts"], case["store"]
    observed = {}
    with principal_scope(owner):
        proposal = draft(case) if operation != "propose" else None
        if operation == "publish":
            concepts.decide("demo", case["catalog"], concept_id=proposal["id"], decision="approve")
        original = concepts._packet

        def change_after_commit(*args):
            packet = original(*args)
            observed["packet"] = packet
            if change == "head":
                commit_catalog(case, definition="Finance changed its catalog definition.")
            else:
                revoke_as_operator(case, owner.subject)
            return packet

        monkeypatch.setattr(concepts, "_packet", change_after_commit)
        expected_error = KnowledgeConflict if change == "head" else KnowledgeNotFound
        with pytest.raises(expected_error):
            if operation == "propose":
                draft(case)
            elif operation == "review":
                concepts.decide(
                    "demo", case["catalog"], concept_id=proposal["id"], decision="approve"
                )
            else:
                concepts.publish(
                    "demo", case["catalog"], concept_id=proposal["id"],
                    expected_proposal_digest=proposal["proposal_digest"],
                    rationale="The steward accepts this exact reviewed catalog definition."
                )

    packet = observed["packet"]
    assert count(store, store.inventory_concept_proposals) == 1
    assert count(store, store.decisions) == (0 if operation == "propose" else 1)
    assert count(store, store.inventory_concept_publications) == (operation == "publish")
    with store.engine.connect() as conn:
        persisted = conn.execute(select(store.inventory_concept_proposals)).mappings().one()
        event_types = set(conn.execute(select(store.audit_events.c.event_type)).scalars())
    assert persisted["id"] == packet["id"]
    assert persisted["proposal_digest"] == packet["proposal_digest"]
    assert "proposal.submit" in event_types
    if operation != "propose":
        assert "proposal.decide" in event_types
    if operation == "publish":
        assert "inventory_concept.publish" in event_types
    assert count(store, store.snapshots) == 1


def test_publication_withdrawn_during_publish_response_is_retained_but_not_returned(
    case, monkeypatch
):
    concepts = case["concepts"]
    proposal = draft(case)
    concepts.decide("demo", case["catalog"], concept_id=proposal["id"], decision="approve")
    original = concepts._packet

    def withdraw_after_commit(*args):
        packet = original(*args)
        concepts.withdraw(
            "demo", case["catalog"], concept_id=proposal["id"],
            reason="The steward withdraws this definition during response assembly."
        )
        return packet

    monkeypatch.setattr(concepts, "_packet", withdraw_after_commit)
    with pytest.raises(KnowledgeConflict, match="assembly"):
        concepts.publish(
            "demo", case["catalog"], concept_id=proposal["id"],
            expected_proposal_digest=proposal["proposal_digest"],
            rationale="Publish the approved local definition before the simultaneous withdrawal."
        )
    with case["store"].engine.connect() as conn:
        publication = conn.execute(select(case["store"].inventory_concept_publications)).mappings().one()
        event_types = set(conn.execute(select(case["store"].audit_events.c.event_type)).scalars())
    assert publication["revoked_at"] is not None
    assert publication["proposal_digest"] == proposal["proposal_digest"]
    assert {"inventory_concept.publish", "inventory_concept.withdraw"} <= event_types


@pytest.mark.parametrize("operation", ["get", "page"])
def test_reader_grant_withdrawal_during_context_assembly_returns_no_definition(
    case, monkeypatch, operation
):
    owner, _, reader = access_case(case)
    with principal_scope(owner):
        published = publish(case)
    original = case["concepts"]._packet

    def revoke_after_assembly(*args):
        packet = original(*args)
        revoke_as_operator(case, reader.subject)
        return packet

    monkeypatch.setattr(case["concepts"], "_packet", revoke_after_assembly)
    with principal_scope(reader), pytest.raises(KnowledgeNotFound):
        if operation == "get":
            case["concepts"].get("demo", case["catalog"], concept_id=published["id"])
        else:
            page(case)
    assert count(case["store"], case["store"].inventory_concept_publications) == 1


def test_empty_authorized_page_is_recorded_as_a_source_read(case):
    with source_read_trace() as trace:
        result = page(case)
    assert result["items"] == []
    assert trace.sources() == [("demo", case["catalog"])]


def test_denied_empty_page_does_not_emit_a_source_read(case):
    _, _, reader = access_case(case)
    revoke_as_operator(case, reader.subject)
    with principal_scope(reader), source_read_trace() as trace, pytest.raises(KnowledgeNotFound):
        page(case)
    assert trace.sources() == []
