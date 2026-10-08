# PostgreSQL schema migrations

OIDC deployments use explicit, versioned Alembic migrations. The application checks the recorded revision and physical table shape before accepting traffic and on readiness probes; it does not create or alter tables on startup. The local synthetic demo retains automatic table creation for a new unversioned evaluation. If a PostgreSQL registry already has an Alembic revision, demo startup also requires the current revision and physical metadata parity; it cannot create new tables ahead of migration.

## New OIDC installation

Provision a dedicated customer-managed PostgreSQL database and an owner-only `DATABASE_URL_FILE` or operator-managed `DATABASE_URL`. Run the migration before starting the service:

```sh
docker compose -f compose.oidc-pilot.yaml run --rm knowledge python -m knowledge_layer.migrate upgrade
docker compose -f compose.oidc-pilot.yaml run --rm knowledge python -m knowledge_layer.migrate status
docker compose -f compose.oidc-pilot.yaml up --build -d --wait
```

The Helm chart enables a guarded `pre-install,pre-upgrade` migration Job by default. It uses the application image, the existing database URL Secret, a non-root account, a read-only root filesystem, and no service-account token. The Job refuses an unversioned database containing registry tables. Set `migration.enabled: false` only when an operator runs `python -m knowledge_layer.migrate upgrade` separately before installing or upgrading the Deployment.

Revision `0002_source_grants` adds [explicit source metadata grants](source-access.md). Existing sources receive no automatic grants; provision their authorized users or groups with the offline operator command after the upgrade. This is a deliberate fail-closed transition for OIDC reads.

Revision `0003_refresh_jobs` adds the durable refresh ledger and one-active-job-per-source index. Apply it before starting the OIDC API or worker. The worker refuses an older or drifted schema, and the API readiness probe does the same.

Revision `0004_source_publications` records a steward's source-level publication or withdrawal against one immutable snapshot version. Older snapshots do not inherit publication status. The API refuses connector-provided readiness flags, including from rows created before this migration.

Revision `0005_decision_digests` binds each new owner decision to the reviewed proposal and cited evidence. Earlier decision rows remain unbound and fail closed; a steward must re-review them before they can support `agent_ready`. Check the number of existing decisions and arrange re-review before applying this revision to a customer registry.

Revision `0006_audit_events` adds the [governance event ledger](audit-events.md). It starts recording changes after upgrade; prior source grants, reviews, and publications are not backfilled. Back up and verify the v5 database before applying it. The application requires v6 before accepting OIDC traffic.

Revision `0007_proposal_lifecycle` adds submitter attribution and logical archival fields to both proposal tables. Existing rows are attributed to `legacy`; no proposals or decisions are removed. Back up and verify the v6 database before applying it. The application requires v7 before accepting OIDC traffic. Archived rows remain in PostgreSQL for historical reads and audit; storage retention is an operator policy, not an automatic purge.

Revision `0008_source_paging_index` adds a composite index on connection tenant, creation time, and ID for ordered source-list cursor pages. It does not alter source records. Back up and verify the v7 database first. The migration builds the index inside the normal schema transaction; schedule the upgrade in a write-quiet window for a large registry and measure the build against a customer-like copy before production rollout. The application requires v8 before accepting OIDC traffic.

Revision `0009_concept_search` adds one current metadata search row per concept and a PostgreSQL full-text GIN index. The migration seek-pages current snapshots ten sources at a time and backfills concept text and active local proposed definitions; historical snapshot rows are untouched. Snapshot refreshes and proposal archives replace the source's search rows in the same transaction. Back up and verify v8 before upgrading, stop API and workers during the backfill, and measure a copy of the customer registry before scheduling the write-quiet window. This index remains required at later schema heads. The source-level grants remain in the query, while search returns no more than 256 ranked lexical candidates. A broad query reports `index_limit_reached`; a term with no lexical overlap will not enter optional semantic reranking. PostgreSQL query work has a 1.5-second statement timeout, so an overloaded search fails instead of occupying a database connection indefinitely.

Revision `0010_refresh_schedules` adds the source schedule ledger, an optional schedule link on jobs, and separate attempt/completed-inventory clocks on connections. Existing clocks are left null because earlier refresh timestamps could include review saves; no historical completion is fabricated. Existing jobs and snapshots are preserved. Back up and verify v9, stop API and workers for upgrade, then run `python -m knowledge_layer.migrate upgrade`. A disposable PostgreSQL v9-to-v10 drill preserved a legacy row and verified the physical schema; customer backups, deployment and recovery objectives remain separate gates.

Revision `0011_company_guidance` adds `knowledge_guidance_bundles` and immutable `knowledge_guidance_revisions`. The publication pointer remains separate from the newest draft, and governance events use the existing audit ledger. It does not turn imported documents into authoritative instructions. Before applying this historical revision, verify a v10 backup/restore. The local demo creates the new tables automatically; managed OIDC installations must run the explicit migration command.

Revision `0012_inventory` adds row-based scoped discovery runs, pages, entities,
evidence and current heads. Revision `0013_autonomy` adds versioned source
maintenance policies and attributed runs. Revision `0014_inventory_results`
adds a typed inventory result on refresh jobs, distinct from snapshot results.
These revisions preserve separate discovery, review and publication authority.

Current managed startup requires `0015_inventory_concepts` and physical metadata
parity. It adds immutable local primary-concept proposals and separate steward
publications over exact inventory observations. No snapshots, clocks, native
approvals or historical decisions are fabricated or backfilled. Existing data
stays intact. Stop API/workers, verify a backup and restore at the installed
revision, run the explicit upgrade command, and verify schema parity before
restart. Downgrades are refused; rollback restores the verified backup. Recovery
censuses include both new tables at v15 and preserve historical revision sets.

## Existing unversioned alpha registry

Take and verify a PostgreSQL backup first. `upgrade` deliberately refuses existing unversioned Knowledge Layer tables. `adopt` checks every baseline table, column type, constraint, and index against the frozen first revision, then records that revision without changing registry data. It refuses incomplete or altered schemas.

```sh
docker compose -f compose.oidc-pilot.yaml run --rm knowledge python -m knowledge_layer.migrate adopt
docker compose -f compose.oidc-pilot.yaml run --rm knowledge python -m knowledge_layer.migrate upgrade
docker compose -f compose.oidc-pilot.yaml run --rm knowledge python -m knowledge_layer.migrate status
```

If adoption refuses the database, inspect the schema privately and prepare a reviewed migration from that exact state. Do not stamp a mismatched schema. The historical 16-character snapshot status column is one such mismatch; widening it must be planned against a verified backup before adoption.

Migration attempts use a PostgreSQL transaction-scoped advisory lock. The baseline is forward-only: rollback is a verified database restore, not an automatic table drop. Test each new revision against an empty database and an adopted prior release before publishing an image. The [local migration drill](../scripts/drill_schema_migrations.py) does both, checks startup refusal of an unversioned schema, tests drift rejection, and proves a preexisting record survives adoption.
