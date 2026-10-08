"""Human acceptance of an exact, evidence-bound selection of knowledge drafts."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any
from uuid import uuid4

from sqlalchemy import insert, select, update

from knowledge_layer.access import current_principal
from knowledge_layer.contracts import strip_materialized_assertions
from knowledge_layer.store import (
    _ALLOWED_SNAPSHOT_FIELDS,
    KnowledgeConflict,
    KnowledgeError,
    KnowledgeNotFound,
    KnowledgeValidationError,
    _evidence_ids,
    _proposal_digest,
    _record_id,
    utc_now,
    validate_tenant_id,
)

PENDING = {"candidate", "pending", "pending_review", "needs_review"}
MAX_PREVIEW_BYTES = 128 * 1024


def _validate_selection(proposal_ids: list[str], snapshot_id: str, version: int) -> None:
    if (not isinstance(proposal_ids, list) or not 1 <= len(proposal_ids) <= 100
            or any(not isinstance(value, str) or not 1 <= len(value) <= 160
                   for value in proposal_ids)
            or len(set(proposal_ids)) != len(proposal_ids)):
        raise KnowledgeValidationError("Select 1-100 distinct proposal IDs")
    if (not isinstance(snapshot_id, str) or not 1 <= len(snapshot_id) <= 128
            or type(version) is not int or version < 1):
        raise KnowledgeValidationError("Review requires an exact source snapshot and version")


def _selection(store: Any, tenant: str, connection_id: str, proposal_ids: list[str],
               snapshot_id: str, version: int) -> tuple[dict, list[dict], str]:
    _validate_selection(proposal_ids, snapshot_id, version)
    store._require_source_manager()
    store._require_access(tenant, connection_id, manage=True)
    snapshot = store.get_snapshot(tenant, connection_id)
    if snapshot is None:
        raise KnowledgeNotFound("Source snapshot was not found")
    if snapshot.get("id") != snapshot_id or snapshot.get("version") != version:
        raise KnowledgeConflict("Source snapshot changed; review the current drafts again")
    indexed = {_record_id(item): item for item in snapshot.get("proposals", [])}
    selected = []
    for proposal_id in proposal_ids:
        proposal = indexed.get(proposal_id)
        if proposal is None:
            raise KnowledgeNotFound("Selected proposal was not found")
        if proposal.get("status") not in PENDING:
            raise KnowledgeConflict("Selected proposal is no longer pending review")
        store._validate_proposal_approval(tenant, proposal, snapshot)
        selected.append(proposal)
    digests = sorted((_record_id(item), _proposal_digest(item, snapshot["evidence"]))
                     for item in selected)
    digest = hashlib.sha256(json.dumps({
        "connection_id": connection_id, "snapshot_id": snapshot_id,
        "snapshot_version": version, "proposals": digests,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return snapshot, selected, digest


def preview_review_batch(store: Any, tenant_id: str, connection_id: str, *,
                         proposal_ids: list[str], expected_snapshot_id: str,
                         expected_snapshot_version: int) -> dict[str, Any]:
    tenant = validate_tenant_id(tenant_id)
    snapshot, proposals, digest = _selection(
        store, tenant, connection_id, proposal_ids,
        expected_snapshot_id, expected_snapshot_version,
    )
    evidence_ids = {eid for item in proposals for eid in _evidence_ids(item)}
    result = {
        "connection_id": connection_id, "snapshot_id": snapshot["id"],
        "snapshot_version": snapshot["version"], "review_digest": digest,
        "proposals": proposals,
        "evidence": [item for item in snapshot["evidence"] if _record_id(item) in evidence_ids],
        "remaining_pending_count": sum(item.get("status") in PENDING
                                       for item in snapshot["proposals"]) - len(proposals),
    }
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_PREVIEW_BYTES:
        raise KnowledgeValidationError("Review preview exceeds 128 KiB; select fewer drafts")
    # Recheck grants and the exact selection after assembling the preview.
    _, _, current_digest = _selection(store, tenant, connection_id, proposal_ids,
                                     expected_snapshot_id, expected_snapshot_version)
    if current_digest != digest:
        raise KnowledgeConflict("Selected drafts changed during preview; review them again")
    return result


def accept_review_batch(store: Any, tenant_id: str, connection_id: str, *,
                        proposal_ids: list[str], expected_snapshot_id: str,
                        expected_snapshot_version: int, expected_review_digest: str,
                        rationale: str, publish: bool = False) -> dict[str, Any]:
    """Commit all selected approvals and one review snapshot together.

    Optional publication follows this commit through the existing steward gate.
    A publication failure is explicit and never rolls back a completed review.
    """
    tenant = validate_tenant_id(tenant_id)
    _validate_selection(proposal_ids, expected_snapshot_id, expected_snapshot_version)
    if (not isinstance(expected_review_digest, str) or len(expected_review_digest) != 64
            or any(char not in "0123456789abcdef" for char in expected_review_digest)):
        raise KnowledgeValidationError("An exact review preview digest is required")
    if not isinstance(rationale, str) or not 16 <= len(rationale.strip()) <= 1000:
        raise KnowledgeValidationError("Review rationale must be 16-1000 characters")
    if type(publish) is not bool:
        raise KnowledgeValidationError("publish must be a boolean")
    store._require_source_manager()
    if publish:
        # Reject an unauthorized publication before recording any approvals.
        store._publication_actor()
    rationale = rationale.strip()
    reviewer = current_principal()
    actor = reviewer.subject if reviewer else "operator"
    with store.engine.begin() as conn:
        store._lock_source(conn, tenant, connection_id)
        snapshot, proposals, digest = _selection(
            store, tenant, connection_id, proposal_ids,
            expected_snapshot_id, expected_snapshot_version,
        )
        if digest != expected_review_digest:
            raise KnowledgeConflict("Selected claims or evidence changed; review them again")
        latest = conn.execute(select(store.snapshots.c.snapshot_id, store.snapshots.c.version).where(
            store.snapshots.c.tenant_id == tenant,
            store.snapshots.c.connection_id == connection_id,
        ).order_by(store.snapshots.c.version.desc()).limit(1)).first()
        if latest is None or tuple(latest) != (expected_snapshot_id, expected_snapshot_version):
            raise KnowledgeConflict("Source snapshot changed before acceptance")
        decided_at = utc_now()
        for proposal in proposals:
            proposal_id = _record_id(proposal)
            proposal_digest = _proposal_digest(proposal, snapshot["evidence"])
            existing = conn.execute(select(store.decisions).where(
                store.decisions.c.tenant_id == tenant,
                store.decisions.c.connection_id == connection_id,
                store.decisions.c.proposal_id == proposal_id,
            )).mappings().first()
            if existing and existing["decision"] != "approve":
                raise KnowledgeConflict("Selected proposal already has a different decision")
            values = {"decision": "approve", "rationale": rationale,
                      "decided_at": decided_at, "proposal_digest": proposal_digest}
            if existing:
                conn.execute(update(store.decisions).where(
                    store.decisions.c.tenant_id == tenant,
                    store.decisions.c.connection_id == connection_id,
                    store.decisions.c.proposal_id == proposal_id,
                ).values(**values))
            else:
                conn.execute(insert(store.decisions).values(
                    tenant_id=tenant, connection_id=connection_id,
                    proposal_id=proposal_id, **values,
                ))
            store._audit(conn, tenant, connection_id, "proposal.decide", actor, {
                "proposal_id": proposal_id, "decision": "approve",
                "proposal_digest": proposal_digest, "review_batch_digest": digest,
                "rationale_digest": hashlib.sha256(rationale.encode()).hexdigest(),
                "re_review": existing is not None,
            })
        versioned = {field: deepcopy(snapshot[field]) for field in _ALLOWED_SNAPSHOT_FIELDS
                     if field in snapshot}
        versioned["snapshot_id"] = f"snapshot_review_{uuid4().hex}"
        strip_materialized_assertions(versioned)
        clean, status, snapshot_id = store._prepare_snapshot(versioned)
        store._save_snapshot_in_transaction(conn, tenant, connection_id, clean,
            status=status, snapshot_id=snapshot_id,
            review_parent=(expected_snapshot_id, expected_snapshot_version))
        reviewed_version = expected_snapshot_version + 1
    result = {
        "status": "accepted", "reviewed_proposal_ids": list(proposal_ids),
        "snapshot_id": snapshot_id, "snapshot_version": reviewed_version,
        "remaining_pending_count": sum(item.get("status") in PENDING
                                       for item in snapshot["proposals"]) - len(proposals),
    }
    if publish:
        try:
            result["publication"] = store.publish_source(
                tenant, connection_id, expected_snapshot_id=snapshot_id,
                expected_snapshot_version=reviewed_version, rationale=rationale,
            )
            result["status"] = "published"
        except KnowledgeError as exc:
            result["status"] = "accepted_publication_blocked"
            result["publication_error"] = str(exc)
    return result
