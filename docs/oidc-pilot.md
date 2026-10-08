# Single-organization OIDC pilot

The lightweight image can run with customer-managed PostgreSQL and an OIDC provider. The same image serves the connection/status UI, guarded HTTP API, compact MCP endpoint, detailed MCP endpoint, and a separate refresh worker. It has no built-in model or source data warehouse. This profile is a **pilot**, not a claim of enterprise production readiness: source ACL mirroring, per-source credential isolation, customer backup policy, and qualified warehouse drivers still need implementation and deployment tests. Operator-approved [source identities](source-access.md) now gate creation and refresh; this boundary has local tests but needs a real IdP and restricted-source drill. The schema has a versioned migration path; see [schema migrations](schema-migrations.md).

## Configure

Use a public HTTPS origin at `KNOWLEDGE_PUBLIC_URL`. Terminate TLS at a customer-managed reverse proxy and forward to the loopback port. Register `${KNOWLEDGE_PUBLIC_URL}/auth/callback` as the web client's redirect URI. The IdP must issue signed ID tokens with an organization group claim (`groups` by default) and signed JWT access tokens for the agent audience. Assign one group for any knowledge access and a second group for review decisions. Agent tokens need the configured `knowledge.agent` scope. The OAuth protected-resource metadata endpoint is `/.well-known/oauth-protected-resource`.

The browser requests `openid profile email` by default. Set `KNOWLEDGE_OIDC_LOGIN_SCOPES` (or Helm `oidc.loginScopes`) to the exact scopes registered for the client. It must include `openid`; some providers require a separate group scope, while a client mapper can place groups in the ID token without one. The provider must actually return the configured group claim in the signed ID token. `KNOWLEDGE_OIDC_BROWSER_SESSION_SECONDS` (Helm `oidc.browserSessionSeconds`) caps the signed browser session at 900 seconds by default, with a permitted range of 60–3600 seconds; an earlier ID-token expiry wins. Removing an IdP group does not revoke an already signed browser session or agent JWT before its expiry. Use short provider token lifetimes and revoke the registry source grant when immediate metadata denial is required.

Create four owner-only files readable by the container's non-root UID: a PostgreSQL URL, the confidential OIDC web client secret, a random session signing key of at least 32 characters, and the [source policy JSON](source-access.md). Pass their absolute paths as `KNOWLEDGE_DATABASE_URL_FILE`, `KNOWLEDGE_OIDC_CLIENT_SECRET_FILE`, `KNOWLEDGE_OIDC_SESSION_KEY_FILE`, and `KNOWLEDGE_SOURCE_POLICY_FILE`. The Compose profile defaults to UID/GID 10001; set `KNOWLEDGE_RUN_UID` and `KNOWLEDGE_RUN_GID` to the file owner's numeric IDs for a host bind mount when needed. Never put secret values in the Compose YAML or connection form. The source connector also accepts `KNOWLEDGE_SOURCE_*_FILE` references for operator-provisioned credentials; it reads the file anew on each refresh. File references require a small owner-only regular file and reject symbolic links, including projected secret-volume link layouts. Use a regular-file mount or the chart's existing Secret environment mapping, and qualify rotation with the selected secret manager.

Set `KNOWLEDGE_ENABLED_KINDS` to an explicit comma-separated list, such as `postgres,sql_catalog,external_adapter`. The UI and API reject kinds outside it. Agent source management is disabled until `KNOWLEDGE_AGENT_SOURCE_MANAGEMENT=1`; its `KNOWLEDGE_AGENT_ALLOWED_KINDS` is intersected with the UI allowlist. The core image includes the PostgreSQL SQL driver. SQLAlchemy dialects for Snowflake, BigQuery, Redshift, and other warehouses need optional drivers and real account qualification; a selector in the UI is not a support guarantee.

Migrate the database before starting a Compose pilot:

```sh
docker compose -f compose.oidc-pilot.yaml run --rm knowledge python -m knowledge_layer.migrate upgrade
```

An existing unversioned alpha database requires a verified backup and the guarded `adopt` command described in [schema migrations](schema-migrations.md). Then start with:

```sh
docker compose -f compose.oidc-pilot.yaml up --build -d --wait
```

The service binds only to `127.0.0.1:8273` by default. The operator-managed proxy must preserve the external HTTPS `Host` and origin. The browser uses authorization code with PKCE, a signed HttpOnly Secure session, and CSRF checks on writes. MCP and agent HTTP validate JWT signature, issuer, audience, expiry, and scope. The resource-server URL is `${KNOWLEDGE_PUBLIC_URL}/agent/mcp/`; `/agent/advanced-mcp/` has the wider inspection surface. Source refresh requests return HTTP 202 with a job ID; the UI tracks status while the worker runs separately. Agent clients can read the job through `get_refresh_job` or `GET /agent/api/jobs/{job_id}`.

After reviewing evidence, a steward with a source `manage` grant can [publish the current snapshot for agent use](source-publication.md) from the selected-source panel. Publication is pinned to that snapshot version. A connector, model, or agent cannot self-publish, and draft claims retain their trust labels.

## Kubernetes chart

The [pilot Helm chart](../charts/knowledge-layer/Chart.yaml) deploys the same non-root image and requires an existing Secret for the PostgreSQL URL, OIDC client secret, session key, and `source-policy` JSON key. It injects the policy into both API and worker. It runs a guarded migration Job before install/upgrade, adds readiness checks against PostgreSQL, a read-only root filesystem, resource bounds, a service account without an API token, and optional TLS ingress. Set an immutable image tag or digest and use customer-managed PostgreSQL, IdP, ingress, and source secrets. The bundled `ci-values.yaml` uses fictitious hosts and is only for chart validation.

