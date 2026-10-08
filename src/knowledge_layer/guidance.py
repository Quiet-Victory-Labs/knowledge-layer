"""Reviewed company procedures and advisory rules, independent of agent harnesses.

Only explicit manager-authored drafts and steward publications enter this registry.
Source documents remain evidence. Prose and declared capabilities never grant tool
permission, enforce another harness's behavior, or make source writes permissible.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, insert, select, text, update

from knowledge_layer.access import Principal
from knowledge_layer.inventory_citations import inventory_citation_operation
from knowledge_layer.store import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeStore,
    KnowledgeValidationError,
    validate_tenant_id,
)

MAX_BUNDLES = 100
MAX_REVISIONS = 100
MAX_PAYLOAD_BYTES = 32 * 1024
MAX_RESOLUTION_BYTES = 256 * 1024
MAX_RESOLUTION_SOURCES = 100
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _keys(value: Any, required: set[str], optional: set[str] | None = None) -> None:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - (optional or set()):
        raise KnowledgeValidationError("Company guidance has missing or unknown fields")


def _text(value: Any, name: str, maximum: int, *, multiline: bool = False) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or _CONTROLS.search(value) or (not multiline and ("\n" in value or "\t" in value or "\r" in value))):
        raise KnowledgeValidationError(f"Guidance {name} must be nonblank bounded text")
    return value


def _slug(value: Any, maximum: int = 64) -> str:
    if not isinstance(value, str) or len(value) > maximum or not _SLUG.fullmatch(value):
        raise KnowledgeValidationError("Guidance identifiers must be bounded lowercase kebab names")
    return value


def _strings(value: Any, name: str, maximum: int, width: int = 120) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise KnowledgeValidationError(f"Guidance {name} exceeds its list bound")
    result = [_text(item, name, width) for item in value]
    if len(set(result)) != len(result):
        raise KnowledgeValidationError(f"Guidance {name} must be distinct")
    return result


def _integer(value: Any, name: str, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= 2147483647:
        raise KnowledgeValidationError(f"Guidance {name} must be a bounded integer")
    return value


def _clock(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise KnowledgeValidationError("Guidance validity clocks must be aware ISO timestamps")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        raise KnowledgeValidationError("Guidance validity clocks must be aware ISO timestamps") from None


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _now(value: datetime | None = None) -> datetime:
    value = value if value is not None else datetime.now(UTC)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise KnowledgeValidationError("An aware guidance clock is required")
    return value.astimezone(UTC)


def _scalar(value: Any) -> Any:
    if value is None or type(value) is bool:
        return value
    if type(value) is int and -(2 ** 63) <= value < 2 ** 63:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, str) and len(value) <= 500 and not re.search(r"[\x00-\x1f\x7f]", value):
        return value
    raise KnowledgeValidationError("Guidance rule value must be a bounded JSON scalar")


def _encoded(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise KnowledgeValidationError("Guidance must contain valid bounded JSON text") from None


def guidance_digest(payload: dict[str, Any]) -> str:
    """SHA256 of the exact validated payload's canonical UTF-8 JSON."""
    return hashlib.sha256(_encoded(payload)).hexdigest()


