"""Whole-source metadata permission attestations, separate from management grants.

An operator partition is an explicit assertion, never native ACL mirroring.
Native exporters must be explicitly trusted by the operator; the repository does
not itself contact a provider to qualify their permission assertions.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from knowledge_layer.access import Principal

MAX_ATTESTATIONS = 128
MAX_MANIFESTS = 256
MAX_TOTAL_MANIFESTS = 1024
MAX_PERMISSION_SECONDS = 900
SNAPSHOT_MANIFEST_VERSION = "snapshot-full-v1"
_DIGEST = re.compile(r"[0-9a-f]{64}")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def snapshot_resource_manifest(payload: dict[str, Any]) -> str:
    """Bind exact sanitized source resources and evidence, including field text.

Local semantic review does not change the underlying resource permission set.
The server computes this value; connector-supplied permission claims are ignored.
"""
    controls = {"source_permission_manifest_digest", "source_permission_identity_digest",
                "source_permission_manifest_version"}
    return digest({"version": SNAPSHOT_MANIFEST_VERSION,
                   "metadata": {key: value for key, value in payload.items() if key not in controls}})


def manifest_digest(manifests: list[str] | tuple[str, ...]) -> str:
    return digest(sorted(manifests))


def _text(value: Any, label: str) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 255
            or value != value.strip() or re.search(r"[\x00-\x1f\x7f]", value)):
        raise ValueError(f"Metadata permission {label} must be bounded printable text")
    return value


def _members(value: Any) -> frozenset[str]:
    if not isinstance(value, list) or len(value) > 1000:
        raise ValueError("Metadata permission audience must be a bounded list")
    members = frozenset(_text(member, "audience member") for member in value)
    if len(members) != len(value):
        raise ValueError("Metadata permission audience cannot repeat members")
    return members


def _clock(value: Any) -> datetime:
    try:
        if not isinstance(value, str) or len(value) > 40:
            raise ValueError
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(UTC)
    except (ValueError, OverflowError):
        raise ValueError("Metadata permission clocks require bounded timezone-aware timestamps") from None


@dataclass(frozen=True)
class PermissionAttestation:
    tenant_id: str
    kind: str
    identity: str
    selector: dict[str, Any]
    identity_digest: str
    basis: str
    authority: str
    revision: str
    state: str
    checked_at: datetime
    expires_at: datetime
    subjects: frozenset[str]
    groups: frozenset[str]
    manifest_digests: tuple[str, ...]
    resource_manifest_digest: str

    def current(self, trusted_exporters: frozenset[str], *, now: datetime | None = None) -> bool:
        clock = now or datetime.now(UTC)
        return (self.state == "complete" and self.checked_at <= clock < self.expires_at
                and (self.basis == "operator_partition" or self.authority in trusted_exporters))

    def audience(self, principal: Principal) -> bool:
        return principal.subject in self.subjects or bool(principal.groups & self.groups)


class SourcePermissions:
    def __init__(self, *, enforce: bool = False,
                 trusted_native_exporters: frozenset[str] = frozenset(),
                 attestations: tuple[PermissionAttestation, ...] = ()):
        self.enforce = enforce
        self.trusted_native_exporters = trusted_native_exporters
        self.attestations = attestations

    @classmethod
    def from_payload(cls, payload: Any) -> SourcePermissions:
        from knowledge_layer.source_policy import IDENTITY_FIELDS, REQUIRED_FIELDS, source_identity
        from knowledge_layer.store import validate_connection_config, validate_tenant_id

        if (not isinstance(payload, dict) or set(payload) != {
                "enforce", "trusted_native_exporters", "attestations"}
                or type(payload["enforce"]) is not bool
                or not isinstance(payload["attestations"], list)
                or len(payload["attestations"]) > MAX_ATTESTATIONS):
            raise ValueError("Metadata permissions require enforcement, trusted exporters and bounded attestations")
        trusted = _members(payload["trusted_native_exporters"])
        attestations = []
        seen = set()
        total_manifests = 0
        for item in payload["attestations"]:
            fields = {"tenant_id", "kind", "selector", "source_identity_digest", "basis", "authority",
                "revision", "state", "checked_at", "expires_at", "allowed_subjects", "allowed_groups",
                "manifest_digests", "resource_manifest_digest"}
            if not isinstance(item, dict) or set(item) != fields:
                raise ValueError("Metadata permission attestation has missing or unsupported fields")
            tenant = validate_tenant_id(_text(item["tenant_id"], "tenant"))
            kind = item["kind"]
            if (not isinstance(kind, str) or kind not in IDENTITY_FIELDS
                    or not isinstance(item["selector"], dict)
                    or not REQUIRED_FIELDS[kind] <= set(item["selector"])
                    or not set(item["selector"]) <= IDENTITY_FIELDS[kind]):
                raise ValueError("Metadata permission requires the exact complete source selector")
            config = validate_connection_config(kind, item["selector"])
            identity = source_identity(kind, config)
            selector = json.loads(identity)[1]
            identity_pin = digest(identity)
            if item["source_identity_digest"] != identity_pin:
                raise ValueError("Metadata permission source identity digest does not match its selector")
            if (tenant, identity) in seen:
                raise ValueError("Metadata permission attestations cannot repeat a source identity")
            seen.add((tenant, identity))
            if item["basis"] not in {"operator_partition", "native_exporter"}:
                raise ValueError("Metadata permission basis is unsupported")
            if item["state"] not in {"complete", "unknown", "partial", "stale", "withdrawn"}:
                raise ValueError("Metadata permission state is unsupported")
            checked, expires = _clock(item["checked_at"]), _clock(item["expires_at"])
            if not 0 < (expires - checked).total_seconds() <= MAX_PERMISSION_SECONDS:
                raise ValueError("Metadata permission lifetime must be positive and at most 900 seconds")
            subjects, groups = _members(item["allowed_subjects"]), _members(item["allowed_groups"])
            if not subjects and not groups:
                raise ValueError("Metadata permission requires an explicit audience")
            manifests = item["manifest_digests"]
            if (not isinstance(manifests, list) or not 1 <= len(manifests) <= MAX_MANIFESTS
                    or any(not isinstance(value, str) or not _DIGEST.fullmatch(value) for value in manifests)
                    or len(set(manifests)) != len(manifests)
                    or item["resource_manifest_digest"] != manifest_digest(manifests)):
                raise ValueError("Metadata permission requires an exact exhaustive resource manifest digest")
            total_manifests += len(manifests)
            if total_manifests > MAX_TOTAL_MANIFESTS:
                raise ValueError("Metadata permission total manifest budget exceeded")
            attestations.append(PermissionAttestation(tenant, kind, identity, selector, identity_pin,
                item["basis"], _text(item["authority"], "authority"), _text(item["revision"], "revision"),
                item["state"], checked, expires, subjects, groups, tuple(sorted(manifests)),
                item["resource_manifest_digest"]))
        return cls(enforce=payload["enforce"], trusted_native_exporters=trusted,
                   attestations=tuple(attestations))

    def eligible(self, tenant: str, principal: Principal) -> tuple[PermissionAttestation, ...]:
        return tuple(item for item in self.attestations if item.tenant_id == tenant
                     and item.current(self.trusted_native_exporters) and item.audience(principal))


def access_clause(store, tenant: str, connection_column, permissions: SourcePermissions,
                  principal: Principal, approvals=()):
    """Correlated SQL only: no extra DB reads while assembling authorization.

