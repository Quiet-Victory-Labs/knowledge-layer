"""A configured model bridge can propose, abstain, and reject invented evidence."""

from __future__ import annotations

import json

import httpx
import pytest

from knowledge_layer.model_curator import run_once


def _packet() -> dict:
    return {
        "work_id": "work-1", "connection_id": "source-1", "snapshot_id": "snapshot-1",
        "target_concept_id": "concept-1", "evidence_ids": ["evidence-1"],
        "candidate": {"name": "Return Window", "candidate_definitions": [{"text": "Returns within 14 days."}]},
    }


def test_model_bridge_submits_only_cited_pending_proposals():
    calls = []

    def knowledge(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer knowledge-secret"
        if request.url.path == "/agent/api/curation-work":
            return httpx.Response(200, json={"work": [_packet()], "more_available": False})
        calls.append(json.loads(request.content))
        return httpx.Response(202, json={"proposal": {"id": "proposal-1"}, "approval_required": True})

    def model(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer model-secret"
        body = json.loads(request.content)
        assert body["max_tokens"] == 350
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "action": "propose", "proposed_definition": "A return window is 14 days from purchase.",
            "rationale": "The cited source states a fourteen day return period.",
            "evidence_ids": ["evidence-1"],
        })}}]})

    report = run_once(
        knowledge_url="http://localhost:8001", knowledge_token="knowledge-secret",
        model_url="http://localhost:9000/v1/chat/completions", model_name="test-model",
        model_token="model-secret", knowledge_transport=httpx.MockTransport(knowledge),
        model_transport=httpx.MockTransport(model),
    )
    assert report["pending_review"] == 1
    assert calls[0]["expected_snapshot_id"] == "snapshot-1"
    assert calls[0]["evidence_ids"] == ["evidence-1"]


def test_model_bridge_rejects_unsupported_evidence_without_submission():
    submitted = []

    def knowledge(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent/api/curation-work":
            return httpx.Response(200, json={"work": [_packet()], "more_available": False})
        submitted.append(request)
        return httpx.Response(202, json={"proposal": {"id": "bad"}})

    def model(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "action": "propose", "proposed_definition": "Invented", "rationale": "This is a plausible sounding claim.",
            "evidence_ids": ["invented-evidence"],
        })}}]})

    report = run_once(
        knowledge_url="http://localhost:8001", knowledge_token="knowledge-secret",
        model_url="http://localhost:9000/v1/chat/completions", model_name="test-model",
        model_token="model-secret", knowledge_transport=httpx.MockTransport(knowledge),
        model_transport=httpx.MockTransport(model),
    )
    assert report["pending_review"] == 0
    assert report["outcomes"][0]["status"] == "model_or_proposal_error"
    assert not submitted


def test_model_bridge_requires_secure_remote_endpoints():
    with pytest.raises(ValueError, match="HTTPS"):
        run_once(
            knowledge_url="http://localhost:8001", knowledge_token="knowledge-secret",
            model_url="http://public.example/v1/chat/completions", model_name="test-model",
            model_token="model-secret",
        )


def test_model_bridge_requires_explicit_remote_model_egress():
    with pytest.raises(ValueError, match="egress permission"):
        run_once(
            knowledge_url="http://localhost:8001", knowledge_token="knowledge-secret",
            model_url="https://model.example/v1/chat/completions", model_name="test-model",
            model_token="model-secret",
        )


def test_model_bridge_rejects_invented_relationship_mapping():
    submitted = []
    packet = {
        **_packet(), "kind": "review_relationship",
        "candidate": {"proposed_mapping_shape": {
            "relationship_id": "rel-1", "subject_id": "concept-1",
            "predicate": "belongs_to", "object_id": "concept-2",
        }},
    }

    def knowledge(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent/api/curation-work":
            return httpx.Response(200, json={"work": [packet], "more_available": False})
        submitted.append(request)
        return httpx.Response(202, json={"proposal": {"id": "bad"}})

    def model(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "action": "propose", "proposed_mapping": {
                **packet["candidate"]["proposed_mapping_shape"], "object_id": "invented",
            },
            "rationale": "The cited source supports the stated relationship.",
            "evidence_ids": ["evidence-1"],
        })}}]})

    report = run_once(
        knowledge_url="http://localhost:8001", knowledge_token="knowledge-secret",
        model_url="http://localhost:9000/v1/chat/completions", model_name="test-model",
        model_token="model-secret", knowledge_transport=httpx.MockTransport(knowledge),
        model_transport=httpx.MockTransport(model),
    )
    assert report["pending_review"] == 0
    assert not submitted
