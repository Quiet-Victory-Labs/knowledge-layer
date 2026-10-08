# Native connector qualification

Native discovery reads metadata. It does not execute customer SQL, profile
application rows, read Iceberg data files, or certify business meaning. Operators
must separately qualify the source account, grants, auth mode, transport, and
deployment. Use the [source access policy](source-access.md) to approve exact
source identities before enabling them for users or agents.

## Qualified driver policies

| Connector | Implemented boundary | Qualification evidence in this change |
| --- | --- | --- |
| PostgreSQL | Privilege-filtered catalog columns; 100 tables and 5,000 columns; connection and statement timeout of 1–10 seconds; server read-only transaction | Mocked catalog statements, physical type drift, negative/unmeasured estimates |
| SQLAlchemy PostgreSQL | Connection timeout 5 seconds; statement timeout 10 seconds; read-only transaction plus server readback; 100 tables and 5,000 columns | Mocked reflection and installed dialect compilation |
| SQLAlchemy SQLite | Existing file opened with `mode=ro`; query-only readback; lock timeout 5 seconds; VM deadline 10 seconds; 100 tables and 5,000 columns | Actual temporary file read-only enforcement and interrupted VM execution |
| SQLAlchemy ClickHouse | Official `clickhousedb` dialect; connect timeout 5 seconds, read timeout 15 seconds, retries disabled; read-only and 10-second execution setting readback; server result limits | Installed dialect physical type compilation and mocked server policy readback |
| Iceberg REST | Operator alias; SDK metadata requests only; approved outbound origins; connection timeout 5 seconds and read timeout 15 seconds; 8 MiB per response; 100 tables and 5,000 nested fields | Installed PyIceberg REST transport exercised with offline responses, including bootstrap OAuth |

These are connector policies and focused test results. They do not establish a
customer latency SLO, permission mirroring, live ClickHouse account qualification,
or cloud catalog readiness. SQLAlchemy reflection may fetch a metadata result
before applying its client count limit; PostgreSQL statement timeouts bound each
query, not the entire reflection job. ClickHouse also has a server result budget.
The SQL provider menu offers only PostgreSQL, ClickHouse, and SQLite. Other known
dialect families refuse with `sql_policy_unavailable`, even if their driver is
installed; use an explicitly qualified external adapter until a native policy is
implemented and verified.

## Generic REST catalogs, including Lakekeeper

The API accepts an alias and selectors, never a URL or credentials:

```json
{"catalog":"synthetic_catalog","namespace":"silver","tables":["selected_table"]}
```

Provision the alias map in the worker and API environment, using
`KNOWLEDGE_ICEBERG_PROFILES` or an owner-only `KNOWLEDGE_ICEBERG_PROFILES_FILE`:

```json
{"synthetic_catalog":"KNOWLEDGE_SOURCE_SYNTHETIC_ICEBERG"}
```

The named source secret, or its owner-only `_FILE` variant, contains a flat SDK
profile with the existing REST base URI and warehouse identifier. For example:

```json
{"uri":"http://127.0.0.1:8181/catalog","warehouse":"existing-warehouse-identifier"}
```

Replace the example warehouse with the catalog's actual configured warehouse.
Use the URI from the existing source configuration. Profile properties support
`token`, or legacy SDK `credential` plus `oauth2-server-uri`, `scope`, `resource`,
and `audience`. `ssl` can select a CA bundle or client certificate; disabling TLS
verification is rejected. Nested auth plugins are refused because their SDK
transport is not qualified by this policy. HTTPS is required for non-loopback
origins unless the operator explicitly enables
`KNOWLEDGE_ALLOW_INSECURE_CATALOG_ENDPOINTS=1`.

No Polaris or object-storage credentials are needed for a generic metadata
census. The existing Polaris environment path remains available for compatibility;
its `POLARIS_CLIENT_FILE` must also be an absolute owner-only regular file.
Remote catalog config cannot choose an independent outbound origin, authentication
plugin, or TLS policy. Redirects are disabled. Table responses use a fixed SDK
FileIO implementation without object reads or remotely selected IO plugins.

