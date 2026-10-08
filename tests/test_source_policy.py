"""Operator source approvals bind underlying selectors, not connection names."""

from __future__ import annotations

import json

import pytest

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.jobs import RefreshQueue
from knowledge_layer.source_onboarding import connect_source
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import KnowledgeAccessDenied, KnowledgeStore
from knowledge_layer.worker import _store as worker_store


def _policy(*sources):
    return SourcePolicy.from_json(json.dumps({"version": 1, "sources": list(sources)}))


def _source(kind, selector, *, subjects=(), groups=("stewards",)):
    return {
        "tenant_id": "demo",
        "kind": kind,
        "selector": selector,
        "allowed_subjects": list(subjects),
        "allowed_groups": list(groups),
    }


def test_policy_rejects_missing_or_malformed_approval(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_SOURCE_POLICY", raising=False)
    monkeypatch.delenv("KNOWLEDGE_SOURCE_POLICY_FILE", raising=False)
    with pytest.raises(ValueError, match="requires KNOWLEDGE_SOURCE_POLICY"):
        SourcePolicy.from_environment()
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    with pytest.raises(ValueError, match="requires KNOWLEDGE_SOURCE_POLICY"):
        worker_store()
    with pytest.raises(ValueError, match="complete source identity"):
        _policy(_source("local_files", {"path": "."}))
    with pytest.raises(ValueError, match="Duplicate source approval"):
        source = _source("local_files", {"path": ".", "include": "*.csv"})
        _policy(source, source)
    with pytest.raises(ValueError, match="at least one"):
        _policy(_source("local_files", {"path": ".", "include": "*.csv"}, groups=()))


def test_steward_cannot_select_unapproved_source_or_retarget_existing_grant(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'policy.db'}", access_enforced=True)
    store.source_admin_group = "stewards"
    store.source_policy = _policy(
        _source("local_files", {"path": "orders", "include": "*.csv"}),
        _source("postgres", {"secret_ref": "KNOWLEDGE_SOURCE_ORDERS", "schema": "public"}),
    )
    owner = Principal("owner", frozenset({"stewards"}))
    outsider = Principal("outsider", frozenset(), frozenset({"knowledge.sources.manage"}))
    with principal_scope(owner):
        source = store.create_connection(
            "demo",
            kind="local_files",
            name="Orders",
            config={"path": "orders", "include": "*.csv"},
        )
        source_id = source["id"]
        for config in (
            {"path": "payroll", "include": "*.csv"},
            {"path": "orders", "include": "**/*"},
            {"path": "orders", "include": "*.csv", "retain_semantic_samples": True},
        ):
            with pytest.raises(KnowledgeAccessDenied):
                store.create_connection("demo", kind="local_files", name="Other", config=config)
            with pytest.raises(KnowledgeAccessDenied, match="new connection"):
                store.update_connection_config("demo", source_id, config)
        with pytest.raises(KnowledgeAccessDenied):
            store.create_connection(
                "demo",
                kind="postgres",
                name="Payroll",
                config={
                    "secret_ref": "KNOWLEDGE_SOURCE_PAYROLL",
                    "schema": "public",
                },
            )
        store.update_connection_config(
            "demo",
            source_id,
            {"path": "orders", "include": "*.csv", "max_files": 5},
        )
        assert RefreshQueue(store).enqueue("demo", source_id)["status"] == "queued"
        # An agent retry by name cannot reuse the existing connection for a forbidden selector.
        with pytest.raises(KnowledgeAccessDenied):
            connect_source(
                store,
                tenant_id="demo",
                allowed_kinds=frozenset({"local_files"}),
                kind="local_files",
                name="Orders",
                config={"path": "payroll"},
            )
    with principal_scope(outsider), pytest.raises(KnowledgeAccessDenied):
        store.create_connection(
            "demo",
            kind="postgres",
            name="Orders 2",
            config={
                "secret_ref": "KNOWLEDGE_SOURCE_ORDERS",
                "schema": "public",
            },
        )
    store.source_policy = _policy(_source("local_files", {"path": "other", "include": "*.csv"}))
    with principal_scope(owner), pytest.raises(KnowledgeAccessDenied):
        RefreshQueue(store).enqueue("demo", source_id)
    # The worker has service authority but still rejects a withdrawn source before IO.
    store.access_enforced = False
    with pytest.raises(KnowledgeAccessDenied):
        store.refresh_connection(
            "demo", source_id, curator=lambda *_: pytest.fail("source was read")
        )
    store.close()


def test_policy_scopes_catalog_namespace_and_table_selection():
    policy = _policy(
        _source("iceberg", {"catalog": "main", "namespace": "sales", "tables": ["orders"]})
    )
    owner = Principal("owner", frozenset({"stewards"}))
    assert policy.allows(
        "demo", "iceberg", {"catalog": "main", "namespace": "sales", "tables": ["orders"]}, owner
    )
    assert not policy.allows(
        "demo", "iceberg", {"catalog": "main", "namespace": "payroll", "tables": ["orders"]}, owner
    )
    assert not policy.allows("demo", "iceberg", {"catalog": "main", "namespace": "sales"}, owner)
    assert not policy.allows(
        "other", "iceberg", {"catalog": "main", "namespace": "sales", "tables": ["orders"]}, owner
    )
