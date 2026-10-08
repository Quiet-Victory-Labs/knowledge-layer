"""Translate complete synthetic catalog exports without persisting records.

Run: uv run --no-sync python examples/02_catalog_federation.py
Registry ingestion, review and vendor writeback are not performed by translation.
"""

from __future__ import annotations

from knowledge_layer import KnowledgeClient
from knowledge_layer.connectors.common import ConnectorError

EXPORTS = {
    "alation": [{"id": 101, "name": "orders", "table_type": "TABLE"}],
    "openmetadata": {"data": [{"id": "synthetic-products", "name": "products", "tableType": "Regular"}]},
    "collibra": {"results": [{"id": "synthetic-revenue", "name": "Revenue", "type": {"name": "Metric"}}]},
}


def main() -> None:
    with KnowledgeClient(database_url="sqlite:///:memory:", tenant_id="synthetic-catalogs") as client:
        for provider, raw in EXPORTS.items():
            translated = client.translate_catalog(
                raw,
                catalog_format=provider,
                scope={
                    "provider": provider,
                    "source_identity": f"synthetic-{provider}",
                    "namespace": "demo",
                    "scope_id": "complete-fixture",
                    "selectors": ["domain:demo"],
                },
                listing_complete=True,
            )
            assert translated["operation"] == "translation"
            assert translated["persisted"] is False
            assert translated["record_count"] == 1
            observation = translated["package"]["observation"]
            assert observation["source_observed_at"] is None
            assert observation["native_revision"] is None
            assert observation["field_coverage"]["definitions"] == "unknown"
            assert client.list_connections() == []
            print(f"{provider}: one record translated; provenance remains unknown; nothing persisted.")

        try:
            client.translate_catalog(
                {"data": EXPORTS["openmetadata"]["data"], "paging": {"total": 100, "after": "next-page"}},
                catalog_format="openmetadata",
                scope={
                    "provider": "openmetadata", "source_identity": "synthetic-openmetadata",
                    "namespace": "demo", "scope_id": "partial-fixture", "selectors": ["domain:demo"],
                },
                listing_complete=True,
            )
        except ConnectorError as error:
            assert error.code == "catalog_export_incomplete"
            print("Partial export correctly refused even when the caller claims completeness.")
        else:
            raise AssertionError("Partial export was incorrectly accepted")
        assert client.list_connections() == []


if __name__ == "__main__":
    main()