Every stored resource manifest must be covered, including historical snapshots
and inventories. Unknown legacy snapshots deny the source until requalified.
"""
    from sqlalchemy import and_, cast, exists, false, func, or_, select
    from sqlalchemy.dialects.postgresql import JSONB

    from knowledge_layer.source_policy import IDENTITY_FIELDS

    source = store.connections.alias("permission_source")
    snapshots = store.snapshots.alias("permission_snapshot")
    inventories = store.inventory_runs.alias("permission_inventory")
    clauses = []
    approved = {item.identity for item in approvals if item.tenant_id == tenant}
    for item in permissions.eligible(tenant, principal):
        if item.identity not in approved:
            continue
        identity = [source.c.kind == item.kind]
        for key in IDENTITY_FIELDS[item.kind]:
            value = item.selector.get(key)
            field = source.c.config[key]
            if isinstance(value, list):
                if store.backend == "postgresql":
                    array = cast(field, JSONB)
                    identity.append(and_(func.jsonb_array_length(array) == len(value), array.contains(value)))
                else:
                    members = func.json_each(field.as_string()).table_valued("value").alias("permission_tables")
                    identity.append(and_(func.json_array_length(field.as_string()) == len(value),
                        ~exists(select(members.c.value).where(~members.c.value.in_(value)))))
            elif type(value) is bool:
                identity.append(func.coalesce(field.as_boolean(), False) == value)
            else:
                identity.append(field.as_string().is_(None) if value is None else field.as_string() == value)
        # No partial redaction: one unqualified artifact hides the entire source.
        snapshot_scope = and_(snapshots.c.tenant_id == tenant,
                              snapshots.c.connection_id == source.c.id)
        snapshot_pin = snapshots.c.payload["source_permission_manifest_digest"].as_string()
        identity_pin = snapshots.c.payload["source_permission_identity_digest"].as_string()
        version_pin = snapshots.c.payload["source_permission_manifest_version"].as_string()
        invalid_snapshots = exists(select(snapshots.c.row_id).where(snapshot_scope, or_(
            snapshot_pin.is_(None), ~snapshot_pin.in_(item.manifest_digests),
            identity_pin.is_(None), identity_pin != item.identity_digest,
            version_pin.is_(None), version_pin != SNAPSHOT_MANIFEST_VERSION)))
        inventory_scope = and_(inventories.c.tenant_id == tenant,
                               inventories.c.connection_id == source.c.id)
        invalid_inventories = exists(select(inventories.c.id).where(inventory_scope, or_(
            ~inventories.c.manifest_digest.in_(item.manifest_digests),
            inventories.c.source_identity_digest != item.identity_digest)))
        has_resources = or_(exists(select(snapshots.c.row_id).where(snapshot_scope)),
                            exists(select(inventories.c.id).where(inventory_scope)))
        clauses.append(and_(*identity, has_resources, ~invalid_snapshots, ~invalid_inventories))
    if not clauses:
        return false()
    return exists(select(source.c.id).where(source.c.tenant_id == tenant,
        source.c.id == connection_column, or_(*clauses)).correlate_except(source))
