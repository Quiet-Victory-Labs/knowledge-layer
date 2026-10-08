# Durable source refresh jobs

In the OIDC profile, connecting a source saves its scoped configuration and enqueues discovery. Browser refresh returns HTTP 202 with a `job` instead of waiting for profiling. Agent HTTP refresh currently returns HTTP 200 with the queued `job`; that response does not mean discovery completed. Browser users can read `GET /api/knowledge/jobs/{job_id}`; agents can use `GET /agent/api/jobs/{job_id}` or the `get_refresh_job` MCP tool. The source list also includes an active job summary so the UI can recover its status after a page reload. Only principals with a source metadata grant can read that source's job. Cancellation requires source management permission and a `manage` grant.

The queue lives in the existing PostgreSQL registry, so no Redis or message broker is required. It permits one active job per source and at most 100 active jobs across the single-organization pilot. The worker claims a job with a database row lock and a 30-second lease, renews the lease while running a child process, and retries a failed job up to three times with bounded backoff. The child has a five-minute default time limit (configurable from 30 to 900 seconds). `queued`, `running`, `retry_wait`, `succeeded`, `failed`, and `cancelled` are the externally visible states.

Run the worker as a separate process from the API. The Helm chart supplies a worker sidecar by default. The OIDC Compose profile supplies a `worker` service. Both must use the same PostgreSQL database and connector allowlist. Provision source credentials and source mounts on the worker, which performs connector I/O; the Helm chart intentionally does not mount them on the API container. For a Compose override, add `KNOWLEDGE_SOURCE_*` references and source mounts to the worker. The worker uses no browser or agent token.
It refuses to start without an explicit `KNOWLEDGE_ENABLED_KINDS` allowlist.

```sh
python -m knowledge_layer.migrate upgrade
python -m knowledge_layer.worker
```

The worker refuses an old or drifted schema. A process restart leaves the job in PostgreSQL; once its lease expires, the worker can claim it again. Execution is **at least once**. A crash after saving a snapshot but before recording job success can create an extra metadata snapshot version on retry. Cancellation is best effort while a child is running; a snapshot can finish before the cancellation reaches it. Do not run multiple worker replicas for an enterprise customer until overlapping expired-lease execution and source credential isolation are qualified.

The [local worker drill](../scripts/drill_refresh_jobs.py) creates a disposable PostgreSQL database, enqueues a bounded source, exits that request process, runs a separate worker container, checks the job and snapshot, and drops the database. It does not establish customer recovery objectives or warehouse driver qualification.

## Customer-controlled recurring schedules

Hourly, daily, weekly and monthly schedules live in the same PostgreSQL registry. The existing worker dispatches due schedules before claiming jobs, including with `--once`; no new broker, scheduler service or model runtime is required. Start the worker explicitly for a local/demo install. An API response saving a schedule does not prove a worker is running.

Use compact or advanced MCP `set_refresh_schedule` / `get_refresh_schedule`, agent HTTP `PUT` / `GET /agent/api/sources/{connection_id}/refresh-schedule`, or browser HTTP `PUT` / `GET /api/knowledge/connections/{connection_id}/refresh-schedule`. The browser path uses its existing identity and CSRF boundary. Agent tools/routes require enabled source management and connector allowlists. Schedule management requires the source management role, a source `manage` grant and current operator approval. In OIDC mode, creating a schedule also requires a **direct user** manage grant: the worker cannot refresh IdP group membership. The creator receives this grant when creating an approved source. The demo/trusted operator owner is reserved and cannot be claimed by an OIDC subject.

A PUT body, or matching MCP arguments, uses:

```json
{
  "config": {
    "cadence": "daily",
    "timezone": "America/Chicago",
    "hour": 2,
    "minute": 15,
    "freshness_max_age_seconds": 86400
  },
  "paused": false
}
```

| Cadence | Required calendar fields |
| --- | --- |
| hourly | `minute` (0–59) |
| daily | `hour` (0–23), `minute` |
| weekly | `hour`, `minute`, `weekday` (Monday 0 through Sunday 6) |
| monthly | `hour`, `minute`, `day` (1–31) |

