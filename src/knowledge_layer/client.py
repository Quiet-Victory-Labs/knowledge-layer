"""First-class native Python client SDK for Knowledge Layer."""

from __future__ import annotations

import json
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from knowledge_layer.adapters.catalog_presets import translate_catalog_export
from knowledge_layer.contracts import (
    search_document_evidence,
    search_source_evidence,
    semantic_contract,
)
from knowledge_layer.source_policy import source_identity
from knowledge_layer.store import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeStore,
    KnowledgeValidationError,
    validate_connection_config,
    validate_tenant_id,
)


class KnowledgeClient:
    """High-level Python SDK client for Knowledge Layer.

    Provides a clean, ergonomic programmatic interface for applications, data pipelines,
    and AI agents (Gemini, Claude, LangChain, Pydantic AI) to discover data sources,
    retrieve reviewed semantic contracts, resolve business terms, search document
    evidence, and serve Model Context Protocol (MCP) context.
    """

    def __init__(
        self,
        database_url: str | None = None,
        *,
        tenant_id: str = "default",
        local_files_root: str | Path | None = None,
        initialize_schema: bool = True,
        store: KnowledgeStore | None = None,
    ) -> None:
        self.tenant_id = validate_tenant_id(tenant_id)
        if store is not None:
            self.store = store
        else:
            self.store = KnowledgeStore(
                database_url=database_url,
                local_files_root=local_files_root,
                initialize_schema=initialize_schema,
            )

    def connect_source(
        self,
        kind: str,
        config: dict[str, Any],
        *,
        name: str | None = None,
        connection_id: str | None = None,
    ) -> dict[str, Any]:
        """Connect, discover, and curate any supported data source.

        Supported kinds include:
        - postgres / sql_catalog (PostgreSQL, Neon, ClickHouse, SQLite)
        - sql_catalog with provider="snowflake" or "bigquery" (warehouse metadata)
        - iceberg (Apache Iceberg REST Catalogs: Polaris, Lakekeeper, Unity Catalog, Nessie)
        - local_files (Parquet, CSV, JSONL, PDF, Markdown, HTML)
        - google_drive / object_store (Cloud storage & documents)
        - external_adapter (Universal HTTP Adapter Protocol)
        """
        display_name = name or f"{kind} source"
        validated_config = validate_connection_config(kind, config)
        self.store._require_source_manager()
        self.store._require_source_approval(self.tenant_id, kind, validated_config)

        existing = self.store.find_connection_by_name(self.tenant_id, kind=kind, name=display_name)
        if existing is None:
            existing = self.store._find_management_source_by_name(
                self.tenant_id, kind=kind, name=display_name,
            )
        if existing is not None:
            if source_identity(kind, existing["config"]) != source_identity(kind, validated_config):
                raise KnowledgeAccessDenied(
                    "A source with this name has a different identity; choose a new name and obtain its source approval"
                )
            if existing["config"] != validated_config:
                raise KnowledgeValidationError(
                    "A source with this name has different settings; edit the existing connection before refreshing"
                )
            if connection_id is not None and connection_id != existing["id"]:
                raise KnowledgeValidationError("The source name belongs to a different connection ID")
            conn_id = existing["id"]
        elif connection_id:
            conn_id = connection_id
            self.store.create_connection(self.tenant_id, kind=kind, name=display_name, config=validated_config, connection_id=conn_id)
        else:
            created = self.store.create_connection(self.tenant_id, kind=kind, name=display_name, config=validated_config)
            conn_id = created["id"]

        refreshed = self.store.refresh_connection(self.tenant_id, conn_id)
        saved = refreshed.get("snapshot") or {}
        current = refreshed["connection"]

        return {
            "connection_id": conn_id,
            "name": display_name,
            "kind": kind,
            "status": current["status"],
            "snapshot_id": saved.get("id"),
            "assets_count": len(saved.get("assets", [])),
            "evidence_count": len(saved.get("evidence", [])),
            "snapshot": refreshed.get("snapshot"),
            **({"inventory": refreshed["inventory"]} if refreshed.get("inventory") is not None else {}),
            **({"metadata_unavailable": True} if current.get("metadata_unavailable") else {}),
        }

    def get_connection(self, connection_id: str) -> dict[str, Any] | None:
        """Get connection metadata and curation summary."""
        return self.store.get_connection(self.tenant_id, connection_id)

    def list_connections(self, limit: int = 50) -> list[dict[str, Any]]:
        """List active connections for this tenant."""
        page = self.store.page_connections(self.tenant_id, limit=limit)
        return page.get("connections", [])

    def get_semantic_contract(self, connection_id: str) -> dict[str, Any]:
        """Read source bindings, table grains, partition keys, tenant boundaries, metrics, and schema."""
        return semantic_contract(self.store, tenant_id=self.tenant_id, connection_id=connection_id)

    def resolve_term(self, term: str, connection_id: str | None = None) -> dict[str, Any] | None:
        """Ground a business term against reviewed concepts, cited glossary terms, and metric definitions."""
        clean_term = term.strip()
        if not clean_term:
            raise ValueError("term must be a non-empty string")
        return self.store.get_context(self.tenant_id, term=clean_term, connection_id=connection_id)

    def search_evidence(
        self,
        query: str,
        *,
        connection_id: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Search bounded document and metadata passages with source locators."""
        clean_query = query.strip()
        if not clean_query:
            return []
        if connection_id:
            result = search_source_evidence(
                self.store, tenant_id=self.tenant_id, connection_id=connection_id, query=clean_query, limit=limit,
            )
            return result.get("matches", [])
        result = search_document_evidence(self.store, tenant_id=self.tenant_id, query=clean_query, limit=limit)
        return result.get("matches", [])

    def propose_assertion(
        self,
        connection_id: str,
        assertion_kind: str | None = None,
        asset_id: str | None = None,
        payload: dict[str, Any] | None = None,
        evidence_ids: list[str] | None = None,
        rationale: str = "",
        expected_snapshot_id: str | None = None,
        evidence_refs: list[dict[str, str]] | None = None,
        *,
        kind: str | None = None,
    ) -> dict[str, Any]:
        """Submit a typed human-in-the-loop contract assertion proposal.

        Supported kinds: grain, tenant_boundary, metric, join_rule, dataset_description, field_description, query_example.
        """
        resolved_kind = kind or assertion_kind
        if not resolved_kind:
            raise ValueError("assertion_kind or kind must be provided")
        if asset_id is None or payload is None or evidence_ids is None:
            raise ValueError("asset_id, payload, and evidence_ids are required")

        if not expected_snapshot_id:
            conn = self.store.get_connection(self.tenant_id, connection_id)
            if not conn or not conn.get("snapshot"):
                raise KnowledgeNotFound(f"No active snapshot found for connection '{connection_id}'")
            expected_snapshot_id = conn["snapshot"]["id"]

        return self.store.submit_contract_assertion(
            self.tenant_id,
            connection_id,
            assertion_kind=resolved_kind,
            asset_id=asset_id,
            payload=payload,
            evidence_ids=evidence_ids,
            rationale=rationale,
            expected_snapshot_id=expected_snapshot_id,
            evidence_refs=evidence_refs,
        )

    def decide_assertion(
        self,
        connection_id: str,
        proposal_id: str,
        decision: str = "approve",
    ) -> dict[str, Any]:
        """Approve or reject a pending contract assertion proposal."""
        return self.store.decide_proposal(self.tenant_id, connection_id, proposal_id, decision=decision)

    def certify_snapshot(
        self,
        connection_id: str,
        *,
        reviewer: str = "data_steward",
        rationale: str = "The steward accepts this reviewed source metadata for external agents.",
    ) -> dict[str, Any]:
        """Publish already reviewed, current metadata through the steward gate.

        Pending proposals require a separate human decision. ``reviewer`` remains
        accepted for compatibility; the verified principal supplies publication
        authority and the recorded actor. Publication does not certify meaning.
        """
        snapshot = self.store.get_snapshot(self.tenant_id, connection_id)
        if not snapshot:
            raise KnowledgeNotFound(f"Connection '{connection_id}' has no snapshot to publish")
        publication = self.store.publish_source(
            self.tenant_id, connection_id, expected_snapshot_id=snapshot["id"],
            expected_snapshot_version=snapshot["version"], rationale=rationale,
        )
        current = self.store.get_snapshot(self.tenant_id, connection_id) or {}
        contract = self.get_semantic_contract(connection_id)
        observed = self.store.get_snapshot(self.tenant_id, connection_id) or {}
        published_pin = (publication["snapshot_id"], publication["snapshot_version"], True)
        snapshot_pin = (current.get("id"), current.get("version"),
                        current.get("agent_ready") is True)
        contract_pin = (contract.get("snapshot_id"), contract.get("snapshot_version"),
                        contract.get("agent_ready") is True)
        if snapshot_pin != published_pin or contract_pin != published_pin or observed != current:
            raise KnowledgeConflict("Source changed during publication response assembly; reload current metadata")

        return {
            "connection_id": connection_id,
            "snapshot_id": current.get("id"),
            "snapshot_version": current.get("version"),
            "meaning_certified": current.get("meaning_certified") is True,
            "agent_ready": current.get("agent_ready") is True,
            "publication": publication,
            "contract": contract,
        }

    def translate_catalog(
        self,
        raw_data: bytes | str | dict[str, Any] | list[Any],
        catalog_format: str = "auto",
        *,
        mapping: dict[str, Any] | None = None,
        scope: dict[str, Any] | None = None,
        name: str | None = None,
        observation: dict[str, Any] | None = None,
        listing_complete: bool | None = None,
        next_cursor: str | None = None,
    ) -> dict[str, Any]:
        """Translate a complete external catalog export without persisting it.

        The producer supplies exact scope, completeness and optional observation
        declarations. ``name`` is accepted for compatibility and creates no source.

        Supported catalog formats:
        - "alation" / "alation_tables" / "alation_terms"
        - "collibra" / "collibra_direct"
        - "atlan"
        - "openmetadata" / "openmetadata_terms"
        - "datahub"
        - custom mappings
        """
        export_bytes = translate_catalog_export(
            raw_data,
            catalog_format=catalog_format,
            mapping=mapping,
            scope=scope,
            observation=observation,
            listing_complete=listing_complete,
            next_cursor=next_cursor,
        )
        parsed = json.loads(export_bytes.decode("utf-8"))
        record_count = parsed.get("record_count", len(parsed.get("records", [])))

        return {
            "operation": "translation",
            "persisted": False,
            "catalog_format": catalog_format,
            "schema_version": parsed.get("schema_version"),
            "scope": parsed.get("scope"),
            "record_count": record_count,
            "exported_at": parsed.get("observation", {}).get("exported_at"),
            "package_bytes": len(export_bytes),
            "package": parsed,
        }

    def import_catalog(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Compatibility alias for :meth:`translate_catalog`; does not import or persist."""
        return self.translate_catalog(*args, **kwargs)

    def get_mcp_server(
        self,
        *,
        enable_source_management: bool = False,
        allowed_kinds: frozenset[str] = frozenset({"local_files"}),
        durable_jobs: bool = False,
        enable_autonomy: bool = False,
    ):
        """Get an initialized Model Context Protocol (MCP) server ready for Gemini/Claude pairing."""
        from knowledge_layer.mcp import build_knowledge_mcp_server

        return build_knowledge_mcp_server(
            self.store,
            enable_source_management=enable_source_management,
            allowed_kinds=allowed_kinds,
            durable_jobs=durable_jobs,
            enable_autonomy=enable_autonomy,
            tenant_id=self.tenant_id,
        )

    def close(self) -> None:
        """Dispose the underlying store connections."""
        self.store.close()

    def __enter__(self) -> Self:
        """Use the client with deterministic registry connection disposal."""
        return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc_value: BaseException | None, traceback: TracebackType | None) -> None:
        """Dispose the store on exit, preserving the caller's exception."""
        self.close()
