# Enterprise qualification, 2026-10-05

The installation remains an alpha pilot. This increment implements the deploy →
connect → agent build → human review/accept → agent consumption flow, with a
small model-free core. Passing local tests does not qualify a customer deployment.

The later [portable self-host qualification](self-host-qualification-2026-10-05.md)
adds a real 31-check owner/worker/native-MCP flow, exact human publication,
restart and complete cold recovery. It also records the current app/PostgreSQL
image bindings and remaining release blockers. The older test counts below
describe their original increment.

## Implemented and verified locally

- Durable, source-scoped agent build tasks with leases, snapshot pins, budgets,
  resumable progress and evidence-bound results. External agents propose;
  human stewards accept an exact preview digest and separately control publication.
- Minimal Radix UI: Build, Review and Sources, with optional review notes and
  details revealed when needed. The UI build and four graph-view tests pass.
- PostgreSQL, SQL catalog, Iceberg, object stores, PDFs/local files, Drive,
  BigQuery, Snowflake and remote-document adapters have bounded configuration
  and refusal paths. Adapter presence is not live vendor certification.
- Whole-source metadata permission enforcement binds every sanitized native
  snapshot field and complete inventory lineage. Missing, expired, partial or
  withdrawn attestations deny the source and its derived context. Operator
  partition assertions remain distinct from provider-native ACL propagation.
- Native source reads run in a separate credential-filtered process, with an
  optional immutable-image Docker backend that drops capabilities and mounts
  no host paths. Process mode does not provide an OS sandbox. Dedicated worker
  scope applies both to schedule dispatch and claims; current direct requester
  authority is checked before IO, during work and before commits.
- Committed source-read start/finish audit events use the verified requester and
  exact queue fence. Audit insertion failure prevents IO. Abrupt termination can
  leave an unmatched start; the database ledger needs external immutable export
  and monitored delivery for tamper resistance.
- Composite metrics support bounded aggregate expressions over one approved
  grain and shared complete-case cohort. Real SQLite arithmetic is tested;
  PostgreSQL/ClickHouse compilation is tested. Live precision, uniqueness,
  scan/truncation limits and execution permissions need consumer qualification.

The full local suite, including IAP, passed **3,073 tests, seven skipped**.
Subsequent bounded JWKS, registry bootstrap and legacy-review repairs passed
**184 focused tests**, including distribution refusal checks. A real local
PostgreSQL 18.6 run verified the repaired bootstrap with a nonsuperuser operator,
restricted app attributes, correct database/schema ownership and no retained
temporary self-grant. Its container was removed. Ruff, UI build, four graph-view tests and clean-checkout
wheel/source installation checks passed. These are local checks, not hosted proof.

## Real source and native-agent evidence

Quiet Victory Labs' dedicated keyless qualification service account obtained
metadata for one explicitly granted BigQuery table (one asset, four fields).
Another table's metadata and table-data access were denied; an expired profile
was refused. No query job, row retrieval or upstream source write was performed.
This is a restricted metadata proof, not field/row ACL mirroring or a complete
customer semantic journey.

A synthetic Codex native harness completed ten durable tasks, with two proposals
and eight abstentions, followed by exact human acceptance/publication and agent
consumption. The full Claude drill failed; the latest Cursor invocation timed out
without output. Neither is qualified for the full native journey. No failure is
silently treated as success.

## Container and release limits

The freshly rescanned default image and all-eight-extra profile have 47 scanner matches, including
15 High findings. The evaluated Chainguard profile has eight matches (six Medium,
two Low, zero High). Both still fail the strict all-known release gate,
including negligible severity;
no ignore or exception was added. Snowflake 4.8.0 is incompatible with the
candidate's OpenSSL 4 runtime and candidate builds explicitly refuse that extra.
The earlier arm64 runtime receipts do not establish amd64 security qualification.
A temporary amd64 cloud image built successfully, but the hosted database
bootstrap failed and no authenticated application journey was reached.

A fresh focused security verification found six issues in manifest coverage,
startup policy downgrade, hidden-source repair/retries, worker schedule scope and
container credential references. Those paths were fixed and regression-tested.
This does not constitute a sealed review of every changed source file or a new
whole-source audit. Independent legal review, exact-commit CI, trusted signed-tag
and release-environment approval gates still apply before distribution.

## Temporary GCP qualification and cost policy

Cloud qualification requires an authorized operator, a bounded cost budget and
cleanup of all test-owned resources. Use disposable synthetic data, explicit
read-only identities and a resource inventory. Verify deletion of databases,
backups, jobs/services, image repositories, build objects and test secret versions
afterward. Private project inventory and operational results are excluded from
this public guide; no retained customer deployment is claimed.

IAP can provide a Google-managed browser sign-in client and HTTPS Cloud Run URL.
The application separately verifies the signed assertion, exact service audience,
issuer, algorithm and lifetime, then applies its explicit identity policy.
Browser writes require identity-bound CSRF and same-origin requests. Agents use
explicitly allowed identities; unsigned convenience headers never supply roles.
Google-managed IAP does not provide ordinary user OAuth programmatic exchange;
service-account signed JWT access requires an explicitly authorized identity.

A Cloud Run application does not implement a Docker host for isolated source
workers. Do not enable process-based remote source IO and call it container
isolation. A separately qualified container host, provider-native ACL exporter,
every required live adapter, deployed native agents, immutable audit export,
representative load/recovery tests and sustained monitoring remain necessary
before an enterprise-ready claim.

A cloud recipe or local offline test does not establish a working hosted
installation. Record exact-commit deployment, identity, database, recovery and
cleanup evidence for the chosen account before declaring it qualified.
