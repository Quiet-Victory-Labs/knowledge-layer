# Explicit aborted-inventory retention

The offline operator command can remove a bounded, previewed set of **already-aborted** discovery inventories from the Knowledge Layer registry. It deletes their run, page, entity, and evidence rows. It never connects to an enterprise source, changes source data, stamps a freshness clock, approves meaning, or publishes a snapshot. No automatic deletion runs, and no HTTP or MCP tool exposes this command.

This is a small local-storage maintenance operation, not a general evidence-erasure or legal-hold engine. The customer supplies the exact source and creation cutoff under its own policy. Knowledge Layer does not choose a retention age.

## Preview and apply

Use an offline process configured with `DATABASE_URL` or an owner-only `DATABASE_URL_FILE` for the **Knowledge Layer PostgreSQL registry**. The command checks the installed migration head and physical schema; it refuses SQLite and out-of-date schemas and does not perform migrations. The library uses SQLite only for isolated offline tests.

```sh
python -m knowledge_layer.inventory_retention_cli \
  --tenant company --connection conn_example \
  --created-before 2026-01-01T00:00:00Z \
  --max-runs 20 --max-rows 2000 \
  --output /private/inventory-preview.json
```

Preview is the default and performs no registry writes. Read its `preview_digest`, run IDs and aggregate counts, then repeat the **same tenant, source, cutoff and budgets** with explicit apply:

```sh
python -m knowledge_layer.inventory_retention_cli \
  --tenant company --connection conn_example \
  --created-before 2026-01-01T00:00:00Z \
  --max-runs 20 --max-rows 2000 \
  --apply --expected-preview-digest PREVIEW_SHA256 \
  --output /private/inventory-application.json
```

Replace `PREVIEW_SHA256` with the exact lowercase 64-character digest. A changed cohort, child digest, count, cutoff or budget refuses the entire operation and requires a new preview. A successfully applied nonempty preview cannot be replayed. An unchanged empty preview remains a no-op and inserts no audit event.

`--output` is optional. Each output file must be new and absolute; it is reserved through one descriptor with mode `0600` **before** any registry operation. Existing files and symlinks are refused without applying. JSON is also printed to stdout. Reports exclude source text, record payloads, credentials, and child IDs, but **tenant/source/run IDs, digests, timestamps and counts are sensitive registry metadata**. Protect both files and captured stdout. Error output uses fixed codes and never renders arbitrary database/driver exception messages.

If writing or syncing the reserved file fails after apply commits, stdout retains the actual `status=applied`, counts and `registry_writes_executed=true`, with `receipt_file_written=false` and a fixed output error. Exit code 1 signals that output failure; it does not roll back the committed deletion. Partial private output is retained for operator inspection, and no file is unlinked by pathname after a failure or replacement. A successfully synced file is checked against the reserved device/inode before stdout reports it saved. The persisted file contains the registry result; the additional file-write state appears on stdout. Exit code 2 means refusal. If a database driver itself fails at a commit boundary, the error receipt leaves registry writes unknown; check the audit ledger rather than assuming rollback.

## Eligibility and boundaries

The cutoff must be an aware ISO timestamp or, through the library, an aware `datetime`. It is normalized to UTC and is exclusive: a run created exactly at the cutoff is retained. Persisted clocks must match the canonical UTC form emitted by `utc_now()`, including its omitted fraction at zero microseconds; invalid or noncanonical clocks refuse cleanup rather than guessing an age. Creation time is used because aborted inventories currently have no abort or last-activity clock. This cannot implement “aborted for N days.”

The run budget is 1–20, and the deletion-row budget is 1–10,000. `max_rows` counts **run + page + entity + evidence rows actually removed**; the separately inserted audit row is excluded. These are row budgets, not a measurement of stored payload bytes or database vacuum space. Cleanup validates real child counts and per-page counts instead of trusting stale run counters. Child IDs and stored digests are read internally for the preview binding; source payload JSON is not loaded. This does not re-hash payload bodies or certify that a database administrator left their stored digests truthful.

A source-scoped scan validates at most 2,000 aborted run creation clocks before selection. Larger populations refuse rather than claiming unbounded support. Within that bound, the earliest eligible runs by creation time and ID form the selected cohort. An additional boundary witness binds pagination. `has_more=true` means more eligible runs remain; the population count is explicitly unknown in that response. A fresh preview after each applied batch selects the next cohort. Changes outside the selected cohort and its boundary do not claim to invalidate or certify the entire remaining estate.

Preview and apply refuse when the source has a queued, running or retry-wait refresh job, even if its lease expired or cancellation was requested. Unknown job states also refuse. The operator must use existing job lifecycle controls to reach a terminal state; the retention command does not cancel jobs or infer quiescence from a clock.

The following remain untouched:

- Staging inventories and committed inventories, including historical predecessors needed by removal comparisons.
- Current inventory heads and all inventories referenced through a `previous_inventory_id`, even when an aborted reference reflects corruption.
- Legacy snapshots, concepts, graphs, metric/semantic contracts, proposals, publication records, guidance, autonomy records, refresh jobs, grants and schedules.
- Existing audit history, source status and all source freshness/attempt clocks.

Withdrawn connector approval does not block cleanup of local aborted metadata. The source row must still exist under the exact tenant/source selection. Orphaned inventories after source deletion are not handled by this slice.

## Reusable API and transaction contract

```python
from knowledge_layer.inventory_retention import InventoryRetention

retention = InventoryRetention(store)
preview = retention.preview(
    tenant_id, connection_id,
    created_before=aware_cutoff, max_runs=20, max_rows=2000,
)
receipt = retention.apply(
    tenant_id, connection_id,
    created_before=aware_cutoff, max_runs=20, max_rows=2000,
    expected_preview_digest=preview["preview_digest"],
)
```

Both calls require `store.access_enforced=False` and no ambient request principal. A manager or steward request is not an offline operator. Do not mount this API in an agent or browser transport.

Apply uses PostgreSQL `READ COMMITTED`, takes the exact registry source row lock before relevant job/run/child locks, and recalculates the preview. Existing inventory and queue writers use that source lock before changing source-owned state. Queue heartbeat/cancel paths lock only their job and do not acquire the source lock in reverse order. Retention does not acquire the queue's global enqueue advisory lock. Per-statement PostgreSQL timeout is 30 seconds and lock timeout is 5 seconds; these are not a whole-command wall-clock guarantee.

Explicit child deletes, exact affected-row checks and remaining-row checks precede the run delete. One `inventory.retention` event records the preview digest, selected run IDs, cutoff and row counts with actor `operator`, in the same transaction. An audit insertion failure or mismatched deletion count rolls back all deletes. The ledger's existing [audit limitations](audit-events.md) still apply, including customer-managed retention and no tamper-evidence guarantee.

Offline regression coverage is in `tests/test_inventory_retention.py`: actual deletion/readbacks, unchanged tables and clocks, strict/equal/offset cutoffs, permission refusal, withdrawn connector approval, active jobs, corrupt references/digests/counters, changed previews, bounded populations and audit rollback. The separate `scripts/drill_autonomy.py --managed --include-inventory` drill qualifies PostgreSQL contention, rollback, durable deletion/audit readbacks, private CLI preview and empty apply on a guarded disposable registry. See the exact [frozen build evidence](build-progress-2026-10-03.md). Neither path establishes provider connectivity, legal policy or a customer deployment.

Removing these local rows does not erase old snapshot excerpts, downstream exports, client downloads, independent logs, backups or database WAL. Broader sensitive-metadata retention needs explicit dependency handling and customer policy before deleting those histories. No such purge is implied here.