Every config requires `cadence` and an IANA `timezone`. Unknown fields and boolean integers refuse. Optional `freshness_max_age_seconds` is 60–2678400. The package includes a `tzdata` fallback for hosts without an IANA database, following [Python's timezone data guidance](https://docs.python.org/3/library/zoneinfo.html#data-sources). It is an age threshold, not an alert delivery service or freshness guarantee.

Daily/weekly/monthly schedules use the first occurrence of a repeated wall time and skip nonexistent times. Hourly schedules can run both repeated hours. A monthly day beyond the month's end clamps to the last day. Stored instants are UTC; the host timezone does not select execution times. First dispatch and resume select a strictly future calendar time. Missed occurrences coalesce into one refresh and advance to the next future time; there is no catch-up storm. Existing active work is reused without relabeling a manual job as scheduled. Each poll examines at most 20 due schedules and respects the shared 100-active-job limit. Sources with active jobs are considered first so capacity does not prevent coalescing. This single-organization queue has no per-tenant fairness or enterprise scheduling SLO.

Dispatch atomically enqueues and advances the schedule under database locks. Current enabled kinds, source approval and the owner's direct manage grant are checked at dispatch, immediately before child source I/O, and on the parent's five-second heartbeat. A blocked schedule pauses with a bounded reason. Pause cancels queued scheduled jobs and requests running cancellation; manual jobs are separate. Revoking a direct grant blocks future scheduled reads, while in-flight cancellation remains best effort. The worker has no initiating JWT, cannot revalidate live IdP membership or account status, and does not propagate source-native ACLs. Withdraw the direct grant and source approval when those changes must stop scheduled access. Policy files are startup-loaded: restart API and worker after policy changes as described in [source access](source-access.md).

A customer scheduler can still call the manual refresh endpoints. Manual jobs retain their previous authorization and cancellation behavior: grant revocation is not their cancellation mechanism. Consumers should inspect source/job status before curating changed evidence.

## Failure, drift and freshness

Consumers must distinguish job execution, successful inventory, curation completeness, reviewed publication and source observation time. A `succeeded` job can produce a partial first snapshot. It does not approve definitions, certify an import or prove every required object was observed. Source summaries expose discovery/readiness and snapshot version; contract query guidance also says the latest source schema has not been live-verified for analytical execution.

If a previously complete source refresh fails or reports incomplete discovery, the registry retains its prior complete snapshot and marks the connection as an error. Error handling does not advance the completed-discovery clock or `last_refreshed_at` / `last_synced_at`. Active refreshes, source errors and stale approved evidence suspend publication readiness. The retained snapshot is historical evidence, not a newly fresh source read. Source summaries expose `last_refresh_attempt_at` and `last_complete_refresh_at` separately. Only an actual source refresh with explicit completed inventory advances the latter; complete inventory can coexist with partial semantic curation. Review decisions and stored metadata saves never advance either source clock or clear an existing source error. Old rows retain null complete clocks because earlier `last_refreshed_at` values could include metadata reviews. Schedule responses show configured age, unknown/within-window/overdue state, source status and refresh error separately; a within-window historical success does not erase a failed attempt. An adapter's source observation clock is separate from the time the service processed its response.

A completed bounded inventory can report added, removed and changed assets. Prior snapshot versions remain available, and changed citations or schema bindings require revalidation. Protocol 1.2 imports scoped upstream definitions as observed evidence, with native namespace, ID, kind, version and optional owner, authority, reference, review status, observation clock, unit and formula. A null native version stays unknown. Definition, version, authority, unit, formula and pointer changes invalidate local reviews bound to the changed evidence; an upstream approved flag never approves local meaning. This is local evidence invalidation, not a general semantic dependency or authority reconciliation engine.

An object removed from a complete listing of the same approved scope is absent from current context, with its prior versions retained. This does not emit an explicit upstream tombstone or withdrawal record. A failed read, narrowed or denied scope, partial listing or missing credential cannot establish that an upstream definition was deleted. The current graph/evidence system is not a general catalog change feed or tombstone/dependency reconciliation service.

## Reconciliation target and remaining work

The intended customer loop is scheduled source read, namespaced upstream import, drift assessment, permission and revision checks, bounded curation work, accountable reconciliation and permitted publication. Reuse governed upstream definitions rather than reconstructing them from table names. Preserve competing business-unit meanings and authority until policy resolves them. All reconciliation writes belong in Knowledge Layer's registry; no source repair or upstream publication is performed.

Automatic recovery is limited to queue retries, expiring leases and bounded connector execution. Observed semantic imports use the same refresh and local review workflow; imported declarations do not become executable metrics. Attempt and completed-inventory clocks remain distinct. Deletion/withdrawal reconciliation, policy-controlled repair, alert delivery, multi-worker fencing, representative vendor auth/ACL behavior and customer recovery objectives require their declared qualification. See [the federation contract](architecture.md#federation-and-reconciliation).

Use a disposable registry and a synthetic complete adapter response to check supported recurrences, exact cross-source HTTP/MCP citations, injected failures and grant revocation. Verify migration and cold recovery before dropping the registry. The [validation plan](refresh-and-citations-2026-10-03.md) describes checks without claiming a live customer run.