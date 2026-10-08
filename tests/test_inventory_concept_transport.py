"""Inventory-only concepts preserve owner authority across HTTP and MCP."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_inventory_citations import commit_catalog, setup_case

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.agent_api import build_agent_api
from knowledge_layer.inventory_transport import create_inventory_router, inventory_operation
from knowledge_layer.quick_mcp import build_quick_mcp_server
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import KnowledgeAccessDenied, KnowledgeValidationError


def owner(case):
    return principal_scope(Principal("owner", frozenset({case["store"].source_admin_group}),
                                     frozenset({"knowledge.sources.manage"})))


def reader():
    return principal_scope(Principal("reader", frozenset()))


@pytest.fixture
def concept_case(tmp_path):
    case = setup_case(tmp_path)
    store, cid = case["store"], case["catalog"]
    for subject, role in (("owner", "manage"), ("reader", "read")):
        store.grant_source_access("demo", cid, principal_type="user", principal_id=subject, role=role)
    store.source_policy = SourcePolicy.from_json(json.dumps({"version": 1, "sources": [{
        "tenant_id": "demo", "kind": "local_files", "selector": {"path": "catalog", "include": "**/*"},
        "allowed_subjects": ["owner", "reader"], "allowed_groups": [],
    }]}))
    store.access_enforced = True
    # Policy changes invalidate older discovery heads. Capture a current head
    # under the operator's actual approval rather than borrowing the old pins.
    with owner(case):
        commit_catalog(case)
    try:
        yield case
    finally:
        store.close()


def payload(case, *, name="Net revenue"):
    return {"evidence_ref": deepcopy(case["ref"]), "name": name,
            "definition": "Net revenue excludes refunds.", "concept_kind": "business_term",
            "rationale": "The local owner interprets this exact observed finance definition."}


def pins(case):
    return {"inventory_id": case["ref"]["inventory_id"],
            "expected_manifest_digest": case["ref"]["manifest_digest"]}


def browser(store):
    app = FastAPI()
    app.include_router(create_inventory_router(store, allow_writes=True, allow_curation_review=True,
                                               allowed_kinds=frozenset({"local_files"})))
    return TestClient(app)


def review_and_publish(client, case, draft):
    prefix = f"/sources/{case['catalog']}/inventory-concepts"
    reviewed = client.post(prefix + "/review_concept", json={"payload": {
        "concept_id": draft["id"], "decision": "approve", "rationale": "The finance owner reviewed this definition.",
    }})
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["proposal"]["publication_state"] == "unpublished"
    assert reviewed.json()["proposal"]["concept"]["definition_status"] == "approved"
    published = client.post(prefix + "/publish_concept", json={"payload": {
        "concept_id": draft["id"], "expected_proposal_digest": draft["proposal_digest"],
        "rationale": "Publish this exact owner-reviewed local concept for current inventory consumers.",
    }})
    assert published.status_code == 200, published.text
    return published.json()


def test_http_inventory_only_concept_lifecycle_and_specific_route_order(concept_case):
    case, store = concept_case, concept_case["store"]
    cid = case["catalog"]
    page_path = f"/sources/{cid}/inventory/{case['ref']['inventory_id']}/concepts"
    prefix = f"/sources/{cid}/inventory-concepts"
    with owner(case), browser(store) as client:
        missing_pin = client.get(page_path)
        assert missing_pin.status_code == 422
        initial = client.get(page_path, params={"expected_manifest_digest": case["ref"]["manifest_digest"]})
        assert initial.status_code == 200 and initial.json()["items"] == []
        assert initial.json()["basis"] == "inventory"
        draft_response = client.post(prefix + "/propose_concept", json={"payload": payload(case)})
        assert draft_response.status_code == 200, draft_response.text
        draft = draft_response.json()
        assert draft["publication_state"] == "unpublished"
        assert draft["concept"]["definition_status"] == "proposed"
        assert draft["proposal_digest"] and draft["evidence_ref"] == case["ref"]
        assert client.get(prefix + f"/{draft['id']}").status_code == 404
        default = client.get(page_path, params={"expected_manifest_digest": case["ref"]["manifest_digest"]})
        assert default.json()["items"] == []
        drafts = client.get(page_path, params={"expected_manifest_digest": case["ref"]["manifest_digest"], "include_drafts": True})
        assert [item["id"] for item in drafts.json()["items"]] == [draft["id"]]
        published = review_and_publish(client, case, draft)
        assert published["publication_state"] == "published"
        assert published["concept"]["trust_state"] == "locally_reviewed"
        assert published["evidence_ref"] == case["ref"]
        full = client.get(prefix + f"/{draft['id']}")
        assert full.status_code == 200 and full.json() == published
        current = client.get(page_path, params={"expected_manifest_digest": case["ref"]["manifest_digest"]})
        assert current.status_code == 200
        assert current.json()["items"] == [published]
        assert current.json()["manifest_digest"] == case["ref"]["manifest_digest"]
        withdrawn = client.post(prefix + "/withdraw_concept", json={"payload": {
            "concept_id": draft["id"], "reason": "The owner withdraws this local definition from consumers.",
        }})
        assert withdrawn.status_code == 200
        assert withdrawn.json()["publication_state"] == "withdrawn"
        assert "concept" not in withdrawn.json()
        assert client.get(prefix + f"/{draft['id']}").status_code == 404
        assert client.get(page_path, params={"expected_manifest_digest": case["ref"]["manifest_digest"]}).json()["items"] == []
        assert store.get_snapshot("demo", cid) is None
        assert published["source_agent_ready"] is published["locally_executable"] is published["source_reads_executed"] is False


def test_agents_can_propose_but_cannot_review_publish_withdraw_or_stage_curation(concept_case):
    case, store = concept_case, concept_case["store"]
    cid = case["catalog"]
    prefix = f"/sources/{cid}/inventory-concepts"
    with reader(), TestClient(build_agent_api(store, enable_source_management=True)) as client:
        response = client.post(prefix + "/propose_concept", json={"payload": payload(case)})
        assert response.status_code == 200, response.text
        draft = response.json()
        for operation in ("review_concept", "publish_concept", "withdraw_concept"):
            assert client.post(prefix + "/" + operation, json={"payload": {"concept_id": draft["id"]}}).status_code in {404, 405}
        assert client.post(f"/sources/{cid}/inventory/propose_concept", json={"payload": payload(case)}).status_code == 422
        page_path = f"/sources/{cid}/inventory/{case['ref']['inventory_id']}/concepts"
        visible = client.get(page_path, params={"expected_manifest_digest": case["ref"]["manifest_digest"]})
        assert visible.status_code == 200 and visible.json()["items"] == []
        denied_drafts = client.get(page_path, params={"expected_manifest_digest": case["ref"]["manifest_digest"], "include_drafts": True})
        assert denied_drafts.status_code == 403
    with owner(case), browser(store) as client:
        review_and_publish(client, case, draft)
    with reader(), TestClient(build_agent_api(store)) as client:
        assert client.get(prefix + f"/{draft['id']}").status_code == 200
        assert client.post(prefix + "/propose_concept", json={"payload": payload(case)}).status_code in {404, 405}


def test_mcp_proposal_and_read_schema_preserve_pins_and_owner_boundary(concept_case):
    case, store = concept_case, concept_case["store"]
    cid = case["catalog"]

    async def run():
        from mcp.server.mcpserver.exceptions import ToolError

        server = build_quick_mcp_server(store, enable_source_management=True)
        tools = {tool.name: tool for tool in await server.list_tools()}
        curate_schema = tools["curate_inventory"].input_schema["properties"]["operation"]
        assert curate_schema.get("enum", [curate_schema.get("const")]) == ["propose_concept"]
        stage_schema = tools["stage_inventory"].input_schema["properties"]["operation"]
        assert set(stage_schema["enum"]) == {"begin", "append", "commit", "abort"}
        assert {"concepts", "concept"}.issubset(tools["get_inventory"].input_schema["properties"]["operation"]["enum"])
        with reader():
            proposed = await server.call_tool("curate_inventory", {
                "connection_id": cid, "operation": "propose_concept", "payload": payload(case),
            })
            draft = proposed.structured_content
            assert json.loads(proposed.content[0].text) == draft
            assert draft["publication_state"] == "unpublished" and draft["evidence_ref"] == case["ref"]
            for operation in ("review_concept", "publish_concept", "withdraw_concept"):
                with pytest.raises(ToolError):
                    await server.call_tool("curate_inventory", {"connection_id": cid, "operation": operation, "payload": {"concept_id": draft["id"]}})
            with pytest.raises(ToolError):
                await server.call_tool("stage_inventory", {"connection_id": cid, "operation": "propose_concept", "payload": payload(case)})
        with owner(case), browser(store) as client:
            published = review_and_publish(client, case, draft)
        with reader():
            page = (await server.call_tool("get_inventory", {"connection_id": cid, "operation": "concepts", "payload": pins(case)})).structured_content
            assert page["items"] == [published]
            full = (await server.call_tool("get_inventory", {"connection_id": cid, "operation": "concept", "payload": {"concept_id": draft["id"]}})).structured_content
            assert full == published
            names = {tool.name for tool in await build_quick_mcp_server(store).list_tools()}
            assert "get_inventory" in names and "curate_inventory" not in names and "stage_inventory" not in names

    asyncio.run(run())


@pytest.mark.parametrize("operation,base", [
    ("concepts", {}), ("concept", {"concept_id": "inventory_concept_" + "a" * 32}),
    ("propose_concept", {}), ("review_concept", {}), ("publish_concept", {}), ("withdraw_concept", {}),
])
@pytest.mark.parametrize("authority", ["tenant_id", "principal", "publication_state", "agent_ready"])
def test_inventory_concept_operations_refuse_caller_authority_fields(concept_case, operation, base, authority):
    with owner(concept_case), pytest.raises(KnowledgeValidationError, match="unsupported"):
        inventory_operation(concept_case["store"], concept_case["catalog"], operation,
                            {**base, authority: "caller-claim"}, allow_writes=True)


@pytest.mark.parametrize("operation", ["concepts", "concept", "propose_concept", "review_concept", "publish_concept", "withdraw_concept"])
def test_inventory_concept_operations_bind_required_keys_before_execution(concept_case, operation):
    with owner(concept_case), pytest.raises(KnowledgeValidationError, match="required"):
        inventory_operation(concept_case["store"], concept_case["catalog"], operation, {}, allow_writes=True)


def test_concept_payload_budget_finite_json_and_source_kind_restriction_write_no_proposal(concept_case):
    case, store = concept_case, concept_case["store"]
    with reader():
        for bad, message in (({**payload(case), "definition": "Ω" * (128 * 1024)}, "256 KiB"),
                             ({**payload(case), "definition": float("nan")}, "finite UTF-8"),
                             ({**payload(case), "definition": "\ud800"}, "finite UTF-8")):
            with pytest.raises(KnowledgeValidationError, match=message):
                inventory_operation(store, case["catalog"], "propose_concept", bad, allow_writes=True)
        with pytest.raises(KnowledgeAccessDenied, match="source kind"):
            inventory_operation(store, case["catalog"], "propose_concept", payload(case),
                                allow_writes=True, allowed_kinds=frozenset({"postgres"}))
        with pytest.raises(KnowledgeAccessDenied, match="not enabled"):
            inventory_operation(store, case["catalog"], "propose_concept", payload(case))
    with store.engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(store.inventory_concept_proposals)).scalar_one() == 0


def test_http_concept_page_query_cursor_digest_and_read_permission_are_preserved(concept_case):
    case, store = concept_case, concept_case["store"]
    cid = case["catalog"]
    page_path = f"/sources/{cid}/inventory/{case['ref']['inventory_id']}/concepts"
    with owner(case), browser(store) as client:
        drafts = []
        for name in ("Net revenue alpha", "Net revenue beta"):
            response = client.post(f"/sources/{cid}/inventory-concepts/propose_concept", json={"payload": payload(case, name=name)})
            assert response.status_code == 200, response.text
            drafts.append(response.json())
            review_and_publish(client, case, drafts[-1])
        query = {"expected_manifest_digest": case["ref"]["manifest_digest"], "query": "Net revenue", "limit": 1}
        first = client.get(page_path, params=query)
        assert first.status_code == 200 and len(first.json()["items"]) == 1
        assert first.json()["next_cursor"]
        second = client.get(page_path, params={**query, "after": first.json()["next_cursor"]})
        assert second.status_code == 200 and len(second.json()["items"]) == 1
        assert first.json()["items"][0]["id"] != second.json()["items"][0]["id"]
        assert second.json()["next_cursor"] is None
        changed_query = client.get(page_path, params={**query, "query": "beta", "after": first.json()["next_cursor"]})
        assert changed_query.status_code == 422
        wrong_pin = client.get(page_path, params={"expected_manifest_digest": "0" * 64})
        assert wrong_pin.status_code == 409
        assert client.get(page_path, params={**query, "limit": 21}).status_code == 422
        assert client.get(page_path, params={**query, "query": "x" * 201}).status_code == 422
    with principal_scope(Principal("stranger", frozenset())), TestClient(build_agent_api(store)) as client:
        assert client.get(page_path, params={"expected_manifest_digest": case["ref"]["manifest_digest"]}).status_code == 404
        assert client.get(f"/sources/{cid}/inventory-concepts/{drafts[0]['id']}").status_code == 404
