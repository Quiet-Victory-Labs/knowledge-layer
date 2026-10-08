"""Decision routing is bounded and cannot promote unreviewed meaning."""

from __future__ import annotations

import json

import httpx
import pytest

from knowledge_layer.decision_evaluation import evaluate_decisions
from knowledge_layer.decisions import Decision, RuleDecisionProvider, SystemOneDecisionProvider
from knowledge_layer.model_curator import run_once


def _packet() -> dict:
    return {
        "work_id": "work-1", "connection_id": "source-1", "snapshot_id": "snapshot-1",
        "target_concept_id": "concept-1", "kind": "define_concept",
        "candidate": {"name": "Return Window", "semantic_type": "document_business_term"},
        "evidence_ids": ["evidence-1"],
        "evidence": [{"id": "evidence-1", "type": "source_document",
                      "summary": "Returns are allowed for fourteen days.",
                      "source_pointer": "private/source/document.md"}],
    }


def test_rules_escalate_only_complete_zero_overlap():
    packet = _packet()
    packet["kind"] = "review_relationship"
    packet["candidate"]["join_probe"] = {
        "complete_source_and_target": True,
        "source_nonnull_rows_probed": 10, "matched_source_rows": 0,
    }
    assert RuleDecisionProvider().assess(packet).route == "needs_steward"
    packet["candidate"]["join_probe"]["complete_source_and_target"] = False
    assert RuleDecisionProvider().assess(packet).route == "curate"
    packet["evidence_ids"] = []
    assert RuleDecisionProvider().assess(packet).route == "defer"


def test_system_one_adapter_sends_bounded_evidence_and_parses_choice():
    def provider(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret"
        body = json.loads(request.content)
        assert body["model"] == "jev-test"
        assert body["questions"]["triage"]["type"] == "choice"
        assert set(body["questions"]["triage"]["criteria"]) == {"curate", "needs_steward", "defer"}
        assert body["state"]["evidence"][0]["summary"] == "Returns are allowed for fourteen days."
        assert "source_pointer" not in body["state"]["evidence"][0]
        return httpx.Response(200, json={
            "model": "jev-1", "answers": {"triage": {
                "type": "choice", "choice": "defer",
                "probabilities": {"curate": 0.01, "needs_steward": 0.01, "defer": 0.98},
            }}, "usage": {"input_tokens": 123},
        })

    decision = SystemOneDecisionProvider(
        url="https://decision.example/v1/systemone", model="jev-test", token="secret",
        transport=httpx.MockTransport(provider), allow_remote=True,
    ).assess(_packet())
    assert decision.route == "defer"
    assert decision.probability == 0.98
    assert decision.input_tokens == 123


@pytest.mark.parametrize("url", [
    "http://remote.example/v1/systemone",
    "https://secret@decision.example/v1/systemone",
    "https://decision.example/v1/systemone?token=secret",
])
def test_system_one_adapter_rejects_insecure_or_embedded_credentials(url):
    with pytest.raises(ValueError):
        SystemOneDecisionProvider(url=url, model="model", token="secret", allow_remote=True)


def test_system_one_adapter_requires_explicit_remote_egress():
    with pytest.raises(ValueError, match="egress permission"):
        SystemOneDecisionProvider(url="https://decision.example/v1/systemone", model="jev", token="secret")


def test_system_one_adapter_rejects_invalid_probability_distribution():
    def provider(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answers": {"triage": {
            "choice": "defer", "probabilities": {
                "curate": 0.9, "needs_steward": 0.0, "defer": 0.1,
            },
        }}})

    with pytest.raises(ValueError, match="inconsistent"):
        SystemOneDecisionProvider(
            url="http://localhost:9000/v1/systemone", model="laya",
            transport=httpx.MockTransport(provider),
        ).assess(_packet())


class _FixedProvider:
    def assess(self, _packet: dict) -> Decision:
        return Decision("defer", "system_one", model="local-test", probability=0.99)


class _UncertainProvider:
    def assess(self, _packet: dict) -> Decision:
        return Decision("defer", "system_one", model="local-test", probability=0.60)


@pytest.mark.parametrize("mode,expected_status,expected_submissions", [
    ("shadow", "pending_review", 1),
    ("gate", "defer", 0),
])
def test_runner_shadows_before_gating(mode, expected_status, expected_submissions):
    submissions = []
    model_calls = []

    def knowledge(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent/api/curation-work":
            return httpx.Response(200, json={"work": [_packet()], "more_available": False})
        submissions.append(json.loads(request.content))
        return httpx.Response(202, json={"proposal": {"id": "proposal-1"}})

    def model(request: httpx.Request) -> httpx.Response:
        model_calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "action": "propose", "proposed_definition": "Returns are allowed for fourteen days.",
            "rationale": "The cited document states the duration of the return period.",
            "evidence_ids": ["evidence-1"],
        })}}]})

    report = run_once(
        knowledge_url="http://localhost:8001", knowledge_token="knowledge-secret",
        model_url="http://localhost:9000/v1/chat/completions", model_name="test-model",
        model_token="model-secret", knowledge_transport=httpx.MockTransport(knowledge),
        model_transport=httpx.MockTransport(model), decision_provider=_FixedProvider(),
        decision_mode=mode, decision_calibration_id="held-out-v1" if mode == "gate" else None,
    )
    outcome = report["outcomes"][0]
    assert outcome["status"] == expected_status
    assert len(submissions) == len(model_calls) == expected_submissions
    assert outcome["decision"]["applied"] == (mode == "gate")
    assert all(item.get("approval_required", True) for item in submissions)


def test_gate_requires_calibration_reference():
    with pytest.raises(ValueError, match="calibration"):
        run_once(
            knowledge_url="http://localhost:8001", knowledge_token="knowledge-secret",
            model_url="http://localhost:9000/v1/chat/completions", model_name="test-model",
            model_token="model-secret", decision_provider=_FixedProvider(), decision_mode="gate",
        )


def test_uncertain_decision_cannot_skip_generation():
    model_calls = []

    def knowledge(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent/api/curation-work":
            return httpx.Response(200, json={"work": [_packet()], "more_available": False})
        return httpx.Response(202, json={"proposal": {"id": "proposal-1"}})

    def model(request: httpx.Request) -> httpx.Response:
        model_calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"action":"abstain"}'}}]})

    report = run_once(
        knowledge_url="http://localhost:8001", knowledge_token="knowledge-secret",
        model_url="http://localhost:9000/v1/chat/completions", model_name="test-model",
        model_token="model-secret", knowledge_transport=httpx.MockTransport(knowledge),
        model_transport=httpx.MockTransport(model), decision_provider=_UncertainProvider(),
        decision_mode="gate", decision_calibration_id="held-out-v1",
    )
    assert report["outcomes"][0]["status"] == "abstained"
    assert report["outcomes"][0]["decision"]["applied"] is False
    assert len(model_calls) == 1


def test_evaluation_exposes_false_gated_skips_without_source_text():
    metrics = evaluate_decisions([
        {"packet": _packet(), "expected_route": "curate"},
        {"packet": {**_packet(), "work_id": "work-2"}, "expected_route": "defer"},
    ], _FixedProvider())
    assert metrics["gate_candidates"] == 2
    assert metrics["false_gate_skips"] == 1
    assert metrics["confusion"]["curate"]["defer"] == 1
    assert "Returns are allowed" not in json.dumps(metrics)
