# Native Snowflake and BigQuery metadata

These optional paths connect one Snowflake database/schema or one BigQuery project/dataset directly. They use the existing `sql_catalog` source kind, source approval identity, refresh worker, evidence projection and curation workflow. The caller receives metadata and reviewable candidates; no application rows, analytical queries or source writes are performed.

## Install and enable

For a Python deployment, install the selected optional dependency:

```sh
uv sync --extra snowflake
uv sync --extra bigquery
```

Install both extras when both providers are needed. For a container deployment, build the runtime image with `KNOWLEDGE_RUNTIME_EXTRAS="snowflake bigquery"` and use that same image for the API and refresh worker. Connector enablement still requires `sql_catalog` in `KNOWLEDGE_ENABLED_KINDS`; agent source management additionally requires its existing operator opt-in and allowed-kinds settings.

Provision a dedicated source credential and restricted native account/IAM permissions. Knowledge Layer connection grants select who can retrieve the retained metadata; they do not mirror source-native row, field or document access controls. Split retained source connections along audience boundaries.

## Snowflake

The connection's `schema` must contain the exact native `DATABASE.SCHEMA` identity. Unquoted Snowflake names normally appear in uppercase; use the catalog's actual case. Both components use ordinary identifier characters, and the combined selector is capped at 128 characters. This path does not accept arbitrary SQL or custom endpoints.

```json
{
  "provider": "snowflake",
  "secret_ref": "KNOWLEDGE_SOURCE_SALES_SNOWFLAKE",
  "schema": "SALES.PUBLIC",
  "tables": ["CUSTOMERS", "ORDERS"],
  "max_tables": 100,
  "max_columns": 5000
}
```

The named server-side secret contains a JSON credential profile. Set either `KNOWLEDGE_SOURCE_SALES_SNOWFLAKE` or `KNOWLEDGE_SOURCE_SALES_SNOWFLAKE_FILE`; mounted files must be regular owner-only files within the existing 64 KiB secret limit. Supply `account`, `user`, optional `role`/`warehouse`, and exactly one credential mode:

| Mode | Profile fields |
| --- | --- |
| Key pair | `private_key_pem`, optional `private_key_password`; RSA key at least 2048 bits |
| Password | `password`; the native account must permit this programmatic authentication mode |
| OAuth | `token`; the operator rotates/renews the token |

For example, an operator-managed key-pair profile has this shape:

```json
{
  "account": "organization-account",
  "user": "KNOWLEDGE_METADATA_READER",
  "role": "KNOWLEDGE_METADATA_ROLE",
  "warehouse": "METADATA_WAREHOUSE",
  "private_key_pem": "<operator-provisioned PEM key>"
}
```

The connector uses the optional Snowflake Python SDK and fixed `INFORMATION_SCHEMA.SCHEMATA`, `TABLES` and `COLUMNS` statements. Database identifiers are validated and quoted; schema/table filters use bound parameters. It checks that the schema remains visible before accepting an empty table inventory. Selected tables must all be returned, and table/column overflows fail the whole refresh instead of silently clipping it.

Table types, descriptions, creation metadata, field descriptions, nullability and physical types become observed metadata. Precision/scale changes and observed table recreation change the metadata fingerprint. Descriptions remain unreviewed source text. Key and foreign-key state stays unknown; no row profiling or join verification is inferred from numeric/name heuristics.