Explicit table selectors load those tables directly. An unselected namespace
must fit one complete listing within 100 tables. A continuation token or excessive
listing causes refusal; it is never reported as complete. The selected table
metadata also has the response and nested field budgets above.

PyIceberg 0.12.0 was inspected and exercised for this change. It does not implement
the newer `rest.client.*` timeout properties in the documentation. The connector
therefore extends the existing SDK catalog's sessions with request bounds,
including its initial OAuth request. These are connection/read inactivity bounds
per request, not an absolute discovery deadline. SDK retries and multiple metadata
requests can extend total duration. Managed discovery's existing child-process
timeout provides the job boundary; the synchronous local demo does not provide
that worker guarantee. Requalify this extension when upgrading the SDK.

## Schema and absence invariants

- Normalized display types remain broad. `native_type`, nullability, Iceberg
  field IDs, and current schema ID retain the source facts used for drift.
  Nested containers classify as structured before inspecting their scalar type.
- PostgreSQL negative or missing `reltuples` is an unknown estimate. Zero is a
  measured catalog estimate of zero, never an exact population count. Exact
  PostgreSQL and SQLAlchemy row counts remain null.
- Iceberg row counts come only from the current snapshot's `total-records`
  summary. Missing, negative, or invalid summaries remain null; valid zero is
  retained. No manifest or table scan fills an absent count.
- PostgreSQL/SQLAlchemy revisions retain physical width, precision, scale, and
  timezone. Iceberg revisions bind the current native schema, nested field IDs,
  schema ID, source table UUID, and current snapshot ID. Replacing a table under
  the same name invalidates evidence even when schema and snapshot are identical.
  A data snapshot change and a schema change both invalidate evidence, while
  unchanged metadata remains stable. A missing source UUID remains explicitly
  unknown; the connector ignores the SDK's generated UUID default. Without that
  source identity, identical replacements cannot be distinguished by these facts.
- SQL key reflection distinguishes unsupported metadata from observed empty
  metadata. Source-declared composite keys remain technical candidates; they
  grant no approved join or business relationship authority.

## Verification commands and live follow-up

```bash
uv sync --frozen --extra dev --extra iceberg --extra clickhouse
.venv/bin/pytest -q tests/test_knowledge_curation.py tests/test_knowledge_connectors_extended.py tests/test_knowledge_secret_files.py tests/test_native_connector_qualification.py
.venv/bin/ruff check src/knowledge_layer/connectors/iceberg.py src/knowledge_layer/connectors/postgres.py src/knowledge_layer/connectors/sql_catalog.py tests/test_native_connector_qualification.py
```

The focused suite executed 97 tests after these changes. Offline REST fixtures
use real SDK models and request execution; they do not contact a catalog. SQLite
tests use temporary files. PostgreSQL and ClickHouse policy/type tests use mocks
and installed dialects; they do not contact customer databases.

For a synthetic Iceberg check, provision a generic REST alias from an operator-owned fixture catalog and select one or two exact tables. Compare native schema fields, IDs and snapshot-summary counts with independently retained fixture metadata. Keep discovery metadata-only. A successful local run is separate from cloud reachability, customer permissions and production qualification.

Implementation references: [PyIceberg configuration](https://py.iceberg.apache.org/configuration/),
[PyIceberg REST catalog source](https://github.com/apache/iceberg-python/blob/main/pyiceberg/catalog/rest/__init__.py),
[SQLAlchemy engine configuration](https://docs.sqlalchemy.org/en/20/core/engines.html),
[ClickHouse Python driver API](https://github.com/ClickHouse/clickhouse-docs/blob/main/docs/integrations/language-clients/python/driver-api.md),
and [PostgreSQL information schema](https://www.postgresql.org/docs/current/information-schema.html).
