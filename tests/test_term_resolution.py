"""Consumer term resolution preserves scope, review and suggestion boundaries."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.agent_api import build_agent_api
from knowledge_layer.mcp import _compact_term_context, build_knowledge_mcp_server
from knowledge_layer.quick_mcp import build_quick_mcp_server
from knowledge_layer.retrieval import context_text_match
from knowledge_layer.store import KnowledgeStore


@pytest.fixture
def reviewed_metrics(tmp_path):
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    (tmp_path / "sim_runs.csv").write_text("run_id,cost_actual_usd\nR-1,0.1\n", encoding="utf-8")
    source = store.create_connection("demo", kind="local_files", name="Research local", config={"path": "."})
    snapshot = store.refresh_connection("demo", source["id"])["snapshot"]
    asset = snapshot["assets"][0]
    field_evidence = next(item["id"] for item in snapshot["evidence"]
                          if (item.get("details") or {}).get("field") == "cost_actual_usd")
    proposal = store.submit_contract_assertion(
        "demo", source["id"], asset_id=asset["id"], assertion_kind="metric",
        expected_snapshot_id=snapshot["id"], evidence_ids=[field_evidence],
        payload={"name": "Recorded Sim Lab completion cost", "measure_field": "cost_actual_usd",
                 "description": "Optional execution completion costs recorded for Sim Lab; missing values remain unknown.",
                 "aggregation": "sum", "unit": "USD"}, rationale="The source measure records completion costs.",
    )
    store.decide_proposal("demo", source["id"], proposal["id"], decision="approve")
    yield store, source["id"], asset["id"], proposal["id"]
    store.close()


def test_direct_approved_metric_resolves_with_exact_current_source(reviewed_metrics):
    store, source_id, asset_id, proposal_id = reviewed_metrics
    result = store.get_context("demo", term="Recorded Sim Lab completion cost", connection_id=source_id)
    metric = result["metric_matches"][0]
    assert result["matches"] == result["retrieval_candidates"] == []
    assert metric["match_basis"] == "exact_name"
    assert metric["decision_proposal_id"] == proposal_id
    assert metric["source_asset_id"] == asset_id
    assert metric["field"] == "cost_actual_usd"
    assert metric["contract_ref"] == {"connection_id": source_id, "asset_id": asset_id}
    assert metric["snapshot_id"] == store.get_snapshot("demo", source_id)["id"]
    assert metric["trust_state"] == "owner_reviewed"
    assert metric["certified"] is False


def test_paraphrase_returns_suggestion_and_not_an_approved_alias(reviewed_metrics):
    store, source_id, _, _ = reviewed_metrics
    result = store.get_context("demo", term="Sim Lab execution costs", connection_id=source_id)
    assert result["matches"] == result["metric_matches"] == result["cited_glossary"] == []
    candidate = next(item for item in result["retrieval_candidates"] if item["candidate_type"] == "metric")
    assert candidate["result_kind"] == "retrieval_candidate"
    assert candidate["match_basis"] == "token_overlap"
    assert candidate["requires_confirmation"] is True
    assert candidate["matched_tokens"] == ["cost", "execution", "lab", "sim"]
    assert candidate["context"]["status"] == "approved"


def test_pending_and_schema_stale_metrics_do_not_resolve(reviewed_metrics, tmp_path):
    store, source_id, asset_id, _ = reviewed_metrics
    snapshot = store.get_snapshot("demo", source_id)
    evidence_id = next(item["id"] for item in snapshot["evidence"]
                       if (item.get("details") or {}).get("field") == "cost_actual_usd")
    store.submit_contract_assertion(
        "demo", source_id, asset_id=asset_id, assertion_kind="metric",
        expected_snapshot_id=snapshot["id"], evidence_ids=[evidence_id],
        payload={"name": "Unreviewed research expense", "measure_field": "cost_actual_usd",
                 "description": "An unreviewed alternative meaning", "aggregation": "sum", "unit": "USD"},
        rationale="This meaning is still awaiting owner review.",
    )
    assert store.get_context("demo", term="Unreviewed research expense", connection_id=source_id)["metric_matches"] == []
    (tmp_path / "sim_runs.csv").write_text("run_id\nR-1\n", encoding="utf-8")
    store.refresh_connection("demo", source_id)
    stale = store.get_context("demo", term="Sim Lab execution costs", connection_id=source_id)
    assert stale["metric_matches"] == []
    assert not any(item["candidate_type"] == "metric" for item in stale["retrieval_candidates"])


def test_source_scope_and_revoked_access_do_not_borrow_metric(reviewed_metrics):
    store, source_id, _, _ = reviewed_metrics
    other = store.create_connection("demo", kind="local_files", name="Research production", config={})
    store.save_snapshot("demo", other["id"], {"snapshot_id": "empty", "assets": [], "concepts": []})
    result = store.get_context("demo", term="Recorded Sim Lab completion cost", connection_id=other["id"])
    assert result["metric_matches"] == result["retrieval_candidates"] == []
    principal = Principal("consumer", frozenset())
    store.grant_source_access("demo", source_id, principal_type="user", principal_id="consumer", role="read")
    store.access_enforced = True
    with principal_scope(principal):
        assert store.get_context("demo", term="Recorded Sim Lab completion cost")["metric_matches"]
    store.access_enforced = False
    store.revoke_source_access("demo", source_id, principal_type="user", principal_id="consumer")
    store.access_enforced = True
    with principal_scope(principal):
        assert store.get_context("demo", term="Recorded Sim Lab completion cost", connection_id=source_id) is None
        assert store.get_context("demo", term="Recorded Sim Lab completion cost")["metric_matches"] == []


def test_both_mcp_surfaces_and_http_expose_suggestions_and_metrics(reviewed_metrics):
    store, source_id, _, _ = reviewed_metrics

    async def run():
        for server, tool in ((build_quick_mcp_server(store), "resolve_context"),
                             (build_knowledge_mcp_server(store), "resolve_business_term")):
            result = (await server.call_tool(tool, {"term": "Sim Lab execution costs", "connection_id": source_id})).structured_content
            assert result["retrieval_candidates"][0]["requires_confirmation"] is True
            exact = (await server.call_tool(tool, {"term": "Recorded Sim Lab completion cost", "connection_id": source_id})).structured_content
            assert exact["metric_matches"][0]["field"] == "cost_actual_usd"
    asyncio.run(run())
    with TestClient(build_agent_api(store)) as client:
        assert client.get("/context", params={"term": "Sim Lab execution costs", "connection_id": source_id}).status_code == 200
        assert client.get("/context", params={"term": "Totally unrelated bananas", "connection_id": source_id}).status_code == 404


@pytest.mark.parametrize("query,name,text", [
    ("tickets resolved", "Resolved ticket", "Resolved ticket count for support"),
    ("player projection input snapshot", "Projection serving", "Player projection serving rows identify the input snapshot"),
    ("order net revenue", "Net revenue", "Net revenue per order in EUR"),
])
def test_token_suggestions_generalize_without_domain_synonyms(query, name, text):
    match = context_text_match(query, name=name, text=text)
    assert match["result_kind"] == "retrieval_candidate"
    assert match["requires_confirmation"] is True


@pytest.mark.parametrize("query", ["missing ticket revenue", "billing spend", "request", "", "of the"])
def test_unsupported_or_generic_token_overlap_is_not_a_suggestion(query):
    assert context_text_match(query, name="Ticket revenue", text="Ticket revenue includes resolved requests") is None


@pytest.mark.parametrize("query,name,text", [
    ("gross revenue", "Net revenue", "Net revenue excludes gross revenue"),
    ("billed charges", "Recorded completion cost", "Run completion cost does not establish billed charges"),
])
def test_excluded_description_phrase_is_relevance_only(query, name, text):
    match = context_text_match(query, name=name, text=text)
    assert match["result_kind"] == "retrieval_candidate"
    assert match["match_basis"] == "text_phrase"
    assert match["requires_confirmation"] is True


def test_stopword_only_phrase_does_not_match_a_description():
    assert context_text_match("of the", name="Invoice", text="Amount of the invoice") is None


def test_broad_term_ranks_exact_name_before_bounded_graph_expansion(monkeypatch):
    store = KnowledgeStore(database_url="sqlite:///:memory:")
    source = store.create_connection("demo", kind="local_files", name="Orders", config={})
    store.save_snapshot("demo", source["id"], {
        "assets": [], "snapshot_id": "large-schema",
        "concepts": [{"id": f"c-{index:04}", "name": f"Order field {index}"} for index in range(1_000)]
        + [{"id": "exact-last", "name": "Order"}],
    })
    expanded = []
    original = store._context_for_concept

    def count(*args):
        expanded.append(args[1]["id"])
        return original(*args)

    monkeypatch.setattr(store, "_context_for_concept", count)
    result = store.get_context("demo", term="Order", connection_id=source["id"])
    assert expanded[0] == "exact-last"
    assert len(expanded) == 10
    assert result["total_matches"] == 1_001
    compact = _compact_term_context(result)
    assert compact["total_matches"] == 1_001
    assert compact["more_available"] is True
    assert len(compact["matches"]) == 3
    store.close()


def test_metric_only_scoped_lookup_does_not_read_other_source_concepts(reviewed_metrics, monkeypatch):
    store, source_id, _, _ = reviewed_metrics
    scopes = []
    original = store.get_concepts

    def track(tenant_id, connection_id=None):
        scopes.append(connection_id)
        return original(tenant_id, connection_id)

    monkeypatch.setattr(store, "get_concepts", track)
    assert store.get_context("demo", term="Recorded Sim Lab completion cost", connection_id=source_id)["metric_matches"]
    # Scoped concepts come from the already-read source; no global scan is needed.
    assert scopes == []


def test_reviewed_metric_is_not_hidden_by_many_inferred_concepts(reviewed_metrics):
    store, source_id, _, _ = reviewed_metrics
    snapshot = store.get_snapshot("demo", source_id)
    snapshot["concepts"] += [
        {"id": f"noise-{index}", "name": "Sim Lab cost", "description": "Sim Lab execution costs",
         "status": "candidate"} for index in range(11)
    ]
    store.save_snapshot("demo", source_id, snapshot)
    result = store.get_context("demo", term="Sim Lab execution costs", connection_id=source_id)
    assert result["retrieval_candidate_total_matches"] == 12
    compact = _compact_term_context(result)
    assert compact["retrieval_candidates"][0]["candidate_type"] == "metric"
    assert compact["retrieval_candidates"][0]["requires_confirmation"] is True
