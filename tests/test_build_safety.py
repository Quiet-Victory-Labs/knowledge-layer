"""Adversarial durable-build checks across restarts, grants and source changes."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.agent_api import build_agent_api
from knowledge_layer.builds import BuildRuns
from knowledge_layer.review_batches import accept_review_batch, preview_review_batch
from knowledge_layer.store import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeStore,
    KnowledgeValidationError,
)


@pytest.fixture
def estate(tmp_path):
    root = tmp_path / "sources"
    root.mkdir()
    (root / "orders.csv").write_text("order_id,total\nO-1,12\nO-2,15\n", encoding="utf-8")
    url = f"sqlite:///{tmp_path / 'knowledge.db'}"
    store = KnowledgeStore(database_url=url, local_files_root=root)
    source = store.create_connection("demo", name="Orders", kind="local_files", config={"path": "."})
    snapshot = store.refresh_connection("demo", source["id"])["snapshot"]
    try:
        yield store, source["id"], snapshot, url, root
    finally:
        store.close()


def _dataset_result(task):
    assert task["packet"]["track"] == "contract"
    assert task["packet"]["kind"] == "dataset_description"
    return {"action": "propose", "reason": "The leased evidence describes order records in this exact source.",
            "evidence_ids": task["packet"]["evidence_ids"],
            "payload": {"description": "Order records observed in this source."}}


def _checkpoint(runs, run_id, task, result):
    return runs.complete("demo", run_id, task_id=task["id"], lease_token=task["lease_token"], result=result)


def _supported_result(task):
    packet = task["packet"]
    result = {"action": "propose", "reason": "The exact source metadata supports this bounded fixture definition.",
              "evidence_ids": packet["evidence_ids"]}
    if packet["track"] == "meaning":
        if packet["kind"] == "review_relationship":
            result["proposed_mapping"] = packet.get("proposed_mapping") or packet["candidate"]["proposed_mapping_shape"]
        else:
            result["proposed_definition"] = "An observed term whose meaning is limited to this source dataset."
    elif packet["kind"] == "dataset_description":
        result["payload"] = {"description": "Order records observed in this source."}
    elif packet["kind"] == "field_description":
        result["payload"] = {"field": packet["candidate"]["field"], "description": "An observed field within the source order records."}
    elif packet["kind"] == "grain":
        result["payload"] = {"description": "One observed source row per order identifier.",
                             "key_fields": packet["candidate"]["candidate_key_fields"][:1]}
    elif packet["kind"] == "metric":
        result["payload"] = {"name": "Observed total", "description": "Sum of observed source total values.",
                             "measure_field": packet["candidate"]["field"], "aggregation": "sum", "unit": "source units"}
    else:
        raise AssertionError(f"Unexpected fixture work kind: {packet['kind']}")
    return result


def _proposal_count(store):
    with store.engine.connect() as conn:
        return sum(conn.execute(select(func.count()).select_from(table)).scalar_one()
                   for table in (store.proposals, store.contract_proposals))


def _grant(store, cid, subject, role="read"):
    with store.engine.begin() as conn:
        conn.execute(store.source_grants.insert().values(
            tenant_id="demo", connection_id=cid, principal_type="user",
            principal_id=subject, role=role, granted_at="2026-10-04T00:00:00Z", granted_by="test-operator"))


def test_checkpoint_and_lease_survive_reopening_database(estate):
    store, cid, snapshot, url, root = estate
    run = BuildRuns(store).start("demo", connection_ids=[cid])
    task = BuildRuns(store).claim("demo", run["id"])["task"]
    result = _dataset_result(task)
    store.close()
    reopened = KnowledgeStore(database_url=url, local_files_root=root)
    try:
        runs = BuildRuns(reopened)
        completed = _checkpoint(runs, run["id"], task, result)
        assert completed["status"] == "proposed" and not completed["replayed"]
        reopened.close()
        reopened = KnowledgeStore(database_url=url, local_files_root=root)
        replay = _checkpoint(BuildRuns(reopened), run["id"], task, result)
        assert replay["replayed"] and replay["proposal_id"] == completed["proposal_id"]
        assert _proposal_count(reopened) == 1
        assert reopened.get_snapshot("demo", cid)["version"] == snapshot["version"]
        assert reopened.get_snapshot("demo", cid)["agent_ready"] is False
    finally:
        reopened.close()


def test_independent_agents_do_not_receive_same_live_lease(estate):
    store, cid, _, url, root = estate
    run = BuildRuns(store).start("demo", connection_ids=[cid])
    ready = Barrier(2)

    def claim():
        worker_store = KnowledgeStore(database_url=url, local_files_root=root)
        try:
            ready.wait(timeout=10)
            runs = BuildRuns(worker_store)
            try:
                return runs.claim("demo", run["id"])["task"]
            except KnowledgeConflict:
                # SQLite uses a conditional update instead of SKIP LOCKED.
                # The losing claimant may retry, but must never share a lease.
                return runs.claim("demo", run["id"])["task"]
        finally:
            worker_store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        tasks = [future.result(timeout=20) for future in futures]
    assert all(tasks)
    assert len({task["id"] for task in tasks}) == 2
    assert len({task["lease_token"] for task in tasks}) == 2
    assert BuildRuns(store).get("demo", run["id"])["counts"]["leased"] == 2


def test_same_snapshot_evidence_change_refuses_old_checkpoint(estate):
    store, cid, snapshot, _, _ = estate
    runs = BuildRuns(store)
    run = runs.start("demo", connection_ids=[cid])
    task = runs.claim("demo", run["id"])["task"]
    before = _proposal_count(store)
    with store.engine.begin() as conn:
        row = conn.execute(select(store.snapshots).where(
            store.snapshots.c.connection_id == cid,
            store.snapshots.c.version == snapshot["version"])).mappings().one()
        payload = deepcopy(row["payload"])
        cited = set(task["packet"]["evidence_ids"])
        for evidence in payload["evidence"]:
            if evidence["id"] in cited:
                evidence["summary"] = "Changed source meaning after the agent received its evidence."
        conn.execute(update(store.snapshots).where(store.snapshots.c.row_id == row["row_id"]).values(payload=payload))
    assert store.get_snapshot("demo", cid)["id"] == task["packet"]["snapshot_id"]
    with pytest.raises(KnowledgeConflict):
        _checkpoint(runs, run["id"], task, _dataset_result(task))
    assert _proposal_count(store) == before


def test_snapshot_change_between_sync_and_claim_never_leases_old_packet(estate, monkeypatch):
    store, cid, _, _, _ = estate
    runs = BuildRuns(store)
    run = runs.start("demo", connection_ids=[cid])
    before = store.get_snapshot("demo", cid)
    original = runs._sync

    def changed_after_sync(tenant, run_id):
        original(tenant, run_id)
        store.save_snapshot("demo", cid, {**before, "snapshot_id": "changed-before-lease"})

    monkeypatch.setattr(runs, "_sync", changed_after_sync)
    try:
        claimed = runs.claim("demo", run["id"])["task"]
    except KnowledgeConflict:
        return
    assert claimed is None or claimed["packet"]["snapshot_id"] == store.get_snapshot("demo", cid)["id"]


def test_relationship_work_accepts_exact_returned_mapping(estate):
    store, cid, snapshot, _, _ = estate
    concepts = snapshot["concepts"]
    assert len(concepts) >= 2
    relationship = {"id": "relationship-explicit-order-scope", "status": "candidate",
                    "subject_id": concepts[0]["id"], "predicate": "related_to",
                    "object_id": concepts[1]["id"], "evidence_ids": [snapshot["evidence"][0]["id"]]}
    store.save_snapshot("demo", cid, {**snapshot, "snapshot_id": "with-mapping-work",
                                       "relationships": [*snapshot["relationships"], relationship]})
    runs = BuildRuns(store)
    run = runs.start("demo", connection_ids=[cid])
    task = None
    for _ in range(run["counts"]["total"]):
        leased = runs.claim("demo", run["id"])["task"]
        if leased["packet"]["kind"] == "review_relationship":
            task = leased
            break
    assert task is not None
    mapping = task["packet"].get("proposed_mapping") or task["packet"]["candidate"]["proposed_mapping_shape"]
    outcome = _checkpoint(runs, run["id"], task, {
        "action": "propose", "reason": "The exact recorded relationship is supported by its cited source evidence.",
        "evidence_ids": task["packet"]["evidence_ids"], "proposed_mapping": mapping})
    assert outcome["status"] == "proposed"


def test_reader_cannot_complete_or_cancel_another_principals_work(estate):
    store, cid, _, _, _ = estate
    _grant(store, cid, "agent-a")
    _grant(store, cid, "agent-b")
    store.access_enforced = True
    runs = BuildRuns(store)
    with principal_scope(Principal("agent-a", frozenset())):
        run = runs.start("demo", connection_ids=[cid])
        task = runs.claim("demo", run["id"])["task"]
    with principal_scope(Principal("agent-b", frozenset())):
        assert runs.get("demo", run["id"])["counts"]["leased"] == 1
        with pytest.raises(KnowledgeConflict, match="owned"):
            _checkpoint(runs, run["id"], task, _dataset_result(task))
        with pytest.raises(KnowledgeAccessDenied):
            runs.cancel("demo", run["id"], reason="Another reader should not cancel this owner's run.")
    with principal_scope(Principal("agent-a", frozenset())):
        assert _checkpoint(runs, run["id"], task, _dataset_result(task))["status"] == "proposed"


def test_partial_scope_and_revocation_hide_entire_build_and_preserve_receipts(estate):
    store, cid, _, _, _ = estate
    other = store.create_connection("demo", name="Other source", kind="local_files", config={"path": "."})
    store.refresh_connection("demo", other["id"])
    for source in (cid, other["id"]):
        _grant(store, source, "owner")
    _grant(store, cid, "partial-reader")
    store.access_enforced = True
    owner = Principal("owner", frozenset())
    runs = BuildRuns(store)
    with principal_scope(owner):
        run = runs.start("demo", connection_ids=[cid, other["id"]])
        task = runs.claim("demo", run["id"])["task"]
    with principal_scope(Principal("partial-reader", frozenset())):
        assert runs.list("demo", connection_id=cid) == []
        for action in (runs.get, runs.claim):
            with pytest.raises(KnowledgeNotFound):
                action("demo", run["id"])
    with store.engine.begin() as conn:
        conn.execute(store.source_grants.delete().where(
            store.source_grants.c.connection_id == other["id"],
            store.source_grants.c.principal_id == "owner"))
    with principal_scope(owner), pytest.raises(KnowledgeNotFound):
        _checkpoint(runs, run["id"], task, _dataset_result(task))
    assert _proposal_count(store) == 0
    with store.engine.connect() as conn:
        receipt = conn.execute(select(store.build_tasks).where(store.build_tasks.c.id == task["id"])).mappings().one()
    assert receipt["status"] == "leased" and receipt["result_digest"] is None


def test_expired_runs_do_not_consume_live_owner_capacity(estate):
    store, _, _, _, _ = estate
    source = store.create_connection("demo", name="Waiting source", kind="local_files", config={"path": "."})
    runs = BuildRuns(store)
    for _ in range(10):
        runs.start("demo", connection_ids=[source["id"]], max_seconds=60)
    with store.engine.begin() as conn:
        conn.execute(update(store.builds).values(expires_at="2000-01-01T00:00:00Z"))
    replacement = runs.start("demo", connection_ids=[source["id"]])
    assert replacement["status"] == "waiting_for_discovery"


def test_in_memory_authorized_multi_source_sync_keeps_every_task(tmp_path):
    (tmp_path / "orders.csv").write_text("order_id,total\nO-1,12\n", encoding="utf-8")
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    try:
        ids = []
        for name in ("First orders", "Second orders"):
            source = store.create_connection("demo", name=name, kind="local_files", config={"path": "."})
            store.refresh_connection("demo", source["id"])
            ids.append(source["id"])
            _grant(store, source["id"], "owner")
        store.access_enforced = True
        with principal_scope(Principal("owner", frozenset())):
            runs = BuildRuns(store)
            run = runs.start("demo", connection_ids=ids)
            assert {task["connection_id"] for task in run["tasks"]} == set(ids)
            with store.engine.connect() as conn:
                persisted = conn.execute(select(store.build_tasks).where(store.build_tasks.c.run_id == run["id"])).mappings().all()
            assert len(persisted) == run["counts"]["total"]
            task = runs.claim("demo", run["id"])["task"]
            completed = _checkpoint(runs, run["id"], task, _supported_result(task))
            assert completed["status"] == "proposed"
            current = runs.get("demo", run["id"])
            assert current["counts"]["total"] == len(persisted)
            assert current["counts"]["proposed"] == 1
            assert _proposal_count(store) == 1
    finally:
        store.close()


def test_completed_work_becomes_ready_after_review_snapshot_is_published(estate):
    store, cid, _, _, _ = estate
    runs = BuildRuns(store)
    run = runs.start("demo", connection_ids=[cid])
    proposals = []
    for _ in range(run["counts"]["total"]):
        task = runs.claim("demo", run["id"])["task"]
        completed = _checkpoint(runs, run["id"], task, _supported_result(task))
        proposals.append(completed["proposal_id"])
    drained = runs.get("demo", run["id"])
    assert drained["status"] == "awaiting_review" and drained["counts"]["pending"] == 0
    snapshot = store.get_snapshot("demo", cid)
    arguments = {"proposal_ids": proposals, "expected_snapshot_id": snapshot["id"],
                 "expected_snapshot_version": snapshot["version"]}
    preview = preview_review_batch(store, "demo", cid, **arguments)
    accepted = accept_review_batch(store, "demo", cid, **arguments,
        expected_review_digest=preview["review_digest"], publish=True,
        rationale="I reviewed these exact fixture claims and source evidence before publication.")
    assert accepted["status"] == "published"
    ready = runs.get("demo", run["id"])
    assert ready["status"] == "ready"
    assert ready["sources"][0]["snapshot_id"] == accepted["snapshot_id"]
    assert ready["counts"]["pending"] == ready["counts"]["leased"] == 0
    assert store.get_snapshot("demo", cid)["meaning_certified"] is False


def test_human_review_wait_does_not_exhaust_finished_machine_work(estate):
    store, cid, _, _, _ = estate
    runs = BuildRuns(store)
    run = runs.start("demo", connection_ids=[cid])
    for _ in range(run["counts"]["total"]):
        task = runs.claim("demo", run["id"])["task"]
        _checkpoint(runs, run["id"], task, _supported_result(task))
    assert runs.get("demo", run["id"])["status"] == "awaiting_review"
    with store.engine.begin() as conn:
        conn.execute(update(store.builds).where(store.builds.c.id == run["id"]).values(expires_at="2000-01-01T00:00:00Z"))
    waiting = runs.get("demo", run["id"])
    assert waiting["counts"]["pending"] == waiting["counts"]["leased"] == 0
    assert waiting["status"] == "awaiting_review"


@pytest.mark.parametrize("handles", [[[]], [{}], [True], ["", "not-a-task-evidence-id"]])
def test_malformed_evidence_handles_are_controlled_validation_failures(estate, handles):
    store, cid, _, _, _ = estate
    runs = BuildRuns(store)
    run = runs.start("demo", connection_ids=[cid])
    task = runs.claim("demo", run["id"])["task"]
    result = {**_dataset_result(task), "evidence_ids": handles}
    with pytest.raises(KnowledgeValidationError):
        _checkpoint(runs, run["id"], task, result)
    assert _proposal_count(store) == 0
    assert runs.get("demo", run["id"])["counts"]["leased"] == 1


def test_three_expired_leases_record_error_and_stop_reclaiming(estate):
    store, cid, _, _, _ = estate
    runs = BuildRuns(store)
    run = runs.start("demo", connection_ids=[cid])
    first = runs.claim("demo", run["id"])["task"]
    task = first
    for attempt in range(1, 4):
        assert task["id"] == first["id"] and task["attempt"] == attempt
        with store.engine.begin() as conn:
            conn.execute(update(store.build_tasks).where(store.build_tasks.c.id == task["id"]).values(lease_until="2000-01-01T00:00:00Z"))
        if attempt < 3:
            task = runs.claim("demo", run["id"])["task"]
    current = runs.get("demo", run["id"])
    outcome = next(item for item in current["tasks"] if item["id"] == first["id"])
    assert outcome["status"] == "error" and "three times" in outcome["reason"]
    assert current["counts"]["errors"] == 1
    claimed = runs.claim("demo", run["id"])["task"]
    assert claimed is None or claimed["id"] != first["id"]
    with pytest.raises(KnowledgeConflict):
        _checkpoint(runs, run["id"], task, _dataset_result(task))


def test_oversized_exact_evidence_is_deferred_instead_of_clipped_and_leased(estate):
    store, cid, snapshot, _, _ = estate
    with store.engine.begin() as conn:
        row = conn.execute(select(store.snapshots).where(
            store.snapshots.c.connection_id == cid,
            store.snapshots.c.version == snapshot["version"])).mappings().one()
        payload = deepcopy(row["payload"])
        evidence = next(item for item in payload["evidence"] if item["evidence_type"] == "asset_metadata")
        evidence["summary"] = "Exact retained metadata must not be clipped. " * 2000
        conn.execute(update(store.snapshots).where(store.snapshots.c.row_id == row["row_id"]).values(payload=payload))
    runs = BuildRuns(store)
    run = runs.start("demo", connection_ids=[cid])
    oversized = [task for task in run["tasks"] if task["kind"] == "dataset_description"]
    assert oversized and all(task["status"] == "deferred" for task in oversized)
    assert all("64 KiB" in task["reason"] for task in oversized)
    claimed = runs.claim("demo", run["id"])["task"]
    assert claimed is None or claimed["id"] not in {task["id"] for task in oversized}
    assert run["counts"]["deferred"] >= len(oversized)


def test_agent_http_converts_malformed_checkpoint_into_422(estate):
    store, cid, _, _, _ = estate
    with TestClient(build_agent_api(store)) as client:
        run = client.post("/builds", json={"connection_ids": [cid]}).json()["build"]
        task = client.post(f"/builds/{run['id']}/claim", json={}).json()["task"]
        result = {**_dataset_result(task), "evidence_ids": [{}]}
        response = client.post(f"/builds/{run['id']}/complete", json={
            "task_id": task["id"], "lease_token": task["lease_token"], "result": result})
        assert response.status_code == 422
        persisted = client.get(f"/builds/{run['id']}").json()["build"]
        assert persisted["counts"]["proposed"] == 0 and persisted["counts"]["leased"] == 1


def test_multi_source_sync_uses_same_lock_order_as_checkpoint(estate, monkeypatch):
    store, cid, _, _, _ = estate
    other = store.create_connection("demo", name="Second orders", kind="local_files", config={"path": "."})
    store.refresh_connection("demo", other["id"])
    scope = sorted([cid, other["id"]], reverse=True)
    acquired = []
    original = store._lock_source

    def record_lock(conn, tenant, connection_id, *, manage=True):
        acquired.append(connection_id)
        return original(conn, tenant, connection_id, manage=manage)

    monkeypatch.setattr(store, "_lock_source", record_lock)
    run = BuildRuns(store).start("demo", connection_ids=scope)
    assert run["counts"]["total"] > 0
    # start locks both before insert, then _sync locks them again. PostgreSQL
    # needs the latter order to match claim/complete/cancel source locks.
    assert acquired[-2:] == sorted(scope)
