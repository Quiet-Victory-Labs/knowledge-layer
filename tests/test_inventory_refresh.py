"""Real refresh orchestration over a bounded synthetic HTTP transport and local registry."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.api import create_knowledge_router
from knowledge_layer.connectors import external_adapter, external_inventory
from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.inventory import MAX_PAGE_BYTES, InventoryStore, _encoded, inventory_digest
from knowledge_layer.inventory_transport import inventory_operation
from knowledge_layer.jobs import RefreshQueue
from knowledge_layer.refresh_fence import refresh_fence
from knowledge_layer.schedules import RefreshSchedules
from knowledge_layer.source_onboarding import refresh_source
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import (
    CurationFailed,
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeStore,
    KnowledgeValidationError,
    validate_connection_config,
)

REFERENCE = "KNOWLEDGE_SOURCE_PAGING_FIXTURE"
CONFIG = {"secret_ref": REFERENCE, "scope": "finance", "discovery_mode": "inventory",
          "retain_inventory_payloads": True}
PROFILE = {"endpoint": "https://adapter.invalid/discover", "provider": "fixture",
           "allowed_scopes": ["finance"], "protocol_version": "1.3", "revision": "fixture-v1",
           "source_identity": "synthetic.finance"}
WATERMARK = {"kind": "unknown", "value": None}
ENTITY = {"id": "term.1", "kind": "semantic_definition", "evidence_ids": ["proof.1"],
          "payload": {"definition": "Exact fixture text: café 🧭.\n\nSecond paragraph.",
                      "unknown": None, "zero": 0, "empty": "", "false": False}}
EVIDENCE = {"id": "proof.1", "kind": "upstream_definition", "payload": {"revision": None}}
OWNER = Principal("owner", frozenset(), frozenset({"knowledge.sources.manage"}))
OMIT = {"inventory_limits", "max_entities", "max_evidence", "max_response_bytes"}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def response(controls):
    first = controls["page_index"] == 0
    return {key: deepcopy(value) for key, value in controls.items() if key not in OMIT} | {
        "next_cursor": "https://cursor.invalid/opaque" if first else None,
        "listing_complete": not first, "watermark": deepcopy(WATERMARK),
        "entities": [deepcopy(ENTITY)] if first else [],
        "evidence": [] if first else [deepcopy(EVIDENCE)],
    }


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv(REFERENCE, json.dumps(PROFILE))
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'native-pages.db'}")
    source = store.create_connection("demo", kind="external_adapter", name="Fixture inventory", config=CONFIG)
    calls, behavior = [], {"respond": response}

    def transport(request):
        controls = json.loads(request.content)
        calls.append(controls)
        assert str(request.url) == PROFILE["endpoint"] and request.method == "POST"
        assert request.headers["accept-encoding"] == "identity"
        supplied = behavior["respond"](controls)
        return supplied if isinstance(supplied, httpx.Response) else httpx.Response(200, stream=httpx.ByteStream(encoded(supplied)))

    client = httpx.Client
    monkeypatch.setattr(external_inventory.httpx, "Client", lambda **options: client(
        transport=httpx.MockTransport(transport), **options))
    # These tests exercise controller fences against an in-process MockTransport.
    # Real subprocess/credential boundaries have their own integration suite.
    from knowledge_layer.connectors.external_adapter import _profile

    monkeypatch.setattr("knowledge_layer.connector_isolation.isolated_inventory_page",
        lambda config, request, *, timeout_seconds: external_inventory.fetch_inventory_page(
            _profile(config["secret_ref"], config["scope"]), request))
    yield store, source["id"], calls, behavior
    store.close()


def rows(store, table):
    with store.engine.connect() as conn:
        return [dict(row) for row in conn.execute(select(table)).mappings()]


def counts(store, table):
    with store.engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def refresh(registry):
    store, source, _calls, _behavior = registry
    return store.refresh_connection("demo", source)


def approve(store, source):
    store.grant_source_access("demo", source, principal_type="user", principal_id="owner", role="manage")
    store.source_policy = SourcePolicy.from_json(json.dumps({"version": 1, "sources": [{
        "tenant_id": "demo", "kind": "external_adapter", "selector": CONFIG,
        "allowed_subjects": ["owner"], "allowed_groups": [],
    }]}))
    store.access_enforced = True


def test_actual_pages_commit_exact_text_unknowns_and_scoped_clocks(registry):
    store, source, calls, _behavior = registry
    result = refresh(registry)
    manifest, connection = result["inventory"], result["connection"]
    assert result["snapshot"] is None and connection["snapshot"] is None
    assert manifest["status"] == "committed" and manifest["page_count"] == 2
    assert manifest["entity_count"] == manifest["evidence_count"] == 1
    assert manifest["watermark"] == WATERMARK
    assert calls[0]["cursor"] is None and calls[1]["cursor"] == "https://cursor.invalid/opaque"
    assert calls[1]["expected_watermark_digest"] == inventory_digest(WATERMARK)
    assert connection["last_refresh_attempt_at"] and connection["status"] == "inventory_complete"
    assert connection["last_refreshed_at"] is None and connection["last_complete_refresh_at"] is None
    assert connection["counts"] == dict.fromkeys(("assets", "concepts", "relationships", "proposals"))
    assert connection["discovery_complete"] is None and connection["stage"] == "inventory_complete"
    assert not connection["agent_ready"] and counts(store, store.snapshots) == 0
    inventory = InventoryStore(store)
    current = inventory.current("demo", source, scope_digest=manifest["scope_digest"], policy_digest=manifest["policy_digest"])
    assert current["last_complete_scope_at"] == current["completed_at"]
    assert inventory.page("demo", source, manifest["id"])["items"] == [ENTITY | {"digest": inventory_digest(ENTITY)}]
    assert inventory.page("demo", source, manifest["id"], collection="evidence")["items"] == [EVIDENCE | {"digest": inventory_digest(EVIDENCE)}]


def test_empty_scope_is_measured_zero_without_empty_legacy_semantics(registry):
    store, _source, calls, behavior = registry
    behavior["respond"] = lambda controls: response(controls) | {
        "next_cursor": None, "listing_complete": True, "entities": [], "evidence": []}
    result = refresh(registry)
    assert len(calls) == 1 and result["inventory"]["page_count"] == 1
    assert result["inventory"]["entity_count"] == result["inventory"]["evidence_count"] == 0
    assert result["connection"]["asset_count"] is None and counts(store, store.snapshots) == 0


@pytest.mark.parametrize("fault", ["unavailable", "expired", "pin", "watermark", "cursor", "duplicate", "missing_ref", "oversize", "redirect"])
def test_later_failure_preserves_previous_head_and_retires_only_failed_local_scan(registry, fault):
    store, source, calls, behavior = registry
    previous = refresh(registry)["inventory"]
    heads = rows(store, store.inventory_heads)
    calls.clear()

    def fail(controls):
        page = response(controls)
        if controls["page_index"] == 0:
            return page
        if fault in {"unavailable", "expired", "redirect"}:
            return httpx.Response({"unavailable": 503, "expired": 410, "redirect": 307}[fault],
                                  headers={"location": "https://other.invalid/discover"})
        if fault == "pin":
            page["profile_digest"] = "0" * 64
        if fault == "watermark":
            page["watermark"] = {"kind": "native", "value": "changed"}
        if fault == "cursor":
            page.update(next_cursor=controls["cursor"], listing_complete=False)
        if fault == "duplicate":
            page["entities"] = [deepcopy(ENTITY)]
        if fault == "missing_ref":
            page["evidence"] = []
        if fault == "oversize":
            return httpx.Response(200, stream=httpx.ByteStream(b" " * (MAX_PAGE_BYTES + 1)))
        return page

    behavior["respond"] = fail
    with pytest.raises((CurationFailed, KnowledgeConflict)):
        refresh(registry)
    assert len(calls) == 2 and rows(store, store.inventory_heads) == heads
    history = rows(store, store.inventory_runs)
    assert [(row["status"], row["id"] == previous["id"]) for row in history] == [("committed", True), ("aborted", False)]
    assert store.get_connection("demo", source)["last_complete_refresh_at"] is None
    assert store.get_connection("demo", source)["status"] == "error"
    assert counts(store, store.snapshots) == 0


@pytest.mark.parametrize("withdraw", ["policy", "grant", "profile", "limits"])
def test_change_during_second_io_refuses_commit_and_retires_unclaimed_scan(registry, monkeypatch, withdraw):
    store, source, calls, behavior = registry
    approve(store, source)

    def change(controls):
        if controls["page_index"] == 1:
            if withdraw == "policy":
                store.source_policy = SourcePolicy.from_json('{"version":1,"sources":[]}')
            elif withdraw == "grant":
                with store.engine.begin() as conn:
                    conn.execute(delete(store.source_grants).where(store.source_grants.c.connection_id == source))
            elif withdraw == "profile":
                monkeypatch.setenv(REFERENCE, json.dumps(PROFILE | {"revision": "changed"}))
            else:
                with store.engine.begin() as conn:
                    normalized = validate_connection_config("external_adapter", CONFIG)
                    normalized["inventory_limits"]["max_pages"] = 1
                    conn.execute(update(store.connections).where(store.connections.c.id == source).values(config=normalized))
        return response(controls)

    behavior["respond"] = change
    with principal_scope(OWNER), pytest.raises((KnowledgeAccessDenied, KnowledgeConflict, CurationFailed)):
        refresh(registry)
    assert len(calls) == 2 and counts(store, store.inventory_heads) == 0
    assert len(rows(store, store.inventory_runs)) == 1
    assert rows(store, store.inventory_runs)[0]["status"] == "aborted"
    assert rows(store, store.inventory_pages)[0]["page_index"] == 0


def test_audit_failure_rolls_back_head_status_and_result(registry, monkeypatch):
    store, source, _calls, _behavior = registry
    queue = RefreshQueue(store)
    queue.enqueue("demo", source)
    job = queue.claim("worker")
    original = store._audit

    def refuse(conn, tenant, connection, action, *args):
        if action == "inventory.commit":
            raise RuntimeError("fixture transaction rejection")
        return original(conn, tenant, connection, action, *args)

    monkeypatch.setattr(store, "_audit", refuse)
    with refresh_fence(job_id=job["id"], worker_id="worker", attempt=job["attempt"]), pytest.raises(CurationFailed):
        refresh(registry)
    assert counts(store, store.inventory_heads) == 0
    assert rows(store, store.refresh_jobs)[0]["result_inventory_id"] is None
    assert rows(store, store.inventory_runs)[0]["status"] == "aborted"


def test_native_job_attribution_is_durable_and_typed(registry):
    store, source, _calls, _behavior = registry
    queue = RefreshQueue(store)
    job = queue.enqueue("demo", source)
    claim = queue.claim("worker")
    with refresh_fence(job_id=job["id"], worker_id="worker", attempt=claim["attempt"]):
        result = refresh(registry)
    queue.finish(job["id"], "worker", attempt=claim["attempt"], result_inventory_id=result["inventory"]["id"])
    persisted = rows(store, store.refresh_jobs)[0]
    assert persisted["status"] == "succeeded" and persisted["result_inventory_id"] == result["inventory"]["id"]
    assert persisted["result_snapshot_id"] is None and counts(store, store.snapshots) == 0


def test_http_and_onboarding_return_manifest_without_fabricated_projection(registry):
    store, source, _calls, _behavior = registry
    app = FastAPI()
    app.include_router(create_knowledge_router(store=store, allowed_kinds=frozenset({"external_adapter"})))
    with TestClient(app) as client:
        result = client.post(f"/api/knowledge/connections/{source}/refresh")
    assert result.status_code == 200 and result.json()["inventory"]["status"] == "committed"
    assert result.json()["snapshot"] is None and result.json()["connection"]["asset_count"] is None
    report = refresh_source(store, tenant_id="demo", allowed_kinds=frozenset({"external_adapter"}), connection_id=source)
    assert report["inventory_counts"] == {"pages": 2, "entities": 1, "evidence": 1}
    assert report["counts"] == dict.fromkeys(("assets", "concepts", "relationships", "proposals", "evidence"))
    assert report["snapshot_id"] is report["discovery_complete"] is None and not report["agent_ready"]


@pytest.mark.parametrize("mode", [False, [], {}, 1, None, "other"])
def test_invalid_modes_are_validation_errors(mode):
    with pytest.raises(KnowledgeValidationError):
        validate_connection_config("external_adapter", CONFIG | {"discovery_mode": mode})


def test_profile_modes_cannot_fall_back_to_legacy_or_model(registry, monkeypatch):
    _store, _source, calls, _behavior = registry
    with pytest.raises(ConnectorError, match="inventory"):
        external_adapter.discover(validate_connection_config("external_adapter", {"secret_ref": REFERENCE, "scope": "finance"}))
    assert calls == []
    monkeypatch.setenv(REFERENCE, json.dumps(PROFILE | {"protocol_version": "1.2"}))
    with pytest.raises(CurationFailed):
        refresh(registry)
    assert calls == []


def live_request():
    start = datetime.now(UTC)
    return external_inventory.make_request(provider="fixture", scope="finance", profile_digest="0" * 64,
        scan_id="fixture", page_index=0, cursor=None, scan_started_at=start.isoformat().replace("+00:00", "Z"),
        scan_expires_at=(start + timedelta(seconds=300)).isoformat().replace("+00:00", "Z"),
        inventory_limits=validate_connection_config("external_adapter", CONFIG)["inventory_limits"])


def test_compression_refused_before_reading_body(monkeypatch):
    controls = live_request()

    class Unread(httpx.SyncByteStream):
        def __iter__(self):
            pytest.fail("Compressed source bytes must not be read/decompressed")

    client = httpx.Client
    monkeypatch.setattr(external_inventory.httpx, "Client", lambda **options: client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Unread())), **options))
    with pytest.raises(ConnectorError) as error:
        external_inventory.fetch_inventory_page(PROFILE, controls)
    assert error.value.code == "adapter_inventory_encoding_refused"


def test_dribbling_stream_rechecks_live_deadline_per_chunk(monkeypatch):
    controls = live_request()
    observed = {"now": datetime.fromisoformat(controls["scan_started_at"])}

    class Clock(datetime):
        @classmethod
        def now(cls, _tz=None):
            return cls.fromtimestamp(observed["now"].timestamp(), UTC)

    class Dribble(httpx.SyncByteStream):
        def __iter__(self):
            yield b"{"
            observed["now"] = datetime.fromisoformat(controls["scan_expires_at"])
            yield b"}"
            pytest.fail("Expired stream must stop consuming chunks")

    client = httpx.Client
    monkeypatch.setattr(external_inventory, "datetime", Clock)
    monkeypatch.setattr(external_inventory.httpx, "Client", lambda **options: client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, stream=Dribble())), **options))
    with pytest.raises(ConnectorError) as error:
        external_inventory.fetch_inventory_page(PROFILE, controls)
    assert error.value.code == "adapter_inventory_expired"


def test_canonical_budget_includes_all_three_append_digests():
    controls = live_request()
    supplied = response(controls) | {"entities": [
        {"id": f"entity.{index}", "kind": "asset", "payload": {"numbers": [1e-9] * 8600}, "evidence_ids": []}
        for index in range(5)], "evidence": []}
    packet = {key: supplied[key] for key in ("page_index", "cursor", "next_cursor", "entities", "evidence")}
    padding = MAX_PAGE_BYTES - 100 - len(_encoded(packet))
    assert 0 < padding < 10000
    supplied["entities"][0]["payload"]["padding"] = "x" * padding
    packet["entities"] = supplied["entities"]
    assert len(_encoded(packet)) < MAX_PAGE_BYTES
    assert len(_encoded(packet | {key: "0" * 64 for key in ("scope_digest", "policy_digest", "watermark_digest")})) > MAX_PAGE_BYTES
    raw = encoded(supplied).replace(b"1e-09", b"1e-9")
    assert len(raw) < MAX_PAGE_BYTES
    with pytest.raises(ConnectorError) as error:
        external_inventory.validate_inventory_page(raw, controls)
    assert error.value.code == "adapter_response_too_large"


def test_budget_tightening_at_commit_is_checked_under_actual_transaction(registry, monkeypatch):
    store, source, _calls, _behavior = registry
    original = InventoryStore.commit

    def tighten(inventory, tenant, connection, identifier, **kwargs):
        with store.engine.begin() as conn:
            config = validate_connection_config("external_adapter", CONFIG)
            config["inventory_limits"]["max_entities"] = 0
            conn.execute(update(store.connections).where(store.connections.c.id == source).values(config=config))
        return original(inventory, tenant, connection, identifier, **kwargs)

    monkeypatch.setattr(InventoryStore, "commit", tighten)
    with pytest.raises(KnowledgeConflict, match="budgets"):
        refresh(registry)
    assert counts(store, store.inventory_heads) == 0 and rows(store, store.inventory_runs)[0]["status"] == "aborted"


def test_worker_policy_reload_failure_stops_second_request(registry):
    store, source, calls, behavior = registry
    approve(store, source)
    current = {"policy": store.source_policy}
    store.source_policy_loader = lambda: current["policy"]

    def withdraw(controls):
        if controls["page_index"] == 0:
            # First fetch still returns a valid page; post-IO policy check refuses it.
            current["policy"] = None
        return response(controls)

    behavior["respond"] = withdraw
    with principal_scope(OWNER), pytest.raises(KnowledgeAccessDenied, match="unavailable"):
        refresh(registry)
    assert len(calls) == 1 and counts(store, store.inventory_runs) == 0


def test_inventory_schedule_with_group_only_approval_blocks_without_dispatch_crash(registry):
    store, source, _calls, _behavior = registry
    approve(store, source)
    schedules = RefreshSchedules(store)
    start = datetime(2026, 10, 3, 12, tzinfo=UTC)
    with principal_scope(OWNER):
        schedules.set("demo", source, {"cadence": "hourly", "timezone": "UTC", "minute": 0}, now=start)
    store.access_enforced = False
    store.source_policy = SourcePolicy.from_json(json.dumps({"version": 1, "sources": [{
        "tenant_id": "demo", "kind": "external_adapter", "selector": CONFIG,
        "allowed_subjects": [], "allowed_groups": ["team"],
    }]}))
    result = schedules.dispatch_due({"external_adapter"}, now=start + timedelta(hours=1))
    assert result[0]["status"] == "blocked" and result[0]["error_code"] == "inventory_owner_not_approved"
    assert rows(store, store.refresh_schedules)[0]["paused"] and counts(store, store.refresh_jobs) == 0


def test_transport_cannot_enable_source_refresh_attribution(registry):
    store, source, _calls, _behavior = registry
    with pytest.raises(KnowledgeValidationError):
        inventory_operation(store, source, "commit", {
            "inventory_id": "invented", "completion": {}, "source_refresh": True,
        }, allow_writes=True)
    assert counts(store, store.inventory_heads) == 0


@pytest.mark.parametrize("budget", ["max_entities", "max_total_bytes"])
def test_single_page_cannot_exceed_whole_scan_budget(registry, budget):
    store, source, calls, _behavior = registry
    config = validate_connection_config("external_adapter", CONFIG)
    config["inventory_limits"][budget] = 0 if budget == "max_entities" else 1
    store.update_connection_config("demo", source, config)
    with pytest.raises(CurationFailed):
        refresh(registry)
    assert len(calls) == 1 and counts(store, store.inventory_runs) == 0
