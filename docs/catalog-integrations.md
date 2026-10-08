# Catalog and lakehouse integrations

Knowledge Layer separates live source discovery, export translation, registry
ingestion and human publication. A supported payload shape is not proof that a
vendor account, permission model or production workload has been qualified.

## Live discovery

Connection records contain source selectors and named operator references,
not raw credentials or arbitrary source URLs. The operator provisions the
read-only source account and enables the connector for the installation.

| Source | Connection kind | Configuration and qualification |
| --- | --- | --- |
| PostgreSQL / Neon | `postgres` | Named `secret_ref`, schema and selected tables; [native qualification](native-connector-qualification.md) |
| ClickHouse / SQLite | `sql_catalog` | Provider, named `secret_ref` and selected scope; [native qualification](native-connector-qualification.md) |
| Snowflake / BigQuery | `sql_catalog` | Provider, named credential profile and exact schema/dataset; [cloud catalog connectors](cloud-catalog-connectors.md) |
| Iceberg REST catalogs | `iceberg` | Operator-provisioned catalog name, namespace and selected tables; [Iceberg profiles](native-connector-qualification.md) |
| Documents / object stores | Connector-specific | Retention, account visibility and limits in [remote documents](remote-document-connectors.md) |

Snowflake and BigQuery are provider values, not standalone connection kinds.
For example, after an operator provisions `KNOWLEDGE_SOURCE_SALES_SNOWFLAKE`:

```python
from knowledge_layer import KnowledgeClient

with KnowledgeClient(database_url="sqlite:///knowledge.db") as client:
    result = client.connect_source(
        kind="sql_catalog",
        name="Sales metadata",
        config={
            "provider": "snowflake",
            "secret_ref": "KNOWLEDGE_SOURCE_SALES_SNOWFLAKE",  # pragma: allowlist secret (operator variable name)
            "schema": "SALES.PUBLIC",
            "tables": ["CUSTOMERS", "ORDERS"],
        },
    )
```

This example requires the Snowflake extra and an approved native credential;
it is not an offline demo. Provider authentication profiles are documented in
the [cloud guide](cloud-catalog-connectors.md).

Iceberg connection configuration refers to an operator catalog profile:

```python
config = {"catalog": "analytics", "namespace": "sales", "tables": ["orders"]}
```

Helpers such as `polaris_profile`, `lakekeeper_profile`, `unity_catalog_profile`
and `nessie_profile` build **operator-side PyIceberg profiles**. Their returned
credential/endpoint dictionaries are not connection configuration. Use the
[operator profile helpers](../src/knowledge_layer/connectors/iceberg.py) for exact constructor signatures,
secret placement and read-only scope.

## One-way catalog export translation

The named presets accept selected Alation, Collibra, Atlan, OpenMetadata and
DataHub JSON shapes. They preserve the mapped records' native IDs and types.
They do not implement vendor writeback, mirror ACLs, certify definitions, or
persist the translated package. There is no built-in OpenLineage preset.
Use a reviewed custom mapping or external adapter for other shapes.

A portable catalog package requires an explicitly complete export with no
remaining pages. Supply the exact native scope; do not substitute an import
clock for the source's observation time. Omitted observation values are unknown.
The SDK's `translate_catalog` is the explicit translation operation;
`import_catalog` remains a compatibility alias and reports `persisted: false`.

```python
from knowledge_layer import KnowledgeClient

with KnowledgeClient(database_url="sqlite:///:memory:") as client:
    translated = client.translate_catalog(
        [{"id": 101, "name": "orders", "table_type": "TABLE"}],
        catalog_format="alation",
        scope={
            "provider": "alation",
            "source_identity": "synthetic-sales-catalog",
            "namespace": "sales",
            "scope_id": "tables",
            "selectors": ["schema:sales"],
        },
        listing_complete=True,  # Caller has verified every selected page is present.
    )
    assert translated["operation"] == "translation"
    assert translated["persisted"] is False
    assert client.list_connections() == []
```

For a live export, supply only upstream values that you can substantiate via
`observation`: `exported_at`, `source_observed_at`, `native_revision`,
`field_coverage`, and `redaction`. An explicit complete flag cannot override
contradictory cursor/count information in the vendor response. Aggregate pages
through an audited exporter before invoking the translator.

## Ingestion and review are separate steps

To connect a translated package, expose it through the bounded catalog adapter
contract and provision that adapter as an approved source. See
[catalog import and mapping contract](catalog-agnostic-import.md),
and [external connector protocol](external-connector-protocol.md). This step
creates the connection and retained evidence that the translation helper alone
does not create.

Imported upstream definitions retain their native provenance and review state.
They require current evidence and local owner review before effective semantic
use; discovery or a vendor's certification label does not grant local approval.
See [agent build and review](agent-build-flow.md) and
[source publication](source-publication.md).

Run the synthetic translation example without vendor credentials:

```sh
uv run --no-sync python examples/02_catalog_federation.py
```

## Retention and authority

Native database/Iceberg discovery follows metadata paths. Local structured-file
profiles read rows; document evidence and optional semantic samples retain
bounded content. Choose retention and egress policy before enabling those paths.
Knowledge Layer does not write connected sources. Source-native permissions,
coverage and production behavior require independent qualification; see
[architecture](architecture.md) and the [capability matrix](capability-support-matrix.md).
