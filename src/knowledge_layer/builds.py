"""Model-neutral build orchestration. Agents lease work; only humans approve it."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, insert, select, update

from knowledge_layer.access import current_principal
from knowledge_layer.store import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeValidationError,
    utc_now,
    validate_tenant_id,
)

ACTIVE = {"queued", "running", "waiting_for_discovery"}
FINAL = {"cancelled", "budget_exhausted"}
MAX_PACKET_BYTES = 64 * 1024
RESULT_KEYS = {"action", "reason", "evidence_ids", "proposed_definition", "proposed_mapping", "payload"}


def _later(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _actor(store: Any) -> str:
    principal = current_principal()
    if store.access_enforced and principal is None:
        raise KnowledgeAccessDenied("An authenticated build principal is required")
    return principal.subject if principal else "operator"


class _PinnedSource:
    def __init__(self, connection: dict):
        self.connection = connection

    def get_connection(self, *_):
        return self.connection

    def get_concepts(self, *_):
        return [{**item, "connection_id": self.connection["id"],
                 "connection_name": self.connection["name"]}
                for item in self.connection["snapshot"].get("concepts", [])]


def _packets(source: dict) -> tuple[list[dict], bool]:
    """Enumerate both bounded queues against one captured source; never clip evidence."""
    from knowledge_layer.contracts import list_contract_work
    from knowledge_layer.work import list_curation_work

    captured = _PinnedSource(source)
    snapshot = source["snapshot"]
    evidence = {item["id"]: item for item in snapshot.get("evidence", [])}
    assets = {item["id"]: item for item in snapshot.get("assets", [])}
    result: list[dict] = []
    complete = True
    for track, builder in (("meaning", list_curation_work), ("contract", list_contract_work)):
        for offset in range(0, 500, 10):
            page = builder(captured, tenant_id="demo", connection_id=source["id"], limit=10, offset=offset)
            complete = complete and page["total"] < 500
            for item in page["work"]:
                packet = deepcopy(item)
                packet["source_work_id"] = packet["work_id"]
                packet["work_id"] = "build_work_" + hashlib.sha256(
                    f"{snapshot['version']}:{packet['work_id']}".encode()).hexdigest()
                packet.update(track=track, snapshot_version=snapshot["version"],
                              evidence=[deepcopy(evidence[eid]) for eid in item["evidence_ids"]],
                              submission_tool="build_knowledge",
                              source_state={key: source.get(key) for key in
                                            ("status", "last_error", "last_refreshed_at", "last_complete_refresh_at")})
                if item.get("asset_id") in assets:
                    packet["asset"] = deepcopy(assets[item["asset_id"]])
                packet["result_contract"] = {
                    "action": "propose | abstain | defer | error",
                    "reason": "8-1000 characters explaining evidence or the blocker",
                    "evidence_ids": item["evidence_ids"],
                    "proposal": ("proposed_definition and/or exact proposed_mapping" if track == "meaning"
                                 else "payload matching the contract assertion kind"),
                    "approval": "human_review_required",
                }
                result.append(packet)
            if not page.get("more_available"):
                break
    result.sort(key=lambda item: (-item.get("priority", 0), item["track"], item["work_id"]))
    return result, complete


class BuildRuns:
    def __init__(self, store: Any):
        self.store = store
        if not hasattr(store, "builds") or not hasattr(store, "build_tasks"):
            raise KnowledgeValidationError("Durable builds require a persistent knowledge registry")
        self.runs, self.tasks = store.builds, store.build_tasks

    def _load(self, tenant: str, run_id: str, conn=None, *, lock=False) -> dict:
        def read(connection):
            query = select(self.runs).where(self.runs.c.tenant_id == tenant, self.runs.c.id == run_id)
            row = connection.execute(query.with_for_update() if lock else query).mappings().first()
            if row is None:
                raise KnowledgeNotFound("Build was not found")
            return dict(row)
        if conn is None:
            with self.store.engine.connect() as connection:
                row = read(connection)
        else:
            row = read(conn)
        if not all(self.store._has_access(tenant, cid) for cid in row["connection_ids"]):
            raise KnowledgeNotFound("Build was not found")
        return row

    def _task_rows(self, conn, tenant: str, run_id: str) -> list[dict]:
        return [dict(row) for row in conn.execute(select(*(column for column in self.tasks.c
            if column.name != "packet")).where(
            self.tasks.c.tenant_id == tenant, self.tasks.c.run_id == run_id,
        ).order_by(self.tasks.c.position, self.tasks.c.id)).mappings()]

    def start(self, tenant_id: str, *, connection_ids: list[str], max_items=200, max_seconds=3600) -> dict:
        tenant = validate_tenant_id(tenant_id)
        actor = _actor(self.store)
        if (not isinstance(connection_ids, list) or not 1 <= len(connection_ids) <= 8
                or any(not isinstance(cid, str) or not 1 <= len(cid) <= 64 for cid in connection_ids)
                or len(set(connection_ids)) != len(connection_ids)):
            raise KnowledgeValidationError("Build scope requires 1-8 distinct existing source IDs")
        if type(max_items) is not int or not 1 <= max_items <= 500:
            raise KnowledgeValidationError("max_items must be 1-500")
        if type(max_seconds) is not int or not 60 <= max_seconds <= 86400:
            raise KnowledgeValidationError("max_seconds must be 60-86400")
        for cid in connection_ids:
            if self.store.get_connection(tenant, cid) is None:
                raise KnowledgeNotFound("Connection was not found")
        now = utc_now()
        row = {"id": f"build_{uuid4().hex}", "tenant_id": tenant, "created_by": actor,
               "connection_ids": connection_ids, "sources": [], "status": "queued",
               "max_items": max_items, "created_at": now, "updated_at": now,
               "expires_at": _later(max_seconds), "cancel_reason": None,
               "coverage": {"complete": False, "source_count": len(connection_ids)}}
        with self.store.engine.begin() as conn:
            if self.store.backend == "postgresql":
                conn.exec_driver_sql("SELECT pg_advisory_xact_lock(hashtext('knowledge_build_capacity'))")
            count = conn.execute(select(func.count()).select_from(self.runs)).scalar_one()
            owned_active = conn.execute(select(func.count()).select_from(self.runs).where(
                self.runs.c.tenant_id == tenant, self.runs.c.created_by == actor,
                self.runs.c.status.in_(ACTIVE), self.runs.c.expires_at > now)).scalar_one()
            if count >= 1000 or owned_active >= 10:
                raise KnowledgeConflict("Build registry capacity reached; finish or retire existing builds")
            for cid in sorted(connection_ids):
                self.store._lock_source(conn, tenant, cid, manage=False)
            conn.execute(insert(self.runs).values(**row))
            self.store._audit(conn, tenant, None, "build.start", actor,
                              {"build_id": row["id"], "connection_ids": connection_ids,
                               "max_items": max_items, "max_seconds": max_seconds})
        return self.get(tenant, row["id"])

    def _sync(self, tenant: str, run_id: str) -> None:
        initial = self._load(tenant, run_id)
        if initial["status"] in FINAL:
            return
        sources = [self.store.get_connection(tenant, cid) for cid in initial["connection_ids"]]
        if any(source is None for source in sources):
            raise KnowledgeNotFound("Build source was not found")
        # Completed work is a receipt, not a scheduler. Review snapshots must
        # not silently relaunch abstained or deferred work. A new build is explicit.
        if initial["status"] in {"awaiting_review", "needs_attention", "ready"}:
            states = []
            for source in sources:
                snapshot = source.get("snapshot") or {}
                previous = next((item for item in initial["sources"] if item["connection_id"] == source["id"]), {})
                states.append({**previous, "connection_id": source["id"],
                               "snapshot_id": snapshot.get("id"), "snapshot_version": snapshot.get("version"),
                               "status": "published" if snapshot.get("agent_ready") else "discovered"})
            with self.store.engine.begin() as conn:
                run = self._load(tenant, run_id, conn, lock=True)
                if run["status"] not in {"awaiting_review", "needs_attention", "ready"}:
                    return
                for source in sorted(sources, key=lambda item: item["id"]):
                    self.store._lock_source(conn, tenant, source["id"], manage=False)
                    latest = conn.execute(select(self.store.snapshots.c.snapshot_id, self.store.snapshots.c.version).where(
                        self.store.snapshots.c.tenant_id == tenant, self.store.snapshots.c.connection_id == source["id"],
                    ).order_by(self.store.snapshots.c.version.desc()).limit(1)).first()
                    snapshot = source.get("snapshot") or {}
                    if latest is None or tuple(latest) != (snapshot.get("id"), snapshot.get("version")):
                        raise KnowledgeConflict("Source changed while reading completed build; retry")
                tasks = self._task_rows(conn, tenant, run_id)
                unresolved = any(task["status"] in {"abstained", "deferred", "error", "stale"} for task in tasks)
                status = ("ready" if all(item["status"] == "published" for item in states) else
                          "needs_attention" if unresolved else "awaiting_review")
                conn.execute(update(self.runs).where(self.runs.c.id == run_id).values(
                    sources=states, status=status, updated_at=utc_now()))
            return
        old_pins = {item["connection_id"]: item for item in initial["sources"]}
        generated = {}
        for source in sources:
            snapshot = source.get("snapshot") or {}
            if (snapshot.get("discovery_complete") is True and
                    (old_pins.get(source["id"], {}).get("snapshot_id"), old_pins.get(source["id"], {}).get("snapshot_version")) != (snapshot.get("id"), snapshot.get("version"))):
                generated[source["id"]] = _packets(source)
        with self.store.engine.begin() as conn:
            run = self._load(tenant, run_id, conn, lock=True)
            if run["status"] in FINAL:
                return
            now = utc_now()
            tasks = self._task_rows(conn, tenant, run_id)
            if now >= run["expires_at"]:
                conn.execute(update(self.tasks).where(self.tasks.c.run_id == run_id,
                    self.tasks.c.status.in_(("pending", "leased"))).values(
                        status="deferred", reason="Build wall-clock budget expired", lease_until=None,
                        lease_token=None, updated_at=now))
                conn.execute(update(self.runs).where(self.runs.c.id == run_id).values(
                    status="budget_exhausted", updated_at=now))
                return
            if self.store.backend == "postgresql":
                conn.exec_driver_sql("SELECT pg_advisory_xact_lock(hashtext('knowledge_build_capacity'))")
            stored_tasks = conn.execute(select(func.count()).select_from(self.tasks)).scalar_one()
            new_sources, complete, waiting, capped = [], True, False, False
            existing_work = {item["work_id"] for item in tasks}
            allocated = len(tasks)
            for source in sorted(sources, key=lambda item: item["id"]):
                cid = source["id"]
                self.store._lock_source(conn, tenant, cid, manage=False)
                snapshot = source.get("snapshot") or {}
                pin, version = snapshot.get("id"), snapshot.get("version")
                latest = conn.execute(select(self.store.snapshots.c.snapshot_id).where(
                    self.store.snapshots.c.tenant_id == tenant,
                    self.store.snapshots.c.connection_id == cid,
                ).order_by(self.store.snapshots.c.version.desc()).limit(1)).scalar_one_or_none()
                if latest != pin:
                    raise KnowledgeConflict("Source changed while preparing build work; retry")
                discovered = snapshot.get("discovery_complete") is True
                waiting = waiting or not discovered
                complete = complete and discovered
                source_state = {"connection_id": cid, "snapshot_id": pin, "snapshot_version": version,
                                "status": "published" if snapshot.get("agent_ready") else
                                "discovered" if discovered else "waiting_for_discovery"}
                previous = next((item for item in run["sources"] if item["connection_id"] == cid), {})
                source_state["queue_complete"] = previous.get("queue_complete", False)
                if (previous.get("snapshot_id"), previous.get("snapshot_version")) != (pin, version):
                    conn.execute(update(self.tasks).where(self.tasks.c.run_id == run_id,
                        self.tasks.c.connection_id == cid,
                        self.tasks.c.status.in_(("pending", "leased"))).values(
                            status="stale", reason="Source snapshot changed; lease invalidated",
                            lease_token=None, lease_until=None, updated_at=now))
                    if discovered and cid in generated:
                        packets, queue_complete = generated[cid]
                        source_state["queue_complete"] = queue_complete
                        for packet in packets:
                            if packet["work_id"] in existing_work:
                                continue
                            if allocated >= run["max_items"] or stored_tasks >= 20000:
                                capped = True
                                source_state["queue_complete"] = False
                                break
                            oversized = len(json.dumps(packet, ensure_ascii=False).encode()) > MAX_PACKET_BYTES
                            task = {"id": f"task_{uuid4().hex}", "tenant_id": tenant, "run_id": run_id,
                                    "connection_id": cid, "work_id": packet["work_id"], "track": packet["track"],
                                    "kind": packet["kind"], "snapshot_id": pin, "snapshot_version": version,
                                    "packet": {} if oversized else packet,
                                    "status": "deferred" if oversized else "pending", "attempt": 0, "position": allocated,
                                    "lease_token": None, "lease_until": None, "leased_by": None,
                                    "proposal_id": None, "result_digest": None,
                                    "reason": "Exact evidence packet exceeds 64 KiB; narrow the source scope" if oversized else None,
                                    "created_at": now, "updated_at": now}
                            conn.execute(insert(self.tasks).values(**task))
                            allocated += 1
                            stored_tasks += 1
                complete = complete and source_state["queue_complete"]
                new_sources.append(source_state)
            # Expired leases are reclaimable up to three attempts; restart never loses a receipt.
            conn.execute(update(self.tasks).where(self.tasks.c.run_id == run_id,
                self.tasks.c.status == "leased", self.tasks.c.lease_until <= now,
                self.tasks.c.attempt < 3).values(status="pending", lease_token=None,
                    lease_until=None, leased_by=None, updated_at=now))
            conn.execute(update(self.tasks).where(self.tasks.c.run_id == run_id,
                self.tasks.c.status == "leased", self.tasks.c.lease_until <= now,
                self.tasks.c.attempt >= 3).values(status="error", lease_token=None, lease_until=None,
                    reason="Agent lease expired three times", updated_at=now))
            tasks = self._task_rows(conn, tenant, run_id)
            counts = Counter(task["status"] for task in tasks)
            coverage = {"complete": complete, "source_count": len(sources)}
            if not complete:
                coverage["reason"] = "Awaiting complete discovery" if waiting else "Work/item enumeration budget reached"
            status = ("running" if counts["leased"] else "queued" if counts["pending"] else
                      "waiting_for_discovery" if waiting else
                      "budget_exhausted" if capped or not complete else
                      "needs_attention" if counts["abstained"] + counts["deferred"] + counts["error"] else
                      "ready" if all(item["status"] == "published" for item in new_sources) else "awaiting_review")
            conn.execute(update(self.runs).where(self.runs.c.id == run_id).values(
                sources=new_sources, coverage=coverage, status=status, updated_at=now))

    def get(self, tenant_id: str, run_id: str, *, include_tasks=True, synchronize=True) -> dict:
        tenant = validate_tenant_id(tenant_id)
        if synchronize:
            self._sync(tenant, run_id)
        row = self._load(tenant, run_id)
        with self.store.engine.connect() as conn:
            tasks = self._task_rows(conn, tenant, run_id)
        counts = Counter(task["status"] for task in tasks)
        result = {key: row[key] for key in ("id", "status", "connection_ids", "created_at", "updated_at",
                                           "expires_at", "max_items", "sources", "coverage", "cancel_reason")}
        result["counts"] = {"total": len(tasks), **{key: counts[key] for key in
            ("pending", "leased", "proposed", "abstained", "deferred", "stale")}, "errors": counts["error"]}
        result["resume"] = {"owner": "customer_harness", "mcp_tool": "build_knowledge",
                            "arguments": {"operation": "claim", "arguments": {"run_id": run_id}}}
        result["execution"] = {"dispatch": "customer_harness"}
        result["state_basis"] = "current_reconciliation" if synchronize else "last_checkpoint"
        if include_tasks:
            result["tasks"] = [{key: task[key] for key in ("id", "connection_id", "track", "kind", "status",
                                  "reason", "proposal_id", "snapshot_id", "snapshot_version", "attempt")}
                               for task in tasks]
        self._load(tenant, run_id)  # Recheck all grants after assembling metadata.
        return result

    def list(self, tenant_id: str, *, connection_id=None, limit=20) -> list[dict]:
        tenant = validate_tenant_id(tenant_id)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise KnowledgeValidationError("limit must be 1-50")
        with self.store.engine.connect() as conn:
            # Apply source grants before LIMIT, so hidden runs cannot starve the page.
            visible = conn.execute(select(self.runs.c.id, self.runs.c.connection_ids).where(
                self.runs.c.tenant_id == tenant).order_by(self.runs.c.created_at.desc()).limit(1000)).all()
        result = []
        for run_id, ids in visible:
            if connection_id is not None and connection_id not in ids:
                continue
            if all(self.store._has_access(tenant, cid) for cid in ids):
                result.append(self.get(tenant, run_id, include_tasks=False, synchronize=False))
                if len(result) >= limit:
                    break
        return result

    def claim(self, tenant_id: str, run_id: str, *, lease_seconds=300) -> dict:
        tenant = validate_tenant_id(tenant_id)
        actor = _actor(self.store)
        if type(lease_seconds) is not int or not 30 <= lease_seconds <= 900:
            raise KnowledgeValidationError("lease_seconds must be 30-900")
        self._sync(tenant, run_id)
        with self.store.engine.begin() as conn:
            run = self._load(tenant, run_id, conn, lock=True)
            if run["status"] not in ACTIVE:
                return {"task": None, "status": run["status"]}
            task = conn.execute(select(self.tasks).where(self.tasks.c.run_id == run_id,
                self.tasks.c.status == "pending").order_by(self.tasks.c.position, self.tasks.c.id)
                .with_for_update(skip_locked=True).limit(1)).mappings().first()
            if task is None:
                return {"task": None, "status": run["status"]}
            for cid in sorted(run["connection_ids"]):
                self.store._lock_source(conn, tenant, cid, manage=False)
            latest = conn.execute(select(self.store.snapshots.c.snapshot_id, self.store.snapshots.c.version).where(
                self.store.snapshots.c.tenant_id == tenant,
                self.store.snapshots.c.connection_id == task["connection_id"],
            ).order_by(self.store.snapshots.c.version.desc()).limit(1)).first()
            if latest is None or tuple(latest) != (task["snapshot_id"], task["snapshot_version"]):
                raise KnowledgeConflict("Source changed before the claim; retry with current work")
            token, until = uuid4().hex, min(_later(lease_seconds), run["expires_at"])
            changed = conn.execute(update(self.tasks).where(self.tasks.c.id == task["id"],
                self.tasks.c.status == "pending").values(status="leased", lease_token=token,
                    lease_until=until, leased_by=actor, attempt=task["attempt"] + 1, updated_at=utc_now()))
            if changed.rowcount != 1:
                raise KnowledgeConflict("Work was claimed by another agent; retry")
            conn.execute(update(self.runs).where(self.runs.c.id == run_id).values(status="running", updated_at=utc_now()))
            return {"task": {"id": task["id"], "run_id": run_id, "lease_token": token,
                             "lease_until": until, "attempt": task["attempt"] + 1,
                             "packet": task["packet"]}, "status": "running"}

    def complete(self, tenant_id: str, run_id: str, *, task_id: str, lease_token: str, result: dict) -> dict:
        tenant = validate_tenant_id(tenant_id)
        actor = _actor(self.store)
        if not isinstance(result, dict) or set(result) - RESULT_KEYS:
            raise KnowledgeValidationError("Unknown build result fields")
        action, reason = result.get("action"), result.get("reason")
        if not isinstance(action, str) or action not in {"propose", "abstain", "defer", "error"}:
            raise KnowledgeValidationError("Result action must be propose, abstain, defer or error")
        if not isinstance(reason, str) or not 8 <= len(reason.strip()) <= 1000:
            raise KnowledgeValidationError("Result reason must be 8-1000 characters")
        if len(json.dumps(result, ensure_ascii=False).encode()) > 16 * 1024:
            raise KnowledgeValidationError("Build result exceeds 16 KiB")
        digest = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self.store.engine.begin() as conn:
            run = self._load(tenant, run_id, conn, lock=True)
            task = conn.execute(select(self.tasks).where(self.tasks.c.id == task_id,
                self.tasks.c.tenant_id == tenant, self.tasks.c.run_id == run_id).with_for_update()).mappings().first()
            if task is None:
                raise KnowledgeNotFound("Build task was not found")
            if task["leased_by"] != actor or task["lease_token"] != lease_token:
                raise KnowledgeConflict("Build lease is not owned by this agent")
            if task["result_digest"]:
                if task["result_digest"] != digest:
                    raise KnowledgeConflict("A different result already completed this task")
                return {"task_id": task_id, "status": task["status"], "proposal_id": task["proposal_id"], "replayed": True}
            if run["status"] in FINAL or utc_now() >= run["expires_at"]:
                raise KnowledgeConflict("Build is cancelled or its budget expired")
            if task["status"] != "leased" or not task["lease_until"] or task["lease_until"] <= utc_now():
                raise KnowledgeConflict("Agent lease expired; claim current work again")
            for cid in sorted(run["connection_ids"]):
                self.store._lock_source(conn, tenant, cid, manage=False)
            snapshot = self.store.get_snapshot(tenant, task["connection_id"]) or {}
            if snapshot.get("id") != task["snapshot_id"] or snapshot.get("version") != task["snapshot_version"]:
                raise KnowledgeConflict("Source snapshot changed; claim fresh work")
            current_evidence = {item["id"]: item for item in snapshot.get("evidence", [])}
            packet = task["packet"]
            if any(current_evidence.get(item["id"]) != item for item in packet.get("evidence", [])):
                raise KnowledgeConflict("Leased evidence changed; claim fresh work")
            if packet.get("asset") and packet["asset"] not in snapshot.get("assets", []):
                raise KnowledgeConflict("Leased source schema changed; claim fresh work")
            proposal_id = None
            if action == "propose":
                packet = task["packet"]
                handles = result.get("evidence_ids")
                if (not isinstance(handles, list) or not 1 <= len(handles) <= 20
                        or any(not isinstance(eid, str) or not 1 <= len(eid) <= 160 for eid in handles)
                        or len(set(handles)) != len(handles)
                        or not set(handles).issubset(packet["evidence_ids"])):
                    raise KnowledgeValidationError("Proposal must cite evidence from the leased task")
                if task["track"] == "meaning":
                    if "payload" in result:
                        raise KnowledgeValidationError("Meaning task requires a definition or mapping")
                    mapping = result.get("proposed_mapping")
                    if packet["kind"] == "review_relationship" and mapping != packet.get("candidate", {}).get("proposed_mapping_shape"):
                        raise KnowledgeValidationError("Mapping must match the exact leased relationship")
                    if packet["kind"] == "define_concept" and mapping is not None:
                        raise KnowledgeValidationError("Definition task cannot submit a relationship mapping")
                    proposal = self.store.submit_proposal(tenant, task["connection_id"],
                        target_concept_id=packet["target_concept_id"],
                        proposed_definition=result.get("proposed_definition"), proposed_mapping=mapping,
                        evidence_ids=handles, rationale=reason, expected_snapshot_id=task["snapshot_id"],
                        _transaction=conn)
                else:
                    if "proposed_definition" in result or "proposed_mapping" in result:
                        raise KnowledgeValidationError("Contract task requires its typed payload")
                    proposal = self.store.submit_contract_assertion(tenant, task["connection_id"],
                        assertion_kind=packet["kind"], asset_id=packet["asset_id"],
                        payload=result.get("payload"), evidence_ids=handles, rationale=reason,
                        expected_snapshot_id=task["snapshot_id"], _transaction=conn)
                proposal_id = proposal["id"]
            elif set(result) - {"action", "reason"}:
                raise KnowledgeValidationError("Non-proposal outcomes may contain only action and reason")
            status = {"propose": "proposed", "abstain": "abstained", "defer": "deferred", "error": "error"}[action]
            conn.execute(update(self.tasks).where(self.tasks.c.id == task_id).values(
                status=status, proposal_id=proposal_id, reason=reason.strip(), result_digest=digest,
                lease_until=None, updated_at=utc_now()))
            self.store._audit(conn, tenant, task["connection_id"], "build.checkpoint", actor,
                {"build_id": run_id, "task_id": task_id, "action": action, "proposal_id": proposal_id})
        return {"task_id": task_id, "status": status, "proposal_id": proposal_id, "replayed": False}

    def cancel(self, tenant_id: str, run_id: str, *, reason: str) -> dict:
        tenant = validate_tenant_id(tenant_id)
        actor = _actor(self.store)
        if not isinstance(reason, str) or not 8 <= len(reason.strip()) <= 1000:
            raise KnowledgeValidationError("Cancellation reason must be 8-1000 characters")
        with self.store.engine.begin() as conn:
            run = self._load(tenant, run_id, conn, lock=True)
            if run["created_by"] != actor:
                self.store._publication_actor()
            for cid in sorted(run["connection_ids"]):
                self.store._lock_source(conn, tenant, cid, manage=False)
            conn.execute(update(self.runs).where(self.runs.c.id == run_id).values(
                status="cancelled", cancel_reason=reason.strip(), updated_at=utc_now()))
            conn.execute(update(self.tasks).where(self.tasks.c.run_id == run_id,
                self.tasks.c.status.in_(("pending", "leased"))).values(status="deferred",
                    lease_token=None, lease_until=None, reason="Build cancelled", updated_at=utc_now()))
            self.store._audit(conn, tenant, None, "build.cancel", actor, {"build_id": run_id})
        return self.get(tenant, run_id)
