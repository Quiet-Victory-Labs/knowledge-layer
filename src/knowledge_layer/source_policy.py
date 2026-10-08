"""Operator-approved source identities for the single-organization OIDC deployment."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from knowledge_layer.access import Principal
from knowledge_layer.operator_secrets import operator_secret
from knowledge_layer.source_permissions import SourcePermissions

# These fields determine which underlying data can be reached. Profiling budgets
# may change, but changing one of these fields requires a new connection and grant.
IDENTITY_FIELDS: dict[str, frozenset[str]] = {
    "local_files": frozenset({"path", "include", "retain_semantic_samples"}),
    "iceberg": frozenset({"catalog", "namespace", "tables"}),
    "postgres": frozenset({"secret_ref", "schema", "tables"}),
    "sql_catalog": frozenset({"provider", "secret_ref", "schema", "tables"}),
    "object_store": frozenset({"secret_ref", "prefix"}),
    "google_drive": frozenset({"secret_ref", "folder_id"}),
    "external_adapter": frozenset({"secret_ref", "scope", "retain_semantic_samples",
                                    "discovery_mode", "retain_inventory_payloads"}),
}
REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "local_files": frozenset({"path", "include"}),
    "iceberg": frozenset({"catalog", "namespace"}),
    "postgres": frozenset({"secret_ref", "schema"}),
    "sql_catalog": frozenset({"provider", "secret_ref"}),
    "object_store": frozenset({"secret_ref", "prefix"}),
    "google_drive": frozenset({"secret_ref", "folder_id"}),
    "external_adapter": frozenset({"secret_ref", "scope"}),
}


def source_identity(kind: str, config: dict[str, Any]) -> str:
    """Canonical source selector; normalized config is required at the call site."""
    fields = IDENTITY_FIELDS[kind]
    selector = {key: config[key] for key in fields if key in config}
    if kind in {"local_files", "external_adapter"}:
        selector["retain_semantic_samples"] = config.get("retain_semantic_samples", False)
    if "tables" in selector:
        selector["tables"] = sorted(selector["tables"])
    return json.dumps([kind, selector], sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class SourceApproval:
    tenant_id: str
    identity: str
    subjects: frozenset[str]
    groups: frozenset[str]


class SourcePolicy:
    def __init__(self, approvals: tuple[SourceApproval, ...], *,
                 metadata_permissions: SourcePermissions | None = None):
        self.approvals = approvals
        self.metadata_permissions = metadata_permissions or SourcePermissions()

    @classmethod
    def from_environment(cls) -> SourcePolicy:
        raw = operator_secret("KNOWLEDGE_SOURCE_POLICY")
        if not raw:
            raise ValueError(
                "OIDC source management requires KNOWLEDGE_SOURCE_POLICY or KNOWLEDGE_SOURCE_POLICY_FILE"
            )
        return cls.from_json(raw)

    @classmethod
    def from_json(cls, raw: str) -> SourcePolicy:
        # Imported here to keep the identity definition independent of the store.
        from knowledge_layer.store import validate_connection_config, validate_tenant_id

        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Source policy cannot repeat JSON object keys")
                result[key] = value
            return result

        try:
            if not isinstance(raw, str) or len(raw) > 1024 * 1024:
                raise ValueError
            payload = json.loads(raw, object_pairs_hook=unique_object)
        except (TypeError, ValueError) as exc:
            raise ValueError("Source policy must be valid JSON") from exc
        if (
            not isinstance(payload, dict)
            or type(payload.get("version")) is not int
            or payload["version"] not in {1, 2}
            or set(payload) != ({"version", "sources"} if payload["version"] == 1 else
                                {"version", "sources", "metadata_permissions"})
            or not isinstance(payload["sources"], list)
            or len(payload["sources"]) > 1000
        ):
            raise ValueError("Source policy must have version 1 or 2 and a sources array")
        approvals = []
        seen: set[tuple[str, str]] = set()
        for item in payload["sources"]:
            if not isinstance(item, dict) or set(item) != {
                "tenant_id",
                "kind",
                "selector",
                "allowed_subjects",
                "allowed_groups",
            }:
                raise ValueError(
                    "Each source approval needs tenant_id, kind, selector, allowed_subjects, and allowed_groups"
                )
            kind = item["kind"]
            selector = item["selector"]
            if kind not in IDENTITY_FIELDS or not isinstance(selector, dict):
                raise ValueError("Source approval has an invalid kind or selector")
            if (
                not REQUIRED_FIELDS[kind] <= set(selector)
                or not set(selector) <= IDENTITY_FIELDS[kind]
            ):
                raise ValueError("Source approval must specify only the complete source identity")
            if any(
                not isinstance(value, list)
                or len(value) > 1000
                or any(
                    not isinstance(member, str)
                    or not member.strip()
                    or len(member) > 255
                    for member in value
                )
                for value in (item["allowed_subjects"], item["allowed_groups"])
            ):
                raise ValueError("Source approval subjects and groups must be nonempty strings")
            subjects = frozenset(item["allowed_subjects"])
            groups = frozenset(item["allowed_groups"])
            if not subjects and not groups:
                raise ValueError("Source approval must allow at least one subject or group")
            if not isinstance(item["tenant_id"], str) or not item["tenant_id"].strip():
                raise ValueError("Source approval tenant_id must be a nonempty string")
            tenant = validate_tenant_id(item["tenant_id"])
            normalized = validate_connection_config(kind, selector)
            identity = source_identity(kind, normalized)
            key = (tenant, identity)
            if key in seen:
                raise ValueError("Duplicate source approval")
            seen.add(key)
            approvals.append(SourceApproval(tenant, identity, subjects, groups))
        permissions = (SourcePermissions.from_payload(payload["metadata_permissions"])
                       if payload["version"] == 2 else SourcePermissions())
        return cls(tuple(approvals), metadata_permissions=permissions)

    def approved(self, tenant_id: str, kind: str, config: dict[str, Any]) -> bool:
        identity = source_identity(kind, config)
        return any(
            item.tenant_id == tenant_id and item.identity == identity for item in self.approvals
        )

    def allows(
        self, tenant_id: str, kind: str, config: dict[str, Any], principal: Principal
    ) -> bool:
        identity = source_identity(kind, config)
        return any(
            item.tenant_id == tenant_id
            and item.identity == identity
            and (principal.subject in item.subjects or bool(principal.groups & item.groups))
            for item in self.approvals
        )
