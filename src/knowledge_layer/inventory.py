"""Bounded staged inventories with atomic scoped completion, not approved meaning.

Every read uses current registry source grants/policy. A complete discovery manifest
neither updates legacy snapshot clocks nor certifies an estate-wide semantic graph.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from contextlib import contextmanager
from copy import deepcopy
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, exists, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from knowledge_layer.access_log import record_source_read
from knowledge_layer.refresh_fence import (
    assert_refresh_fence,
    current_refresh_claim,
    refresh_fence,
)
from knowledge_layer.source_policy import source_identity
from knowledge_layer.store import (
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeValidationError,
    utc_now,
    validate_tenant_id,
)

SCHEMA_VERSION = "knowledge-inventory-v1"
MAX_PAGE_BYTES = 256 * 1024
MAX_RECORD_BYTES = 64 * 1024
MAX_ENTITIES_PER_PAGE = 100
MAX_EVIDENCE_PER_PAGE = 200
HARD_LIMITS = {"max_pages": 10000, "max_entities": 1000000,
               "max_evidence": 2000000, "max_total_bytes": 256 * 1024 * 1024}
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_KINDS = {"asset", "semantic_definition", "document", "relationship"}


def _object(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise KnowledgeValidationError(f"Inventory {label} has missing or unknown fields")
    return value


def _text(value, label, maximum=256):
    if (not isinstance(value, str) or not 1 <= len(value) <= maximum
            or not value.strip() or re.search(r"[\x00-\x1f\x7f]", value)):
        raise KnowledgeValidationError(f"Inventory {label} must be bounded nonblank text")
    return value


def _int(value, label, maximum, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise KnowledgeValidationError(f"Inventory {label} is outside its integer budget")
    return value


def _encoded(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError) as exc:
        raise KnowledgeValidationError("Inventory content must be complete finite JSON") from exc


def inventory_digest(value: Any) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise KnowledgeValidationError("Inventory cursor repeats a JSON member")
        result[key] = value
    return result


def _claim(value):
    observed = current_refresh_claim()
    if value is None:
        value = observed
    elif observed is not None and value != observed:
        raise KnowledgeConflict("Inventory claim disagrees with the active refresh attempt")
    if value is not None:
        _object(value, {"job_id", "worker_id", "attempt"}, "claim")
        _text(value["job_id"], "job ID", 64)
        _text(value["worker_id"], "worker ID", 80)
        _int(value["attempt"], "attempt", 10**9, 1)
    return deepcopy(value)


def _scope(value):
    _object(value, {"namespace", "selectors"}, "scope")
    _text(value["namespace"], "namespace", 120)
    selectors = value["selectors"]
    if not isinstance(selectors, list) or not 1 <= len(selectors) <= 200:
        raise KnowledgeValidationError("Inventory scope requires 1-200 explicit selectors")
    for item in selectors:
        _text(item, "selector")
    if len(set(selectors)) != len(selectors):
        raise KnowledgeValidationError("Inventory selectors must be distinct")
    return {"namespace": value["namespace"], "selectors": sorted(selectors)}


def _pins(scope, policy, watermark):
    scope = _scope(scope)
    _object(policy, {"id", "revision"}, "policy")
    _text(policy["id"], "policy ID", 120)
    if policy["revision"] is not None:
        _text(policy["revision"], "policy revision", 120)
    _object(watermark, {"kind", "value"}, "watermark")
    _text(watermark["kind"], "watermark kind", 80)
    value = watermark["value"]
    if value is not None:
        if type(value) is int:
            _int(value, "watermark", 2**63 - 1)
        else:
            _text(value, "watermark value", 512)
    if watermark["kind"] == "unknown" and value is not None:
        raise KnowledgeValidationError("An unknown watermark must remain null")
    return {"scope": scope, "policy": deepcopy(policy), "watermark": deepcopy(watermark)}


def _limits(value):
    _object(value, HARD_LIMITS, "limits")
    for key, maximum in HARD_LIMITS.items():
        _int(value[key], key, maximum, 1 if key in {"max_pages", "max_total_bytes"} else 0)
    return deepcopy(value)


def _records(value, *, evidence=False):
    maximum = MAX_EVIDENCE_PER_PAGE if evidence else MAX_ENTITIES_PER_PAGE
    if not isinstance(value, list) or len(value) > maximum:
        raise KnowledgeValidationError("Inventory page row budget exceeded")
    seen = set()
    for item in value:
        _object(item, {"id", "kind", "payload"} if evidence else
                {"id", "kind", "payload", "evidence_ids"}, "record")
        identifier = _text(item["id"], "record ID")
        if identifier in seen:
            raise KnowledgeValidationError("Inventory page repeats a record ID")
        seen.add(identifier)
        _text(item["kind"], "record kind", 64)
        if not evidence and item["kind"] not in _KINDS:
            raise KnowledgeValidationError("Inventory entity kind is unsupported")
        if not isinstance(item["payload"], dict):
            raise KnowledgeValidationError("Inventory record payload must be a metadata object")
        if not evidence:
            ids = item["evidence_ids"]
            if not isinstance(ids, list) or len(ids) > 20:
                raise KnowledgeValidationError("Inventory entity evidence references are bounded")
            for identifier in ids:
                _text(identifier, "evidence ID")
            if len(set(ids)) != len(ids):
                raise KnowledgeValidationError("Inventory evidence references must be distinct")
        if len(_encoded(item)) > MAX_RECORD_BYTES:
            raise KnowledgeValidationError("Inventory complete record exceeds byte budget")
    return deepcopy(value)


class InventoryStore:
    def __init__(self, store):
        self.store = store
        self.runs = store.inventory_runs
        self.pages = store.inventory_pages
        self.entities = store.inventory_entities
        self.evidence = store.inventory_evidence
        self.heads = store.inventory_heads

    @contextmanager
    def _current_read_connection(self):
        with self.store.engine.connect() as conn:
            # The final authorization/version read must observe commits made
            # during response assembly even if the operator's registry default
            # is REPEATABLE READ. SQLAlchemy restores isolation on pool return.
            if conn.dialect.name == "postgresql":
                conn = conn.execution_options(isolation_level="READ COMMITTED")
            yield conn

    def _owner(self):
        principal = self.store._principal()
        payload = {"subject": principal.subject, "groups": sorted(principal.groups),
                   "scopes": sorted(principal.scopes)} if principal else {"operator": True}
        return inventory_digest(payload), principal.subject if principal else "operator"

    @contextmanager
    def _write_transaction(self):
        # Grant/fence reads after a source-lock wait need committed visibility.
        with self._current_read_connection() as conn, conn.begin():
            yield conn

    def _source(self, conn, tenant, connection_id, *, manage=False):
        _text(connection_id, "connection ID", 64)
        if manage:
            self.store._require_source_manager()
        source = (self.store._lock_source(conn, tenant, connection_id) if manage else
                  conn.execute(select(self.store.connections).where(
                      self.store.connections.c.tenant_id == tenant,
                      self.store.connections.c.id == connection_id,
                      self.store._access_clause(tenant, self.store.connections.c.id),
                  )).mappings().first())
        if source is None:
            raise KnowledgeNotFound("Inventory source was not found")
        if source["kind"] == "external_adapter" and source["config"].get("discovery_mode") == "inventory":
            from knowledge_layer.inventory_refresh import assert_inventory_execution

            # Worker stores reload operator policy; requester grants are resolved
            # under this locked source before each source IO and commit boundary.
            if manage:
                assert_inventory_execution(self.store, conn, tenant, source)
        if manage or not (self.store.source_policy and self.store.source_policy.metadata_permissions.enforce):
            self.store._require_source_approval(tenant, source["kind"], source["config"])
        else:
            self.store._require_source_identity_approval(tenant, source["kind"], source["config"])
        identity = source_identity(source["kind"], source["config"])
        approvals = self.store.source_policy.approvals if self.store.source_policy else None
        policy = sorted([{"subjects": sorted(a.subjects), "groups": sorted(a.groups)}
                         for a in approvals if a.tenant_id == tenant and a.identity == identity],
                        key=lambda item: _encoded(item)) if approvals is not None else None
        return dict(source), inventory_digest(identity), inventory_digest(policy)

    def _fence(self, conn, tenant, connection_id, claim):
        if claim:
            with refresh_fence(**claim):
                assert_refresh_fence(self.store, conn, tenant, connection_id)
        else:
            assert_refresh_fence(self.store, conn, tenant, connection_id)

    def _run(self, conn, tenant, connection_id, inventory_id, *, manage=False, claim=None):
        _text(inventory_id, "manifest ID", 64)
        source, identity, policy = self._source(conn, tenant, connection_id, manage=manage)
        query = select(self.runs).where(self.runs.c.id == inventory_id,
            self.runs.c.tenant_id == tenant, self.runs.c.connection_id == connection_id)
        row = conn.execute(query.with_for_update() if manage else query).mappings().first()
        if row is None:
            raise KnowledgeNotFound("Inventory was not found")
        row = dict(row)
        if source["kind"] == "external_adapter" and source["config"].get("discovery_mode") == "inventory":
            from knowledge_layer.connectors.common import ConnectorError
            from knowledge_layer.inventory_refresh import adapter_profile

            try:
                current_profile = adapter_profile(source["config"])[1]
            except ConnectorError:
                raise KnowledgeConflict("Current inventory adapter profile is unavailable") from None
            if row["policy"] != {"id": "external-adapter.inventory", "revision": current_profile}:
                raise KnowledgeConflict("Inventory adapter identity or revision changed")
            if manage and row["status"] == "staging" and any(
                    row["limits"][key] > maximum for key, maximum in source["config"]["inventory_limits"].items()):
                raise KnowledgeConflict("Inventory scan budgets were tightened")
        if identity != row["source_identity_digest"] or policy != row["source_policy_digest"]:
            raise KnowledgeConflict("Inventory source identity or approval policy changed")
        if (manage or row["status"] != "committed") and row["owner_digest"] != self._owner()[0]:
            raise KnowledgeNotFound("Inventory was not found")
        if manage:
            if row["claim"] != _claim(claim):
                raise KnowledgeConflict("Inventory refresh claim changed")
            self._fence(conn, tenant, connection_id, row["claim"])
        elif source["status"] == "error":
            raise KnowledgeConflict("Inventory source is currently unavailable")
        return row

    def _head_keys(self, tenant, connection_id, scope_digest, policy_digest):
        return (self.heads.c.tenant_id == tenant, self.heads.c.connection_id == connection_id,
                self.heads.c.scope_digest == scope_digest, self.heads.c.policy_digest == policy_digest)

    @staticmethod
    def _response(row):
        keys = {"id", "connection_id", "status", "scope", "scope_digest", "policy", "policy_digest",
                "watermark", "watermark_digest", "page_count", "entity_count", "evidence_count",
                "byte_count", "manifest_digest", "created_at", "completed_at", "next_cursor",
                "source_identity_digest", "source_policy_digest"}
        return {"schema_version": SCHEMA_VERSION, **{k: deepcopy(row[k]) for k in keys},
                "compatibility": {"legacy_snapshot": False, "meaning_certified": False,
                                  "agent_ready": False, "semantic_projection": "bounded_curation_required"}}

    def begin(self, tenant_id, connection_id, *, scope, policy, watermark, limits,
              expected_counts=None, claim=None):
        tenant = validate_tenant_id(tenant_id)
        pins = _pins(scope, policy, watermark)
        limits = _limits(limits)
        expected = {"pages": None, "entities": None, "evidence": None} if expected_counts is None else expected_counts
        _object(expected, {"pages", "entities", "evidence"}, "expected counts")
        for key, limit_key in {"pages": "max_pages", "entities": "max_entities", "evidence": "max_evidence"}.items():
            if expected[key] is not None:
                _int(expected[key], key, limits[limit_key], 1 if key == "pages" else 0)
        owner, subject = self._owner()
        claim = _claim(claim)
        with self._write_transaction() as conn:
            _source, identity, source_policy = self._source(conn, tenant, connection_id, manage=True)
            self._fence(conn, tenant, connection_id, claim)
            digests = {f"{key}_digest": inventory_digest(value) for key, value in pins.items()}
            head = conn.execute(select(self.heads).where(*self._head_keys(tenant, connection_id,
                digests["scope_digest"], digests["policy_digest"]))).mappings().first()
            predecessor = conn.execute(select(self.runs).where(self.runs.c.id == head["inventory_id"])).mappings().first() if head else None
            comparable = (predecessor is not None and predecessor["source_identity_digest"] == identity
                          and predecessor["source_policy_digest"] == source_policy)
            row = {"id": f"inventory_{uuid4().hex}", "tenant_id": tenant, "connection_id": connection_id,
                   "status": "staging", "owner_digest": owner, "owner_subject": subject, "claim": claim,
                   "source_identity_digest": identity, "source_policy_digest": source_policy,
                   **pins, **digests, "limits": limits, "expected_counts": deepcopy(expected),
                   "base_generation": head["generation"] if head else 0,
                   "previous_inventory_id": head["inventory_id"] if comparable else None,
                   "page_count": 0, "entity_count": 0, "evidence_count": 0, "byte_count": 0,
                   "next_cursor": None, "terminal_seen": 0,
                   "manifest_digest": inventory_digest({"schema_version": SCHEMA_VERSION,
                       "tenant_id": tenant, "connection_id": connection_id,
                       "source_identity_digest": identity, "source_policy_digest": source_policy,
                       **pins, "limits": limits, "expected_counts": expected}),
                   "created_at": utc_now(), "completed_at": None}
            conn.execute(insert(self.runs).values(**row))
            self.store._audit(conn, tenant, connection_id, "inventory.begin", subject,
                              {"inventory_id": row["id"], **digests})
            self._fence(conn, tenant, connection_id, claim)
        return self._response(row)

    def append(self, tenant_id, connection_id, inventory_id, *, page_index, cursor, next_cursor,
               entities, evidence, scope_digest, policy_digest, watermark_digest, claim=None):
        tenant = validate_tenant_id(tenant_id)
        _int(page_index, "page index", HARD_LIMITS["max_pages"] - 1)
        for value in (cursor, next_cursor):
            if value is not None:
                _text(value, "native cursor", 2048)
        for value in (scope_digest, policy_digest, watermark_digest):
            if not isinstance(value, str) or not _DIGEST.fullmatch(value):
                raise KnowledgeValidationError("Inventory page pins require exact digests")
        if cursor is not None and cursor == next_cursor:
            raise KnowledgeValidationError("Inventory cursor must advance")
        entities, evidence = _records(entities), _records(evidence, evidence=True)
        envelope = {"page_index": page_index, "cursor": cursor, "next_cursor": next_cursor,
                    "entities": entities, "evidence": evidence,
                    "scope_digest": scope_digest, "policy_digest": policy_digest,
                    "watermark_digest": watermark_digest}
        body = _encoded(envelope)
        if len(body) > MAX_PAGE_BYTES:
            raise KnowledgeValidationError("Inventory complete page exceeds byte budget")
        digest = hashlib.sha256(body).hexdigest()
        try:
            with self._write_transaction() as conn:
                row = self._run(conn, tenant, connection_id, inventory_id, manage=True, claim=claim)
                if row["status"] != "staging":
                    raise KnowledgeConflict("Inventory is no longer staging")
                for key in ("scope_digest", "policy_digest", "watermark_digest"):
                    if envelope[key] != row[key]:
                        raise KnowledgeConflict("Inventory page scope/policy/watermark pins changed")
                previous = conn.execute(select(self.pages.c.digest).where(
                    self.pages.c.inventory_id == inventory_id, self.pages.c.page_index == page_index,
                )).scalar_one_or_none()
                if previous is not None:
                    if previous != digest:
                        raise KnowledgeConflict("Inventory page replay changed content")
                    return {**self._response(row), "page_digest": digest, "replayed": True}
                if row["terminal_seen"] or page_index != row["page_count"] or cursor != row["next_cursor"]:
                    raise KnowledgeConflict("Inventory page is out of order or follows a terminal cursor")
                if next_cursor is not None and conn.execute(select(exists().where(
                    self.pages.c.inventory_id == inventory_id,
                    self.pages.c.cursor == next_cursor,
                ))).scalar_one():
                    raise KnowledgeConflict("Inventory cursor cycle detected")
                counts = {"page_count": row["page_count"] + 1, "entity_count": row["entity_count"] + len(entities),
                          "evidence_count": row["evidence_count"] + len(evidence),
                          "byte_count": row["byte_count"] + len(body)}
                for count, limit_key in [("page_count", "max_pages"), ("entity_count", "max_entities"),
                                         ("evidence_count", "max_evidence"), ("byte_count", "max_total_bytes")]:
                    if counts[count] > row["limits"][limit_key]:
                        raise KnowledgeValidationError("Inventory total budget exceeded")
                for table, records in [(self.entities, entities), (self.evidence, evidence)]:
                    if records:
                        if conn.execute(select(table.c.id).where(table.c.inventory_id == inventory_id,
                            table.c.id.in_([item["id"] for item in records])).limit(1)).first():
                            raise KnowledgeConflict("Inventory repeats an ID across pages")
                        conn.execute(insert(table), [{"inventory_id": inventory_id, "page_index": page_index,
                            **item, "digest": inventory_digest(item)} for item in records])
                conn.execute(insert(self.pages).values(inventory_id=inventory_id, page_index=page_index,
                    cursor=cursor, next_cursor=next_cursor, digest=digest, byte_count=len(body),
                    entity_count=len(entities), evidence_count=len(evidence)))
                changes = {**counts, "next_cursor": next_cursor, "terminal_seen": int(next_cursor is None),
                           "manifest_digest": hashlib.sha256((row["manifest_digest"] + digest).encode()).hexdigest()}
                conn.execute(update(self.runs).where(self.runs.c.id == inventory_id).values(**changes))
                row.update(changes)
                self._fence(conn, tenant, connection_id, row["claim"])
        except IntegrityError as exc:
            raise KnowledgeConflict("Inventory page conflicted with a concurrent append") from exc
        return {**self._response(row), "page_digest": digest, "replayed": False}

    def commit(self, tenant_id, connection_id, inventory_id, *, completion, claim=None, source_refresh=False):
        tenant = validate_tenant_id(tenant_id)
        if type(source_refresh) is not bool:
            raise KnowledgeValidationError("Inventory refresh flag must be explicit")
        _object(completion, {"listing_complete", "pages", "entities", "evidence", "manifest_digest"}, "completion")
        if completion["listing_complete"] is not True:
            raise KnowledgeValidationError("Inventory requires an explicit complete scoped listing")
        for key, maximum in {"pages": HARD_LIMITS["max_pages"], "entities": HARD_LIMITS["max_entities"],
                             "evidence": HARD_LIMITS["max_evidence"]}.items():
            _int(completion[key], key, maximum, 1 if key == "pages" else 0)
        with self._write_transaction() as conn:
            row = self._run(conn, tenant, connection_id, inventory_id, manage=True, claim=claim)
            if row["status"] != "staging" or not row["terminal_seen"] or not row["page_count"]:
                raise KnowledgeConflict("Inventory has no complete staged listing")
            if completion["manifest_digest"] != row["manifest_digest"]:
                raise KnowledgeConflict("Inventory complete manifest digest changed")
            for key, count in {"pages": "page_count", "entities": "entity_count", "evidence": "evidence_count"}.items():
                if completion[key] != row[count] or (row["expected_counts"][key] is not None
                                                    and row["expected_counts"][key] != row[count]):
                    raise KnowledgeConflict("Inventory complete counts disagree")
            # Stream bounded batches; evidence can be supplied on a later page.
            records = conn.execute(select(self.entities.c.evidence_ids).where(
                self.entities.c.inventory_id == inventory_id).execution_options(yield_per=100))
            for batch in records.scalars().partitions(100):
                refs = sorted({identifier for ids in batch for identifier in ids})
                found = set()
                for offset in range(0, len(refs), 500):
                    found.update(conn.execute(select(self.evidence.c.id).where(
                        self.evidence.c.inventory_id == inventory_id,
                        self.evidence.c.id.in_(refs[offset:offset + 500]))).scalars())
                if found != set(refs):
                    raise KnowledgeConflict("Inventory has unresolved exact evidence references")
            keys = self._head_keys(tenant, connection_id, row["scope_digest"], row["policy_digest"])
            head = conn.execute(select(self.heads).where(*keys).with_for_update()).mappings().first()
            if (head["generation"] if head else 0) != row["base_generation"]:
                raise KnowledgeConflict("A newer complete inventory superseded this staging generation")
            now = utc_now()
            values = {"inventory_id": inventory_id, "manifest_digest": row["manifest_digest"],
                      "generation": row["base_generation"] + 1, "last_complete_scope_at": now}
            if head:
                conn.execute(update(self.heads).where(*keys).values(**values))
            else:
                conn.execute(insert(self.heads).values(tenant_id=tenant, connection_id=connection_id,
                    scope_digest=row["scope_digest"], policy_digest=row["policy_digest"], **values))
            conn.execute(update(self.runs).where(self.runs.c.id == inventory_id).values(status="committed", completed_at=now))
            self.store._audit(conn, tenant, connection_id, "inventory.commit", row["owner_subject"],
                {"inventory_id": inventory_id, "manifest_digest": row["manifest_digest"],
                 "scope_digest": row["scope_digest"], "policy_digest": row["policy_digest"],
                 "pages": row["page_count"], "entities": row["entity_count"], "evidence": row["evidence_count"]})
            row.update(status="committed", completed_at=now)
            if source_refresh:
                source, _, _ = self._source(conn, tenant, connection_id, manage=True)
                if source["kind"] != "external_adapter" or source["config"].get("discovery_mode") != "inventory":
                    raise KnowledgeConflict("Source is not configured for inventory refresh")
                from knowledge_layer.refresh_fence import record_refresh_inventory

                # This status cannot activate an old legacy semantic publication.
                conn.execute(update(self.store.connections).where(
                    self.store.connections.c.tenant_id == tenant,
                    self.store.connections.c.id == connection_id,
                ).values(status="inventory_complete", last_error=None, updated_at=now))
                record_refresh_inventory(self.store, conn, tenant, connection_id, inventory_id)
            self._fence(conn, tenant, connection_id, row["claim"])
        return {**self._response(row), "generation": values["generation"], "last_complete_scope_at": now}

    def current(self, tenant_id, connection_id, *, scope_digest, policy_digest):
        tenant = validate_tenant_id(tenant_id)
        for value in (scope_digest, policy_digest):
            if not isinstance(value, str) or not _DIGEST.fullmatch(value):
                raise KnowledgeValidationError("Inventory selectors require exact scope/policy digests")
        with self.store.engine.connect() as conn:
            self._source(conn, tenant, connection_id)
            head = conn.execute(select(self.heads).where(*self._head_keys(tenant, connection_id,
                scope_digest, policy_digest))).mappings().first()
            record_source_read(tenant, connection_id)
            if head is None:
                return None
            row = self._run(conn, tenant, connection_id, head["inventory_id"])
            return {**self._response(row), "generation": head["generation"],
                    "last_complete_scope_at": head["last_complete_scope_at"]}

    def status(self, tenant_id, connection_id, inventory_id):
        tenant = validate_tenant_id(tenant_id)
        with self.store.engine.connect() as conn:
            row = self._run(conn, tenant, connection_id, inventory_id)
            record_source_read(tenant, connection_id)
            return self._response(row)

    def _current_committed(self, conn, tenant, connection_id, inventory_id, expected_manifest_digest=None):
        row = self._run(conn, tenant, connection_id, inventory_id)
        if row["status"] != "committed":
            raise KnowledgeConflict("Inventory selection requires a committed current manifest")
        head = conn.execute(select(self.heads).where(*self._head_keys(
            tenant, connection_id, row["scope_digest"], row["policy_digest"]))).mappings().first()
        if (head is None or head["inventory_id"] != inventory_id
                or head["manifest_digest"] != row["manifest_digest"]
                or (expected_manifest_digest is not None and row["manifest_digest"] != expected_manifest_digest)):
            raise KnowledgeConflict("Inventory current manifest changed; reload discovery")
        return row, dict(head)

    def scopes(self, tenant_id, connection_id, *, after=None, limit=10):
        """Discover bounded current scope handles without loading estate records."""
        tenant = validate_tenant_id(tenant_id)
        _int(limit, "scope page limit", 20, 1)
        marker = None
        if after is not None:
            try:
                _text(after, "scope cursor", 1024)
                raw = base64.b64decode(after.encode("ascii"), altchars=b"-_", validate=True)
                token = json.loads(raw, object_pairs_hook=_unique_json)
                _object(token, {"tenant_id", "connection_id", "after"}, "scope cursor")
                if (token["tenant_id"] != tenant or token["connection_id"] != connection_id
                        or not isinstance(token["after"], list) or len(token["after"]) != 2
                        or any(not isinstance(v, str) or not _DIGEST.fullmatch(v) for v in token["after"])
                        or base64.urlsafe_b64encode(raw).decode("ascii") != after):
                    raise ValueError
                marker = token["after"]
            except (ValueError, UnicodeError, TypeError, RecursionError) as exc:
                raise KnowledgeValidationError("Inventory scope cursor is invalid or mismatched") from exc
        with self._current_read_connection() as conn:
            source, _, _ = self._source(conn, tenant, connection_id)
            if source["status"] == "error":
                raise KnowledgeConflict("Inventory source is currently unavailable")
            query = select(self.heads).where(self.heads.c.tenant_id == tenant,
                                             self.heads.c.connection_id == connection_id)
            if marker is not None:
                if conn.execute(query.where(self.heads.c.scope_digest == marker[0],
                    self.heads.c.policy_digest == marker[1]).limit(1)).first() is None:
                    raise KnowledgeValidationError("Inventory scope cursor does not resolve")
                query = query.where(or_(self.heads.c.scope_digest > marker[0],
                    and_(self.heads.c.scope_digest == marker[0], self.heads.c.policy_digest > marker[1])))
            fetched = conn.execute(query.order_by(self.heads.c.scope_digest,
                self.heads.c.policy_digest).limit(limit + 1)).mappings().all()
            items = []
            for head in fetched[:limit]:
                row, observed = self._current_committed(conn, tenant, connection_id, head["inventory_id"])
                if observed["generation"] != head["generation"]:
                    raise KnowledgeConflict("Inventory current manifest changed; reload discovery")
                items.append({**self._response(row), "generation": observed["generation"],
                              "last_complete_scope_at": observed["last_complete_scope_at"]})
            next_cursor = None
            if len(fetched) > limit:
                last = fetched[limit - 1]
                next_cursor = base64.urlsafe_b64encode(_encoded({"tenant_id": tenant,
                    "connection_id": connection_id, "after": [last["scope_digest"], last["policy_digest"]]})).decode("ascii")
            result = {"schema_version": SCHEMA_VERSION, "connection_id": connection_id,
                      "items": items, "next_cursor": next_cursor,
                      "coverage": "current_discovery_scopes_for_source", "meaning_certified": False,
                      "agent_ready": False, "source_reads_executed": False}
            if len(_encoded(result)) > MAX_PAGE_BYTES:
                raise KnowledgeValidationError("Inventory scope response exceeds 256 KiB; narrow the page")
            # A READ COMMITTED recheck refuses changes while building a packet.
            source, _, _ = self._source(conn, tenant, connection_id)
            if source["status"] == "error":
                raise KnowledgeConflict("Inventory source is currently unavailable")
            for item in items:
                _, head = self._current_committed(conn, tenant, connection_id, item["id"], item["manifest_digest"])
                if head["generation"] != item["generation"]:
                    raise KnowledgeConflict("Inventory current manifest changed; reload discovery")
            record_source_read(tenant, connection_id)
            return result

    def select(self, tenant_id, connection_id, inventory_id, *, expected_manifest_digest, entity_ids):
        """Read an exact current selection and its complete same-manifest evidence.

        This is discovery material for a customer harness, never a semantic
        snapshot, publication, company instruction or source execution grant.
        """
        tenant = validate_tenant_id(tenant_id)
        if not isinstance(expected_manifest_digest, str) or not _DIGEST.fullmatch(expected_manifest_digest):
            raise KnowledgeValidationError("Inventory selection requires an exact manifest digest")
        if not isinstance(entity_ids, list) or not 1 <= len(entity_ids) <= 20:
            raise KnowledgeValidationError("Inventory selection requires 1-20 distinct entity IDs")
        for identifier in entity_ids:
            _text(identifier, "selected entity ID")
        if len(set(entity_ids)) != len(entity_ids):
            raise KnowledgeValidationError("Inventory selection requires distinct entity IDs")
        with self._current_read_connection() as conn:
            row, head = self._current_committed(conn, tenant, connection_id, inventory_id, expected_manifest_digest)
            entities, evidence, size, references = [], [], 0, set()
            query = select(self.entities).where(self.entities.c.inventory_id == inventory_id,
                self.entities.c.id.in_(entity_ids)).order_by(self.entities.c.id).execution_options(yield_per=1)
            for stored in conn.execute(query).mappings():
                public = {k: deepcopy(stored[k]) for k in ("id", "kind", "payload", "evidence_ids")}
                _records([public])
                if inventory_digest(public) != stored["digest"]:
                    raise KnowledgeConflict("Inventory record integrity check failed")
                public["digest"] = stored["digest"]
                size += len(_encoded(public))
                if size > MAX_PAGE_BYTES:
                    raise KnowledgeValidationError("Inventory complete selection exceeds 256 KiB; narrow the selection")
                entities.append(public)
                references.update(public["evidence_ids"])
            if len(entities) != len(entity_ids):
                raise KnowledgeNotFound("Selected inventory entity was not found")
            if len(references) > 200:
                raise KnowledgeValidationError("Inventory selection exceeds 200 evidence records; narrow the selection")
            if references:
                query = select(self.evidence).where(self.evidence.c.inventory_id == inventory_id,
                    self.evidence.c.id.in_(references)).order_by(self.evidence.c.id).execution_options(yield_per=1)
                for stored in conn.execute(query).mappings():
                    public = {k: deepcopy(stored[k]) for k in ("id", "kind", "payload")}
                    _records([public], evidence=True)
                    if inventory_digest(public) != stored["digest"]:
                        raise KnowledgeConflict("Inventory record integrity check failed")
                    public["digest"] = stored["digest"]
                    size += len(_encoded(public))
                    if size > MAX_PAGE_BYTES:
                        raise KnowledgeValidationError("Inventory complete selection exceeds 256 KiB; narrow the selection")
                    evidence.append(public)
            if len(evidence) != len(references):
                raise KnowledgeConflict("Inventory selection evidence is unavailable")
            result = {"schema_version": "knowledge-inventory-selection-v1", "connection_id": connection_id,
                      "inventory": {**self._response(row), "generation": head["generation"],
                                    "last_complete_scope_at": head["last_complete_scope_at"]},
                      "entities": entities, "evidence": evidence, "text_complete": True,
                      "meaning_certified": False, "agent_ready": False, "source_reads_executed": False,
                      "source_text_authority": "untrusted_evidence"}
            if len(_encoded(result)) > MAX_PAGE_BYTES:
                raise KnowledgeValidationError("Inventory complete selection exceeds 256 KiB; narrow the selection")
            _, observed = self._current_committed(conn, tenant, connection_id, inventory_id, expected_manifest_digest)
            if observed["generation"] != head["generation"]:
                raise KnowledgeConflict("Inventory current manifest changed; reload discovery")
            record_source_read(tenant, connection_id)
            return result

    def abort(self, tenant_id, connection_id, inventory_id, *, claim=None):
        tenant = validate_tenant_id(tenant_id)
        with self._write_transaction() as conn:
            row = self._run(conn, tenant, connection_id, inventory_id, manage=True, claim=claim)
            if row["status"] != "staging":
                raise KnowledgeConflict("Only a staging inventory can be aborted")
            conn.execute(update(self.runs).where(self.runs.c.id == inventory_id).values(status="aborted"))
            row["status"] = "aborted"
            self.store._audit(conn, tenant, connection_id, "inventory.abort", row["owner_subject"],
                              {"inventory_id": inventory_id})
            self._fence(conn, tenant, connection_id, row["claim"])
        return self._response(row)

    def page(self, tenant_id, connection_id, inventory_id, *, collection="entities", after=None, limit=100):
        """Exact row text, bounded keyset output; old manifests remain labeled historical."""
        tenant = validate_tenant_id(tenant_id)
        _int(limit, "read page limit", 100, 1)
        if collection not in {"entities", "evidence", "removed"}:
            raise KnowledgeValidationError("Inventory collection is unsupported")
        marker = None
        if after is not None:
            try:
                _text(after, "read cursor", 1024)
                token = json.loads(base64.urlsafe_b64decode(after.encode("ascii")), object_pairs_hook=_unique_json)
                _object(token, {"inventory_id", "collection", "after"}, "read cursor")
                if token["inventory_id"] != inventory_id or token["collection"] != collection:
                    raise ValueError
                marker = _text(token["after"], "read cursor ID")
            except (ValueError, UnicodeError, TypeError) as exc:
                raise KnowledgeValidationError("Inventory read cursor is invalid or mismatched") from exc
        with self.store.engine.connect() as conn:
            row = self._run(conn, tenant, connection_id, inventory_id)
            table = self.evidence if collection == "evidence" else self.entities
            target_id = inventory_id
            if collection == "removed":
                if row["status"] != "committed" or row["previous_inventory_id"] is None:
                    raise KnowledgeConflict("Inventory has no comparable complete predecessor")
                target_id = row["previous_inventory_id"]
            query = select(table).where(table.c.inventory_id == target_id)
            if collection == "removed":
                current = self.entities.alias("current_inventory_entities")
                query = query.where(~exists(select(current.c.id).where(
                    current.c.inventory_id == inventory_id, current.c.id == table.c.id)))
            if marker is not None:
                if conn.execute(query.where(table.c.id == marker).limit(1)).first() is None:
                    raise KnowledgeValidationError("Inventory read cursor does not resolve")
                query = query.where(table.c.id > marker)
            fetched = conn.execute(query.order_by(table.c.id).limit(limit + 1)).mappings().all()
            items, size = [], 0
            for item in fetched[:limit]:
                public = {k: deepcopy(item[k]) for k in ("id", "kind", "payload", "digest")}
                if collection != "evidence":
                    public["evidence_ids"] = deepcopy(item["evidence_ids"])
                width = len(_encoded(public))
                if size + width > MAX_PAGE_BYTES - 4096:
                    break
                items.append(public)
                size += width
            next_cursor = None
            if len(fetched) > len(items):
                if not items:
                    raise KnowledgeValidationError("Inventory exact row exceeds read response budget")
                next_cursor = base64.urlsafe_b64encode(_encoded({"inventory_id": inventory_id,
                    "collection": collection, "after": items[-1]["id"]})).decode("ascii")
            head = conn.execute(select(self.heads.c.inventory_id).where(*self._head_keys(
                tenant, connection_id, row["scope_digest"], row["policy_digest"]))).scalar_one_or_none()
            record_source_read(tenant, connection_id)
            return {"schema_version": SCHEMA_VERSION, "inventory_id": inventory_id, "collection": collection,
                    "status": row["status"], "current": head == inventory_id, "items": items,
                    "next_cursor": next_cursor, "text_complete": True,
                    "semantic_projection": "bounded_curation_required", "agent_ready": False}