```sh
helm lint charts/knowledge-layer -f /path/to/customer-values.yaml
helm template knowledge charts/knowledge-layer -f /path/to/customer-values.yaml
helm upgrade --install knowledge charts/knowledge-layer -f /path/to/customer-values.yaml
```

Use `sourceSecretRefs` to map additional `KNOWLEDGE_SOURCE_*` names to existing Kubernetes Secret keys. The chart injects the configured Secret values as environment variables into the refresh worker only; the API receives the source policy but no source credential or file mount. Source credentials remain shared worker capabilities, not isolated per-source processes. The chart schema rejects arbitrary environment overrides. The chart does not create a Secret, database, or ingress controller. The first pilot uses one API replica and one worker. Read the [refresh job runbook](refresh-jobs.md) before changing that topology.

The optional Iceberg connector requires an absolute `POLARIS_CLIENT_FILE` path plus operator-provisioned Polaris and object-store settings in the worker. The stock chart does not mount this file or inject those credentials, so enabling `iceberg` in the connector allowlist alone does not make it operational. Qualify a customer-managed worker extension and restricted catalog role before offering that connector in a pilot.

## Disposable OIDC protocol drill

From a development checkout with Docker, OpenSSL, and the Python development environment installed, run:

```sh
PYTHONPATH=src .venv/bin/python scripts/drill_oidc_keycloak.py
```

The script starts temporary loopback Keycloak and PostgreSQL containers, creates a synthetic realm and one local CSV source, migrates the registry, and exercises browser authorization code plus PKCE, CSRF and local logout, signed agent HTTP/MCP tokens, source grants, group removal, invalid signature, missing scope, wrong audience, and natural token expiry. It also provisions a separate confidential service client using `client_credentials`, independently reviews meaning and delegation through the browser, then publishes once through HTTP and replays through compact/advanced MCP. Missing autonomy scope, wrong subject, revoked direct grant, publication withdrawal and policy withdrawal refuse on all three surfaces; persisted actor, run, review and budget are read back. It stores an owner-only summary under ignored `.local/recovery/` and removes its ownership-labeled containers on exit; unverifiable ownership or cleanup failure fails the drill. The Keycloak image may remain cached for later runs. This is a model-free custom HTTP/MCP ASGI harness against a real disposable IdP, not native Codex/Claude activation. Repeat it through the customer's IdP, deployed HTTPS proxy, managed PostgreSQL, and restricted source before a pilot.

After upgrading an existing registry, provision [source metadata grants](source-access.md) from an operator shell. Previous connections are invisible to OIDC users and agents until they have grants. The MCP transport allows the configured public HTTPS hostname and loopback hosts; the proxy must preserve the external `Host` header.

The drill gives each of its two exact image downloads a 180-second budget, then
launches cached images with `--pull=never` and a separate 45-second budget.
Failure output contains fixed phase/error codes; arbitrary Docker, HTTP and
database diagnostics remain suppressed. A passed receipt requires verified
cleanup. See the [qualification record](enterprise-withdrawal-qualification-2026-10-03.md)
for exact source revisions and the customer/native-client boundaries.

Run `scripts/generate_sbom.sh` to create CycloneDX manifests for the Python service and web package under ignored `.local/sbom/`. Review component licenses, source-driver terms, image layers, and sample-data rights before any public release; an SBOM alone does not establish license compatibility.

## Operational limits

- Each OIDC principal sees only sources with an explicit user or group metadata grant. New sources grant `manage` to their creator only; an offline operator can grant `read` or `manage` to other principals. The browser requires the steward group for source creation, refresh, configuration changes, and review decisions. Source row/document ACLs and field-level entitlements are not mirrored. A source's entire stored metadata snapshot is visible to its grantees, so split or exclude sources with finer internal restrictions.
- The signed browser session expires after at most 15 minutes by default, or sooner when the ID token expires; it is not centrally revocable before expiry. Local logout clears the application cookie but does not end the IdP's SSO session. Rotate the session signing key to invalidate all browser sessions. Agent JWT verification moves JWKS fetches off the request event loop and backs off briefly during provider outages; verify this behavior against the customer's IdP and proxy timeouts.
- Discovery runs in a bounded PostgreSQL queue with a separate process, three attempts, lease recovery, cancellation, and a default five-minute child-process timeout. A restart may reprofile once more if the previous worker saved a snapshot before it recorded success. OIDC startup and worker startup refuse unversioned or drifted databases; migrations are forward-only and require a verified backup for rollback.
- Metadata snapshots may contain explicitly retained document excerpts or semantic samples. Keep `retain_semantic_samples` disabled unless the organization allows this retention.
- Authenticated browser, agent HTTP, and MCP requests emit bounded [access events](audit-events.md) to standard output. Authorized registry reads also emit one source-metadata event per distinct connection touched, including across MCP and cross-source search. Tool arguments and query text are never logged. Configure protected delivery, loss monitoring, and retention in the customer's log platform; source-native worker reads and exact MCP tool identity remain outside this log.
- The operator must separately secure PostgreSQL, the reverse proxy, OIDC provider, backup, and source roles. Grant only metadata/read access at each source. Keep the database URL and client secret outside Git and prompts.

For a local account-free proof, use `compose.yaml` with its generated demo bearer credential. That demo mode is fixed to one `demo` tenant and must remain loopback-only.
