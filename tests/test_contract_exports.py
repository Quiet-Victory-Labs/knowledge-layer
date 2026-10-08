"""Machine contracts survive the actual HTTP and MCP serialization boundaries."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack, asynccontextmanager
from importlib.resources import files

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from knowledge_layer import mcp
from knowledge_layer.agent_api import build_agent_api
from knowledge_layer.auth import AgentBearerBoundary
from knowledge_layer.contracts import ossie_preview, semantic_contract, validate_assertion
from knowledge_layer.mcp import build_knowledge_mcp_server
from knowledge_layer.quick_mcp import build_quick_mcp_server
from knowledge_layer.store import KnowledgeValidationError

_HEADERS = {"Authorization": "Bearer test-only"}
_PRIVATE_MARKER = "fixture-private-source-content"


class ProjectedStore:
    """A source projection fixture; no database or source clients are opened."""

    def __init__(self, *, oversized: bool = False):
        description = "Ω" * 1300 if not oversized else _PRIVATE_MARKER + "x" * 1970
        fields = [
            {"name": f"measure_{index}", "data_type": "number", "nullable": None,
             "native_type": "decimal(38,12)", "precision": 38, "scale": 12,
             "field_id": index + 1}
            for index in range(1300 if oversized else 67)
        ]
        fields[0]["name"] = "record_id"
        fields[30]["name"] = "cost_estimate_usd"
        fields[31]["name"] = "cost_actual_usd"
        for field in fields if oversized else fields[-1:]:
            field.update(description=description, description_status="approved")
        assets = [
            {"id": f"asset-{index:02d}", "name": f"measures_{index}", "asset_type": "table",
             "source_pointer": f"postgres/public/measures_{index}",
             "schema": fields if index == 0 else fields[:1],
             "profile": {"semantic_samples": {"raw_rows": [_PRIVATE_MARKER]}}}
            for index in range(24)
        ]
        assets[0]["grain_contract"] = {
            "description": "One record per declared identifier", "key_fields": ["record_id"],
            "status": "approved", "evidence_ids": ["evidence-00"],
        }
        assets[0]["query_examples"] = [{
            "proposal_id": "query-example", "question": "Read the cost measures",
            "dialect": "ANSI_SQL", "sql": "SELECT " + ", ".join('"cost_actual_usd"' for _ in range(90))
            + " FROM public.measures_0", "expected_grain": "One row per record",
            "execution_verified": False, "status": "approved_unverified", "evidence_ids": ["evidence-00"],
        }]
        proposals = [
            {"id": f"proposal-{index:02d}", "proposal_type": "contract_assertion",
             "assertion_kind": "dataset_description", "asset_id": "asset-00",
             "source_snapshot_id": "snapshot-current", "payload": {"description": description},
             "evidence_ids": [f"evidence-{handle:02d}" for handle in range(12)],
             "evidence_refs": [], "rationale": "Owner must review this source description.",
             "status": "pending_review", "certified": False}
            for index in range(25)
        ]
        proposals.extend([
            {**proposals[0], "id": "proposal-approved", "status": "approved",
             "effective_on_current_snapshot": True},
            {**proposals[0], "id": "proposal-stale", "status": "approved",
             "effective_on_current_snapshot": False},
        ])
        metrics = [{"id": f"contract:metric-proposal-{index}", "name": f"Total cost {index}",
                    "description": "Sum of the actual cost measure.", "source_asset_id": "asset-00",
                    "field": "cost_actual_usd", "aggregation": "sum", "unit": "USD dollars",
                    "status": "approved", "trust_state": "owner_reviewed", "certified": False,
                    "evidence_ids": ["evidence-00"], "decision_proposal_id": f"metric-proposal-{index}"}
                   for index in range(12)]
        proposals.extend([
            {"id": metric["decision_proposal_id"], "proposal_type": "contract_assertion",
             "assertion_kind": "metric", "asset_id": "asset-00", "source_snapshot_id": "snapshot-current",
             "payload": {"name": metric["name"], "description": metric["description"],
                         "measure_field": metric["field"], "aggregation": metric["aggregation"], "unit": metric["unit"]},
             "evidence_ids": ["evidence-00"], "evidence_refs": [], "status": "approved",
             "effective_on_current_snapshot": True, "certified": False}
            for metric in metrics
        ])
        self.snapshot = {
            "id": "snapshot-current", "version": 3, "agent_ready": False, "assets": assets,
            "evidence": [{"id": f"evidence-{index:02d}", "asset_id": asset["id"],
                          "evidence_type": "asset_metadata", "summary": "Observed table schema"}
                         for index, asset in enumerate(assets)],
            "proposals": proposals, "concepts": [],
            "metrics": metrics,
            "relationships": [{"id": f"join-{index}", "subject_id": "asset-00", "object_id": f"asset-{index:02d}",
                               "source_asset_id": "asset-00", "target_asset_id": f"asset-{index:02d}",
                               "source_field": "record_id", "target_field": "record_id", "predicate": "joins_to",
                               "cardinality": "many_to_one", "caveat": "Owner-reviewed identifier scope only.",
                               "status": "approved", "trust_state": "owner_reviewed", "certified": False,
                               "evidence_ids": ["evidence-00", f"evidence-{index:02d}"]}
                              for index in range(1, 13)],
        }

    def get_connection(self, tenant_id, connection_id):
        assert tenant_id == "demo"
        if connection_id != "source-1":
            return None
        return {"kind": "postgres", "config": {"password": _PRIVATE_MARKER}, "snapshot": self.snapshot}


def _app(store):
    advanced = build_knowledge_mcp_server(store)
    compact = build_quick_mcp_server(store)

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(advanced.session_manager.run())
            await stack.enter_async_context(compact.session_manager.run())
            yield

    private = FastAPI()
    private.mount("/api", build_agent_api(store))
    for path, server in (("/advanced-mcp", advanced), ("/mcp", compact)):
        private.mount(path, server.streamable_http_app(
            streamable_http_path="/", json_response=True, stateless_http=True,
        ))
    app = FastAPI(lifespan=lifespan)
    app.mount("/agent", AgentBearerBoundary(private, "test-only"))
    return app


def _read(client, surface, *, ossie=False):
    if surface == "http":
        suffix = "ossie-preview" if ossie else "contract"
        return client.get(f"/agent/api/sources/source-1/{suffix}", headers=_HEADERS)
    tool = "get_ossie_preview" if ossie else "get_semantic_contract"
    arguments = {"connection_id": "source-1"}
    if surface == "compact":
        tool = "get_contract"
        arguments["asset_id"] = "asset-00"
    path = "/agent/mcp/" if surface == "compact" else "/agent/advanced-mcp/"
    return client.post(path, headers=_HEADERS | {
        "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18",
    }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": tool, "arguments": arguments}})


def _payload(response, surface):
    assert response.status_code == 200, response.text
    payload = response.json()
    if surface == "http":
        return payload
    result = payload["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    return result["structuredContent"]


@pytest.mark.parametrize("surface", ["http", "advanced", "compact"])
def test_machine_contract_preserves_wide_schema_pending_assertions_and_exact_metadata(surface):
    store = ProjectedStore()
    with TestClient(_app(store), base_url="http://127.0.0.1:8765") as client:
        response = _read(client, surface)
        payload = _payload(response, surface)
    native = semantic_contract(store, tenant_id="demo", connection_id="source-1")
    if surface == "compact":
        dataset = payload["dataset"]
        assertions = payload["assertions"]
        assert payload["pending_assertions"] == 25
        assert payload["stale_assertions"] == 1
        assert dataset == native["datasets"][0]
    else:
        assert payload == native
        assert len(payload["datasets"]) == 24
        dataset = payload["datasets"][0]
        assertions = {state: payload[f"{state}_assertions"] for state in ("approved", "pending", "stale")}
    assert len(dataset["fields"]) == 67
    assert dataset["fields"][30]["name"] == "cost_estimate_usd"
    assert dataset["fields"][31]["name"] == "cost_actual_usd"
    assert dataset["fields"][-1] == store.snapshot["assets"][0]["schema"][-1]
    assert dataset["query_examples"][0]["sql"] == store.snapshot["assets"][0]["query_examples"][0]["sql"]
    assert dataset["query_examples"][0]["execution_verified"] is False
    assert len(payload["joins"]) == len(payload["metrics"]) == 12
    guidance = payload["metrics"][0]["query_guidance"]
    assert guidance["source_binding"] == {
        "connection_id": "source-1", "snapshot_id": "snapshot-current", "snapshot_version": 3,
        "asset_id": "asset-00", "source_pointer": "postgres/public/measures_0", "field": "cost_actual_usd",
        "data_type": "number", "native_type": "decimal(38,12)", "field_id": 32, "nullable": None,
    }
    assert guidance["aggregation_policy"]["result_when_no_known_values"] == "null"
    assert guidance["value_policy"]["replace_missing_with_zero"] is False
    assert guidance["diagnostics"]["counts_execution_state"] == "not_computed"
    assert guidance["execution"]["executor"] == "customer_harness"
    assert guidance["execution"]["execution_verified"] is False
    assert len(assertions["pending"]) == 25
    assert assertions["pending"][-1]["payload"]["description"] == "Ω" * 1300
    assert len(assertions["pending"][-1]["evidence_ids"]) == 12
    for state, expected in (("approved", "owner_reviewed"), ("pending", "pending_review"), ("stale", "stale_review")):
        assert {item["trust_state"] for item in assertions[state]} == {expected}
        assert all(item["certified"] is False for item in assertions[state])
    assert payload["agent_ready"] is False
    assert _PRIVATE_MARKER not in response.text
    assert "[more detail omitted]" not in response.text


@pytest.mark.parametrize("surface", ["http", "advanced"])
def test_transmitted_ossie_model_is_complete_and_valid_against_pinned_schema(surface):
    store = ProjectedStore()
    with TestClient(_app(store), base_url="http://127.0.0.1:8765") as client:
        payload = _payload(_read(client, surface, ossie=True), surface)
    assert payload == ossie_preview(store, tenant_id="demo", connection_id="source-1")
    schema = json.loads(files("knowledge_layer").joinpath("schemas/ossie-schema-v0.2.0.dev0.json").read_text())
    Draft202012Validator(schema).validate(payload["model"])
    assert payload["status"] == "draft_valid"
    assert payload["meaning_certified"] is False
    model = payload["model"]
    assert len(model["datasets"]) == 24
    assert len(model["datasets"][0]["fields"]) == 67
    assert model["datasets"][0]["fields"][31]["expression"]["dialects"][0]["expression"] == '"cost_actual_usd"'
    assert len(model["metrics"]) == len(model["relationships"]) == 12
    assert model["metrics"][0]["expression"]["dialects"][0]["expression"].startswith("SUM(")


@pytest.mark.parametrize("surface,ossie", [("http", False), ("advanced", False), ("compact", False),
                                          ("http", True), ("advanced", True)])
def test_oversized_contract_is_an_explicit_refusal_without_partial_model_or_source_content(surface, ossie):
    with TestClient(_app(ProjectedStore(oversized=True)), base_url="http://127.0.0.1:8765") as client:
        response = _read(client, surface, ossie=ossie)
    if surface == "http":
        assert response.status_code == 413
        detail = response.json()["detail"]
        assert detail["code"] == "contract_response_too_large"
        assert detail["max_response_bytes"] == mcp.MAX_CONTRACT_RESPONSE_BYTES
    else:
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["isError"] is True
        assert "contract_response_too_large" in result["content"][0]["text"]
        assert not result.get("structuredContent")
    assert "draft_valid" not in response.text
    assert _PRIVATE_MARKER not in response.text


def test_machine_contract_budget_counts_utf8_and_allows_exact_boundary(monkeypatch):
    payload = {"definition": "Ω" * 40}
    encoded_size = len(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    monkeypatch.setattr(mcp, "MAX_CONTRACT_RESPONSE_BYTES", encoded_size)
    assert mcp._complete_contract_response(payload) is payload
    monkeypatch.setattr(mcp, "MAX_CONTRACT_RESPONSE_BYTES", encoded_size - 1)
    with pytest.raises(mcp.ContractResponseTooLarge, match="contract_response_too_large"):
        mcp._complete_contract_response(payload)


@pytest.mark.parametrize("path", ["/agent/api/sources/source-1/contract", "/agent/api/sources/source-1/ossie-preview"])
def test_machine_http_exports_keep_bearer_boundary(path):
    with TestClient(_app(ProjectedStore()), base_url="http://127.0.0.1:8765") as client:
        assert client.get(path).status_code == 401


def _metric_payload(unit):
    return {"name": "Actual cost", "description": "Sum of source actual cost values.",
            "measure_field": "cost_actual_usd", "aggregation": "sum", "unit": unit}


@pytest.mark.parametrize("unit", ["USD", "%", "kg", "μg", "  USD  ", "x" * 80])
def test_metric_accepts_common_short_units_without_normalizing_source_unit(unit):
    clean = validate_assertion(
        ProjectedStore().snapshot, kind="metric", asset_id="asset-00", payload=_metric_payload(unit),
        evidence_ids=["evidence-00"], source_kind="postgres",
    )
    assert clean["unit"] == unit


@pytest.mark.parametrize("unit", ["", " ", "\t\n", None, 0, "x" * 81])
def test_metric_refuses_blank_nonstring_and_oversized_units(unit):
    with pytest.raises(KnowledgeValidationError, match="Metric unit must be 1-80 characters"):
        validate_assertion(
            ProjectedStore().snapshot, kind="metric", asset_id="asset-00", payload=_metric_payload(unit),
            evidence_ids=["evidence-00"], source_kind="postgres",
        )


def test_short_metric_unit_does_not_relax_description_requirements():
    with pytest.raises(KnowledgeValidationError, match="metric description must be 4-2000 characters"):
        validate_assertion(
            ProjectedStore().snapshot, kind="metric", asset_id="asset-00",
            payload=_metric_payload("USD") | {"description": "abc"},
            evidence_ids=["evidence-00"], source_kind="postgres",
        )


@pytest.mark.parametrize("aggregation,empty_result", [
    ("sum", "null"), ("avg", "null"), ("min", "null"), ("max", "null"),
    ("count", "zero_count"), ("count_distinct", "zero_count"),
])
def test_metric_guidance_distinguishes_empty_measure_aggregates_from_empty_counts(aggregation, empty_result):
    store = ProjectedStore()
    metric = store.snapshot["metrics"][0]
    store.snapshot["metrics"] = [metric]
    proposal = next(item for item in store.snapshot["proposals"] if item["id"] == metric["decision_proposal_id"])
    for record in (metric, proposal["payload"]):
        record.update(name="Recorded measure", description="Aggregate the known source measure values.",
                      aggregation=aggregation, unit="records" if aggregation.startswith("count") else "USD")
    result = semantic_contract(store, tenant_id="demo", connection_id="source-1")["metrics"][0]
    guidance = result["query_guidance"]
    assert guidance["aggregation_policy"]["result_when_no_known_values"] == empty_result
    assert guidance["aggregation_policy"]["input_values"] == "non_null_measure_values"
    assert guidance["aggregation_policy"]["zero_count_proves_zero_measure_or_spend"] is False
    assert "zero_count_is_not_zero_measure_or_spend" not in guidance["aggregation_policy"]
    assert guidance["value_policy"]["null_measure"] == "missing"
    assert guidance["value_policy"]["numeric_zero_measure"] == "observed_value"
    assert guidance["execution"]["result_state"] == "not_computed"
    assert "value" not in result


def test_text_count_guidance_does_not_request_numeric_zero_count_or_invent_nullability():
    store = ProjectedStore()
    metric = store.snapshot["metrics"][0]
    store.snapshot["metrics"] = [metric]
    metric.update(name="Observed identifiers", description="Count known source identifiers.",
                  field="record_id", aggregation="count", unit="records")
    proposal = next(item for item in store.snapshot["proposals"] if item["id"] == metric["decision_proposal_id"])
    proposal["payload"].update(name=metric["name"], description=metric["description"],
                               measure_field="record_id", aggregation="count", unit="records")
    store.snapshot["assets"][0]["schema"][0].update(data_type="text", native_type="text", nullable=False)
    guidance = semantic_contract(store, tenant_id="demo", connection_id="source-1")["metrics"][0]["query_guidance"]
    assert guidance["source_binding"]["nullable"] is False
    assert guidance["source_binding"]["native_type"] == "text"
    assert guidance["diagnostics"]["zero_measure_count_applicable"] is False
    assert guidance["diagnostics"]["required_counts"] == ["selected_rows", "known_measure_rows", "missing_measure_rows"]
    assert guidance["aggregation_policy"]["result_when_no_known_values"] == "zero_count"


@pytest.mark.parametrize("invalid", [
    "missing_approval", "pending_approval", "stale_approval", "unknown_effectiveness",
    "missing_field", "ambiguous_field", "missing_asset", "missing_evidence", "changed_unit",
])
def test_contract_does_not_expose_metric_planning_for_missing_or_stale_bindings(invalid):
    store = ProjectedStore()
    metric = store.snapshot["metrics"][0]
    store.snapshot["metrics"] = [metric]
    proposal = next(item for item in store.snapshot["proposals"] if item["id"] == metric["decision_proposal_id"])
    if invalid == "missing_approval":
        metric.pop("decision_proposal_id")
    elif invalid == "pending_approval":
        proposal["status"] = "pending_review"
    elif invalid == "stale_approval":
        proposal["effective_on_current_snapshot"] = False
    elif invalid == "unknown_effectiveness":
        proposal.pop("effective_on_current_snapshot")
    elif invalid == "missing_field":
        store.snapshot["assets"][0]["schema"] = [field for field in store.snapshot["assets"][0]["schema"]
                                                  if field["name"] != metric["field"]]
    elif invalid == "ambiguous_field":
        field = next(field for field in store.snapshot["assets"][0]["schema"] if field["name"] == metric["field"])
        store.snapshot["assets"][0]["schema"].append(dict(field))
    elif invalid == "missing_asset":
        metric["source_asset_id"] = "unobserved-asset"
    elif invalid == "missing_evidence":
        proposal["evidence_ids"] = ["unobserved-evidence"]
    else:
        metric["unit"] = "EUR"
    assert semantic_contract(store, tenant_id="demo", connection_id="source-1")["metrics"] == []
