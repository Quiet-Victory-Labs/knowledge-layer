"""Evidence-backed knowledge layer for external agents."""

from __future__ import annotations

from typing import Any

__version__ = "0.1.0"

__all__ = [
    "ALATION_TABLE_MAPPING",
    "ALATION_TERM_MAPPING",
    "ATLAN_ENTITY_MAPPING",
    "COLLIBRA_ASSET_MAPPING",
    "DATAHUB_ENTITY_MAPPING",
    "OPENMETADATA_GLOSSARY_MAPPING",
    "OPENMETADATA_TABLE_MAPPING",
    "KnowledgeClient",
    "KnowledgeStore",
    "__version__",
    "build_knowledge_mcp_server",
    "curate_source",
    "get_preset_mapping",
    "lakekeeper_profile",
    "nessie_profile",
    "polaris_profile",
    "semantic_contract",
    "translate_catalog_export",
    "unity_catalog_profile",
]


def __getattr__(name: str) -> Any:
    if name == "KnowledgeClient":
        from knowledge_layer.client import KnowledgeClient

        return KnowledgeClient
    if name == "KnowledgeStore":
        from knowledge_layer.store import KnowledgeStore

        return KnowledgeStore
    if name == "curate_source":
        from knowledge_layer.curation import curate_source

        return curate_source
    if name == "semantic_contract":
        from knowledge_layer.contracts import semantic_contract

        return semantic_contract
    if name == "build_knowledge_mcp_server":
        from knowledge_layer.mcp import build_knowledge_mcp_server

        return build_knowledge_mcp_server
    if name in {
        "translate_catalog_export",
        "get_preset_mapping",
        "ALATION_TABLE_MAPPING",
        "ALATION_TERM_MAPPING",
        "COLLIBRA_ASSET_MAPPING",
        "ATLAN_ENTITY_MAPPING",
        "OPENMETADATA_TABLE_MAPPING",
        "OPENMETADATA_GLOSSARY_MAPPING",
        "DATAHUB_ENTITY_MAPPING",
    }:
        import knowledge_layer.adapters.catalog_presets as cp

        return getattr(cp, name)
    if name in {"polaris_profile", "lakekeeper_profile", "unity_catalog_profile", "nessie_profile"}:
        import knowledge_layer.connectors.iceberg as ic

        return getattr(ic, name)
    raise AttributeError(f"module 'knowledge_layer' has no attribute '{name}'")