def validate_guidance_payload(payload: Any) -> dict[str, Any]:
    _keys(payload, {"title", "description", "business_units", "tasks", "audience", "skills", "rules", "dependencies"},
          {"connection_ids", "effective_from", "effective_until"})
    result = {
        "title": _text(payload["title"], "title", 120),
        "description": _text(payload["description"], "description", 1000, multiline=True),
        "business_units": _strings(payload["business_units"], "business units", 20),
        "tasks": _strings(payload["tasks"], "tasks", 20),
        "connection_ids": _strings(payload.get("connection_ids", []), "source scope", 20, 64),
    }
    audience = payload["audience"]
    _keys(audience, {"company_wide", "subjects", "groups"})
    if type(audience["company_wide"]) is not bool:
        raise KnowledgeValidationError("Guidance company_wide must be boolean")
    result["audience"] = {
        "company_wide": audience["company_wide"],
        "subjects": _strings(audience["subjects"], "audience subjects", 100, 255),
        "groups": _strings(audience["groups"], "audience groups", 100, 255),
    }
    if not audience["company_wide"] and not (audience["subjects"] or audience["groups"]):
        raise KnowledgeValidationError("Guidance must identify an audience")
    if not isinstance(payload["skills"], list) or len(payload["skills"]) > 8:
        raise KnowledgeValidationError("Guidance permits at most 8 skills")
    skills = []
    for skill in payload["skills"]:
        _keys(skill, {"name", "description", "instructions", "required_capabilities"})
        capabilities = _strings(skill["required_capabilities"], "capabilities", 20)
        if any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,119}", item) for item in capabilities):
            raise KnowledgeValidationError("Guidance capabilities must be descriptive identifiers")
        skills.append({"name": _slug(skill["name"], 64),
                       "description": _text(skill["description"], "skill description", 1000),
                       "instructions": _text(skill["instructions"], "skill instructions", 4000, multiline=True),
                       "required_capabilities": capabilities})
    if len({skill["name"] for skill in skills}) != len(skills):
        raise KnowledgeValidationError("Guidance skill names must be distinct")
    result["skills"] = skills
    if not isinstance(payload["rules"], list) or len(payload["rules"]) > 50:
        raise KnowledgeValidationError("Guidance permits at most 50 advisory rules")
    rules = []
    for rule in payload["rules"]:
        _keys(rule, {"key", "statement", "value"})
        rules.append({"key": _text(rule["key"], "rule key", 120),
                      "statement": _text(rule["statement"], "rule statement", 1000, multiline=True),
                      "value": _scalar(rule["value"])})
    if len({rule["key"] for rule in rules}) != len(rules):
        raise KnowledgeValidationError("Guidance rule keys must be distinct within a bundle")
    result["rules"] = rules
    if not skills and not rules:
        raise KnowledgeValidationError("Guidance needs at least one procedure or advisory rule")
    if not isinstance(payload["dependencies"], list) or len(payload["dependencies"]) > 10:
        raise KnowledgeValidationError("Guidance permits at most 10 pinned dependencies")
    dependencies = []
    for dependency in payload["dependencies"]:
        _keys(dependency, {"connection_id", "snapshot_id", "snapshot_version"})
        dependencies.append({
            "connection_id": _text(dependency["connection_id"], "dependency source", 64),
            "snapshot_id": _text(dependency["snapshot_id"], "dependency snapshot", 128),
            "snapshot_version": _integer(dependency["snapshot_version"], "snapshot version"),
        })
    if len({item["connection_id"] for item in dependencies}) != len(dependencies):
        raise KnowledgeValidationError("Guidance pins at most one snapshot per dependency source")
    result["dependencies"] = dependencies
    for key in ("effective_from", "effective_until"):
        if key in payload:
            result[key] = _stamp(_clock(payload[key]))
    if ("effective_from" in result and "effective_until" in result
            and _clock(result["effective_until"]) <= _clock(result["effective_from"])):
        raise KnowledgeValidationError("Guidance effective_until must follow effective_from")
    if len(_encoded(result)) > MAX_PAYLOAD_BYTES:
        raise KnowledgeValidationError("Guidance payload exceeds the 32 KiB limit")
    return result


def _audience(payload: dict[str, Any], principal: Principal | None) -> bool:
    audience = payload["audience"]
    return principal is None or audience["company_wide"] or principal.subject in audience["subjects"] or bool(
        principal.groups.intersection(audience["groups"]))


def _scope(payload: dict[str, Any], business_unit: str | None, task: str | None,
           connection_ids: list[str] | None) -> bool:
    return (
        (not payload["business_units"] or business_unit in payload["business_units"])
        and (not payload["tasks"] or task in payload["tasks"])
        and (not payload["connection_ids"] or bool(set(payload["connection_ids"]).intersection(connection_ids or [])))
    )


