# Public release checklist

This is an alpha source release candidate, not a qualified enterprise deployment. The checks below distinguish repository hygiene from product readiness.

The [2026-10-05 self-host qualification](self-host-qualification-2026-10-05.md)
records the current portable customer flow, complete cold recovery and runtime
scan limits. The [enterprise qualification](enterprise-qualification-2026-10-05.md)
also covers metadata permissions, isolated source reads, composite metrics and
earlier temporary GCP testing. Earlier receipts remain scoped to their original
revisions and environments.
The [hosted qualification follow-up](ga-qualification-2026-10-05.md) records the
first six-job CI run, its two corrected failures, the stable package update and
the guarded future GA release channel.

The [2026-10-02 enterprise readiness drill](enterprise-readiness-drill-2026-10-02.md) records passed local checks, measured agent payloads, and unresolved enterprise gates. The [local qualification runbook](local-qualification-runbook.md) contains repeatable commands.

The [2026-10-04 hardening increment](enterprise-release-hardening-2026-10-04.md)
adds the small cloud recipe, strict manual release pipeline, backup/restore and
monitoring tools, measured context optimization and bounded security fixes. Its
local evidence does not close public release or customer qualification gates.

## Before making alpha source public

Source visibility and signed binary distribution have separate gates. The
[public alpha checklist](public-alpha-readiness.md) records the current source
preparation and remaining human decisions.

- Merge the audited changes after independent code review and all six exact-commit
  Verify jobs pass. Preserve administrator enforcement, code-owner review,
  latest-push approval and strict required checks.
- Review the complete intended source snapshot, its [fixture provenance](public-fixture-provenance.md),
  LICENSE, NOTICE and current dependency notice evidence. Record source/fixture
  rights and applicable dependency obligations for those exact bytes.
- Choose the publishable Git history. Private pilot reports and source-specific
  adapter code have been removed from the current tree; their earlier Git objects
  remain in the private repository. Current-tree secret checks do not clear that
  history for public disclosure. A new source snapshot can preserve the original
  private repository without carrying its history.
- Verify private delivery to the security address, identify the monitoring and
  escalation owner, and test acknowledgment as described in [SECURITY.md](../SECURITY.md).
  Enable and test GitHub private vulnerability reporting when the chosen repository
  is public. Do not include source records or vulnerability details in the contact test.
- Publish the scope as a `0.1.x` alpha evaluation. SDK, connector, publication,
  deployment and consumer limits remain explicit; a green build does not establish
  customer or enterprise qualification.

## Before distributing signed packages or images

Follow [RELEASING.md](../RELEASING.md) for clean builds, signed tags,
exact-commit CI, lock-bound inventories, notice bundles, corresponding-source
review, actual protected release environments and artifact attestations.
Apply the [container finding policy](container-security-policy.md) to each actual
release image. Historical application/PostgreSQL findings in the
[GA review packet](ga-review-packet.md) are not current exact-image scans and
remain separate from a source-only alpha launch. Unreviewed release-blocking
findings must be resolved before publishing those binaries.

## Before claiming enterprise readiness

- Qualify the implemented operator-approved source identity policy against a real IdP, restricted source roles, chart/Compose deployment, and policy rotation. Connection retargeting is denied in OIDC mode; the worker checks approval before source IO. This is local test evidence, not customer qualification.
- Qualify owner-only file-secret mounts and rotation against the chosen secret manager. File references reject symlinks and read from one validated descriptor; use the chart's Secret environment mapping if the manager exposes only projected symlink volumes.
- Extend the tested source metadata grants to field-level and source-native row/document ACL propagation; qualify denial behavior through a real IdP and source.
- Add tenant isolation or state the single-organization boundary in deployment policy.
- Qualify the PostgreSQL refresh worker under representative source counts, worker loss, timeouts, retries, cancellation, and load. Define monitoring and per-source credential isolation. Repeat the versioned migration and backup/restore drills against a customer-like PostgreSQL deployment and measure recovery objectives.
- Qualify each connector against a real account, restricted grants, auth rotation, schema drift, and denial cases. A generic SQL dialect is not vendor certification.
- Qualify imports from existing catalogs and semantic systems, including two business units with conflicting same-named terms or metrics. Verify native identities, namespace, original definitions, revision and owner provenance survive ingestion and agent retrieval. Upstream review must remain distinct from local review; changed definitions, withdrawals and permission changes must invalidate affected context without silently inventing equivalence. An unavailable or partial listing cannot establish deletion.
- Qualify the implemented customer-controlled recurring refresh schedules for hourly, daily, weekly and monthly operation against customer deployment and load. The [local refresh and citation drill](refresh-and-citations-2026-10-03.md) verifies bounded dispatch and exact semantic citations; it does not qualify a customer SLO. Define timezone/calendar behavior, missed runs, pause/resume, overlapping runs, source budgets and freshness deadlines. Measure complete-success and attempt clocks separately; test outages, retries, restart recovery and expired permissions without advancing freshness or writing upstream sources. Queue polling and a successful job alone do not satisfy this gate.
- Exercise the OIDC and agent JWT path through the customer's real IdP and deployed HTTPS endpoint. The disposable local Keycloak drill covers login, local logout, fresh role changes, and expired agent tokens, but it does not qualify upstream SSO logout, live token revocation, or customer IdP claim mapping.
- Run end-to-end load and latency benchmarks on representative source counts and measure MCP tool bytes, calls, and token use. The v8 paging index has a disposable 20,000-row SQL plan check, batched list projection has a 50-source PostgreSQL parity/query-count drill, and cross-source search has a dense 20,000-snapshot and concept grant-filtered drill. Concept search uses the v9 PostgreSQL current-concept GIN index but reranks only the top 256 lexical candidates semantically; semantic-only recall outside that set remains unqualified. These checks do not qualify API concurrency, complete source-read log volume, or curation memory use under customer load. Maintain PostgreSQL table statistics after large metadata imports.
- Evaluate optional decision providers on a held-out, steward-labeled curation set before enabling `--decision-mode gate`. Review false skipped work, calibration, drift, remote metadata egress, and model version changes; keep the default install model-free.
- Qualify the snapshot-pinned steward publication gate on representative sources and document its permitted-use policy. Configure retention, external immutable storage, and monitored delivery for the [operator audit export and OIDC access logs](audit-events.md). Verify that per-source registry-read events cover every deployed API/MCP path, then implement and qualify attribution for source-native worker reads and any direct source access. The in-database ledger and stdout logs alone are not tamper evident.

The implemented scope and current limits are in [architecture.md](architecture.md).