The [Snowflake connector API](https://docs.snowflake.com/en/developer-guide/python-connector/python-connector-api) documents SDK connection/query controls. This path sets 5-second login/socket limits, bounded network/query waits, 10-second statement and 5-second queued-statement limits, disables session keep-alive, and uses one result-prefetch thread. Native account permissions remain an operator responsibility.

## BigQuery

The connection's `schema` is the exact `project.dataset` identity. Standard project IDs with hyphens are supported. The combined selector is capped at 128 characters; domain-scoped project names and custom Google API universes are outside this path.

```json
{
  "provider": "bigquery",
  "secret_ref": "KNOWLEDGE_SOURCE_SALES_BIGQUERY",
  "schema": "customer-project.sales",
  "tables": ["customers", "orders"],
  "max_tables": 100,
  "max_columns": 5000
}
```

The secret accepts either a standard service-account JSON credential with
`type=service_account`, `project_id`, `private_key`, `client_email` and the public
Google OAuth token endpoint, or this exact keyless profile:

```json
{"type":"short_lived_access_token","access_token":"<operator-minted token>","expires_at":"<timezone-aware ISO timestamp>"}
```

Short-lived authority must have more than 30 seconds and at most one hour left.
Expired or malformed profiles refuse before source HTTP. Rotate the named file
atomically; the connector never falls back to ambient Google credentials. Mint
tokens outside the application using an explicitly restricted identity and
[Google's short-lived credentials API](https://docs.cloud.google.com/iam/docs/create-short-lived-credentials-direct).
Other ordinary service-account JSON identity fields are accepted for key-based
profiles. Caller-selected API/auth URLs, TLS overrides and custom auth plugins
refuse. The credential project may differ from the selected target project when
native IAM explicitly permits that metadata access.

Google Auth signs the credential exchange. Its token request and all catalog requests use the same bounded HTTP transport with redirects and environment proxy overrides disabled. The REST path uses only `tables.list` and `tables.get`; selected table names skip the broader dataset listing. `tables.get` uses the `BASIC` metadata view, which [returns table structure rather than table data and excludes storage statistics](https://docs.cloud.google.com/bigquery/docs/reference/rest/v2/tables/get).

Schema descriptions, physical types/parameters, field modes, nested fields, declared policy-tag identities and partitioning/clustering metadata are observed evidence. Nested fields count against the same global column budget, with a maximum depth of 15. Declared policy tags describe native metadata and are not access-control propagation. Primary/foreign-key and source-row statistics remain unknown. The agent uses a separate authorized source tool when the human later asks it to calculate a result.

## Budgets, provenance and failure behavior

Both paths cap discovery at 100 tables, 5,000 fields, an aggregate 8 MiB metadata/serialization budget and a 60-second checked discovery deadline. Customers can reduce table/column limits. Individual network operations have finite timeouts; Snowflake SDK/socket operations can finish their current attempt before the surrounding deadline is checked. This is a connector budget, not a promise that an external driver thread can be forcibly cancelled at an exact instant.

BigQuery listings have a finite ten-page budget, continuation checks, fixed request origins and at most ten seconds per request with a five-second connect limit. Repeated tokens, incomplete continuation, excessive payloads, scope mismatches, missing selected tables, permission denial and schema errors fail the refresh. Native account visibility defines the observed scope; a successful list does not establish full company access or source-native ACL synchronization.

Asset IDs bind the stable source ID to the provider and exact native scope/object identity. Metadata fingerprints include retained schema/description/creation context; unchanged metadata reuses meaning evidence while physical type or supported metadata changes create new evidence. Secret values and raw driver/API errors never enter failure snapshots. A failed refresh remains incomplete and cannot make missing objects into an accepted inventory removal. Prior retained context and current source/error state remain distinct in the existing store.

The optional extras are locked but remain outside the base image. Offline tests exercise mocked Snowflake calls, actual HTTP construction, finite budgets, source identities and failure behavior. A production account must separately verify exact metadata scope, unauthorized table/row denial, expired-token rotation and zero source writes. Customer SSO, field ACL mirroring, Snowflake authentication and production networking/load remain unqualified by these offline tests.

Repeat against existing approved IAM with `scripts/drill_bigquery_metadata.py`.
It creates no keys, accounts or IAM bindings and retains only a private count/
digest/denial receipt. Use [source-worker isolation](source-worker-isolation.md)
when selecting execution boundaries.

```sh
uv run --no-sync --with google-auth --with snowflake-connector-python python -m pytest -q tests/test_cloud_catalog_connectors.py
```

Use the [agent build flow](agent-build-flow.md) after successful discovery. Existing SQLAlchemy provider profiles retain their original policies; a Snowflake/BigQuery SQL URL without the qualified native scope does not activate these JSON-profile paths.