def _rule_identity(value: Any) -> tuple[str, str]:
    if type(value) in (int, float):
        return "number", str(Decimal(str(value)).normalize())
    return type(value).__name__, _encoded(value).decode("utf-8")


class GuidanceRegistry:
    def __init__(self, store: KnowledgeStore):
        self.store = store
        self.bundles = store.guidance_bundles
        self.revisions = store.guidance_revisions

    def _lock(self, conn, tenant: str, slug: str, *, capacity: bool = False) -> None:
        if self.store.backend == "postgresql":
            if capacity:
                conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                             {"key": f"knowledge_guidance_capacity:{tenant}"})
            conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                         {"key": f"knowledge_guidance:{tenant}:{slug}"})

    def _bundle(self, conn, tenant: str, slug: str) -> dict[str, Any] | None:
        row = conn.execute(select(self.bundles).where(
            self.bundles.c.tenant_id == tenant, self.bundles.c.slug == slug,
        ).with_for_update()).mappings().first()
        return dict(row) if row else None

    def _revision(self, conn, tenant: str, slug: str, revision: int) -> dict[str, Any] | None:
        row = conn.execute(select(self.revisions).where(
            self.revisions.c.tenant_id == tenant, self.revisions.c.slug == slug,
            self.revisions.c.revision == revision,
        )).mappings().first()
        return dict(row) if row else None

    @staticmethod
    def _state(bundle: dict[str, Any], revision: int) -> str:
        if bundle["published_revision"] == revision:
            return "revoked" if bundle["revoked_at"] else "published"
        return "draft" if bundle["latest_revision"] == revision else "historical"

    def _record(self, bundle: dict[str, Any], revision: dict[str, Any], *,
                reasons: list[str] | None = None) -> dict[str, Any]:
        state = self._state(bundle, revision["revision"])
        return {"slug": bundle["slug"], "revision": revision["revision"], "digest": revision["digest"],
                "payload": revision["payload"], "state": state,
                "created_at": revision["created_at"], "created_by": revision["created_by"],
                "published_revision": bundle["published_revision"],
                "publication": {"published_at": bundle["published_at"], "published_by": bundle["published_by"],
                                "rationale": bundle["publication_rationale"], "revoked_at": bundle["revoked_at"],
                                "revoked_by": bundle["revoked_by"], "revoke_reason": bundle["revoke_reason"]},
                "use_as_instructions": state == "published" and not reasons,
                "blocked_reasons": reasons or []}

    def _readable(self, tenant: str, payload: dict[str, Any]) -> bool:
        ids = {*payload["connection_ids"], *(item["connection_id"] for item in payload["dependencies"])}
        return all(self.store._has_access(tenant, source_id) for source_id in sorted(ids))

    def _manage_scopes(self, tenant: str, payload: dict[str, Any], *, require_existing: bool = True) -> None:
        for connection_id in payload["connection_ids"]:
            self.store._require_access(tenant, connection_id, manage=True)
            if require_existing and self.store.get_connection(tenant, connection_id) is None:
                raise KnowledgeNotFound("Guidance source scope was not found")

    def _editor(self, tenant: str, payload: dict[str, Any], principal: Principal | None,
                author: str) -> bool:
        if principal is None:
            return True
        if author != "operator" and principal.subject == author:
            return True
        try:
            self.store._require_source_manager()
            self._manage_scopes(tenant, payload)
        except (KnowledgeAccessDenied, KnowledgeNotFound):
            return False
        return True

    def _dependency_reasons(self, tenant: str, payload: dict[str, Any],
                            cache: dict[str, dict[str, Any] | None] | None = None) -> list[str]:
        cache = cache if cache is not None else {}
        reasons = set()
        source_ids = {*payload["connection_ids"], *(item["connection_id"] for item in payload["dependencies"])}
        for source_id in sorted(source_ids):
            if source_id not in cache:
                source = self.store.get_connection(tenant, source_id)
                snapshot = (source or {}).get("snapshot") or {}
                cache[source_id] = ({"status": source["status"], "error": bool(source.get("last_error")),
                                     "snapshot_id": snapshot.get("id"), "version": snapshot.get("version"),
                                     "agent_ready": snapshot.get("agent_ready") is True} if source else None)
            if source_id in payload["connection_ids"] and cache[source_id] is None:
                reasons.add("source_scope_missing")
        for dependency in payload["dependencies"]:
            current = cache[dependency["connection_id"]]
            if current is None:
                reasons.add("source_dependency_missing")
            elif current["status"] == "error" or current["error"]:
                reasons.add("source_dependency_error")
            elif current["snapshot_id"] != dependency["snapshot_id"] or current["version"] != dependency["snapshot_version"]:
                reasons.add("source_dependency_stale")
            elif not current["agent_ready"]:
                reasons.add("source_dependency_unpublished")
        return sorted(reasons)

    @staticmethod
    def _validity(payload: dict[str, Any], now: datetime) -> list[str]:
        if "effective_from" in payload and now < _clock(payload["effective_from"]):
            return ["not_yet_effective"]
        if "effective_until" in payload and now >= _clock(payload["effective_until"]):
            return ["expired"]
        return []

    def draft(self, tenant: str, slug: str, payload: dict[str, Any], *,
              expected_revision: int = 0) -> dict[str, Any]:
        tenant, slug = validate_tenant_id(tenant), _slug(slug)
        expected_revision = _integer(expected_revision, "expected revision", 0)
        principal = self.store._require_source_manager()
        clean = validate_guidance_payload(payload)
        self._manage_scopes(tenant, clean)
        if not self._readable(tenant, clean):
            raise KnowledgeNotFound("Guidance source dependencies were not found")
        actor = principal.subject if principal else "operator"
        now, digest = _stamp(_now()), guidance_digest(clean)
        with self.store.engine.begin() as conn:
            self._lock(conn, tenant, slug, capacity=True)
            bundle = self._bundle(conn, tenant, slug)
            if bundle:
                for previous_revision in {bundle["latest_revision"], bundle["published_revision"]} - {None}:
                    previous = self._revision(conn, tenant, slug, previous_revision)
                    if (previous is None or not _audience(previous["payload"], principal)
                            or not self._readable(tenant, previous["payload"])):
                        raise KnowledgeNotFound("Guidance was not found")
                    self._manage_scopes(tenant, previous["payload"], require_existing=principal is not None)
            current = bundle["latest_revision"] if bundle else 0
            if current != expected_revision:
                raise KnowledgeConflict("Guidance changed; fetch the current revision before drafting")
            if current >= MAX_REVISIONS:
                raise KnowledgeConflict("Guidance revision capacity reached; preserve and review its history")
            if bundle is None:
                count = conn.execute(select(func.count()).select_from(self.bundles).where(
                    self.bundles.c.tenant_id == tenant,
                )).scalar_one()
                if count >= MAX_BUNDLES:
                    raise KnowledgeConflict("Company guidance bundle capacity reached")
                bundle = {"tenant_id": tenant, "slug": slug, "latest_revision": 1,
                          "published_revision": None, "published_digest": None, "published_at": None,
                          "published_by": None, "publication_rationale": None, "revoked_at": None,
                          "revoked_by": None, "revoke_reason": None, "created_at": now, "updated_at": now}
                conn.execute(insert(self.bundles).values(**bundle))
            else:
                bundle.update(latest_revision=current + 1, updated_at=now)
                conn.execute(update(self.bundles).where(
                    self.bundles.c.tenant_id == tenant, self.bundles.c.slug == slug,
                ).values(latest_revision=current + 1, updated_at=now))
            revision = {"tenant_id": tenant, "slug": slug, "revision": current + 1,
                        "payload": clean, "digest": digest, "created_at": now, "created_by": actor}
            conn.execute(insert(self.revisions).values(**revision))
            self.store._audit(conn, tenant, None, "guidance.draft", actor,
                              {"slug": slug, "revision": current + 1, "digest": digest})
        return self._record(bundle, revision)

    def publish(self, tenant: str, slug: str, *, revision: int, expected_digest: str,
                rationale: str) -> dict[str, Any]:
        tenant, slug = validate_tenant_id(tenant), _slug(slug)
        revision = _integer(revision, "revision")
        rationale = _text(rationale, "publication rationale", 1000, multiline=True)
        if len(rationale.strip()) < 16:
            raise KnowledgeValidationError("Guidance publication rationale must be 16-1000 characters")
        if not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
            raise KnowledgeValidationError("A guidance content digest is required")
        actor = self.store._publication_actor()
        with self.store.engine.begin() as conn:
            self._lock(conn, tenant, slug)
            bundle = self._bundle(conn, tenant, slug)
            item = self._revision(conn, tenant, slug, revision)
            if bundle is None or item is None:
                raise KnowledgeNotFound("Guidance revision was not found")
            if bundle["latest_revision"] != revision or item["digest"] != expected_digest or guidance_digest(item["payload"]) != expected_digest:
                raise KnowledgeConflict("Guidance revision or content changed before publication")
            clean = validate_guidance_payload(item["payload"])
            principal = self.store._principal()
            if not _audience(clean, principal) or not self._readable(tenant, clean):
                raise KnowledgeNotFound("Guidance was not found")
            self._manage_scopes(tenant, clean)
            source_ids = sorted({*clean["connection_ids"], *(dep["connection_id"] for dep in clean["dependencies"])})
            if source_ids:
                conn.execute(select(self.store.connections.c.id).where(
                    self.store.connections.c.tenant_id == tenant,
                    self.store.connections.c.id.in_(source_ids),
                ).order_by(self.store.connections.c.id).with_for_update()).all()
            reasons = self._dependency_reasons(tenant, clean)
            if reasons:
                raise KnowledgeConflict("Guidance publication blocked: " + ", ".join(reasons))
            if bundle["published_revision"] == revision:
                if bundle["revoked_at"]:
                    raise KnowledgeConflict("Withdrawn guidance needs a new draft revision")
                return self._record(bundle, item, reasons=self._validity(clean, _now()))
            now = _stamp(_now())
            changes = {"published_revision": revision, "published_digest": expected_digest,
                       "published_at": now, "published_by": actor, "publication_rationale": rationale,
                       "revoked_at": None, "revoked_by": None, "revoke_reason": None, "updated_at": now}
            conn.execute(update(self.bundles).where(
                self.bundles.c.tenant_id == tenant, self.bundles.c.slug == slug,
            ).values(**changes))
            bundle.update(changes)
            self.store._audit(conn, tenant, None, "guidance.publish", actor,
                              {"slug": slug, "revision": revision, "digest": expected_digest})
        return self._record(bundle, item, reasons=self._validity(clean, _now()))

    def revoke(self, tenant: str, slug: str, *, revision: int, reason: str) -> dict[str, Any]:
        tenant, slug = validate_tenant_id(tenant), _slug(slug)
        revision = _integer(revision, "revision")
        reason = _text(reason, "withdrawal reason", 1000, multiline=True)
        if len(reason.strip()) < 16:
            raise KnowledgeValidationError("Guidance withdrawal reason must be 16-1000 characters")
        actor = self.store._publication_actor()
        with self.store.engine.begin() as conn:
            self._lock(conn, tenant, slug)
            bundle = self._bundle(conn, tenant, slug)
            item = self._revision(conn, tenant, slug, revision)
            if bundle is None or item is None:
                raise KnowledgeNotFound("Guidance revision was not found")
            if bundle["published_revision"] != revision:
                raise KnowledgeConflict("Guidance publication changed before withdrawal")
            principal = self.store._principal()
            if not _audience(item["payload"], principal):
                raise KnowledgeNotFound("Guidance was not found")
            if principal is None:
                # An offline operator can repair a publication whose declared
                # source was deleted. Verified callers still need fresh manage
                # authority for every declared scope, held through withdrawal.
                self._manage_scopes(tenant, item["payload"], require_existing=False)
            else:
                for connection_id in sorted(item["payload"]["connection_ids"]):
                    self.store._lock_source(conn, tenant, connection_id, manage=True)
            # Withdrawal reduces authority. Losing a dependency read grant must
            # not prevent it, and this operation returns no retained content.
            if not bundle["revoked_at"]:
                now = _stamp(_now())
                changes = {"revoked_at": now, "revoked_by": actor, "revoke_reason": reason, "updated_at": now}
                conn.execute(update(self.bundles).where(
                    self.bundles.c.tenant_id == tenant, self.bundles.c.slug == slug,
                ).values(**changes))
                bundle.update(changes)
                self.store._audit(conn, tenant, None, "guidance.revoke", actor,
                                  {"slug": slug, "revision": revision, "digest": item["digest"]})
        return {"slug": slug, "revision": revision, "digest": item["digest"],
                "state": "revoked", "published_revision": bundle["published_revision"],
                "publication": {"revoked_at": bundle["revoked_at"], "revoked_by": bundle["revoked_by"]},
                "use_as_instructions": False, "blocked_reasons": ["revoked"]}

    def get(self, tenant: str, slug: str, revision: int | None = None) -> dict[str, Any] | None:
        tenant, slug = validate_tenant_id(tenant), _slug(slug)
        principal = self.store._principal()
        if revision is not None:
            revision = _integer(revision, "revision")
        with self.store.engine.connect() as conn:
            bundle = self._bundle(conn, tenant, slug)
            if bundle is None:
                return None
            latest = self._revision(conn, tenant, slug, bundle["latest_revision"])
            if latest is None:
                raise KnowledgeConflict("Guidance revision history is incomplete")
            latest_reviewable = (_audience(latest["payload"], principal) and self._readable(tenant, latest["payload"])
                                 and self._editor(tenant, latest["payload"], principal, latest["created_by"]))
            chosen = revision if revision is not None else bundle["latest_revision"] if latest_reviewable else bundle["published_revision"]
            if chosen is None:
                return None
            item = self._revision(conn, tenant, slug, chosen)
        if item is None:
            return None
        payload = item["payload"]
        if not _audience(payload, principal) or not self._readable(tenant, payload):
            return None
        reviewable = self._editor(tenant, payload, principal, item["created_by"])
        state = self._state(bundle, chosen)
        if state != "published" and not reviewable:
            return None
        reasons = []
        if guidance_digest(payload) != item["digest"] or (state == "published" and bundle["published_digest"] != item["digest"]):
            reasons.append("guidance_integrity_failed")
        reasons.extend(self._validity(payload, _now()))
        reasons.extend(self._dependency_reasons(tenant, payload))
        if state == "revoked":
            reasons.append("revoked")
        record = self._record(bundle, item, reasons=sorted(set(reasons)))
        if reasons and not reviewable:
            return {key: record[key] for key in ("slug", "revision", "digest", "blocked_reasons", "use_as_instructions")} | {"state": "blocked"}
        return record

    @inventory_citation_operation()
    def resolve(self, tenant: str, *, business_unit: str | None = None, task: str | None = None,
                connection_ids: list[str] | None = None, now: datetime | None = None) -> dict[str, Any]:
        """Resolve applicable authorized publications; never claim all enterprise rules are present."""
        tenant = validate_tenant_id(tenant)
        principal = self.store._principal()
        business_unit = _text(business_unit, "business unit", 120) if business_unit is not None else None
        task = _text(task, "task", 120) if task is not None else None
        if connection_ids is not None:
            connection_ids = _strings(connection_ids, "requested sources", 20, 64)
        now = _now(now)
        with self.store.engine.connect() as conn:
            entries = conn.execute(select(self.bundles).where(self.bundles.c.tenant_id == tenant)
                                   .order_by(self.bundles.c.slug).limit(MAX_BUNDLES + 1)).mappings().all()
            if len(entries) > MAX_BUNDLES:
                raise KnowledgeConflict("Company guidance inventory exceeds its bounded capacity")
            pairs = []
            for entry in entries:
                if entry["published_revision"] is not None:
                    item = self._revision(conn, tenant, entry["slug"], entry["published_revision"])
                    if item is None:
                        raise KnowledgeConflict("Guidance publication has missing revision history")
                    pairs.append((dict(entry), item))
        active, blocked = [], []
        unavailable = False
        source_cache: dict[str, dict[str, Any] | None] = {}
        selected_source_ids = set()
        for bundle, item in pairs:
            payload = item["payload"]
            if not _audience(payload, principal) or not _scope(payload, business_unit, task, connection_ids):
                continue
            if not self._readable(tenant, payload):
                unavailable = True
                continue
            selected_source_ids.update(payload["connection_ids"])
            selected_source_ids.update(dep["connection_id"] for dep in payload["dependencies"])
            if len(selected_source_ids) > MAX_RESOLUTION_SOURCES:
                raise KnowledgeConflict("Applicable guidance exceeds its source verification budget; narrow the requested context")
            reasons = []
            if guidance_digest(payload) != item["digest"] or bundle["published_digest"] != item["digest"]:
                reasons.append("guidance_integrity_failed")
            if bundle["revoked_at"]:
                reasons.append("revoked")
            reasons.extend(self._validity(payload, now))
            reasons.extend(self._dependency_reasons(tenant, payload, source_cache))
            if reasons:
                blocked.append({"slug": bundle["slug"], "revision": item["revision"], "digest": item["digest"],
                                "reasons": sorted(set(reasons))})
            else:
                active.append({**self._record(bundle, item), "dependency_state": "current"})
        if unavailable:
            blocked.append({"reasons": ["source_dependency_unavailable"]})
        keys: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
        for bundle in active:
            for rule in bundle["payload"]["rules"]:
                values = keys.setdefault(rule["key"], {})
                value = values.setdefault(_rule_identity(rule["value"]), {"value": rule["value"], "bundles": []})
                value["bundles"].append({key: bundle[key] for key in ("slug", "revision", "digest")})
        conflicts = [{"key": key, "values": list(values.values())}
                     for key, values in sorted(keys.items()) if len(values) > 1]
        result = {"status": "blocked" if blocked or conflicts else "ready" if active else "empty",
                  "bundles": active, "blocked": blocked, "conflicts": conflicts,
                  "context": {"business_unit": business_unit, "task": task, "connection_ids": connection_ids},
                  "evaluated_at": _stamp(now),
                  "coverage": "applicable_authorized_guidance_only", "rules_enforced": False}
        if len(_encoded(result)) > MAX_RESOLUTION_BYTES:
            raise KnowledgeConflict("Applicable guidance exceeds the 256 KiB response budget; narrow the requested context")
        # The first source cache is for bounded assembly, not a permission or
        # publication lease. Earlier bundles can become stale while later
        # bundles are resolved, even when their target snapshot IDs stay fixed.
        final_sources: dict[str, dict[str, Any] | None] = {}
        for bundle in active:
            if (not self._readable(tenant, bundle["payload"])
                    or self._dependency_reasons(tenant, bundle["payload"], final_sources)):
                raise KnowledgeConflict("Guidance dependencies changed during assembly; resolve current guidance")
        if active:
            with self.store.engine.connect() as conn:
                for bundle in active:
                    current = conn.execute(select(self.bundles).where(
                        self.bundles.c.tenant_id == tenant, self.bundles.c.slug == bundle["slug"],
                    )).mappings().first()
                    revision = self._revision(conn, tenant, bundle["slug"], bundle["revision"])
                    if (current is None or revision is None
                            or self._record(dict(current), revision)
                            != {key: value for key, value in bundle.items() if key != "dependency_state"}):
                        raise KnowledgeConflict("Guidance publication changed during assembly; resolve current guidance")
        return result
