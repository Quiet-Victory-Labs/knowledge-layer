# Paged adapter worker qualification — 2026-10-03

Protocol 1.3 now refreshes an explicitly approved discovery inventory through the
existing queue, worker and schedule registry. It stages complete JSON records in
bounded pages, atomically replaces one scoped head, and records a typed
`result_inventory_id`. It produces no legacy snapshot or semantic publication.

Implementation is frozen at `9f64987d6833eacfb6a936b034bcdeb482a358bb`.
`995f6f9758d8e0cdf208886f9283b1a1b20527ad` corrects one older cleanup-test fixture's
mocked migration head; product Python code is unchanged between those revisions.

## Independently verified

| Evidence | Observed result | Boundary |
| --- | --- | --- |
| Detached clean checkout of `995f6f9` | 1,883 tests passed; Ruff and immutable secret gate passed for 318 tracked files | Local regression, not a provider or deployment result |
| Wheel and source archive from that checkout | Actual independent base installs, isolated import provenance, complete/partial page conformance, installed source CLI and consumer example passed | Unsigned version 0.1.0 artifacts; current runner architecture only; unpublished |
| Detached clean `9f64987` PostgreSQL drill | 15 native paged refresh checks, 49 existing inventory checks, 16 autonomy/recovery checks passed | Disposable PostgreSQL 18.6; synthetic loopback adapter; no customer source |
| Temporary demo UI | Refused missing payload-retention consent; inventory mode hid legacy controls; created configuration reached the server; failed discovery displayed unknown per-source and total counts as dashes | Synthetic unconfigured source reference; no provider or identity integration |
| Web build | TypeScript and Vite build passed, including the follow-up clearing a stale progress message after discovery failure | Build proof separate from the UI observation above |

The regression emitted one SQLite/Alembic warning: SQLAlchemy cannot reflect the
PostgreSQL expression-based search index through SQLite. The disposable
PostgreSQL execution and migration/recovery evidence are separate checks.

The disposable runtime was capped at 256 MiB and one CPU. The drill used an
owner-only temporary adapter profile and a nonce-owned loopback HTTP server;
readbacks compared exact entity/evidence payloads to the synthetic fixture. The
adapter executes no source SQL or model. Successful scans advanced only the
inventory head's complete-scope clock. Global refresh and semantic publication
clocks remained unset, with zero snapshots and publications created.

An actual worker parent spawned its child for two-page ingestion, scheduled
late-page failure, a fresh-scan retry and cancellation while the second page was
held. Parent retirement aborted only the failed/cancelled staging generation;
the prior committed head and clock remained unchanged. A blocked native inventory
writer used READ COMMITTED despite a REPEATABLE READ engine default. Removing the
requester's direct grant during the actual source-row lock wait refused staging.
A separately claimed child also refused after grant revocation without an
adapter call. These positive refusal effects are qualified; no mutation-based
negative-control or seven-day endurance result is claimed.

SQLite transport/effect tests additionally cover policy/profile/budget withdrawal,
unavailable or expired pages, redirects, watermark changes, cursor repetition,
duplicate records, unresolved evidence, canonical and wire-byte limits, compressed
response refusal, live deadline checks between chunks and audit rollback. Failed
direct scans retire only their exact unclaimed staging generation. Incoming job
results protect local history from retention. HTTP/MCP callers cannot inject the
internal source-refresh attribution flag.

Both disposable drills restored all 23 registry table counts and removed their
exact owned fixtures. Their temporary servers and containers stopped. The demo
UI server and hidden verification tab also closed. Private receipts contain
fixture ownership handles and stay outside the public repository.

## Remaining qualification

This closes generic paged worker orchestration within F03, not its full acceptance
gate. The adapter's declared identity/revision and watermark are assertions, not
native account, cursor or ACL verification. The platform keeps whole-connection
visibility grants and requires explicit complete-payload retention. Each vendor
adapter still needs a restricted-account read-only qualification.

The connector capability therefore retains `live_qualified: false`; the local
registry/worker runtime result above does not establish a customer provider
connection. Estate-scale approved semantic projections, vendor cursor resumption,
real account/permission coverage, customer cloud operation, longer endurance,
paired analyst-value holdouts and a signed public release remain open. Follow
the [completion plan](platform-completion-plan-2026-10-03.md),
[protocol workflow](paged-adapter-refresh.md) and
[release gates](release-readiness.md).
