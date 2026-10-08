"""Reusable synthetic enterprise fixture preserves results and owned-resource cleanup."""

from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path

import pytest

from knowledge_layer.guidance import GuidanceRegistry

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "enterprise_consumer.py"
spec = importlib.util.spec_from_file_location("enterprise_consumer_fixture_example", EXAMPLE)
assert spec and spec.loader
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)
SOURCE_REFERENCES = ("KNOWLEDGE_SOURCE_DEMO_FINANCE_FILE", "KNOWLEDGE_SOURCE_DEMO_SALES_FILE")


def test_example_preserves_cohorts_unknown_values_and_withdrawal_refusal():
    result = asyncio.run(example.run_example())
    assert result["schema_version"] == "enterprise-demo-v1"
    assert result["result"] == "passed" and result["fixture_only"] is True
    assert result["model_calls"] == 0 and result["connected_sources"] == 3
    assert result["results"]["finance"]["known_total"] == 20
    assert result["results"]["sales"]["known_total"] == 1040
    finance_rows = {row["dimension_0"]: row for row in result["results"]["finance"]["rows"]}
    assert finance_rows["east"]["metric_value"] == 20
    assert finance_rows["east"]["zero_measure_rows"] == 1
    assert finance_rows["west"]["metric_value"] is None
    assert finance_rows["west"]["missing_measure_rows"] == 1
    assert result["combined_total"] is None
    assert result["combination_reason"] == (
        "Overlapping incompatible cohorts lack a reviewed composition contract.")
    assert result["guidance_loaded"] == "ready" and result["withdrawn_receipt_refused"] is True


def test_fixture_exposes_current_published_guidance_and_exact_source_bindings():
    with example.enterprise_fixture() as fixture:
        assert set(fixture) == {"root", "store", "bindings", "document_connection_id", "guidance"}
        assert set(fixture["bindings"]) == {"finance", "sales"}
        for binding in fixture["bindings"].values():
            assert set(binding) == {"connection_id", "assertion_id", "database"}
            assert binding["database"].parent == fixture["root"] and binding["database"].is_file()
            snapshot = fixture["store"].get_snapshot("demo", binding["connection_id"])
            assert snapshot["agent_ready"] is True
        binding = fixture["guidance"]
        registry = GuidanceRegistry(fixture["store"])
        current = registry.get("demo", binding["slug"], revision=binding["revision"])
        assert current["state"] == "published" and current["use_as_instructions"] is True
        assert current["digest"] == binding["digest"]
        dependency = current["payload"]["dependencies"][0]
        snapshot = fixture["store"].get_snapshot("demo", fixture["document_connection_id"])
        assert dependency == {"connection_id": fixture["document_connection_id"],
                              "snapshot_id": snapshot["id"], "snapshot_version": snapshot["version"]}


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("consumer_fails", [False, True])
def test_fixture_removes_resources_and_restores_environment(monkeypatch, existing, consumer_fails):
    before = {}
    for index, reference in enumerate(SOURCE_REFERENCES):
        if existing:
            monkeypatch.setenv(reference, f"/previous-fixture-{index}")
        else:
            monkeypatch.delenv(reference, raising=False)
        before[reference] = os.environ.get(reference)
    closed = []
    original_close = example.KnowledgeStore.close

    def close(store):
        original_close(store)
        closed.append(store)

    monkeypatch.setattr(example.KnowledgeStore, "close", close)

    def consume():
        nonlocal root
        with example.enterprise_fixture() as fixture:
            root = fixture["root"]
            for reference in SOURCE_REFERENCES:
                assert Path(os.environ[reference]).parent == root
                assert Path(os.environ[reference]).stat().st_mode & 0o777 == 0o600
            if consumer_fails:
                raise RuntimeError("fixture consumer interrupted")

    root = None
    if consumer_fails:
        with pytest.raises(RuntimeError, match="fixture consumer interrupted"):
            consume()
    else:
        consume()
    assert root is not None and not root.exists()
    assert len(closed) == 1
    assert {reference: os.environ.get(reference) for reference in SOURCE_REFERENCES} == before


def test_partial_setup_failure_closes_store_restores_environment_and_removes_root(monkeypatch):
    for reference in SOURCE_REFERENCES:
        monkeypatch.delenv(reference, raising=False)
    roots, closed = [], []
    original_refresh = example.KnowledgeStore.refresh_connection
    original_close = example.KnowledgeStore.close

    def refresh(store, tenant, cid, *args, **kwargs):
        source = store.get_connection(tenant, cid)
        roots.append(Path(os.environ[SOURCE_REFERENCES[0]]).parent)
        if source["name"] == "Sales":
            raise RuntimeError("second fixture discovery interrupted")
        return original_refresh(store, tenant, cid, *args, **kwargs)

    def close(store):
        original_close(store)
        closed.append(store)

    monkeypatch.setattr(example.KnowledgeStore, "refresh_connection", refresh)
    monkeypatch.setattr(example.KnowledgeStore, "close", close)
    with pytest.raises(RuntimeError, match="second fixture discovery interrupted"), example.enterprise_fixture():
        pytest.fail("Incomplete fixture was yielded")
    assert roots and all(not root.exists() for root in roots) and len(closed) == 1
    assert all(reference not in os.environ for reference in SOURCE_REFERENCES)


def test_environment_and_root_cleanup_still_run_when_store_close_reports_failure(monkeypatch):
    for reference in SOURCE_REFERENCES:
        monkeypatch.setenv(reference, "/previous-fixture-reference")
    original_close = example.KnowledgeStore.close

    def close(store):
        original_close(store)
        raise RuntimeError("fixture close interrupted")

    monkeypatch.setattr(example.KnowledgeStore, "close", close)
    with pytest.raises(RuntimeError, match="fixture close interrupted"), example.enterprise_fixture() as fixture:
        root = fixture["root"]
    assert not root.exists()
    assert all(os.environ[reference] == "/previous-fixture-reference" for reference in SOURCE_REFERENCES)
