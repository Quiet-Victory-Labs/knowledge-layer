"""Steward-reviewed, bounded delegation for publishing unchanged reviewed meaning.

This registry supplies authority independently of advisory company skills/rules.
It neither approves proposals nor reads/writes enterprise sources. The initial
class excludes cross-source dependencies and changes to reviewed metadata.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from knowledge_layer.access import current_principal
from knowledge_layer.refresh_fence import assert_refresh_fence
from knowledge_layer.source_policy import source_identity
from knowledge_layer.store import (
    _VOLATILE_PROPOSAL_FIELDS,
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeValidationError,
    validate_tenant_id,
)

SCHEMA_VERSION = "knowledge-autonomy-policy-v1"
ACTION = "publish_reviewed_unchanged"
MAX_BINDING_BYTES = 1024 * 1024


def _clock() -> datetime:
    return datetime.now(UTC)


def _text(value, label, maximum=255, minimum=1):
    if (not isinstance(value, str) or not minimum <= len(value) <= maximum
            or not value.strip() or re.search(r"[\x00-\x1f\x7f]", value)):
        raise KnowledgeValidationError(f"Autonomy {label} must be bounded nonblank text")
    return value


def _encoded(value):
    try:
        result = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                            allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise KnowledgeValidationError("Autonomy content must be finite JSON") from exc
    if len(result) > MAX_BINDING_BYTES:
        raise KnowledgeValidationError("Autonomy binding exceeds the 1 MiB budget")
    return result


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _timestamp(value):
    try:
        clock = datetime.fromisoformat(value)
        if clock.utcoffset() is None:
            raise ValueError
        return clock.astimezone(UTC)
    except (ValueError, TypeError) as exc:
        raise KnowledgeValidationError("Autonomy expiry must be an aware ISO timestamp") from exc


def _payload(value):
    keys = {"schema_version", "action", "subject", "expires_at", "max_publications", "purpose"}
    if not isinstance(value, dict) or set(value) != keys:
        raise KnowledgeValidationError("Autonomy policy has missing or unknown fields")
    if value["schema_version"] != SCHEMA_VERSION or value["action"] != ACTION:
        raise KnowledgeValidationError("Autonomy action or schema is unsupported")
    _text(value["subject"], "subject")
    _text(value["purpose"], "purpose", 1000, 16)
    if type(value["max_publications"]) is not int or not 1 <= value["max_publications"] <= 1000:
        raise KnowledgeValidationError("Autonomy publication budget must be 1-1000")
    expiry = _timestamp(value["expires_at"])
    now = _clock()
    if not now < expiry <= now + timedelta(days=30):
        raise KnowledgeValidationError("Autonomy expiry must be future and within 30 days")
    return {**value, "expires_at": expiry.isoformat().replace("+00:00", "Z")}


class AutonomyRegistry:
    def __init__(self, store: Any, *, execution_enabled: bool = False):
        self.store = store
        self.heads = store.autonomy_policies
        self.revisions = store.autonomy_revisions
        self.runs = store.autonomy_runs
        self.execution_enabled = execution_enabled

    def _source(self, conn, tenant, cid, *, require_connector_approval=True):
        self.store._require_source_manager()
        source = self.store._lock_source(conn, tenant, cid)
        if require_connector_approval:
            self.store._require_source_approval(tenant, source["kind"], source["config"])
        return source

    def _binding(self, conn, tenant, cid, source):
        snapshot = self.store.get_snapshot(tenant, cid)
        preview = self.store.publication_preview(tenant, cid)
        if not snapshot or not preview["eligible"]:
            raise KnowledgeConflict("Autonomy requires current complete reviewed metadata")
        if (snapshot["id"], snapshot["version"]) != (preview["snapshot_id"], preview["snapshot_version"]):
            raise KnowledgeConflict("Autonomy snapshot changed during validation")
        approved = [p for p in snapshot.get("proposals", [])
                    if p.get("status") == "approved" and p.get("effective_on_current_snapshot") is True]
        if not approved or len(approved) > 1000:
            raise KnowledgeConflict("Autonomy reviewed assertion set is missing or over budget")
        if any(p.get("evidence_refs") or p.get("assertion_kind") == "cross_source_join" for p in approved):
            raise KnowledgeConflict("Autonomy cross-source dependencies require explicit review")
        ids = sorted(p["id"] for p in approved)
        if len(ids) != len(set(ids)):
            raise KnowledgeConflict("Autonomy reviewed assertion identities are ambiguous")
        ledger = conn.execute(select(self.store.decisions).where(
            self.store.decisions.c.tenant_id == tenant,
            self.store.decisions.c.connection_id == cid,
            self.store.decisions.c.proposal_id.in_(ids),
            self.store.decisions.c.decision == "approve",
        )).mappings().all()
        if len(ledger) != len(ids) or any(not row["proposal_digest"] for row in ledger):
            raise KnowledgeConflict("Autonomy reviewed assertion ledger is incomplete")
        metadata = {key: snapshot.get(key, []) for key in
                    ("assets", "concepts", "relationships", "metrics", "evidence")}
        metadata["proposals"] = [{k: v for k, v in p.items() if k not in _VOLATILE_PROPOSAL_FIELDS}
                                 for p in snapshot.get("proposals", [])]
        binding = {
            **self._operator_binding(tenant, source),
            "metadata_digest": _digest(metadata),
            "reviewed_assertions": sorted([{
                "id": row["proposal_id"], "digest": row["proposal_digest"],
                "decided_at": row["decided_at"],
            } for row in ledger], key=lambda row: row["id"]),
        }
        return binding, snapshot

    def _operator_binding(self, tenant, source):
        identity = source_identity(source["kind"], source["config"])
        approvals = self.store.source_policy.approvals if self.store.source_policy else ()
        policy = [{"subjects": sorted(a.subjects), "groups": sorted(a.groups)}
                  for a in approvals if a.tenant_id == tenant and a.identity == identity]
        return {"source_identity_digest": _digest(identity),
                "source_policy_digest": _digest(sorted(policy, key=_encoded))}

    def _subject_approved(self, tenant, source, subject):
        identity = source_identity(source["kind"], source["config"])
        policy = self.store.source_policy
        if policy is None or not any(a.tenant_id == tenant and a.identity == identity
                                    and subject in a.subjects for a in policy.approvals):
            raise KnowledgeAccessDenied("Autonomy subject needs explicit operator source approval")

    def _head(self, conn, tenant, cid):
        return conn.execute(select(self.heads).where(
            self.heads.c.tenant_id == tenant, self.heads.c.connection_id == cid,
        ).with_for_update()).mappings().first()

    def _revision(self, conn, tenant, cid, revision, *, verify_integrity=True):
        if type(revision) is not int or not 1 <= revision <= 2**31 - 1:
            raise KnowledgeValidationError("Autonomy revision must be a positive integer")
        row = conn.execute(select(self.revisions).where(
            self.revisions.c.tenant_id == tenant, self.revisions.c.connection_id == cid,
            self.revisions.c.revision == revision,
        ).with_for_update()).mappings().first()
        if row is None:
            raise KnowledgeNotFound("Autonomy policy was not found")
        if verify_integrity and row["digest"] != _digest({"payload": row["payload"], "bindings": row["bindings"]}):
            raise KnowledgeConflict("Autonomy policy integrity changed")
        return dict(row)

    def get(self, tenant_id, connection_id, *, revision=None):
        tenant = validate_tenant_id(tenant_id)
        if revision is not None and (type(revision) is not int or not 1 <= revision <= 2**31 - 1):
            raise KnowledgeValidationError("Autonomy revision must be a positive integer")
        with self.store.engine.begin() as conn:
            self._source(conn, tenant, connection_id, require_connector_approval=False)
            head = self._head(conn, tenant, connection_id)
            if head is None:
                return {"schema_version": SCHEMA_VERSION, "state": "not_established", "policy": None}
            row = self._revision(conn, tenant, connection_id,
                                 head["latest_revision"] if revision is None else revision)
            active = head["active_revision"] == row["revision"] and head["revoked_at"] is None
            state = ("expired" if active and _timestamp(row["payload"]["expires_at"]) <= _clock()
                     else "active" if active else "revoked" if head["revoked_at"] and
                     head["active_revision"] == row["revision"] else "approved_inactive" if
                     row["approved_at"] else "draft")
            return {"schema_version": SCHEMA_VERSION, "state": state, "policy": row,
                    "execution_enabled": self.execution_enabled}

    def draft(self, tenant_id, connection_id, payload, *, expected_revision=0):
        tenant, payload = validate_tenant_id(tenant_id), _payload(payload)
        if type(expected_revision) is not int or not 0 <= expected_revision <= 2**31 - 2:
            raise KnowledgeValidationError("Autonomy expected revision must be a nonnegative integer")
        actor = current_principal()
        try:
            with self.store.engine.begin() as conn:
                source = self._source(conn, tenant, connection_id)
                self._subject_approved(tenant, source, payload["subject"])
                head = self._head(conn, tenant, connection_id)
                if (head["latest_revision"] if head else 0) != expected_revision:
                    raise KnowledgeConflict("Autonomy policy changed before draft")
                bindings, snapshot = self._binding(conn, tenant, connection_id, source)
                now = _clock().isoformat().replace("+00:00", "Z")
                revision = expected_revision + 1
                if head:
                    changed = conn.execute(update(self.heads).where(
                        self.heads.c.tenant_id == tenant, self.heads.c.connection_id == connection_id,
                        self.heads.c.latest_revision == expected_revision,
                    ).values(latest_revision=revision, updated_at=now))
                    if changed.rowcount != 1:
                        raise KnowledgeConflict("Autonomy policy changed before draft")
                else:
                    conn.execute(insert(self.heads).values(
                        tenant_id=tenant, connection_id=connection_id, latest_revision=revision,
                        active_revision=None, active_digest=None, revoked_at=None, revoked_by=None,
                        revoke_reason=None, created_at=now, updated_at=now,
                    ))
                digest = _digest({"payload": payload, "bindings": bindings})
                row = {"tenant_id": tenant, "connection_id": connection_id, "revision": revision,
                       "payload": payload, "digest": digest, "bindings": bindings, "created_at": now,
                       "created_by": actor.subject if actor else "operator", "approved_at": None,
                       "approved_by": None, "approval_rationale": None, "publication_count": 0}
                conn.execute(insert(self.revisions).values(**row))
                self.store._audit(conn, tenant, connection_id, "autonomy.draft", row["created_by"],
                                  {"revision": revision, "digest": digest, "snapshot_id": snapshot["id"]})
            return row
        except IntegrityError as exc:
            raise KnowledgeConflict("Autonomy policy changed before draft") from exc

    def approve(self, tenant_id, connection_id, *, revision, expected_digest, rationale):
        tenant = validate_tenant_id(tenant_id)
        actor = self.store._publication_actor()
        _text(rationale, "approval rationale", 1000, 16)
        with self.store.engine.begin() as conn:
            source = self._source(conn, tenant, connection_id)
            head = self._head(conn, tenant, connection_id)
            row = self._revision(conn, tenant, connection_id, revision)
            if head["latest_revision"] != revision or row["digest"] != expected_digest:
                raise KnowledgeConflict("Autonomy policy changed before approval")
            self._subject_approved(tenant, source, row["payload"]["subject"])
            current, _snapshot = self._binding(conn, tenant, connection_id, source)
            if current != row["bindings"] or _timestamp(row["payload"]["expires_at"]) <= _clock():
                raise KnowledgeConflict("Autonomy reviewed binding or expiry changed before approval")
            if row["approved_at"]:
                if (head["active_revision"] == revision and head["revoked_at"] is None
                        and head["active_digest"] == expected_digest):
                    return row
                raise KnowledgeConflict("Approved autonomy revisions cannot be reactivated")
            now = _clock().isoformat().replace("+00:00", "Z")
            conn.execute(update(self.revisions).where(
                self.revisions.c.tenant_id == tenant, self.revisions.c.connection_id == connection_id,
                self.revisions.c.revision == revision,
            ).values(approved_at=now, approved_by=actor, approval_rationale=rationale))
            conn.execute(update(self.heads).where(
                self.heads.c.tenant_id == tenant, self.heads.c.connection_id == connection_id,
            ).values(active_revision=revision, active_digest=expected_digest,
                     revoked_at=None, revoked_by=None, revoke_reason=None, updated_at=now))
            self.store._audit(conn, tenant, connection_id, "autonomy.approve", actor,
                              {"revision": revision, "digest": expected_digest})
        return {**row, "approved_at": now, "approved_by": actor, "approval_rationale": rationale}

    def _authorize_run(self, conn, tenant, cid, revision, digest):
        principal = current_principal()
        if (not self.execution_enabled or not self.store.access_enforced or principal is None
                or "knowledge.autonomy" not in principal.scopes):
            raise KnowledgeAccessDenied("Autonomy execution requires opt-in and a verified scoped principal")
        source = self._source(conn, tenant, cid)
        direct = conn.execute(select(self.store.source_grants.c.role).where(
            self.store.source_grants.c.tenant_id == tenant,
            self.store.source_grants.c.connection_id == cid,
            self.store.source_grants.c.principal_type == "user",
            self.store.source_grants.c.principal_id == principal.subject,
        )).scalar_one_or_none()
        if direct != "manage":
            raise KnowledgeAccessDenied("Autonomy requires a direct source manage grant")
        head = self._head(conn, tenant, cid)
        row = self._revision(conn, tenant, cid, revision)
        if principal.subject != row["payload"]["subject"]:
            raise KnowledgeAccessDenied("Autonomy policy does not authorize this subject")
        if (head["active_revision"] != revision or head["active_digest"] != digest
                or row["digest"] != digest or head["revoked_at"] is not None or not row["approved_at"]
                or _timestamp(row["payload"]["expires_at"]) <= _clock()):
            raise KnowledgeConflict("Autonomy policy is inactive, changed, withdrawn or expired")
        self._subject_approved(tenant, source, principal.subject)
        assert_refresh_fence(self.store, conn, tenant, cid)
        return source, row, principal

    def apply(self, tenant_id, connection_id, *, revision, expected_digest,
              expected_snapshot_id, expected_snapshot_version):
        tenant = validate_tenant_id(tenant_id)
        _text(expected_snapshot_id, "snapshot ID", 128)
        if type(expected_snapshot_version) is not int or expected_snapshot_version < 1:
            raise KnowledgeValidationError("Autonomy snapshot version must be a positive integer")
        with self.store.engine.begin() as conn:
            source, policy, principal = self._authorize_run(conn, tenant, connection_id, revision, expected_digest)
            binding, snapshot = self._binding(conn, tenant, connection_id, source)
            if (binding != policy["bindings"] or
                    (snapshot["id"], snapshot["version"]) != (expected_snapshot_id, expected_snapshot_version)):
                raise KnowledgeConflict("Autonomy reviewed snapshot or meaning changed")
            existing = conn.execute(select(self.runs).where(
                self.runs.c.tenant_id == tenant, self.runs.c.connection_id == connection_id,
                self.runs.c.policy_revision == revision, self.runs.c.snapshot_version == expected_snapshot_version,
            )).mappings().first()
            publication = conn.execute(select(self.store.publications).where(
                self.store.publications.c.tenant_id == tenant,
                self.store.publications.c.connection_id == connection_id,
                self.store.publications.c.snapshot_version == expected_snapshot_version,
            )).mappings().first()
            if publication and (publication["revoked_at"] is not None
                                or publication["snapshot_id"] != expected_snapshot_id):
                raise KnowledgeConflict("Autonomy publication was withdrawn or changed")
            if existing:
                if (publication is None or existing["snapshot_id"] != expected_snapshot_id
                        or existing["actor"] != principal.subject
                        or (existing["owns_publication"]
                            and publication["published_by"] != existing["actor"])):
                    raise KnowledgeConflict("Autonomy publication history changed")
                return {"result": "already_applied", "run": dict(existing), "source_executed": False}
            if publication is None and policy["publication_count"] >= policy["payload"]["max_publications"]:
                raise KnowledgeConflict("Autonomy publication budget is exhausted")
            self._authorize_run(conn, tenant, connection_id, revision, expected_digest)
            now = _clock().isoformat().replace("+00:00", "Z")
            _record, created = self.store._publish_source_in_transaction(
                conn, tenant, connection_id, expected_snapshot_id=expected_snapshot_id,
                expected_snapshot_version=expected_snapshot_version, actor=principal.subject, published_at=now,
                rationale=f"Unchanged reviewed publication authorized by policy revision {revision}, digest {expected_digest}.",
                delegation={"policy_revision": revision, "policy_digest": expected_digest},
            )
            if created:
                changed = conn.execute(update(self.revisions).where(
                    self.revisions.c.tenant_id == tenant, self.revisions.c.connection_id == connection_id,
                    self.revisions.c.revision == revision,
                    self.revisions.c.publication_count == policy["publication_count"],
                ).values(publication_count=policy["publication_count"] + 1))
                if changed.rowcount != 1:
                    raise KnowledgeConflict("Autonomy publication budget changed")
            run = {"id": "autonomy_" + uuid4().hex, "tenant_id": tenant, "connection_id": connection_id,
                   "policy_revision": revision, "snapshot_id": expected_snapshot_id,
                   "snapshot_version": expected_snapshot_version, "actor": principal.subject,
                   "created_at": now, "owns_publication": created}
            conn.execute(insert(self.runs).values(**run))
            self.store._audit(conn, tenant, connection_id, "autonomy.apply", principal.subject,
                              {"run_id": run["id"], "revision": revision, "digest": expected_digest,
                               "snapshot_id": expected_snapshot_id, "owns_publication": created})
            # Last precommit guard: no nested projection/connection reads after
            # the first write. A late expiry rolls back publication, usage,
            # run and audit together, including a delayed final audit write.
            if _timestamp(policy["payload"]["expires_at"]) <= _clock():
                raise KnowledgeConflict("Autonomy policy expired before commit")
            if any(policy["bindings"][key] != value
                   for key, value in self._operator_binding(tenant, source).items()):
                raise KnowledgeConflict("Autonomy operator approval changed before commit")
            assert_refresh_fence(self.store, conn, tenant, connection_id)
        return {"result": "published" if created else "already_published", "run": run, "source_executed": False}

    def revoke(self, tenant_id, connection_id, *, revision, expected_digest, reason):
        tenant = validate_tenant_id(tenant_id)
        actor = self.store._publication_actor()
        _text(reason, "withdrawal reason", 1000, 16)
        with self.store.engine.begin() as conn:
            # Withdrawal only reduces stored authority. Lost connector approval
            # or corrupted policy content must not obstruct a real steward's
            # source-scoped kill switch. The approved head pins the request.
            self._source(conn, tenant, connection_id, require_connector_approval=False)
            head = self._head(conn, tenant, connection_id)
            self._revision(conn, tenant, connection_id, revision, verify_integrity=False)
            if head["active_revision"] != revision or head["active_digest"] != expected_digest:
                raise KnowledgeConflict("Autonomy policy changed before withdrawal")
            if head["revoked_at"]:
                return {"result": "already_withdrawn", "revision": revision}
            now = _clock().isoformat().replace("+00:00", "Z")
            conn.execute(update(self.heads).where(
                self.heads.c.tenant_id == tenant, self.heads.c.connection_id == connection_id,
            ).values(revoked_at=now, revoked_by=actor, revoke_reason=reason, updated_at=now))
            owned = select(self.runs.c.snapshot_version).where(
                self.runs.c.tenant_id == tenant, self.runs.c.connection_id == connection_id,
                self.runs.c.policy_revision <= revision, self.runs.c.owns_publication.is_(True),
            )
            result = conn.execute(update(self.store.publications).where(
                self.store.publications.c.tenant_id == tenant,
                self.store.publications.c.connection_id == connection_id,
                self.store.publications.c.snapshot_version.in_(owned),
                self.store.publications.c.revoked_at.is_(None),
            ).values(revoked_by=actor, revoked_at=now, revoke_reason=reason))
            self.store._audit(conn, tenant, connection_id, "autonomy.withdraw", actor,
                              {"revision": revision, "digest": expected_digest,
                               "withdrawn_publications": result.rowcount})
        return {"result": "withdrawn", "revision": revision, "withdrawn_publications": result.rowcount}
