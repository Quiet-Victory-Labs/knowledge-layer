# Company guidance qualification

On October 3, 2026, Knowledge Layer gained a governed company skill/rule registry using its existing PostgreSQL metadata store. Immutable drafts, independent steward publication, scoped authenticated retrieval, dependency checks and portable exports are available through HTTP/MCP and the offline operator CLI. No additional model, queue or service is required.

## Verified runtime

The [repeatable drill](../scripts/drill_company_guidance.py) used a disposable PostgreSQL 18.6 registry in a 256 MB container. It created v10 with a legacy connection row and verified a pre-upgrade backup/restore before applying v11. A versioned demo startup refused the stale v10 schema without creating the new tables. Upgrade preserved the connection, and Alembic physical metadata parity passed.

The runtime drill drafted a procedural template through native streamable HTTP MCP and bound it to reviewed/published Northstar synthetic data. Publication was an evaluator decision in an isolated registry, not an adopted customer company policy. The public [synthetic analytics template](../examples/company-guidance-synthetic.json) now uses fictional scope labels; this historical report does not claim the replacement payload was rerun. The resolver and agent HTTP returned the same published digest, and exports contained the exact tested procedure and rules. Missing context did not select the narrowly scoped bundle. Agent tools and HTTP exposed no publication action.

A second draft preserved the effective first publication. A deliberately conflicting publication produced the declared rule conflict and blocked export. Withdrawal remained visible. A separately scoped published procedure became unusable after its source publication was withdrawn; the resolver returned no active dependent content and export refused. The earlier exported file therefore requires live revalidation and cannot be treated as current after the drill.

Two simultaneous authenticated HTTP drafts used the same slug and expected revision. One returned 201, the other 409. PostgreSQL readback found exactly one immutable revision and one successful draft audit event. The v11 restore compared every persisted table count and content fingerprint, schema revision and snapshot fingerprint. Temporary restore databases were removed.

Development integration exposed and fixed omitted-source context handling, inconsistent token-file loading, exact YAML description handling, hidden-scope edits, offline operator authorship impersonation and withdrawal after a deleted source. Expected MCP refusals now preserve controlled error reasons through the SDK tool-error channel. Unknown or unauthorized revision reads return not-found rather than a successful null response.

The runtime used synthetic source bindings and a procedural template; no customer application data was queried. It did not test named-player outcomes or native Codex/Claude/Cursor/Gemini skill activation. OIDC role/audience and source-grant cases use deterministic principals; a live customer IdP and upstream ACLs remain separate qualification.

## Regression and limits

The full suite passed from a clean detached checkout of implementation commit `b527e67`: **737 tests passed in 22.15 seconds**. Dependencies were installed with the frozen lock and all extras, and imports were verified to come from that checkout. Ruff and the frozen-lock check passed. The tracked-file secret gate passed over 220 files; one deliberately invalid rejection fixture is explicitly identified as synthetic. The single pytest warning concerns SQLite reflection of the existing PostgreSQL expression index.

Tests cover immutable versions, optimistic edits, permission/audience boundaries, steward publication, effective clocks, dependency errors/staleness/withdrawal, typed conflicts, capacity refusal, exact portable text, safe paths, installer preservation and HTTP/MCP boundaries.

The resolver covers applicable authorized guidance. It does not prove complete company policy coverage, infer every natural-language contradiction, compile prose into enforceable policies or control another harness's plugins. Client-specific layouts are format-tested; automatic distribution/revocation and native harness activation remain unqualified. The [guidance guide](company-guidance.md) documents current capacity and trust boundaries.

Private preparation, migration, runtime, concurrency, regression and recovery receipts are retained under ignored `.local/company-guidance-2026-10-03/`. Verified dumps and fingerprint reports remain in owner-only `.local/recovery/`. The task-owned API and disposable PostgreSQL container were stopped and removed after verification. Other retained registries were not upgraded in this drill. Existing versioned installations require a backed-up v11 migration. No customer credentials or records are tracked.
